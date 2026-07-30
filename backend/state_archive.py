#!/usr/bin/env python3
"""Verified cold archive lifecycle for unreferenced state DB backups."""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from mutation_io import (
    SourceIdentityChanged,
    canonical_absolute_path,
    ensure_directory_nofollow,
    inspect_regular_file,
    inspect_regular_file_at,
    opened_directory_nofollow,
    opened_regular_file_nofollow,
    read_json_with_evidence,
    unlink_owned,
)
from project_paths import STATE_DB


ARCHIVE_VERSION = "1.4.10"
ARCHIVE_SCHEMA_VERSION = 1
DEFAULT_KEEP_LATEST_UNREFERENCED = 2
CHUNK_SIZE = 1024 * 1024


def _canonical(path) -> str:
    # Keep the lexical managed root.  Only macOS's stable /var and /tmp
    # aliases are folded; a general resolve()/realpath() would erase a
    # ``backups`` symlink and make an outside directory look authorized.
    return unicodedata.normalize(
        "NFC", os.fspath(canonical_absolute_path(Path(path).expanduser()))
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_payload(payload) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_json_write(path: Path, payload) -> None:
    ensure_directory_nofollow(path.parent)
    with opened_directory_nofollow(path.parent) as directory_fd:
        _atomic_json_write_at(
            directory_fd,
            path.name,
            payload,
            managed_directory=path.parent,
        )


def _assert_pinned_directory_current(directory: Path, descriptor: int) -> None:
    """Fail if a lexical managed root no longer names the pinned directory."""
    current = directory.lstat()
    pinned = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino)
    ):
        raise SourceIdentityChanged(
            f"managed directory changed while pinned: {directory}"
        )


