import errno
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime

from tqdm import tqdm

from normalizer import (
    add_pass_marker,
    get_chosung,
    is_supported_file,
    materialize_title_markup,
    normalize_filename,
    normalize_nfc,
    should_exclude_dir,
    should_exclude_file,
    strip_pass_marker,
    strip_disambig_marker,
    strip_trash_suffix,
    SUPPORTED_EXTENSIONS,
)
from scanner import (
    INDEX_GENERATION_FILENAME,
    IndexSnapshotStale,
    generate_file_list,
    generate_file_list_from_state_db,
    validate_index_snapshot,
    validate_index_generation,
)
from deduplicator import (
    clean_duplicates,
    cleanup_pending_active_distinct_decision_reviews,
    cleanup_post_intake_exact_duplicates,
    cleanup_pending_queue_strong_reviews,
    cleanup_relationship_preserving_queue_exact_duplicates,
    unique_path,
)
from project_paths import HOUSE_DIR, PROJECT_ROOT, TEMP_DIR


DEFAULT_SRC_DIR = str(TEMP_DIR)
DEFAULT_DST_DIR = str(HOUSE_DIR)
PASS_DIR_NAME = "pass"
UNPACK_DIR_NAME = "unpack"
LEGACY_UNPACK_PREFIX = "___"

# 자모 폴더에 같은 이름의 파일이 이미 있을 때 새 파일에 붙는 충돌 회피 suffix.
# 일반 입고와 pass 입고를 분리하여 의미가 섞이지 않게 한다.
NORMAL_CONFLICT_SUFFIX = "_dup"
PASS_CONFLICT_SUFFIX = "_pass"
EXTENSION_INDEX_PATH = os.path.join("extension", "file_index.json")
HOUSE_INDEX_FILENAME = "file_index.json"


class VolumeCoordinateConflict(RuntimeError):
    """One incoming volume overlaps an existing coordinate and must be held."""

    def __init__(self, decision):
        self.decision = dict(decision)
        coordinate = (
            self.decision.get("coordinate_kind"),
            self.decision.get("coordinate_num"),
            self.decision.get("coordinate_den"),
        )
        super().__init__(f"existing volume coordinate conflict: {coordinate}")


def get_now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def emit_folderling_event(event_callback, phase, **payload):
    """Publish one structured event without coupling Folderling to the web UI."""
    if event_callback is not None:
        event_callback({"phase": phase, **payload})


def recent_link_matches_destination(link_path, dst_path):
    """Return True only for a symlink that already names this exact target.

    The target may be missing: legacy title requeue intentionally removed the
    old house file while leaving the user-visible recent symlink untouched.
    Re-materializing that exact target does not mutate or replace the symlink.
    """
    link_path = os.path.abspath(link_path)
    if not os.path.islink(link_path):
        return False
    target = os.readlink(link_path)
    if not os.path.isabs(target):
        target = os.path.join(os.path.dirname(link_path), target)
    return normalize_nfc(os.path.abspath(target)) == normalize_nfc(
        os.path.abspath(dst_path)
    )


def retarget_owned_recent_link(recent_dir, source_path, destination_path):
    """Atomically retarget only a recent symlink proven to name ``source_path``."""

    link_path = os.path.join(recent_dir, os.path.basename(source_path))
    if not os.path.lexists(link_path):
        return "missing"
    if not recent_link_matches_destination(link_path, source_path):
        return "preserved"
    temporary = os.path.join(
        recent_dir,
        f".folderling-retarget-{os.getpid()}-{time.time_ns()}",
    )
    if os.path.lexists(temporary):
        raise RuntimeError(f"unexpected recent retarget path exists: {temporary}")
    try:
        os.symlink(os.path.abspath(destination_path), temporary)
        os.replace(temporary, link_path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)
    return "retargeted"


def create_recent_link(dst_path, clean_name, recent_dir):
    """최근 파일에 대한 심볼릭 링크 생성"""
    from mutation_io import ensure_directory_nofollow
    ensure_directory_nofollow(recent_dir)
    link_path = os.path.join(recent_dir, clean_name)
    abs_dst_path = os.path.abspath(dst_path)

    if os.path.lexists(link_path):
        if recent_link_matches_destination(link_path, abs_dst_path):
            return False
        raise FileExistsError(
            f"_최근 기존 경로는 소유권을 증명할 수 없어 보존합니다: {link_path}"
        )

    os.symlink(abs_dst_path, link_path)
    return True


def ensure_recent_link_slot(clean_name, recent_dir, dst_path=None):
    """Fail before intake when a user-owned non-link occupies the recent path."""
    link_path = os.path.join(recent_dir, clean_name)
    if os.path.lexists(link_path):
        if dst_path is not None and recent_link_matches_destination(
            link_path, dst_path
        ):
            return
        raise FileExistsError(
            f"_최근 기존 경로는 소유권을 증명할 수 없어 입고를 중단합니다: {link_path}"
        )


def cleanup_recent_links(recent_dir, max_days=30):
    """소유권 정보가 없는 기존 recent link를 보존한다."""
    if not os.path.exists(recent_dir):
        return

    for item in os.listdir(recent_dir):
        link_path = os.path.join(recent_dir, item)

        if os.path.islink(link_path):
            # 1.2.2: 기존 링크의 creator identity가 없으므로 사용자 데이터로 보존한다.
            continue


def parse_args(argv):
    src_dir = DEFAULT_SRC_DIR
    dst_dir = DEFAULT_DST_DIR

    for i, arg in enumerate(argv):
        if arg == "--src" and i + 1 < len(argv):
            src_dir = argv[i + 1]
        elif arg == "--dst" and i + 1 < len(argv):
            dst_dir = argv[i + 1]

    return src_dir, dst_dir


def should_skip_source_item(item):
    return should_exclude_dir(item)


def legacy_pass_items(pass_dir):
    """Return reviewable legacy pass entries, excluding Finder metadata."""
    if not os.path.isdir(pass_dir):
        return []
    return [
        name for name in sorted(os.listdir(pass_dir))
        if not should_exclude_file(name)
    ]


def is_unpack_source_dir(name):
    """Recognize the explicit unpack inbox and legacy ``___*`` wrappers."""
    normalized = normalize_nfc(name).strip()
    return bool(normalized) and (
        normalized.casefold() == UNPACK_DIR_NAME
        or normalized.startswith(LEGACY_UNPACK_PREFIX)
    )


def _unpack_roots(src_dir):
    roots = []
    if not os.path.isdir(src_dir):
        return roots
    for name in sorted(os.listdir(src_dir)):
        path = os.path.join(src_dir, name)
        if os.path.isdir(path) and not os.path.islink(path) and is_unpack_source_dir(name):
            roots.append((name, path, name.strip().casefold() == UNPACK_DIR_NAME))
    return roots


def iter_unpack_supported_files(root):
    """Yield supported regular files below one unpack wrapper in stable order."""
    paths = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = [
            name for name in sorted(directories)
            if not should_exclude_dir(name)
            and not os.path.islink(os.path.join(current, name))
        ]
        for filename in sorted(filenames):
            path = os.path.join(current, filename)
            if (
                should_exclude_file(filename)
                or not is_supported_file(filename)
                or os.path.islink(path)
                or not os.path.isfile(path)
            ):
                continue
            paths.append(path)
    return paths


def _tree_has_symlink(root):
    for current, directories, filenames in os.walk(root, followlinks=False):
        if any(os.path.islink(os.path.join(current, name)) for name in directories):
            return True
        if any(os.path.islink(os.path.join(current, name)) for name in filenames):
            return True
    return False


def _tree_file_stats(root):
    count = 0
    size = 0
    for current, _, filenames in os.walk(root, followlinks=False):
        for filename in filenames:
            path = os.path.join(current, filename)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            count += 1
            try:
                size += os.path.getsize(path)
            except OSError:
                pass
    return count, size


def _snapshot_unpack_tree(root):
    files = []
    directories = []
    supported = []
    for current, child_dirs, filenames in os.walk(root, topdown=True, followlinks=False):
        for name in child_dirs:
            path = os.path.join(current, name)
            info = os.lstat(path)
            if not os.path.isdir(path) or os.path.islink(path):
                raise OSError(f"unpack directory is not owned safely: {path}")
            directories.append((path, info.st_dev, info.st_ino))
        for name in filenames:
            path = os.path.join(current, name)
            info = os.lstat(path)
            if not os.path.isfile(path) or os.path.islink(path):
                raise OSError(f"unpack file is not owned safely: {path}")
            record = (
                path, info.st_dev, info.st_ino, info.st_ctime_ns,
                info.st_size, info.st_mtime_ns,
            )
            files.append(record)
            if is_supported_file(name):
                supported.append(path)
    return files, directories, supported


class UnpackCleanupError(OSError):
    def __init__(self, message, *, removed_files=0, removed_bytes=0):
        super().__init__(message)
        self.removed_files = int(removed_files)
        self.removed_bytes = int(removed_bytes)