def _entry_exists_at(directory_fd: int, leaf: str) -> bool:
    try:
        os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _open_unique_temp_at(
    directory_fd: int, *, prefix: str, suffix: str
) -> tuple[int, str]:
    """Create a no-follow temporary leaf beneath a pinned directory."""
    flags = (
        os.O_RDWR | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    for _attempt in range(128):
        leaf = f"{prefix}{secrets.token_hex(12)}{suffix}"
        try:
            return os.open(leaf, flags, 0o600, dir_fd=directory_fd), leaf
        except FileExistsError:
            continue
    raise FileExistsError("could not allocate a unique temporary file")


def _atomic_json_write_at(
    directory_fd: int,
    leaf: str,
    payload,
    *,
    managed_directory: Path | None = None,
) -> None:
    """Publish JSON without releasing or re-resolving its parent directory."""
    if managed_directory is not None:
        _assert_pinned_directory_current(managed_directory, directory_fd)
    descriptor, temporary = _open_unique_temp_at(
        directory_fd, prefix=f".{leaf}.", suffix=".tmp"
    )
    published = False
    completed = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        if _entry_exists_at(directory_fd, leaf):
            raise FileExistsError(leaf)
        os.link(
            temporary,
            leaf,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        published = True
        os.unlink(temporary, dir_fd=directory_fd)
        temporary = None
        os.fsync(directory_fd)
        if managed_directory is not None:
            _assert_pinned_directory_current(managed_directory, directory_fd)
        completed = True
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not completed and published:
            try:
                os.unlink(leaf, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except FileNotFoundError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


@contextmanager
def _opened_regular_at(directory_fd: int, leaf: str, display_path: Path):
    if Path(leaf).name != leaf or leaf in {"", ".", ".."}:
        raise ValueError(f"expected a single file name, got: {leaf!r}")
    descriptor = os.open(
        leaf,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        dir_fd=directory_fd,
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"source is not a regular file: {display_path}")
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _opened_archive_file(archive_path: Path, directory_fd: int | None = None):
    if directory_fd is None:
        with opened_regular_file_nofollow(archive_path) as descriptor:
            yield descriptor
    else:
        with _opened_regular_at(
            directory_fd, archive_path.name, archive_path
        ) as descriptor:
            yield descriptor


@contextmanager
def _archive_lock(state_dir: Path):
    ensure_directory_nofollow(state_dir)
    lock_path = state_dir / ".state_archive.lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _connect_readonly(state_db: Path):
    uri = f"file:{state_db.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _referenced_backup_paths(connection) -> set[str]:
    referenced = {
        _canonical(row[0])
        for row in connection.execute(
            "SELECT DISTINCT backup_path FROM actual_runs WHERE backup_path IS NOT NULL"
        )
        if row[0]
    }
    approved = connection.execute(
        "SELECT value FROM settings WHERE key = 'approved_backup'"
    ).fetchone()
    if approved is not None and approved[0]:
        referenced.add(_canonical(approved[0]))
    return referenced


def _maintenance_blockers(connection) -> list[str]:
    blockers = []
    open_runs = connection.execute(
        "SELECT COUNT(*) FROM actual_runs WHERE state IN ('approved', 'active')"
    ).fetchone()[0]
    if open_runs:
        blockers.append(f"open_actual_runs:{open_runs}")
    unfinished_operations = connection.execute(
        "SELECT COUNT(*) FROM operations "
        "WHERE state IN ('planned', 'fs_done', 'db_done')"
    ).fetchone()[0]
    if unfinished_operations:
        blockers.append(f"unfinished_operations:{unfinished_operations}")
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "operation_groups" in tables:
        unfinished_groups = connection.execute(
            "SELECT COUNT(*) FROM operation_groups "
            "WHERE state IN ('planned', 'fs_done', 'db_done')"
        ).fetchone()[0]
        if unfinished_groups:
            blockers.append(f"unfinished_operation_groups:{unfinished_groups}")
    return blockers


def _archive_paths(state_db: Path, source: Path) -> tuple[Path, Path]:
    root = state_db.parent / "cold_archive" / "backups"
    return root / f"{source.name}.gz", root / f"{source.name}.archive.json"


def _plan_hash_payload(plan) -> dict:
    return {
        "schema_version": plan["schema_version"],
        "kind": plan["kind"],
        "archive_version": plan["archive_version"],
        "state_db": plan["state_db"],
        "backup_dir": plan["backup_dir"],
        "keep_latest_unreferenced": plan["keep_latest_unreferenced"],
        "blockers": plan["blockers"],
        "referenced_paths": plan["referenced_paths"],
        "retained_unreferenced_paths": plan["retained_unreferenced_paths"],
        "unsafe_paths": plan["unsafe_paths"],
        "items": plan["items"],
    }


def build_backup_archive_plan(
    state_db=STATE_DB,
    *,
    keep_latest_unreferenced=DEFAULT_KEEP_LATEST_UNREFERENCED,
):
    """Return a read-only, identity-bound plan for unreferenced backups."""
    if keep_latest_unreferenced < 0:
        raise ValueError("keep_latest_unreferenced must be non-negative")
    state_db = Path(state_db).expanduser().resolve()
    if not state_db.is_file():
        raise FileNotFoundError(state_db)
    backup_dir = state_db.parent / "backups"
    connection = _connect_readonly(state_db)
    try:
        blockers = _maintenance_blockers(connection)
        referenced = _referenced_backup_paths(connection)
    finally:
        connection.close()

    safe = []
    unsafe_paths = []
    try:
        backup_root_info = backup_dir.lstat()
    except FileNotFoundError:
        backup_root_info = None
    if backup_root_info is not None:
        if not stat.S_ISDIR(backup_root_info.st_mode):
            raise RuntimeError(
                f"managed backup root is not a real directory: {backup_dir}"
            )
        # Pin the directory itself. lstat alone still permits replacement with
        # a symlink before a path-based iterdir().
        with opened_directory_nofollow(backup_dir) as backup_fd:
            for name in os.listdir(backup_fd):
                if Path(name).suffix != ".sqlite3":
                    continue
                source = backup_dir / name
                try:
                    info = os.stat(name, dir_fd=backup_fd, follow_symlinks=False)
                except OSError as exc:
                    unsafe_paths.append({"path": str(source), "reason": str(exc)})
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    unsafe_paths.append({
                        "path": str(source),
                        "reason": "not_single_link_regular_file",
                    })
                    continue
                safe.append((info.st_mtime_ns, source, info))

    safe.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    unreferenced = [item for item in safe if _canonical(item[1]) not in referenced]
    retained = unreferenced[:keep_latest_unreferenced]
    eligible = unreferenced[keep_latest_unreferenced:]
    items = []
    for _mtime, source, info in eligible:
        archive_path, metadata_path = _archive_paths(state_db, source)
        items.append({
            "source_path": _canonical(source),
            "archive_path": _canonical(archive_path),
            "metadata_path": _canonical(metadata_path),
            "dev": info.st_dev,
            "ino": info.st_ino,
            "ctime_ns": info.st_ctime_ns,
            "mtime_ns": info.st_mtime_ns,
            "size": info.st_size,
        })
    items.sort(key=lambda item: item["source_path"])
    plan = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "kind": "state_backup_archive_plan",
        "archive_version": ARCHIVE_VERSION,
        "generated_at": _utc_now(),
        "state_db": str(state_db),
        "backup_dir": _canonical(backup_dir),
        "keep_latest_unreferenced": keep_latest_unreferenced,
        "blockers": sorted(blockers),
        "referenced_paths": sorted(referenced),
        "retained_unreferenced_paths": sorted(
            _canonical(path) for _mtime, path, _info in retained
        ),
        "unsafe_paths": sorted(unsafe_paths, key=lambda item: item["path"]),
        "items": items,
        "eligible_count": len(items),
        "eligible_bytes": sum(item["size"] for item in items),
    }
    plan["plan_sha256"] = _hash_payload(_plan_hash_payload(plan))
    return plan


def _sqlite_integrity(path: Path) -> None:
    uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    if result != "ok":
        raise RuntimeError(f"backup integrity_check failed: {path}: {result}")


def _sqlite_integrity_fd(descriptor: int, display_path: Path) -> None:
    """Run SQLite integrity against the same pinned restore file descriptor."""
    fd_root = next(
        (root for root in ("/dev/fd", "/proc/self/fd") if os.path.isdir(root)),
        None,
    )
    if fd_root is None:
        raise RuntimeError("descriptor-backed SQLite validation is unavailable")
    uri = f"file:{fd_root}/{descriptor}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    if result != "ok":
        raise RuntimeError(
            f"backup integrity_check failed: {display_path}: {result}"
        )


def _inspect_compressed_archive(
    archive_path: Path,
    *,
    expected_sha256: str,
    expected_size: int,
    directory_fd: int | None = None,
) -> dict:
    """Recheck the final compressed object without re-inflating its raw bytes."""
    with _opened_archive_file(archive_path, directory_fd) as descriptor:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RuntimeError(
                f"cold archive is not an owned regular file: {archive_path}"
            )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev, before.st_ino, before.st_ctime_ns,
            before.st_size, before.st_mtime_ns,
        )
        after_identity = (
            after.st_dev, after.st_ino, after.st_ctime_ns,
            after.st_size, after.st_mtime_ns,
        )
        if before_identity != after_identity:
            raise SourceIdentityChanged(
                f"cold archive changed while read: {archive_path}"
            )
        sha256 = digest.hexdigest()
        if after.st_size != expected_size or sha256 != expected_sha256:
            raise RuntimeError(f"cold archive SHA-256 mismatch: {archive_path}")
        return {"archive_size": after.st_size, "archive_sha256": sha256}


def _inspect_gzip_archive(
    archive_path: Path,
    *,
    expected_archive_sha256=None,
    expected_archive_size=None,
    expected_raw_sha256=None,
    expected_raw_size=None,
    directory_fd: int | None = None,
) -> dict:
    """Hash compressed and raw bytes from one pinned no-follow descriptor."""
    with _opened_archive_file(archive_path, directory_fd) as descriptor:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RuntimeError(f"cold archive is not an owned regular file: {archive_path}")
        archive_digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, CHUNK_SIZE)
            if not chunk:
                break
            archive_digest.update(chunk)
        archive_sha256 = archive_digest.hexdigest()
        if (
            expected_archive_sha256 is not None
            and archive_sha256 != expected_archive_sha256
        ) or (
            expected_archive_size is not None
            and before.st_size != expected_archive_size
        ):
            raise RuntimeError(f"cold archive SHA-256 mismatch: {archive_path}")
        os.lseek(descriptor, 0, os.SEEK_SET)
        raw_digest = hashlib.sha256()
        raw_size = 0
        duplicate = os.dup(descriptor)
        try:
            with os.fdopen(duplicate, "rb") as raw_stream:
                try:
                    with gzip.GzipFile(fileobj=raw_stream, mode="rb") as compressed:
                        for chunk in iter(lambda: compressed.read(CHUNK_SIZE), b""):
                            raw_size += len(chunk)
                            if (
                                expected_raw_size is not None
                                and raw_size > expected_raw_size
                            ):
                                raise RuntimeError(
                                    f"cold archive expands beyond expected size: {archive_path}"
                                )
                            raw_digest.update(chunk)
                except (OSError, EOFError) as exc:
                    raise RuntimeError(
                        f"cold archive cannot be decompressed: {archive_path}: {exc}"
                    ) from exc
        finally:
            # fdopen owns ``duplicate`` after successful construction.  This
            # branch only matters if construction itself failed.
            try:
                os.close(duplicate)
            except OSError:
                pass
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev, before.st_ino, before.st_ctime_ns,
            before.st_size, before.st_mtime_ns,
        )
        after_identity = (
            after.st_dev, after.st_ino, after.st_ctime_ns,
            after.st_size, after.st_mtime_ns,
        )
        if before_identity != after_identity:
            raise SourceIdentityChanged(f"cold archive changed while read: {archive_path}")
        raw_sha256 = raw_digest.hexdigest()
        if (
            expected_raw_size is not None and raw_size != expected_raw_size
        ) or (
            expected_raw_sha256 is not None and raw_sha256 != expected_raw_sha256
        ):
            raise RuntimeError(f"cold archive raw content mismatch: {archive_path}")
        return {
            "archive_size": after.st_size,
            "archive_sha256": archive_sha256,
            "raw_size": raw_size,
            "raw_sha256": raw_sha256,
        }


def _compress_backup(
    source: Path,
    archive_path: Path,
    expected,
    *,
    archive_dir_fd: int | None = None,
) -> None:
    if archive_dir_fd is None:
        ensure_directory_nofollow(archive_path.parent)
        with opened_directory_nofollow(archive_path.parent) as directory_fd:
            _compress_backup(
                source,
                archive_path,
                expected,
                archive_dir_fd=directory_fd,
            )
        return

    _assert_pinned_directory_current(archive_path.parent, archive_dir_fd)
    descriptor, temporary = _open_unique_temp_at(
        archive_dir_fd, prefix=f".{archive_path.name}.", suffix=".tmp"
    )
    published = False
    completed = False
    try:
        with opened_regular_file_nofollow(source) as source_fd:
            before = os.fstat(source_fd)
            identity = (
                before.st_dev, before.st_ino, before.st_ctime_ns,
                before.st_size, before.st_mtime_ns,
            )
            expected_identity = (
                expected.dev, expected.ino, expected.ctime_ns,
                expected.size, expected.mtime_ns,
            )
            if identity != expected_identity:
                raise SourceIdentityChanged(
                    f"archive source identity changed: {source}"
                )
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "wb", closefd=False) as raw_output:
                with gzip.GzipFile(
                    filename="", mode="wb", compresslevel=6,
                    fileobj=raw_output, mtime=0,
                ) as compressed:
                    while True:
                        chunk = os.read(source_fd, CHUNK_SIZE)
                        if not chunk:
                            break
                        digest.update(chunk)
                        compressed.write(chunk)
                raw_output.flush()
                os.fsync(raw_output.fileno())
            after = os.fstat(source_fd)
            after_identity = (
                after.st_dev, after.st_ino, after.st_ctime_ns,
                after.st_size, after.st_mtime_ns,
            )
            if (
                after_identity != expected_identity
                or digest.hexdigest() != expected.sha256
            ):
                raise SourceIdentityChanged(
                    f"archive source changed while read: {source}"
                )
            _assert_pinned_directory_current(
                archive_path.parent, archive_dir_fd
            )
            if _entry_exists_at(archive_dir_fd, archive_path.name):
                raise FileExistsError(archive_path)
            os.link(
                temporary,
                archive_path.name,
                src_dir_fd=archive_dir_fd,
                dst_dir_fd=archive_dir_fd,
                follow_symlinks=False,
            )
            published = True
            os.unlink(temporary, dir_fd=archive_dir_fd)
            temporary = None
            os.fsync(archive_dir_fd)
            _assert_pinned_directory_current(
                archive_path.parent, archive_dir_fd
            )
            completed = True
    finally:
        os.close(descriptor)
        if not completed and published:
            try:
                os.unlink(archive_path.name, dir_fd=archive_dir_fd)
                os.fsync(archive_dir_fd)
            except FileNotFoundError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=archive_dir_fd)
            except FileNotFoundError:
                pass