def _open_relative_directory(root_fd, parts):
    fd = os.dup(root_fd)
    flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        for part in parts:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _cleanup_unpack_tree_owned(root, *, reusable, files, directories):
    """Delete only entries observed in one no-follow snapshot.

    A file arriving after the snapshot is never passed to unlink.  It instead
    keeps a directory non-empty (or leaves the reusable inbox non-empty), so the
    caller reports cleanup_failed and preserves the late arrival.
    """
    root = os.path.abspath(root)
    removed_files = 0
    removed_bytes = 0
    root_fd = None
    parent_fd = None
    try:
        root_fd = os.open(
            root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        root_identity = os.fstat(root_fd)
        for path, dev, ino, ctime_ns, size, mtime_ns in files:
            relative = os.path.relpath(path, root)
            parts = relative.split(os.sep)
            directory_fd = _open_relative_directory(root_fd, parts[:-1])
            try:
                current = os.stat(parts[-1], dir_fd=directory_fd, follow_symlinks=False)
                if (
                    current.st_dev, current.st_ino, current.st_ctime_ns,
                    current.st_size, current.st_mtime_ns,
                ) != (dev, ino, ctime_ns, size, mtime_ns):
                    raise OSError(f"unpack file changed during cleanup: {path}")
                os.unlink(parts[-1], dir_fd=directory_fd)
            finally:
                os.close(directory_fd)
            removed_files += 1
            removed_bytes += size

        for path, dev, ino in sorted(
            directories,
            key=lambda item: len(os.path.relpath(item[0], root).split(os.sep)),
            reverse=True,
        ):
            relative = os.path.relpath(path, root)
            parts = relative.split(os.sep)
            directory_fd = _open_relative_directory(root_fd, parts[:-1])
            try:
                current = os.stat(parts[-1], dir_fd=directory_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (dev, ino):
                    raise OSError(f"unpack directory changed during cleanup: {path}")
                os.rmdir(parts[-1], dir_fd=directory_fd)
            finally:
                os.close(directory_fd)

        if reusable:
            if os.listdir(root_fd):
                raise OSError("unpack inbox changed during cleanup")
        else:
            parent = os.path.dirname(root)
            leaf = os.path.basename(root)
            parent_fd = os.open(
                parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            current_root = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            if (current_root.st_dev, current_root.st_ino) != (
                root_identity.st_dev, root_identity.st_ino,
            ):
                raise OSError("unpack wrapper changed during cleanup")
            os.rmdir(leaf, dir_fd=parent_fd)
        return removed_files, removed_bytes
    except OSError as exc:
        raise UnpackCleanupError(
            str(exc), removed_files=removed_files, removed_bytes=removed_bytes
        ) from exc
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
        if root_fd is not None:
            os.close(root_fd)


def cleanup_unpack_sources(src_dir, *, before_mutation=None):
    """Discard unpack wrappers only after every supported file left safely.

    ``txt_temp/unpack`` remains as the reusable inbox. Legacy ``___*`` wrapper
    directories are removed completely. Unsupported cover/archive assets are
    intentionally discarded with the wrapper, matching the historic contract.
    A symlink or any remaining supported file fails closed and preserves the
    complete remaining tree for inspection or retry.
    """
    results = []
    for name, root, reusable in _unpack_roots(src_dir):
        try:
            files, directories, remaining = _snapshot_unpack_tree(root)
        except OSError as exc:
            results.append({
                "name": name,
                "path": root,
                "status": "snapshot_blocked",
                "remaining_supported": 0,
                "discarded_files": int(getattr(exc, "removed_files", 0)),
                "discarded_bytes": int(getattr(exc, "removed_bytes", 0)),
                "error": str(exc),
            })
            continue
        if remaining:
            results.append({
                "name": name,
                "path": root,
                "status": "pending_supported_files",
                "remaining_supported": len(remaining),
                "discarded_files": 0,
                "discarded_bytes": 0,
            })
            continue
        try:
            if before_mutation is not None:
                before_mutation()
            discarded_files, discarded_bytes = _cleanup_unpack_tree_owned(
                root,
                reusable=reusable,
                files=files,
                directories=directories,
            )
            status = "cleaned"
        except OSError as exc:
            status = "cleanup_failed"
            results.append({
                "name": name,
                "path": root,
                "status": status,
                "remaining_supported": 0,
                "discarded_files": int(getattr(exc, "removed_files", 0)),
                "discarded_bytes": int(getattr(exc, "removed_bytes", 0)),
                "error": str(exc),
            })
            continue
        results.append({
            "name": name,
            "path": root,
            "status": status,
            "remaining_supported": 0,
            "discarded_files": discarded_files,
            "discarded_bytes": discarded_bytes,
        })
    return results


def prune_empty_intake_tree(path):
    """Remove only empty directories below one successfully handled temp item.

    Folder intake journals files individually so its source directory shells can
    remain after every payload file reaches house. Walk bottom-up without
    following links and remove only directories empty at removal time.
    """
    path = os.path.abspath(path)
    if os.path.islink(path) or not os.path.isdir(path):
        return 0

    removed = 0
    for current, _, _ in os.walk(path, topdown=False, followlinks=False):
        if os.path.islink(current):
            continue
        try:
            os.rmdir(current)
            removed += 1
        except OSError as exc:
            if exc.errno in {errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT}:
                continue
            raise
    return removed


def directory_has_files(path):
    """Return whether a directory tree contains any file-like intake payload."""
    for _, _, files in os.walk(path, followlinks=False):
        if files:
            return True
    return False


def _atomic_publish_file(source, destination):
    """Publish one regular file without following a destination symlink."""
    from mutation_io import atomic_publish_regular_file

    return atomic_publish_regular_file(source, destination)


DIRECTORY_INTAKE_ACTION = "directory_house_ingest"


def _directory_source_inventory(source_root, destination_root):
    """Capture one immutable, no-follow directory intake manifest."""
    from mutation_io import inspect_regular_file

    source_root = os.path.abspath(source_root)
    destination_root = os.path.abspath(destination_root)
    if os.path.islink(source_root):
        raise RuntimeError(f"directory intake source is a symlink: {source_root}")
    items = []
    normalized_paths = set()
    for root, dirs, files in os.walk(source_root, followlinks=False):
        for dirname in dirs:
            directory = os.path.join(root, dirname)
            if os.path.islink(directory):
                raise RuntimeError(f"directory intake contains a symlink: {directory}")
        for filename in files:
            source = os.path.join(root, filename)
            if os.path.islink(source):
                raise RuntimeError(f"directory intake contains a symlink: {source}")
            relative = os.path.relpath(source, source_root)
            normalized_relative = normalize_nfc(relative)
            if normalized_relative in normalized_paths:
                raise RuntimeError(
                    "directory intake normalized path collision: "
                    f"{normalized_relative}"
                )
            normalized_paths.add(normalized_relative)
            evidence = inspect_regular_file(source)
            items.append({
                "rel_path": relative,
                "normalized_rel_path": normalized_relative,
                "destination_rel_path": relative,
                "dev": evidence.dev,
                "ino": evidence.ino,
                "ctime_ns": evidence.ctime_ns,
                "size": evidence.size,
                "mtime_ns": evidence.mtime_ns,
                "sha256": evidence.sha256,
            })
    items.sort(key=lambda item: item["normalized_rel_path"])
    plan_payload = {
        "version": 1,
        "source_root": normalize_nfc(source_root),
        "destination_root": normalize_nfc(destination_root),
        "items": [
            {
                "rel_path": item["normalized_rel_path"],
                "destination_rel_path": normalize_nfc(item["destination_rel_path"]),
                "size": item["size"],
                "sha256": item["sha256"],
            }
            for item in items
        ],
    }
    plan_sha256 = hashlib.sha256(
        json.dumps(
            plan_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return {
        **plan_payload,
        "items": items,
        "plan_sha256": plan_sha256,
    }


def _load_resumable_directory_manifest(conn, source_root, destination_root=None):
    import decision_store

    source_key = decision_store.canonicalize_path(source_root)
    rows = conn.execute(
        """
        SELECT * FROM operation_groups
        WHERE action = ? AND state = 'failed'
        ORDER BY group_id DESC
        """,
        (DIRECTORY_INTAKE_ACTION,),
    ).fetchall()
    for row in rows:
        if not row["source_path"]:
            continue
        if decision_store.canonicalize_path(row["source_path"]) != source_key:
            continue
        if destination_root is not None and decision_store.canonicalize_path(
            row["dest_path"]
        ) != decision_store.canonicalize_path(destination_root):
            continue
        try:
            manifest = json.loads(row["source_manifest_json"] or "")
        except (TypeError, ValueError):
            continue
        if (
            isinstance(manifest, dict)
            and isinstance(manifest.get("items"), list)
            and manifest.get("plan_sha256") == row["plan_sha256"]
        ):
            return row, manifest
    return None, None


def has_resumable_directory_intake(state_db_path, source_root):
    if not state_db_path:
        return False
    import decision_store

    conn = decision_store.connect_state_db(state_db_path)
    try:
        row, _ = _load_resumable_directory_manifest(conn, source_root)
        return row is not None
    finally:
        conn.close()


def _validate_directory_resume_sources(source_root, manifest):
    current = _directory_source_inventory(source_root, manifest["destination_root"])
    expected = {
        item["normalized_rel_path"]: item for item in manifest["items"]
    }
    for item in current["items"]:
        expected_item = expected.get(item["normalized_rel_path"])
        if expected_item is None:
            raise RuntimeError(
                "directory intake source gained an unapproved file after failure: "
                f"{item['rel_path']}"
            )
        if (
            item["dev"] != expected_item["dev"]
            or item["ino"] != expected_item["ino"]
            or item["ctime_ns"] != expected_item["ctime_ns"]
            or item["size"] != expected_item["size"]
            or item["mtime_ns"] != expected_item["mtime_ns"]
            or item["sha256"] != expected_item["sha256"]
        ):
            raise RuntimeError(
                "directory intake source changed after approval: "
                f"{item['rel_path']}"
            )
    return current


def _committed_directory_item(conn, manifest, item, destination):
    """Return whether an earlier group owns this exact committed destination."""
    import decision_store
    from mutation_io import inspect_regular_file

    canonical_destination = decision_store.canonicalize_path(destination)
    row = conn.execute(
        """
        SELECT o.operation_id, o.file_id,
               o.destination_dev, o.destination_ino, o.destination_ctime_ns,
               o.destination_size, o.destination_mtime_ns, o.destination_sha256
        FROM operations AS o
        JOIN operation_groups AS og ON og.group_id = o.operation_group_id
        WHERE og.action = ? AND og.plan_sha256 = ?
          AND o.action = 'house_ingest' AND o.state = 'committed'
          AND o.dest_path = ?
        ORDER BY o.operation_id DESC LIMIT 1
        """,
        (DIRECTORY_INTAKE_ACTION, manifest["plan_sha256"], canonical_destination),
    ).fetchone()
    if row is None or not os.path.lexists(destination):
        return False
    evidence = inspect_regular_file(destination)
    operation_identity = (
        row["destination_dev"], row["destination_ino"], row["destination_ctime_ns"],
        row["destination_size"], row["destination_mtime_ns"],
        row["destination_sha256"],
    )
    current_identity = (
        evidence.dev, evidence.ino, evidence.ctime_ns, evidence.size,
        evidence.mtime_ns, evidence.sha256,
    )
    if (
        current_identity != operation_identity
        or evidence.sha256 != item["sha256"]
    ):
        raise RuntimeError(
            "directory intake committed destination changed: "
            f"{item['destination_rel_path']}"
        )
    file_row = conn.execute(
        "SELECT canonical_path, source, active, dev, ino, ctime_ns, size, mtime_ns "
        "FROM files WHERE file_id = ?",
        (row["file_id"],),
    ).fetchone()
    if (
        file_row is None
        or not file_row["active"]
        or file_row["source"] != "house"
        or file_row["canonical_path"] != canonical_destination
        or (
            file_row["dev"], file_row["ino"], file_row["ctime_ns"],
            file_row["size"], file_row["mtime_ns"],
        ) != current_identity[:5]
    ):
        raise RuntimeError(
            "directory intake committed destination is not aligned with DB: "
            f"{item['destination_rel_path']}"
        )
    return True


def _ingest_directory_group(conn, source_root, destination_root, run_id):
    """Resume-safe directory intake built on the existing per-file journal."""
    import decision_store
    from dedup_mutations import ingest_to_house

    source_root = os.path.abspath(source_root)
    destination_root = os.path.abspath(destination_root)
    prior_group, manifest = _load_resumable_directory_manifest(
        conn, source_root, destination_root
    )
    if manifest is None:
        if os.path.lexists(destination_root):
            raise RuntimeError(
                "directory intake destination exists without a resumable operation group: "
                f"{destination_root}"
            )
        manifest = _directory_source_inventory(source_root, destination_root)
    else:
        _validate_directory_resume_sources(source_root, manifest)

    source_stat = os.lstat(source_root)
    manifest_json = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    with decision_store.transaction(conn):
        group_id = decision_store.create_operation_group(
            conn,
            run_id=run_id,
            action=DIRECTORY_INTAKE_ACTION,
            plan_sha256=manifest["plan_sha256"],
            source_path=decision_store.canonicalize_path(source_root),
            dest_path=decision_store.canonicalize_path(destination_root),
            item_count=len(manifest["items"]),
            source_manifest_json=manifest_json,
        )
        conn.execute(
            """
            UPDATE operation_groups
            SET source_dev = ?, source_ino = ?, source_ctime_ns = ?
            WHERE group_id = ?
            """,
            (source_stat.st_dev, source_stat.st_ino, source_stat.st_ctime_ns, group_id),
        )

    try:
        for item in manifest["items"]:
            source = os.path.join(source_root, item["rel_path"])
            destination = os.path.join(
                destination_root, item["destination_rel_path"]
            )
            source_exists = os.path.lexists(source)
            destination_exists = os.path.lexists(destination)
            if destination_exists and _committed_directory_item(
                conn, manifest, item, destination
            ):
                if source_exists:
                    raise RuntimeError(
                        "directory intake source and committed destination both exist: "
                        f"{item['rel_path']}"
                    )
                continue
            if destination_exists:
                raise RuntimeError(
                    "directory intake destination conflict: "
                    f"{item['destination_rel_path']}"
                )
            if not source_exists:
                raise RuntimeError(
                    "directory intake item is missing from source and destination: "
                    f"{item['rel_path']}"
                )
            canonical_source = decision_store.canonicalize_path(source)
            row = conn.execute(
                "SELECT file_id FROM files WHERE canonical_path = ? AND active = 1",
                (canonical_source,),
            ).fetchone()
            if row is None:
                with decision_store.transaction(conn):
                    row = decision_store.reconcile_file_metadata(
                        conn, source, source="temp"
                    )
            ingest_to_house(
                conn,
                source_file_id=row["file_id"],
                destination=destination,
                run_id=run_id,
                operation_group_id=group_id,
            )

        for item in manifest["items"]:
            destination = os.path.join(
                destination_root, item["destination_rel_path"]
            )
            if not _committed_directory_item(conn, manifest, item, destination):
                raise RuntimeError(
                    "directory intake group is incomplete after ingest: "
                    f"{item['destination_rel_path']}"
                )
        destination_stat = os.lstat(destination_root)
        with decision_store.transaction(conn):
            conn.execute(
                """
                UPDATE operation_groups
                SET destination_dev = ?, destination_ino = ?, destination_ctime_ns = ?
                WHERE group_id = ?
                """,
                (
                    destination_stat.st_dev,
                    destination_stat.st_ino,
                    destination_stat.st_ctime_ns,
                    group_id,
                ),
            )
            decision_store.transition_operation_group(conn, group_id, "fs_done")
            decision_store.transition_operation_group(conn, group_id, "db_done")
            decision_store.transition_operation_group(conn, group_id, "committed")
        return {
            "group_id": group_id,
            "resumed_from_group_id": prior_group["group_id"] if prior_group else None,
            "item_count": len(manifest["items"]),
        }
    except BaseException as exc:
        with decision_store.transaction(conn):
            row = conn.execute(
                "SELECT state FROM operation_groups WHERE group_id = ?", (group_id,)
            ).fetchone()
            if row is not None and row["state"] in {"planned", "fs_done", "db_done"}:
                decision_store.transition_operation_group(
                    conn, group_id, "failed", error=str(exc)
                )
        raise


def recover_directory_intake_group(conn, group_id):
    """Resolve a crash-left directory group after recovering its child journals."""
    import decision_store

    group = conn.execute(
        "SELECT * FROM operation_groups WHERE group_id = ?", (int(group_id),)
    ).fetchone()
    if group is None:
        raise KeyError(group_id)
    if group["action"] != DIRECTORY_INTAKE_ACTION:
        raise ValueError("operation group is not a directory house intake")
    if group["state"] not in {"planned", "fs_done", "db_done"}:
        return group["state"]

    children = conn.execute(
        "SELECT operation_id, state FROM operations "
        "WHERE operation_group_id = ? ORDER BY operation_id",
        (int(group_id),),
    ).fetchall()
    for child in children:
        if child["state"] in {"planned", "fs_done", "db_done"}:
            decision_store.recover_interrupted_operation(
                conn, int(child["operation_id"])
            )

    try:
        manifest = json.loads(group["source_manifest_json"] or "")
        if (
            not isinstance(manifest, dict)
            or manifest.get("plan_sha256") != group["plan_sha256"]
            or not isinstance(manifest.get("items"), list)
        ):
            raise RuntimeError("directory intake recovery manifest is invalid")
        source_root = group["source_path"]
        destination_root = group["dest_path"]
        complete = True
        conflict = False
        for item in manifest["items"]:
            source = os.path.join(source_root, item["rel_path"])
            destination = os.path.join(
                destination_root, item["destination_rel_path"]
            )
            if _committed_directory_item(conn, manifest, item, destination):
                if os.path.lexists(source):
                    conflict = True
                continue
            complete = False
            source_exists = os.path.lexists(source)
            destination_exists = os.path.lexists(destination)
            if destination_exists or not source_exists:
                conflict = True
        child_states = {
            row[0] for row in conn.execute(
                "SELECT state FROM operations WHERE operation_group_id = ?",
                (int(group_id),),
            )
        }
        if child_states & {"planned", "fs_done", "db_done"}:
            raise RuntimeError("directory intake child recovery is incomplete")
        if complete and not conflict:
            with decision_store.transaction(conn):
                current = conn.execute(
                    "SELECT state FROM operation_groups WHERE group_id = ?",
                    (int(group_id),),
                ).fetchone()[0]
                if current == "planned":
                    decision_store.transition_operation_group(conn, group_id, "fs_done")
                    current = "fs_done"
                if current == "fs_done":
                    decision_store.transition_operation_group(conn, group_id, "db_done")
                    current = "db_done"
                if current == "db_done":
                    decision_store.transition_operation_group(conn, group_id, "committed")
            return "committed"
        target = "stale" if conflict and group["state"] == "planned" else "failed"
        with decision_store.transaction(conn):
            decision_store.transition_operation_group(
                conn,
                group_id,
                target,
                error=(
                    "directory intake recovery found conflicting paths"
                    if conflict else "directory intake recovered children; resume required"
                ),
            )
        return target
    except Exception as exc:
        with decision_store.transaction(conn):
            current = conn.execute(
                "SELECT state FROM operation_groups WHERE group_id = ?",
                (int(group_id),),
            ).fetchone()[0]
            if current in {"planned", "fs_done", "db_done"}:
                target = "stale" if current == "planned" else "failed"
                decision_store.transition_operation_group(
                    conn, group_id, target, error=str(exc)
                )
                return target
        raise


def sync_extension_index(file_index_json, script_dir):
    extension_index_json = os.path.join(script_dir, EXTENSION_INDEX_PATH)
    extension_dir = os.path.dirname(extension_index_json)

    if not os.path.isdir(extension_dir):
        print("⚠️ 확장 폴더를 찾을 수 없어 브라우저 확장용 인덱스 복사를 건너뜁니다.")
        return False

    _atomic_publish_file(file_index_json, extension_index_json)
    print(f"✨ 브라우저 확장용 인덱스 동기화 완료: {extension_index_json}")
    return True


def sync_house_index(file_index_json, dst_dir):
    house_index_json = os.path.join(dst_dir, HOUSE_INDEX_FILENAME)
    if os.path.abspath(file_index_json) == os.path.abspath(house_index_json):
        print(f"✨ txt_house 인덱스 최신 상태: {house_index_json}")
        return True

    _atomic_publish_file(file_index_json, house_index_json)
    print(f"✨ txt_house 인덱스 동기화 완료: {house_index_json}")
    return True


def _capture_index_deployment(paths, backup_parent):
    """Save the currently visible multi-surface generation before publication."""
    backup_root = tempfile.mkdtemp(prefix="index-deploy-", dir=backup_parent)
    records = []
    try:
        for index, raw_path in enumerate(paths):
            path = os.path.abspath(raw_path)
            if os.path.islink(path):
                raise RuntimeError(f"index deployment destination is a symlink: {path}")
            if os.path.lexists(path) and not os.path.isfile(path):
                raise RuntimeError(
                    f"index deployment destination is not a regular file: {path}"
                )
            backup = None
            original_sha256 = None
            if os.path.isfile(path):
                from mutation_io import inspect_regular_file

                original_sha256 = inspect_regular_file(path).sha256
                backup = os.path.join(backup_root, f"{index}.backup")
                _atomic_publish_file(path, backup)
            records.append({
                "path": path,
                "backup": backup,
                "original_sha256": original_sha256,
                "expected_current_sha256": None,
            })
        snapshot = {"root": backup_root, "records": records}
        _write_index_deployment_journal(snapshot)
        return snapshot
    except BaseException:
        shutil.rmtree(backup_root, ignore_errors=True)
        raise


def _write_index_deployment_journal(snapshot):
    journal = os.path.join(snapshot["root"], "deployment.json")
    temporary = journal + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, journal)


def _authorize_index_deployment(snapshot, expected_by_path):
    for record in snapshot["records"]:
        expected = expected_by_path.get(os.path.abspath(record["path"]))
        if expected is not None:
            record["expected_current_sha256"] = expected
    _write_index_deployment_journal(snapshot)


def _restore_index_deployment(snapshot):
    from mutation_io import inspect_regular_file

    errors = []
    for record in snapshot["records"]:
        path = record["path"]
        backup = record["backup"]
        try:
            current_sha256 = None
            if os.path.lexists(path):
                if os.path.islink(path) or not os.path.isfile(path):
                    raise RuntimeError(
                        f"refusing to replace changed index destination: {path}"
                    )
                current_sha256 = inspect_regular_file(path).sha256
            allowed = {
                value for value in (
                    record.get("original_sha256"),
                    record.get("expected_current_sha256"),
                ) if value is not None
            }
            if current_sha256 is not None and current_sha256 not in allowed:
                raise RuntimeError(
                    f"index destination changed outside this deployment: {path}"
                )
            if backup is not None:
                _atomic_publish_file(backup, path)
            elif os.path.lexists(path):
                if record.get("expected_current_sha256") is None:
                    raise RuntimeError(
                        f"refusing to remove an unowned new index destination: {path}"
                    )
                os.unlink(path)
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    if errors:
        raise RuntimeError(
            "index deployment rollback incomplete; backups preserved: "
            + "; ".join(errors)
        )
    shutil.rmtree(snapshot["root"], ignore_errors=True)


def _discard_index_deployment_snapshot(snapshot):
    shutil.rmtree(snapshot["root"], ignore_errors=True)


def recover_pending_index_deployments(
    backup_parent, *, file_list_path, file_index_path, house_index_path,
    extension_index_path, before_mutation=None,
):
    """Finish a crash-left validated generation or preserve its recovery data."""
    backup_parent = os.path.abspath(backup_parent)
    expected_paths = {
        os.path.abspath(file_list_path),
        os.path.abspath(file_index_path),
        os.path.abspath(os.path.join(backup_parent, INDEX_GENERATION_FILENAME)),
        os.path.abspath(house_index_path),
        os.path.abspath(extension_index_path),
    }
    outcomes = []
    for name in sorted(os.listdir(backup_parent)):
        if not name.startswith("index-deploy-"):
            continue
        root = os.path.join(backup_parent, name)
        journal = os.path.join(root, "deployment.json")
        if not os.path.isfile(journal) or os.path.islink(journal):
            continue
        with open(journal, "r", encoding="utf-8") as handle:
            snapshot = json.load(handle)
        snapshot["root"] = root
        record_paths = {
            os.path.abspath(record["path"]) for record in snapshot.get("records", [])
        }
        if record_paths != expected_paths:
            raise RuntimeError(
                f"pending index deployment journal has unexpected paths: {journal}"
            )
        for record in snapshot["records"]:
            backup = record.get("backup")
            if backup is not None and os.path.commonpath(
                (root, os.path.abspath(backup))
            ) != root:
                raise RuntimeError(
                    f"pending index deployment backup escaped journal root: {backup}"
                )
        try:
            validate_index_generation(file_index_path, file_list_path)
        except Exception as exc:
            outcomes.append({
                "journal": journal,
                "status": "needs_review",
                "error": str(exc),
            })
            continue
        from mutation_io import inspect_regular_file

        list_sha256 = inspect_regular_file(file_list_path).sha256
        index_sha256 = inspect_regular_file(file_index_path).sha256
        manifest_path = os.path.join(backup_parent, INDEX_GENERATION_FILENAME)
        manifest_sha256 = inspect_regular_file(manifest_path).sha256
        if before_mutation is not None:
            before_mutation()
        _authorize_index_deployment(snapshot, {
            os.path.abspath(file_list_path): list_sha256,
            os.path.abspath(file_index_path): index_sha256,
            os.path.abspath(manifest_path): manifest_sha256,
            os.path.abspath(house_index_path): index_sha256,
            os.path.abspath(extension_index_path): index_sha256,
        })
        _atomic_publish_file(file_index_path, house_index_path)
        extension_parent = os.path.dirname(os.path.abspath(extension_index_path))
        if os.path.isdir(extension_parent):
            _atomic_publish_file(file_index_path, extension_index_path)
        _discard_index_deployment_snapshot(snapshot)
        outcomes.append({"journal": journal, "status": "committed"})
    return outcomes


def move_to_house(
    src_path, dst_dir, recent_dir, clean_name, s_log, source_label,
    is_pass=False, state_db_path=None, run_id=None,
):
    # 자모 폴더는 마커(〔P〕/〔Dn〕)가 없는 원래 제목 첫 글자로 결정한다.
    sort_name = strip_disambig_marker(strip_pass_marker(clean_name))
    first_char = sort_name[0] if sort_name else clean_name[0]
    folder_name = get_chosung(first_char)

    # pass 경유 입고는 파일명 자체에 마커를 새겨, 다음 회차 dedup에서 제외되게 한다.
    final_name_candidate = add_pass_marker(clean_name) if is_pass else clean_name

    # 충돌 시 붙는 suffix를 일반/pass로 분리한다.
    # - 일반 입고 충돌: _dup_N (원래 자리에 같은 이름이 이미 있다는 의미)
    # - pass 입고 충돌: _pass_N (사용자가 명시적으로 통과시킨 항목)
    conflict_suffix = PASS_CONFLICT_SUFFIX if is_pass else NORMAL_CONFLICT_SUFFIX

    target_folder = os.path.join(dst_dir, folder_name)
    auto_volume = None
    explicit_route = None
    if state_db_path and os.path.isfile(src_path) and not is_pass:
        import decision_store
        from library_work_management import resolve_work_route
        from volume_group_mutations import classify_folderling_volume_target

        conn = decision_store.connect_state_db(state_db_path)
        try:
            source_row = conn.execute(
                """
                SELECT f.file_id, fa.core_title, fa.readable_title
                FROM files AS f
                LEFT JOIN file_analysis AS fa ON fa.file_id = f.file_id
                WHERE f.canonical_path = ? AND f.active = 1
                """,
                (decision_store.canonicalize_path(src_path),),
            ).fetchone()
            if source_row is not None:
                explicit_route = resolve_work_route(
                    conn,
                    core_title=source_row["core_title"],
                    readable_title=source_row["readable_title"],
                    folder_name=os.path.basename(os.path.dirname(src_path)),
                )
                if explicit_route["status"] == "target":
                    target_folder = explicit_route["target_folder"]
                elif explicit_route["status"] == "route_conflict":
                    raise RuntimeError(
                        "사람 지정 작품 alias가 서로 다른 작품을 가리킵니다: "
                        f"works={explicit_route['work_bucket_ids']}"
                    )
                elif explicit_route.get("matched"):
                    raise RuntimeError(
                        "사람 지정 작품 alias는 있지만 활성 목적 폴더가 없습니다: "
                        f"work={explicit_route.get('work_bucket_id')}"
                    )
                else:
                    explicit_route = None
                    volume_decision = classify_folderling_volume_target(
                        conn,
                        source_file_id=source_row["file_id"],
                        house_root=dst_dir,
                        new_group_parent=target_folder,
                    )
                    if volume_decision["status"] == "coordinate_conflict":
                        raise VolumeCoordinateConflict(volume_decision)
                    if volume_decision["status"] == "target":
                        auto_volume = volume_decision
        finally:
            conn.close()
        if auto_volume is not None:
            proposed = os.path.join(auto_volume["target_folder"], final_name_candidate)
            if os.path.exists(proposed) or os.path.islink(proposed):
                auto_volume = None
            else:
                target_folder = auto_volume["target_folder"]
    candidate_path = os.path.join(target_folder, final_name_candidate)
    if os.path.exists(candidate_path):
        if explicit_route is not None:
            raise FileExistsError(
                "explicit work route destination already exists; "
                f"manual variant review required: {candidate_path}"
            )
        if os.path.isdir(src_path):
            if not state_db_path:
                raise FileExistsError(
                    "directory intake destination already exists; "
                    f"manual review required: {candidate_path}"
                )
            import decision_store

            resume_conn = decision_store.connect_state_db(state_db_path)
            try:
                resume_group, _ = _load_resumable_directory_manifest(
                    resume_conn, src_path, candidate_path
                )
            finally:
                resume_conn.close()
            if resume_group is None:
                raise FileExistsError(
                    "directory intake destination already exists without a matching "
                    f"failed group; manual review required: {candidate_path}"
                )
            dst_path = candidate_path
        else:
            dst_path = unique_path(target_folder, final_name_candidate, conflict_suffix)
    else:
        dst_path = candidate_path
    final_name = os.path.basename(dst_path)

    ensure_recent_link_slot(final_name, recent_dir, dst_path)

    from mutation_io import ensure_directory_nofollow
    ensure_directory_nofollow(target_folder)
    if state_db_path and os.path.isfile(src_path):
        import decision_store
        from dedup_mutations import ingest_to_house
        from volume_group_mutations import (
            ensure_volume_fingerprints,
            link_volume_relationships,
        )

        conn = decision_store.connect_state_db(state_db_path)
        try:
            row = conn.execute(
                "SELECT file_id FROM files WHERE canonical_path = ? AND active = 1",
                (decision_store.canonicalize_path(src_path),),
            ).fetchone()
            if row is None:
                raise RuntimeError("temp file is not reconciled in the decision DB")
            ingest_result = ingest_to_house(
                conn,
                source_file_id=row["file_id"],
                destination=dst_path,
                run_id=run_id or f"folderling-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
                routing=explicit_route,
            )
            if explicit_route is not None:
                relationship = ingest_result["routing"]
                source_label += (
                    f"[work-route work={relationship['work_bucket_id']} "
                    f"alias={relationship['alias_id']}] "
                )
            elif auto_volume is not None:
                volume_file_ids = auto_volume["existing_file_ids"] + [row["file_id"]]
                ensure_volume_fingerprints(conn, volume_file_ids)
                with decision_store.transaction(conn):
                    relationship = link_volume_relationships(
                        conn,
                        file_ids=volume_file_ids,
                        display_title=auto_volume["display_title"],
                        origin="strong_match",
                    )
                source_label += (
                    f"[volume-auto work={relationship['work_bucket_id']} "
                    f"core={auto_volume['core_title']}] "
                )
        finally:
            conn.close()
    elif state_db_path and os.path.isdir(src_path):
        import decision_store

        conn = decision_store.connect_state_db(state_db_path)
        try:
            _ingest_directory_group(
                conn,
                src_path,
                dst_path,
                run_id or f"folderling-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
            )
        finally:
            conn.close()
    else:
        raise RuntimeError("journaled state DB and active run are required for house intake")
    pass_flag = "pass=Y" if is_pass else "pass=N"
    s_log.write(
        f"[{get_now()}] {pass_flag} {source_label}{src_path} -> {dst_path}\n"
    )
    create_recent_link(dst_path, final_name, recent_dir)

    return dst_path
def iter_process_items(src_dir, pass_dir):
    normal_items = []
    unpack_items = []

    if os.path.exists(src_dir):
        for item in sorted(os.listdir(src_dir)):
            if item == PASS_DIR_NAME:
                continue
            item_path = os.path.join(src_dir, item)
            if is_unpack_source_dir(item):
                if os.path.isdir(item_path) and not os.path.islink(item_path):
                    unpack_items.extend(
                        (os.path.basename(path), path, False)
                        for path in iter_unpack_supported_files(item_path)
                    )
                continue
            normal_items.append((item, item_path, False))

    # 1.2.1: pass/는 더 이상 사람 판정 입력이 아니다. 기존 내용은 손대지 않고
    # dedup_decisions.py 사용 안내만 출력하며 폴더링 대상에서 제외한다.
    return normal_items + unpack_items


def _process_items_authorized(
    src_dir,
    dst_dir,
    script_dir,
    actual_run_id,
    manifest_path,
    *,
    state_db_path=None,
    event_callback=None,
    preflight_validated=False,
):
    workflow_started_at = time.perf_counter()
    performance_metrics = {}
    recent_dir = os.path.join(dst_dir, "_최근")
    success_log = os.path.join(script_dir, "success.log")
    fail_log = os.path.join(script_dir, "fail.log")
    file_list_json = os.path.join(script_dir, "file_list.json")
    file_index_json = os.path.join(script_dir, "file_index.json")
    pass_dir = os.path.join(src_dir, PASS_DIR_NAME)
    state_db_path = state_db_path or os.path.join(
        script_dir, ".dedup_state", "dedup_decisions.sqlite3"
    )

    intake_run_id = actual_run_id
    print(f"🔐 일회성 actual 승인 소비: {actual_run_id}")
    print(f"🧾 실행 전 manifest: {manifest_path}")
    emit_folderling_event(
        event_callback,
        "workflow_started",
        run_id=actual_run_id,
        manifest_path=str(manifest_path),
        source_root=os.path.abspath(src_dir),
        destination_root=os.path.abspath(dst_dir),
    )

    import decision_store

    mutation_phase_marked = False

    def mark_mutation_phase():
        nonlocal mutation_phase_marked
        if mutation_phase_marked:
            return
        marker_conn = decision_store.connect_state_db(state_db_path)
        try:
            with decision_store.transaction(marker_conn):
                decision_store.mark_actual_run_mutation_started(
                    marker_conn, actual_run_id
                )
        finally:
            marker_conn.close()
        mutation_phase_marked = True

    from mutation_io import ensure_directory_nofollow
    if not os.path.isdir(dst_dir):
        mark_mutation_phase()
    ensure_directory_nofollow(dst_dir)
    pending_deployments = recover_pending_index_deployments(
        script_dir,
        file_list_path=file_list_json,
        file_index_path=file_index_json,
        house_index_path=os.path.join(dst_dir, HOUSE_INDEX_FILENAME),
        extension_index_path=os.path.join(script_dir, EXTENSION_INDEX_PATH),
        before_mutation=mark_mutation_phase,
    )
    pending_review = [
        item for item in pending_deployments if item["status"] == "needs_review"
    ]
    if pending_review:
        raise RuntimeError(
            "pending index deployment needs review: " + pending_review[0]["error"]
        )

    legacy_pass_count = len(legacy_pass_items(pass_dir))
    if legacy_pass_count:
        print(
            f"⚠️ legacy pass/ 항목 {legacy_pass_count}개는 자동 입고하지 않습니다. "
            "dedup_decisions.py로 pair 판정을 등록하세요."
        )
        emit_folderling_event(
            event_callback,
            "legacy_pass_skipped",
            status="needs_review",
            item_count=legacy_pass_count,
            source_path=os.path.abspath(pass_dir),
            reason="legacy_pass_requires_pair_decision",
        )

    # ``unpack``과 기존 ``___*`` 묶음은 dedup/auditor에는 일반 temp 파일로
    # 참여하고, 아래 intake 단계에서 지원 파일만 개별 항목으로 펼쳐 입고한다.

    # ── 1단계: 중복 제거 + 검토 큐 격리 (house + temp 통합 스캔) ──
    print("=" * 60)
    print("📦 1단계: 중복/검토 큐 정리 (house + temp 통합)")
    print("=" * 60)
    emit_folderling_event(
        event_callback,
        "dedup_start",
        status="running",
        source_root=os.path.abspath(src_dir),
        house_root=os.path.abspath(dst_dir),
    )
    stage_started_at = time.perf_counter()
    snapshot = validate_index_snapshot(
        dst_dir,
        file_index_json,
        state_db_path,
        allowed_active_run_id=actual_run_id,
        verify_doctor=not preflight_validated,
    )
    performance_metrics["snapshot_seconds"] = round(
        time.perf_counter() - stage_started_at, 6
    )
    pre_index_mode = "verified_snapshot" if snapshot["valid"] else "full_scan_fallback"
    emit_folderling_event(
        event_callback,
        "snapshot_result",
        status="succeeded" if snapshot["valid"] else "fallback",
        index_mode=pre_index_mode,
        inventory_revision=snapshot.get("inventory_revision"),
        fallback_reason=snapshot.get("reason"),
    )
    if snapshot["valid"]:
        print(
            "⚡ 기존 house index 재사용: "
            f"revision={snapshot['inventory_revision'][:12]}"
        )
    else:
        print(
            "🔄 house 전체 Scanner fallback: "
            f"{snapshot['reason']}"
        )
    stage_started_at = time.perf_counter()
    dedup_summary = clean_duplicates(
        house_dir=dst_dir,
        temp_dir=src_dir,
        dry_run=False,
        index_path=file_index_json,
        rescan=not snapshot["valid"],
        move_suspects=True,
        delete_exact=True,
        include_temp=True,
        audit_suspects=True,
        update_index_after_run=False,
        state_db_path=state_db_path,
        require_state_db=True,
        authorized_run_id=actual_run_id,
        event_callback=event_callback,
        verified_index_entries=(snapshot["entries"] if snapshot["valid"] else None),
        verified_house_inventory=(
            snapshot["auditor_inventory"] if snapshot["valid"] else None
        ),
        before_non_cache_mutation=mark_mutation_phase,
    )
    performance_metrics["dedup_seconds"] = round(
        time.perf_counter() - stage_started_at, 6
    )
    emit_folderling_event(
        event_callback,
        "dedup_result",
        status=(
            "needs_review"
            if dedup_summary.get("review_queue_move_count", 0)
            or dedup_summary.get("managed_report_only_count", 0)
            else "succeeded"
        ),
        **{
            key: value
            for key, value in dedup_summary.items()
            if key not in {"pure_plan", "write_surfaces"}
        },
    )
    blocked_intake_paths = {
        normalize_nfc(os.path.abspath(os.fspath(path)))
        for path in dedup_summary.get("blocked_intake_paths", ())
    }
    post_intake_exact_candidate_paths = {
        normalize_nfc(os.path.abspath(os.fspath(path)))
        for path in dedup_summary.get("post_intake_exact_candidate_paths", ())
    }

    # The auditor may run read-only in standalone configurations. Ensure the
    # journal DB still has the same context-proven temp core/coordinate before
    # per-file routing and same-coordinate conflict checks begin.
    mark_mutation_phase()
    context_conn = decision_store.connect_state_db(state_db_path)
    try:
        with decision_store.transaction(context_conn):
            decision_store.sync_contextual_bare_volume_metadata(
                context_conn,
                target_sources=("temp",),
                evidence_sources=("house", "temp"),
            )
    finally:
        context_conn.close()

    # ── 2단계: 폴더링 (temp에 남아있는 파일을 house로 이동) ──
    print("=" * 60)
    print("📂 2단계: 폴더링 (temp → house)")
    print("=" * 60)
    move_count = 0
    pass_count = 0
    skipped_count = 0
    excluded_count = 0
    failure_count = 0
    empty_dir_cleanup_count = 0
    volume_conflict_hold_count = 0
    unpack_cleanup_results = []
    unpack_cleanup_issue_count = 0
    unpack_discarded_file_count = 0
    unpack_discarded_bytes = 0
    post_intake_exact_records = []
    post_intake_exact_resolved_skip_count = 0
    queue_exact_records = []
    queue_strong_records = []
    decided_review_cleanup_records = []

    stage_started_at = time.perf_counter()
    with open(success_log, "w", encoding="utf-8") as s_log, \
         open(fail_log, "w", encoding="utf-8") as f_log:

        items = iter_process_items(src_dir, pass_dir)
        item_total = len(items)
        emit_folderling_event(
            event_callback,
            "intake_start",
            status="running",
            total=item_total,
        )

        for item_index, (item, src_path, is_pass) in enumerate(
            tqdm(items, desc="분류 및 이동 중"), start=1
        ):
            if (
                not is_pass
                and normalize_nfc(os.path.abspath(src_path)) in blocked_intake_paths
            ):
                skipped_count += 1
                f_log.write(
                    f"[{get_now()}] 중복 관계가 여러 관리 작품과 충돌하여 입고 차단: "
                    f"{src_path}\n"
                )
                emit_folderling_event(
                    event_callback,
                    "intake_item",
                    status="needs_review",
                    index=item_index,
                    total=item_total,
                    path=src_path,
                    reason="managed_duplicate_identity_conflict",
                )
                continue
            if not is_pass and should_skip_source_item(item):
                excluded_count += 1
                emit_folderling_event(
                    event_callback,
                    "file_result",
                    stage="intake",
                    status="skipped",
                    reason="excluded_source_item",
                    source_path=os.path.abspath(src_path),
                    source_name=item,
                    completed=item_index,
                    total=item_total,
                )
                continue

            now_str = get_now()

            try:
                # 휴지통 꼬리표(_suspect_N / _dup_N / _pass_N)는 어디서 발견되든
                # normalize_filename(_→공백 치환)이 돌기 전에 미리 떼어낸다.
                raw_name = strip_trash_suffix(item)

                transport_name = normalize_filename(raw_name)
                if not transport_name:
                    failure_count += 1
                    f_log.write(
                        f"[{now_str}] {src_path} | 이름이 비어있어 실패 | "
                        "조치: 원본 파일명을 확인하고 직접 입고하거나 삭제\n"
                    )
                    emit_folderling_event(
                        event_callback,
                        "file_result",
                        stage="intake",
                        status="failed",
                        reason="empty_normalized_name",
                        source_path=os.path.abspath(src_path),
                        source_name=item,
                        error="정규화 후 파일명이 비었습니다.",
                        next_action="원본 파일명을 확인하고 다시 입고",
                        completed=item_index,
                        total=item_total,
                    )
                    continue

                is_dir = os.path.isdir(src_path)
                is_file = os.path.isfile(src_path)
                # ``[[...]]`` 제목 literal과 ``{{...}}`` 구조 힌트는 temp의
                # 새 file_id로 분석 의도를 운반한다. 중복 감사가 끝난 뒤 house
                # 표시 파일명에서는 운반용 괄호만 제거한다.
                clean_name = (
                    materialize_title_markup(transport_name)
                    if is_file else transport_name
                )
                ext = os.path.splitext(clean_name)[1].lower()

                if is_file and ext not in SUPPORTED_EXTENSIONS:
                    skipped_count += 1
                    emit_folderling_event(
                        event_callback,
                        "file_result",
                        stage="intake",
                        status="skipped",
                        reason="unsupported_extension",
                        source_path=os.path.abspath(src_path),
                        source_name=item,
                        extension=ext,
                        completed=item_index,
                        total=item_total,
                    )
                    continue
                if not is_dir and not is_file:
                    skipped_count += 1
                    emit_folderling_event(
                        event_callback,
                        "file_result",
                        stage="intake",
                        status="skipped",
                        reason="source_missing_or_not_regular",
                        source_path=os.path.abspath(src_path),
                        source_name=item,
                        completed=item_index,
                        total=item_total,
                    )
                    continue
                resumable_directory = bool(
                    is_dir
                    and has_resumable_directory_intake(state_db_path, src_path)
                )
                if is_dir and not directory_has_files(src_path) and not resumable_directory:
                    mark_mutation_phase()
                    removed = prune_empty_intake_tree(src_path)
                    empty_dir_cleanup_count += removed
                    s_log.write(
                        f"[{now_str}] [empty-dir] {src_path} | "
                        f"빈 디렉터리 {removed}개 정리\n"
                    )
                    emit_folderling_event(
                        event_callback,
                        "file_result",
                        stage="intake",
                        status="empty_directory_cleaned",
                        reason="empty_directory",
                        source_path=os.path.abspath(src_path),
                        source_name=item,
                        removed_directories=removed,
                        completed=item_index,
                        total=item_total,
                    )
                    continue

                label = "[pass] " if is_pass else ""
                destination_path = move_to_house(
                    src_path, dst_dir, recent_dir, clean_name, s_log, label,
                    is_pass=is_pass, state_db_path=state_db_path, run_id=intake_run_id,
                )
                if is_dir:
                    mark_mutation_phase()
                    empty_dir_cleanup_count += prune_empty_intake_tree(src_path)

                if is_pass:
                    pass_count += 1
                else:
                    move_count += 1
                emit_folderling_event(
                    event_callback,
                    "file_result",
                    stage="intake",
                    status="pass_ingested" if is_pass else "ingested",
                    reason="journaled_house_ingest",
                    source_path=os.path.abspath(src_path),
                    source_name=item,
                    destination_path=os.path.abspath(destination_path),
                    source_type="directory" if is_dir else "file",
                    completed=item_index,
                    total=item_total,
                )

            except VolumeCoordinateConflict as conflict_exc:
                try:
                    import decision_store
                    from volume_group_mutations import hold_folderling_volume_conflict

                    conn = decision_store.connect_state_db(state_db_path)
                    try:
                        source_row = conn.execute(
                            "SELECT file_id FROM files "
                            "WHERE canonical_path = ? AND active = 1 AND source = 'temp'",
                            (decision_store.canonicalize_path(src_path),),
                        ).fetchone()
                        if source_row is None:
                            raise RuntimeError(
                                "volume coordinate conflict source is not active in temp"
                            )
                        held = hold_folderling_volume_conflict(
                            conn,
                            source_file_id=source_row["file_id"],
                            temp_root=src_dir,
                            run_id=actual_run_id,
                            conflict=conflict_exc.decision,
                        )
                    finally:
                        conn.close()
                    volume_conflict_hold_count += 1
                    conflicts = ", ".join(held["conflicting_paths"])
                    s_log.write(
                        f"[{now_str}] [volume-coordinate-hold] "
                        f"{held['source_path']} -> {held['dest_path']} | "
                        f"existing={conflicts}\n"
                    )
                    print(
                        "  ⚠️ 동일 권 좌표 보류: "
                        f"{os.path.basename(held['source_path'])} "
                        f"→ {held['dest_path']}"
                    )
                    emit_folderling_event(
                        event_callback,
                        "file_result",
                        stage="intake",
                        status="warning",
                        reason="volume_coordinate_conflict",
                        source_path=str(held["source_path"]),
                        source_name=os.path.basename(str(held["source_path"])),
                        destination_path=str(held["dest_path"]),
                        existing_paths=list(held["conflicting_paths"]),
                        operation_id=held["operation_id"],
                        completed=item_index,
                        total=item_total,
                        next_action="기존 동일 권 파일과 직접 비교",
                    )
                except Exception as hold_exc:
                    failure_count += 1
                    f_log.write(
                        f"[{now_str}] {src_path} | 동일 권 좌표 보류 실패: {hold_exc} | "
                        "조치: 원본과 기존 동일 권 파일을 직접 비교\n"
                    )
                    emit_folderling_event(
                        event_callback,
                        "file_result",
                        stage="intake",
                        status="failed",
                        reason="volume_coordinate_hold_failed",
                        source_path=os.path.abspath(src_path),
                        source_name=item,
                        existing_paths=list(
                            conflict_exc.decision.get("conflicting_paths") or ()
                        ),
                        error=str(hold_exc),
                        completed=item_index,
                        total=item_total,
                        next_action="원본과 기존 동일 권 파일을 직접 비교",
                    )
            except Exception as e:
                failure_count += 1
                f_log.write(
                    f"[{now_str}] {src_path} | 예외: {e} | "
                    "조치: 경로/권한 확인 후 재실행 또는 수동 입고\n"
                )
                emit_folderling_event(
                    event_callback,
                    "file_result",
                    stage="intake",
                    status="failed",
                    reason=type(e).__name__,
                    source_path=os.path.abspath(src_path),
                    source_name=item,
                    error=str(e),
                    completed=item_index,
                    total=item_total,
                    next_action="경로·권한 확인 후 재실행",
                )

        # A temp-temp exact group has no canonical house keep during the
        # initial snapshot.  If this run just ingested the retained copy, close
        # the now-provable temp-house duplicate before unpack cleanup so one
        # Folderling run reaches a stable result instead of requiring a rerun.
        post_intake_exact_records = cleanup_post_intake_exact_duplicates(
            state_db_path,
            src_dir,
            actual_run_id,
            candidate_paths=post_intake_exact_candidate_paths,
        )
        post_intake_exact_resolved_skip_count = sum(
            normalize_nfc(os.path.abspath(record["source_path"]))
            in blocked_intake_paths
            for record in post_intake_exact_records
        )
        dedup_summary["post_intake_exact_quarantine_count"] = len(
            post_intake_exact_records
        )
        for record in post_intake_exact_records:
            s_log.write(
                f"[{get_now()}] [post-intake-exact] "
                f"{record['source_path']} -> {record['dest_path']} | "
                f"keep={record['keep_path']} sha256={record['raw_sha256']}\n"
            )
            emit_folderling_event(
                event_callback,
                "file_result",
                stage="post_intake_exact",
                status="exact_duplicate",
                reason="same_run_intake_convergence",
                source_path=record["source_path"],
                destination_path=record["dest_path"],
                existing_paths=[record["keep_path"]],
                operation_id=record["operation_id"],
                keep_origin_operation_id=record["keep_origin_operation_id"],
                duplicate_basis="raw_sha256_revalidated",
            )
        emit_folderling_event(
            event_callback,
            "post_intake_exact_result",
            status="succeeded",
            quarantine_count=len(post_intake_exact_records),
            resolved_skip_count=post_intake_exact_resolved_skip_count,
        )

        queue_exact_records = (
            cleanup_relationship_preserving_queue_exact_duplicates(
                state_db_path,
                src_dir,
                actual_run_id,
            )
        )
        dedup_summary["queue_exact_quarantine_count"] = len(
            queue_exact_records
        )
        for record in queue_exact_records:
            s_log.write(
                f"[{get_now()}] [queue-exact] "
                f"{record['source_path']} -> {record['dest_path']} | "
                f"keep={record['keep_path']} sha256={record['raw_sha256']}\n"
            )
            emit_folderling_event(
                event_callback,
                "file_result",
                stage="queue_exact",
                status="exact_duplicate",
                reason="relationship_preserving_queue_convergence",
                source_path=record["source_path"],
                destination_path=record["dest_path"],
                existing_paths=[record["keep_path"]],
                operation_id=record["operation_id"],
                duplicate_basis="raw_sha256_revalidated",
            )
        emit_folderling_event(
            event_callback,
            "queue_exact_result",
            status="succeeded",
            quarantine_count=len(queue_exact_records),
        )

        queue_strong_records = cleanup_pending_queue_strong_reviews(
            state_db_path,
            dst_dir,
            src_dir,
            actual_run_id,
        )
        dedup_summary["queue_strong_quarantine_count"] = len(
            queue_strong_records
        )
        for record in queue_strong_records:
            s_log.write(
                f"[{get_now()}] [queue-strong] "
                f"{record['source_path']} -> {record['dest_path']} | "
                f"keep={record['keep_path']} "
                f"classification={record['classification']}\n"
            )
            emit_folderling_event(
                event_callback,
                "file_result",
                stage="queue_strong",
                status=record["status"],
                reason="relationship_preserving_queue_strong_convergence",
                source_path=record["source_path"],
                destination_path=record["dest_path"],
                existing_paths=[record["keep_path"]],
                operation_id=record["operation_id"],
                review_id=record["review_id"],
                duplicate_basis=record["classification"],
            )
        emit_folderling_event(
            event_callback,
            "queue_strong_result",
            status="succeeded",
            quarantine_count=len(queue_strong_records),
        )

        # A fully warm pair cache can bypass the auditor's review-write path.
        # Close any machine review already vetoed by an active human edition
        # decision independently so a successful rerun never asks again.
        mark_mutation_phase()
        decided_review_cleanup_records = (
            cleanup_pending_active_distinct_decision_reviews(state_db_path)
        )
        decided_review_count = sum(
            len(record["review_ids"])
            for record in decided_review_cleanup_records
        )
        dedup_summary["active_decision_review_suppression_count"] = (
            decided_review_count
        )
        for record in decided_review_cleanup_records:
            s_log.write(
                f"[{get_now()}] [active-decision-review] "
                f"{record['candidate_path']} <-> {record['reference_path']} | "
                f"decision={record['decision_id']} reviews={record['review_ids']}\n"
            )
        emit_folderling_event(
            event_callback,
            "active_decision_review_result",
            status="succeeded",
            pair_count=len(decided_review_cleanup_records),
            review_count=decided_review_count,
        )

        # Wrapper cleanup and index publication are not per-file operations.
        # Mark this boundary durably so a server restart cannot mistake them
        # for a pre-mutation interruption merely because operation counts are 0.
        mark_mutation_phase()
        unpack_cleanup_results = cleanup_unpack_sources(
            src_dir, before_mutation=mark_mutation_phase
        )
        for cleanup in unpack_cleanup_results:
            status = cleanup["status"]
            if status == "cleaned":
                unpack_discarded_file_count += cleanup["discarded_files"]
                unpack_discarded_bytes += cleanup["discarded_bytes"]
                s_log.write(
                    f"[{get_now()}] [unpack-cleanup] {cleanup['path']} | "
                    f"부속 파일 {cleanup['discarded_files']}개 "
                    f"({cleanup['discarded_bytes']} bytes) 삭제\n"
                )
            else:
                unpack_cleanup_issue_count += 1
                f_log.write(
                    f"[{get_now()}] [unpack-preserved] {cleanup['path']} | "
                    f"status={status} "
                    f"remaining_supported={cleanup['remaining_supported']} "
                    f"error={cleanup.get('error', '')}\n"
                )
            emit_folderling_event(
                event_callback,
                "unpack_cleanup",
                status="succeeded" if status == "cleaned" else "needs_review",
                source_path=os.path.abspath(cleanup["path"]),
                source_name=cleanup["name"],
                cleanup_status=status,
                remaining_supported=cleanup["remaining_supported"],
                discarded_files=cleanup["discarded_files"],
                discarded_bytes=cleanup["discarded_bytes"],
                error=cleanup.get("error"),
                next_action=(
                    None if status == "cleaned"
                    else "남은 지원 파일 또는 심볼릭 링크를 확인한 뒤 재실행"
                ),
            )

        emit_folderling_event(
            event_callback,
            "intake_result",
            status=(
                "needs_review"
                if failure_count or volume_conflict_hold_count or unpack_cleanup_issue_count
                else "succeeded"
            ),
            total=item_total,
            move_count=move_count,
            pass_count=pass_count,
            skipped_count=skipped_count,
            resolved_skip_count=post_intake_exact_resolved_skip_count,
            excluded_count=excluded_count,
            failure_count=failure_count,
            volume_conflict_hold_count=volume_conflict_hold_count,
            empty_dir_cleanup_count=empty_dir_cleanup_count,
            unpack_cleanup_issue_count=unpack_cleanup_issue_count,
            unpack_discarded_file_count=unpack_discarded_file_count,
            unpack_discarded_bytes=unpack_discarded_bytes,
        )
    performance_metrics["intake_seconds"] = round(
        time.perf_counter() - stage_started_at, 6
    )

    # A bare-number cohort may become provable only after every incoming file
    # has reached house. Project those contextual cores/coordinates before the
    # all-auto-ready series query, without reading any book body.
    context_conn = decision_store.connect_state_db(state_db_path)
    try:
        with decision_store.transaction(context_conn):
            bare_volume_context = (
                decision_store.sync_contextual_bare_volume_metadata(
                    context_conn,
                    target_sources=("house",),
                    evidence_sources=("house",),
                )
            )
            bare_volume_catalog_rekeys = decision_store.migrate_catalog_title_keys(
                context_conn, bare_volume_context["rekeys"]
            )
    finally:
        context_conn.close()
    print(
        "🔢 숫자 권 문맥 반영: "
        f"{bare_volume_context['promoted_count']}개 문맥 판정, "
        f"좌표 {bare_volume_context['coordinate_changed']}개 갱신"
    )
    emit_folderling_event(
        event_callback,
        "bare_volume_context_result",
        status="succeeded",
        **{
            key: value
            for key, value in bare_volume_context.items()
            if key != "rekeys"
        },
        catalog_rekeys=bare_volume_catalog_rekeys,
    )

    # ── 3단계: 전체 시리즈 자동 묶기 ──
    # 이번 intake에서 건드린 제목만 보지 않는다. 과거부터 loose 상태였던
    # auto_ready 전체를 같은 actual run의 manifest/journal 아래 정리한다.
    print()
    print("=" * 60)
    print("📚 3단계: 전체 시리즈 자동 묶기")
    print("=" * 60)
    emit_folderling_event(
        event_callback,
        "series_group_start",
        status="running",
        scope="all_auto_ready",
    )
    stage_started_at = time.perf_counter()
    from volume_review import apply_auto_ready_volume_groups

    def series_progress(case_index, case_total, item_index, item_total, name):
        emit_folderling_event(
            event_callback,
            "series_group_item",
            status="running",
            case_index=case_index,
            case_total=case_total,
            item_index=item_index,
            item_total=item_total,
            source_name=name,
        )

    auto_volume_summary = apply_auto_ready_volume_groups(
        state_db_path,
        house_dir=dst_dir,
        temp_dir=src_dir,
        run_id=actual_run_id,
        progress=series_progress,
    )
    auto_volume_moved = auto_volume_summary.pop("moved")
    # Per-file evidence is already durable in operations and success.log.  Do
    # not duplicate thousands of records into the web job/event payload.
    auto_volume_summary.pop("applied", None)
    recent_retargeted_count = 0
    recent_preserved_count = 0
    with open(success_log, "a", encoding="utf-8") as s_log:
        for moved in auto_volume_moved:
            recent_status = retarget_owned_recent_link(
                recent_dir,
                moved["source_path"],
                moved["destination"],
            )
            if recent_status == "retargeted":
                recent_retargeted_count += 1
            elif recent_status == "preserved":
                recent_preserved_count += 1
            s_log.write(
                f"[{get_now()}] [series-auto] {moved['source_path']} -> "
                f"{moved['destination']} | recent={recent_status}\n"
            )
    performance_metrics["series_group_seconds"] = round(
        time.perf_counter() - stage_started_at, 6
    )
    emit_folderling_event(
        event_callback,
        "series_group_result",
        status=(
            "needs_review"
            if auto_volume_summary["remaining_summary"].get("review_required", 0)
            else "succeeded"
        ),
        scope="all_auto_ready",
        recent_retargeted_count=recent_retargeted_count,
        recent_preserved_count=recent_preserved_count,
        **auto_volume_summary,
    )
    print(
        "✅ 시리즈 자동 묶기: "
        f"{auto_volume_summary['applied_count']}개 작품, "
        f"{auto_volume_summary['moved_count']}개 파일 이동"
    )
    if auto_volume_summary["remaining_summary"].get("review_required", 0):
        print(
            "  - 사람 승인 필요: "
            f"{auto_volume_summary['remaining_summary']['review_required']}개 관계"
        )

    cleanup_recent_links(recent_dir, max_days=30)

    print(f"✅ 폴더링 완료: 입고 {move_count}개")
    if empty_dir_cleanup_count:
        print(f"  - temp 빈 디렉터리 {empty_dir_cleanup_count}개 정리")
    if pass_count > 0:
        print(f"  - pass 폴더 승인 항목 {pass_count}개 강제 입고됨")
    if volume_conflict_hold_count:
        print(f"  - 동일 권 좌표 보류 {volume_conflict_hold_count}개")
    if unpack_discarded_file_count:
        print(
            "  - unpack 부속 파일 삭제 "
            f"{unpack_discarded_file_count}개 ({unpack_discarded_bytes} bytes)"
        )
    if unpack_cleanup_issue_count:
        print(f"  - unpack 정리 보류 {unpack_cleanup_issue_count}개 묶음")
    print(f"→ 로그 파일({script_dir} 위치)을 확인하세요.")
    print("  - success.log / fail.log")

    # ── 4단계: 인덱스 갱신 ──
    print()
    print("=" * 60)
    print("🔄 4단계: 인덱스 갱신")
    print("=" * 60)
    emit_folderling_event(
        event_callback,
        "index_start",
        status="running",
    )
    index_mode = "state_db_projection"
    index_fallback_reason = None
    index_ready = False
    index_error = None
    index_deployment_error = None
    final_integrity_receipt = None
    stage_started_at = time.perf_counter()
    extension_index_json = os.path.join(script_dir, EXTENSION_INDEX_PATH)
    deployment_snapshot = None
    try:
        deployment_snapshot = _capture_index_deployment(
            [
                file_list_json,
                file_index_json,
                os.path.join(script_dir, INDEX_GENERATION_FILENAME),
                os.path.join(dst_dir, HOUSE_INDEX_FILENAME),
                extension_index_json,
            ],
            script_dir,
        )
    except Exception as snapshot_exc:
        failure_count += 1
        index_error = str(snapshot_exc)
        print(f"⚠️ 기존 인덱스 generation 보존 준비 실패: {snapshot_exc}")

    if deployment_snapshot is not None:
        try:
            projection = generate_file_list_from_state_db(
                dst_dir,
                file_list_json,
                file_index_json,
                state_db_path,
                allowed_active_run_id=actual_run_id,
                temp_root=src_dir,
            )
            final_integrity_receipt = projection.pop(
                "_state_integrity_receipt", None
            )
            if not projection["ok"]:
                raise RuntimeError("DB snapshot index generation returned failure")
            index_ready = True
            print("✨ file_list.json / file_index.json 증분 projection 완료")
        except IndexSnapshotStale as exc:
            index_mode = "full_scan_fallback"
            index_fallback_reason = str(exc)
            print(f"🔄 DB snapshot 검증 실패, 전체 Scanner fallback: {exc}")
            try:
                index_ok = generate_file_list(
                    [dst_dir],
                    file_list_json,
                    file_index_json,
                    state_db_path=state_db_path,
                    temp_root=src_dir,
                )
                if not index_ok:
                    raise RuntimeError("scanner index generation returned failure")
                index_ready = True
                print("✨ file_list.json / file_index.json 전체 갱신 완료")
            except Exception as fallback_exc:
                failure_count += 1
                index_error = str(fallback_exc)
                print(f"⚠️ 파일 인덱스 fallback 중 에러가 발생했습니다: {fallback_exc}")
        except Exception as e:
            failure_count += 1
            index_error = str(e)
            print(f"⚠️ 파일 인덱스 업데이트 중 에러가 발생했습니다: {e}")
    performance_metrics["index_generation_seconds"] = round(
        time.perf_counter() - stage_started_at, 6
    )

    stage_started_at = time.perf_counter()
    if index_ready:
        try:
            from mutation_io import inspect_regular_file

            list_sha256 = inspect_regular_file(file_list_json).sha256
            index_sha256 = inspect_regular_file(file_index_json).sha256
            manifest_sha256 = inspect_regular_file(
                os.path.join(script_dir, INDEX_GENERATION_FILENAME)
            ).sha256
            _authorize_index_deployment(
                deployment_snapshot,
                {
                    os.path.abspath(file_list_json): list_sha256,
                    os.path.abspath(file_index_json): index_sha256,
                    os.path.abspath(
                        os.path.join(script_dir, INDEX_GENERATION_FILENAME)
                    ): manifest_sha256,
                    os.path.abspath(
                        os.path.join(dst_dir, HOUSE_INDEX_FILENAME)
                    ): index_sha256,
                    os.path.abspath(extension_index_json): index_sha256,
                },
            )
            if not sync_house_index(file_index_json, dst_dir):
                raise RuntimeError("house index sync failed")
            extension_dir = os.path.dirname(extension_index_json)
            if os.path.isdir(extension_dir):
                if not sync_extension_index(file_index_json, script_dir):
                    raise RuntimeError("extension index sync failed")
            else:
                print("⚠️ 로컬 확장 폴더 없음: extension surface 배포를 건너뜁니다.")
        except Exception as e:
            failure_count += 1
            index_deployment_error = str(e)
            print(f"⚠️ 파일 인덱스 배포 중 에러가 발생했습니다: {e}")
    if deployment_snapshot is not None:
        if index_ready and not index_deployment_error:
            _discard_index_deployment_snapshot(deployment_snapshot)
        else:
            try:
                _restore_index_deployment(deployment_snapshot)
                print("↩️ 인덱스 배포 실패: 이전 generation을 모든 surface에 복원했습니다.")
            except Exception as rollback_exc:
                failure_count += 1
                index_deployment_error = (
                    f"{index_deployment_error or index_error}; rollback={rollback_exc}"
                )
                print(f"❌ 인덱스 generation 복원 실패: {rollback_exc}")
    performance_metrics["index_deployment_seconds"] = round(
        time.perf_counter() - stage_started_at, 6
    )
    emit_folderling_event(
        event_callback,
        "index_result",
        status="succeeded" if index_ready and not index_deployment_error else "failed",
        index_ready=index_ready,
        index_mode=index_mode,
        fallback_reason=index_fallback_reason,
        error=index_error,
        deployment_error=index_deployment_error,
    )

    # ── 요약 ──
    print()
    print("=" * 60)
    print("📊 최종 요약")
    print("=" * 60)
    if dedup_summary:
        print(
            "  강한 동일성: TXT/EPUB 현재 본문 재검증 후 "
            f"{dedup_summary.get('strong_equivalent_quarantine_count', 0)}개 "
            "최종 복구 가능 격리"
        )
        print(
            f"  중복/검토 큐: 정확 중복 {dedup_summary['exact_count']}개, "
            f"검토 큐 {dedup_summary.get('review_queue_move_count', dedup_summary['suspect_move_count'])}개 격리 "
            f"(같은 작가/미상 {dedup_summary.get('same_author_count', 0)}, "
            f"작가 충돌 {dedup_summary.get('author_conflict_count', 0)})"
        )
        print(
            "  최신판 교체: 완전 포함으로 증명된 짧은 판본 "
            f"{dedup_summary.get('contained_upgrade_count', 0)}개 자동 격리"
        )
        print(
            "  본문 95% 중복: 동일/중첩/외전 총량/화↔권 관계 "
            f"{dedup_summary.get('ordered_body_quarantine_count', 0)}개 자동 격리"
        )
    print(f"  폴더링  : 입고 {move_count}개, pass {pass_count}개")
    print(
        "  시리즈  : 작품 "
        f"{auto_volume_summary['applied_count']}개 자동 묶기, "
        f"파일 {auto_volume_summary['moved_count']}개 이동"
    )
    print(f"  좌표 충돌: warning 보류 {volume_conflict_hold_count}개")
    print(
        "  unpack   : 부속 삭제 "
        f"{unpack_discarded_file_count}개, 정리 보류 {unpack_cleanup_issue_count}개"
    )
    print(f"  빈 폴더 : temp 디렉터리 {empty_dir_cleanup_count}개 정리")
    if failure_count:
        print(f"  실패/부분 완료: {failure_count}건 (actual run은 failed 처리)")

    total = 0
    print("\n📊 폴더별 파일 개수 요약")
    for folder in sorted(os.listdir(dst_dir)):
        if folder == "_최근":
            continue
        path = os.path.join(dst_dir, folder)
        if os.path.isdir(path):
            count = len(os.listdir(path))
            total += count
            print(f"{folder}: {count}개")
    print(f"\n총합: {total}개")
    result = {
        "dedup_summary": dedup_summary,
        "move_count": move_count,
        "pass_count": pass_count,
        "skipped_count": skipped_count,
        "excluded_count": excluded_count,
        "legacy_pass_count": legacy_pass_count,
        "empty_dir_cleanup_count": empty_dir_cleanup_count,
        "failure_count": failure_count,
        "volume_conflict_hold_count": volume_conflict_hold_count,
        "auto_volume_summary": auto_volume_summary,
        "recent_retargeted_count": recent_retargeted_count,
        "recent_preserved_count": recent_preserved_count,
        "unpack_cleanup_results": unpack_cleanup_results,
        "unpack_cleanup_issue_count": unpack_cleanup_issue_count,
        "unpack_discarded_file_count": unpack_discarded_file_count,
        "unpack_discarded_bytes": unpack_discarded_bytes,
        "post_intake_exact_records": post_intake_exact_records,
        "queue_exact_records": queue_exact_records,
        "queue_strong_records": queue_strong_records,
        "decided_review_cleanup_records": decided_review_cleanup_records,
        "post_intake_exact_resolved_skip_count": (
            post_intake_exact_resolved_skip_count
        ),
        "pre_index_mode": pre_index_mode,
        "pre_index_fallback_reason": snapshot["reason"],
        "index_mode": index_mode,
        "index_fallback_reason": index_fallback_reason,
        "performance_metrics": performance_metrics,
    }
    result["review_required_count"] = (
        int(dedup_summary.get("review_queue_move_count", 0))
        + int(dedup_summary.get("managed_report_only_count", 0))
        + volume_conflict_hold_count
        + unpack_cleanup_issue_count
        + legacy_pass_count
        + max(0, skipped_count - post_intake_exact_resolved_skip_count)
        + int(
            auto_volume_summary["remaining_summary"].get("review_required", 0)
        )
    )
    performance_metrics["authorized_total_seconds"] = round(
        time.perf_counter() - workflow_started_at, 6
    )
    print(
        "⏱️ 단계별 시간: "
        + ", ".join(
            f"{name}={seconds:.2f}s"
            for name, seconds in performance_metrics.items()
        )
    )
    emit_folderling_event(
        event_callback,
        "authorized_performance_metrics",
        status="succeeded",
        **performance_metrics,
    )
    emit_folderling_event(
        event_callback,
        "folderling_summary",
        status=(
            "needs_review"
            if failure_count or result["review_required_count"]
            else "succeeded"
        ),
        **result,
    )
    # Same-process only; the outer terminal gate consumes and removes it before
    # returning a public result.
    result["_final_integrity_receipt"] = final_integrity_receipt
    return result


def _process_items_with_lock_held(
    src_dir,
    dst_dir,
    script_dir,
    state_db_path=None,
    *,
    event_callback=None,
    preflight_receipt=None,
):
    """Run Folderling while the caller owns the roots mutation lock."""
    import decision_store

    state_db_path = state_db_path or os.path.join(
        script_dir, ".dedup_state", "dedup_decisions.sqlite3"
    )
    if preflight_receipt is None:
        preflight_receipt = decision_store.consume_preflight_validation_receipt(
            state_db_path, dst_dir, src_dir
        )
    from volume_group_mutations import recover_abandoned_volume_staging

    volume_staging_recovery = recover_abandoned_volume_staging(
        state_db_path,
        house_root=dst_dir,
        temp_root=src_dir,
    )
    emit_folderling_event(
        event_callback,
        "volume_staging_recovery",
        status=(
            "needs_review" if volume_staging_recovery["issues"] else "succeeded"
        ),
        **volume_staging_recovery,
    )
    if volume_staging_recovery["issues"]:
        first = volume_staging_recovery["issues"][0]
        raise RuntimeError(
            "volume staging recovery needs review: "
            f"{first['path']} ({first['reason']})"
        )
    activation_started_at = time.perf_counter()
    actual_run_id, manifest_path = decision_store.prepare_actual_run(
        state_db_path,
        dst_dir,
        src_dir,
        preflight_receipt=preflight_receipt,
    )
    preflight_validated = preflight_receipt is not None
    activation_seconds = round(time.perf_counter() - activation_started_at, 6)
    emit_folderling_event(
        event_callback,
        "actual_run_started",
        status="running",
        run_id=actual_run_id,
        manifest_path=str(manifest_path),
    )
    try:
        import review_actions
        action_summary = {"accepted": [], "discarded": []}
        if review_actions.has_action_files(src_dir):
            action_conn = decision_store.connect_state_db(state_db_path)
            try:
                action_summary = review_actions.process_claimed_actions(
                    action_conn,
                    temp_dir=src_dir,
                    house_dir=dst_dir,
                    run_id=actual_run_id,
                )
            finally:
                action_conn.close()
        action_count = (
            len(action_summary["accepted"]) + len(action_summary["discarded"])
        )
        if action_count:
            print(
                "📥 검토 처리함 완료: "
                f"house {len(action_summary['accepted'])}개, "
                f"delete {len(action_summary['discarded'])}개"
            )
        emit_folderling_event(
            event_callback,
            "review_actions_result",
            status="succeeded",
            accepted_count=len(action_summary["accepted"]),
            discarded_count=len(action_summary["discarded"]),
        )
        result = _process_items_authorized(
            src_dir,
            dst_dir,
            script_dir,
            actual_run_id,
            manifest_path,
            state_db_path=state_db_path,
            event_callback=event_callback,
            preflight_validated=preflight_validated,
        )
        result["review_action_summary"] = action_summary
        result["volume_staging_recovery"] = volume_staging_recovery
        metrics = result.setdefault("performance_metrics", {})
        metrics["activation_seconds"] = activation_seconds
    except (Exception, KeyboardInterrupt) as exc:
        emit_folderling_event(
            event_callback,
            "workflow_failed",
            status="failed",
            run_id=actual_run_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        conn = decision_store.connect_state_db(state_db_path)
        try:
            decision_store.finish_actual_run(
                conn, actual_run_id, success=False, error=str(exc)
            )
        finally:
            conn.close()
        raise
    failure_count = int(result.get("failure_count", 0))
    final_integrity_receipt = result.pop("_final_integrity_receipt", None)
    reuse_final_integrity = decision_store.state_integrity_receipt_is_current(
        final_integrity_receipt,
        state_db_path,
        run_id=actual_run_id,
    )
    doctor_started_at = time.perf_counter()
    conn = decision_store.connect_state_db(state_db_path)
    try:
        final_issues = decision_store.doctor_issues(
            conn,
            allowed_active_run_id=actual_run_id,
            check_integrity=not reuse_final_integrity,
        )
        result["final_doctor_issue_count"] = len(final_issues)
        result["final_doctor_first_issue"] = final_issues[0] if final_issues else None
        if final_issues:
            failure_count += 1
            result["failure_count"] = failure_count
        emit_folderling_event(
            event_callback,
            "final_doctor_result",
            status="succeeded" if not final_issues else "failed",
            issue_count=len(final_issues),
            first_issue=final_issues[0] if final_issues else None,
        )
        decision_store.finish_actual_run(
            conn,
            actual_run_id,
            success=failure_count == 0,
            error=(f"folderling partial failure count: {failure_count}" if failure_count else None),
        )
    finally:
        conn.close()
    result.setdefault("performance_metrics", {})["final_doctor_seconds"] = round(
        time.perf_counter() - doctor_started_at, 6
    )
    result["performance_metrics"]["final_integrity_receipt_reused"] = bool(
        reuse_final_integrity
    )
    emit_folderling_event(
        event_callback,
        "performance_metrics",
        status="succeeded" if failure_count == 0 else "needs_review",
        **result["performance_metrics"],
    )
    emit_folderling_event(
        event_callback,
        "actual_run_finished",
        status="succeeded" if failure_count == 0 else "needs_review",
        run_id=actual_run_id,
        failure_count=failure_count,
    )
    return result


def process_items(
    src_dir, dst_dir, script_dir, state_db_path=None, *, event_callback=None
):
    """Consume and own one persistent actual run for the complete folderling workflow."""
    from mutation_io import mutation_lock_for_roots
    with mutation_lock_for_roots(dst_dir, src_dir, "folderling-command"):
        return _process_items_with_lock_held(
            src_dir,
            dst_dir,
            script_dir,
            state_db_path=state_db_path,
            event_callback=event_callback,
        )


def main():
    src_dir, dst_dir = parse_args(sys.argv)
    script_dir = str(PROJECT_ROOT)
    result = process_items(src_dir, dst_dir, script_dir)
    return 2 if result.get("failure_count", 0) else 0


if __name__ == "__main__":
    sys.exit(main())