def _archive_metadata(
    state_db: Path,
    source: Path,
    archive_path: Path,
    expected,
    *,
    archive_dir_fd: int | None = None,
) -> dict:
    archive_evidence = _inspect_gzip_archive(
        archive_path,
        expected_raw_sha256=expected.sha256,
        expected_raw_size=expected.size,
        directory_fd=archive_dir_fd,
    )
    if (
        archive_evidence["raw_size"] != expected.size
        or archive_evidence["raw_sha256"] != expected.sha256
    ):
        raise RuntimeError(f"archive round-trip verification failed: {source}")
    return {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "kind": "state_backup_archive_object",
        "archive_version": ARCHIVE_VERSION,
        "created_at": _utc_now(),
        "state_db": str(state_db.resolve()),
        "source_path": _canonical(source),
        "archive_path": _canonical(archive_path),
        "source": {
            "dev": expected.dev,
            "ino": expected.ino,
            "ctime_ns": expected.ctime_ns,
            "mtime_ns": expected.mtime_ns,
            "size": expected.size,
            "sha256": expected.sha256,
        },
        "archive": {
            "size": archive_evidence["archive_size"],
            "sha256": archive_evidence["archive_sha256"],
            "format": "gzip",
        },
    }


def _load_metadata(path: Path, *, directory_fd: int | None = None) -> dict:
    if directory_fd is None:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError(
                f"archive metadata is not an owned regular file: {path}"
            )
        _evidence, payload = read_json_with_evidence(path)
    else:
        with _opened_regular_at(directory_fd, path.name, path) as descriptor:
            before = os.fstat(descriptor)
            if before.st_nlink != 1:
                raise RuntimeError(
                    f"archive metadata is not an owned regular file: {path}"
                )
            raw = bytearray()
            while True:
                chunk = os.read(descriptor, CHUNK_SIZE)
                if not chunk:
                    break
                raw.extend(chunk)
            after = os.fstat(descriptor)
            if (
                before.st_dev, before.st_ino, before.st_ctime_ns,
                before.st_size, before.st_mtime_ns,
            ) != (
                after.st_dev, after.st_ino, after.st_ctime_ns,
                after.st_size, after.st_mtime_ns,
            ):
                raise SourceIdentityChanged(
                    f"archive metadata changed while read: {path}"
                )
            payload = json.loads(raw.decode("utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != ARCHIVE_SCHEMA_VERSION
        or payload.get("kind") != "state_backup_archive_object"
    ):
        raise ValueError(f"unsupported archive metadata: {path}")
    return payload


def _verify_cold_object_before_consume(
    state_db: Path,
    source: Path,
    archive_path: Path,
    metadata_path: Path,
    source_evidence,
    *,
    archive_dir_fd: int | None = None,
) -> dict:
    """Reload durable metadata and rehash gzip immediately before source unlink."""
    metadata = _load_metadata(metadata_path, directory_fd=archive_dir_fd)
    archive_metadata = metadata.get("archive", {})
    source_metadata = metadata.get("source", {})
    if (
        metadata.get("state_db") != str(state_db)
        or metadata.get("source_path") != _canonical(source)
        or metadata.get("archive_path") != _canonical(archive_path)
        or source_metadata.get("sha256") != source_evidence.sha256
        or source_metadata.get("size") != source_evidence.size
        or archive_metadata.get("format") != "gzip"
        or not isinstance(archive_metadata.get("sha256"), str)
        or not isinstance(archive_metadata.get("size"), int)
    ):
        raise RuntimeError(f"archive evidence mismatch before consume: {archive_path}")
    _inspect_compressed_archive(
        archive_path,
        expected_sha256=archive_metadata["sha256"],
        expected_size=archive_metadata["size"],
        directory_fd=archive_dir_fd,
    )
    return metadata


def archive_backup_path(state_db, item) -> dict:
    """Archive and consume one currently unreferenced backup."""
    state_db = Path(state_db).expanduser().resolve()
    source = Path(_canonical(item["source_path"]))
    archive_path = Path(_canonical(item["archive_path"]))
    metadata_path = Path(_canonical(item["metadata_path"]))
    expected_backup_root = state_db.parent / "backups"
    expected_archive_root = state_db.parent / "cold_archive" / "backups"
    if (
        source.parent != expected_backup_root
        or archive_path.parent != expected_archive_root
        or metadata_path.parent != expected_archive_root
        or source.suffix != ".sqlite3"
        or archive_path.name != f"{source.name}.gz"
        or metadata_path.name != f"{source.name}.archive.json"
    ):
        raise RuntimeError("archive item is outside managed state roots")
    with opened_directory_nofollow(expected_backup_root):
        pass
    ensure_directory_nofollow(expected_archive_root)
    current = source.lstat()
    identity = (
        current.st_dev, current.st_ino, current.st_ctime_ns,
        current.st_mtime_ns, current.st_size,
    )
    planned = (
        item["dev"], item["ino"], item["ctime_ns"],
        item["mtime_ns"], item["size"],
    )
    if identity != planned or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
        raise SourceIdentityChanged(f"archive plan source changed: {source}")

    connection = _connect_readonly(state_db)
    try:
        if _canonical(source) in _referenced_backup_paths(connection):
            raise RuntimeError(f"backup became referenced after planning: {source}")
    finally:
        connection.close()

    source_evidence = inspect_regular_file(source)
    _sqlite_integrity(source)
    with opened_directory_nofollow(expected_archive_root) as archive_fd:
        _assert_pinned_directory_current(expected_archive_root, archive_fd)
        archive_exists = _entry_exists_at(archive_fd, archive_path.name)
        metadata_exists = _entry_exists_at(archive_fd, metadata_path.name)
        if archive_exists:
            if metadata_exists:
                metadata = _load_metadata(
                    metadata_path, directory_fd=archive_fd
                )
                archive_evidence = _inspect_gzip_archive(
                    archive_path,
                    expected_archive_sha256=(
                        metadata.get("archive", {}).get("sha256")
                    ),
                    expected_archive_size=(
                        metadata.get("archive", {}).get("size")
                    ),
                    expected_raw_sha256=source_evidence.sha256,
                    expected_raw_size=source_evidence.size,
                    directory_fd=archive_fd,
                )
            else:
                # Crash window: the gzip object was durably linked but metadata
                # was not. Reconstruct it only after exact raw SHA/size proof.
                archive_evidence = _inspect_gzip_archive(
                    archive_path,
                    expected_raw_sha256=source_evidence.sha256,
                    expected_raw_size=source_evidence.size,
                    directory_fd=archive_fd,
                )
                metadata = _archive_metadata(
                    state_db,
                    source,
                    archive_path,
                    source_evidence,
                    archive_dir_fd=archive_fd,
                )
                _atomic_json_write_at(
                    archive_fd,
                    metadata_path.name,
                    metadata,
                    managed_directory=expected_archive_root,
                )
            if (
                metadata.get("state_db") != str(state_db)
                or metadata.get("source_path") != _canonical(source)
                or metadata.get("archive_path") != _canonical(archive_path)
                or metadata.get("source", {}).get("sha256")
                != source_evidence.sha256
                or metadata.get("source", {}).get("size")
                != source_evidence.size
                or metadata.get("archive", {}).get("sha256")
                != archive_evidence["archive_sha256"]
                or metadata.get("archive", {}).get("size")
                != archive_evidence["archive_size"]
            ):
                raise RuntimeError(
                    f"existing archive evidence mismatch: {archive_path}"
                )
            if (
                archive_evidence["raw_size"] != source_evidence.size
                or archive_evidence["raw_sha256"] != source_evidence.sha256
            ):
                raise RuntimeError(
                    f"existing archive round-trip mismatch: {archive_path}"
                )
        else:
            if metadata_exists:
                raise RuntimeError(
                    f"archive metadata exists without object: {metadata_path}"
                )
            _compress_backup(
                source,
                archive_path,
                source_evidence,
                archive_dir_fd=archive_fd,
            )
            metadata = _archive_metadata(
                state_db,
                source,
                archive_path,
                source_evidence,
                archive_dir_fd=archive_fd,
            )
            _atomic_json_write_at(
                archive_fd,
                metadata_path.name,
                metadata,
                managed_directory=expected_archive_root,
            )

        _assert_pinned_directory_current(expected_archive_root, archive_fd)

        # Serialize the last reference check and source consumption with every
        # actual-run approval writer. issue_actual_run_token() revalidates its
        # backup inside that same writer boundary, closing both race directions.
        connection = sqlite3.connect(str(state_db), timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            blockers = _maintenance_blockers(connection)
            if blockers:
                raise RuntimeError(
                    "archive maintenance became blocked: " + ", ".join(blockers)
                )
            if _canonical(source) in _referenced_backup_paths(connection):
                raise RuntimeError(
                    f"backup became referenced before consume: {source}"
                )
            metadata = _verify_cold_object_before_consume(
                state_db,
                source,
                archive_path,
                metadata_path,
                source_evidence,
                archive_dir_fd=archive_fd,
            )
            _assert_pinned_directory_current(expected_archive_root, archive_fd)
            unlink_owned(source, expected=source_evidence)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    return {
        "source_path": str(source),
        "archive_path": str(archive_path),
        "metadata_path": str(metadata_path),
        "source_bytes": source_evidence.size,
        "archive_bytes": metadata["archive"]["size"],
        "saved_bytes": source_evidence.size - metadata["archive"]["size"],
        "source_sha256": source_evidence.sha256,
        "archive_sha256": metadata["archive"]["sha256"],
    }


def _validate_plan_confirmation(plan, confirm_count, confirm_plan_sha256) -> None:
    if int(confirm_count) != plan["eligible_count"]:
        raise RuntimeError("archive confirmation count mismatch")
    if str(confirm_plan_sha256) != plan["plan_sha256"]:
        raise RuntimeError("archive confirmation plan SHA-256 mismatch")


def apply_backup_archive_plan(
    state_db,
    plan,
    *,
    confirm_count,
    confirm_plan_sha256,
):
    """Rebuild and apply the exact confirmed archive plan under one lock."""
    state_db = Path(state_db).expanduser().resolve()
    if _canonical(state_db) != plan.get("state_db"):
        raise RuntimeError("archive plan state DB mismatch")
    _validate_plan_confirmation(plan, confirm_count, confirm_plan_sha256)
    with _archive_lock(state_db.parent):
        current = build_backup_archive_plan(
            state_db,
            keep_latest_unreferenced=plan["keep_latest_unreferenced"],
        )
        if current["plan_sha256"] != plan["plan_sha256"]:
            raise RuntimeError("archive plan is stale; rebuild and reconfirm")
        if current["blockers"]:
            raise RuntimeError(
                "archive maintenance is blocked: " + ", ".join(current["blockers"])
            )
        integrity = _connect_readonly(state_db)
        try:
            state_integrity = integrity.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            integrity.close()
        if state_integrity != "ok":
            raise RuntimeError(f"state DB integrity_check failed: {state_integrity}")

        report_dir = state_db.parent / "reports"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        intent_path = report_dir / f"state_archive_1_4_10_intent_{stamp}.json"
        _atomic_json_write(intent_path, {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "kind": "state_backup_archive_intent",
            "archive_version": ARCHIVE_VERSION,
            "created_at": _utc_now(),
            "state_db": str(state_db),
            "plan_sha256": current["plan_sha256"],
            "eligible_count": current["eligible_count"],
            "eligible_bytes": current["eligible_bytes"],
            "items": current["items"],
        })
        archived = [archive_backup_path(state_db, item) for item in current["items"]]
        report = {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "kind": "state_backup_archive_execution",
            "archive_version": ARCHIVE_VERSION,
            "completed_at": _utc_now(),
            "state_db": str(state_db),
            "plan_sha256": current["plan_sha256"],
            "intent_path": str(intent_path),
            "archived": archived,
            "archived_count": len(archived),
            "source_bytes": sum(item["source_bytes"] for item in archived),
            "archive_bytes": sum(item["archive_bytes"] for item in archived),
            "saved_bytes": sum(item["saved_bytes"] for item in archived),
        }
        report_path = report_dir / f"state_archive_1_4_10_{stamp}.json"
        _atomic_json_write(report_path, report)
        report["report_path"] = str(report_path)
        return report


def restore_archived_backup(state_db, metadata_path, *, confirm_raw_sha256):
    """Restore a verified hot SQLite copy without deleting the cold object."""
    state_db = Path(state_db).expanduser().resolve()
    metadata_path = Path(_canonical(metadata_path))
    metadata = _load_metadata(metadata_path)
    if metadata.get("state_db") != str(state_db):
        raise RuntimeError("archive metadata state DB mismatch")
    source = Path(_canonical(metadata["source_path"]))
    archive_path = Path(_canonical(metadata["archive_path"]))
    expected_backup_root = state_db.parent / "backups"
    expected_archive_root = state_db.parent / "cold_archive" / "backups"
    if (
        not state_db.is_file()
        or metadata_path.parent != expected_archive_root
        or archive_path.parent != expected_archive_root
        or source.parent != expected_backup_root
        or metadata_path.name != f"{source.name}.archive.json"
        or archive_path.name != f"{source.name}.gz"
        or source.suffix != ".sqlite3"
    ):
        raise RuntimeError("archive metadata is outside managed state roots")
    with opened_directory_nofollow(expected_archive_root):
        pass
    expected_raw = metadata["source"]["sha256"]
    if confirm_raw_sha256 != expected_raw:
        raise RuntimeError("restore confirmation raw SHA-256 mismatch")
    archive_evidence = _inspect_gzip_archive(
        archive_path,
        expected_archive_sha256=metadata["archive"]["sha256"],
        expected_archive_size=metadata["archive"]["size"],
        expected_raw_sha256=expected_raw,
        expected_raw_size=metadata["source"]["size"],
    )
    if (
        archive_evidence["archive_sha256"] != metadata["archive"]["sha256"]
        or archive_evidence["archive_size"] != metadata["archive"]["size"]
    ):
        raise RuntimeError("cold archive SHA-256 mismatch")
    restored = None
    with opened_directory_nofollow(expected_backup_root) as backup_fd:
        _assert_pinned_directory_current(expected_backup_root, backup_fd)
        if _entry_exists_at(backup_fd, source.name):
            raise FileExistsError(source)
        descriptor, temporary = _open_unique_temp_at(
            backup_fd, prefix=f".{source.name}.", suffix=".restore"
        )
        published = False
        completed = False
        digest = hashlib.sha256()
        total = 0
        try:
            with os.fdopen(os.dup(descriptor), "wb") as output:
                with opened_regular_file_nofollow(archive_path) as archive_descriptor:
                    with os.fdopen(
                        archive_descriptor, "rb", closefd=False
                    ) as archive_stream:
                        with gzip.GzipFile(
                            fileobj=archive_stream, mode="rb"
                        ) as compressed:
                            for chunk in iter(
                                lambda: compressed.read(CHUNK_SIZE), b""
                            ):
                                total += len(chunk)
                                if total > metadata["source"]["size"]:
                                    raise RuntimeError(
                                        "cold archive expands beyond expected restore size"
                                    )
                                digest.update(chunk)
                                output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if (
                total != metadata["source"]["size"]
                or digest.hexdigest() != expected_raw
            ):
                raise RuntimeError("restored backup content mismatch")
            _sqlite_integrity_fd(descriptor, source.parent / temporary)
            _assert_pinned_directory_current(expected_backup_root, backup_fd)
            if _entry_exists_at(backup_fd, source.name):
                raise FileExistsError(source)
            os.link(
                temporary,
                source.name,
                src_dir_fd=backup_fd,
                dst_dir_fd=backup_fd,
                follow_symlinks=False,
            )
            published = True
            os.unlink(temporary, dir_fd=backup_fd)
            temporary = None
            os.fsync(backup_fd)
            restored = inspect_regular_file_at(backup_fd, source.name)
            if (
                restored.size != metadata["source"]["size"]
                or restored.sha256 != expected_raw
            ):
                raise RuntimeError("restored backup verification failed")
            _assert_pinned_directory_current(expected_backup_root, backup_fd)
            completed = True
        finally:
            if not completed and published:
                try:
                    os.unlink(source.name, dir_fd=backup_fd)
                    os.fsync(backup_fd)
                except FileNotFoundError:
                    pass
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=backup_fd)
                except FileNotFoundError:
                    pass
            os.close(descriptor)

    result = {
        "restored_path": str(source),
        "source_bytes": restored.size,
        "source_sha256": restored.sha256,
        "archive_preserved": str(archive_path),
    }
    report_dir = state_db.parent / "reports"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    report_path = report_dir / f"state_archive_1_4_10_restore_{stamp}.json"
    _atomic_json_write(report_path, {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "kind": "state_backup_archive_restore",
        "archive_version": ARCHIVE_VERSION,
        "completed_at": _utc_now(),
        "state_db": str(state_db),
        "metadata_path": str(metadata_path),
        **result,
    })
    result["report_path"] = str(report_path)
    return result


def build_parser():
    parser = argparse.ArgumentParser(description="verified state backup cold archive")
    parser.add_argument("--state-db", default=str(STATE_DB))
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument(
        "--keep-latest-unreferenced", type=int,
        default=DEFAULT_KEEP_LATEST_UNREFERENCED,
    )
    apply = subparsers.add_parser("apply")
    apply.add_argument(
        "--keep-latest-unreferenced", type=int,
        default=DEFAULT_KEEP_LATEST_UNREFERENCED,
    )
    apply.add_argument("--confirm-count", type=int, required=True)
    apply.add_argument("--confirm-plan-sha256", required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--metadata", required=True)
    restore.add_argument("--confirm-raw-sha256", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "restore":
        result = restore_archived_backup(
            args.state_db,
            args.metadata,
            confirm_raw_sha256=args.confirm_raw_sha256,
        )
    else:
        plan = build_backup_archive_plan(
            args.state_db,
            keep_latest_unreferenced=args.keep_latest_unreferenced,
        )
        if args.command == "plan":
            result = plan
        else:
            result = apply_backup_archive_plan(
                args.state_db,
                plan,
                confirm_count=args.confirm_count,
                confirm_plan_sha256=args.confirm_plan_sha256,
            )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
