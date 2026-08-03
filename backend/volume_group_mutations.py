"""Journaled, relationship-preserving mutations for approved volume groups."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Mapping, Sequence

import decision_store
from bare_volume_context import context_name
from dedup_mutations import _ensure_intake_fingerprint, _file_state, _preflight
from mutation_io import (
    copy_no_clobber,
    canonical_absolute_path,
    ensure_directory_nofollow,
    evidence_matches,
    inspect_regular_file,
    mutation_lock,
    mutation_lock_for_roots,
    opened_directory_nofollow,
    read_json_with_evidence,
    unlink_owned,
)
from normalizer import NORMALIZER_VERSION


ACTION = "volume_group_merge"
STAGING_DIRECTORY_NAME = ".volume_group_staging"
_STAGE_MANIFEST_MAX_BYTES = 8 * 1024 * 1024
_STAGE_MANIFEST_MAX_FILES = 10_000


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _coordinate_key(row: Mapping[str, object]):
    kind = row["coordinate_kind"]
    if kind == "volume":
        part = (
            (int(row["part_num"]), int(row["part_den"] or 1))
            if row.get("part_num") is not None else None
        )
        volume = (int(row["volume_num"]), int(row["volume_den"] or 1))
        return kind, part, volume
    if kind == "part":
        return kind, (int(row["part_num"]), int(row["part_den"] or 1)), None
    return None


def _contiguous_integer_values(values) -> bool:
    values = sorted(values)
    return bool(values) and values == list(range(values[0], values[-1] + 1))


def _coordinates_form_contiguous_batch(rows: Sequence[Mapping[str, object]]) -> bool:
    """Accept ordinary volumes, part-only sets, and part+volume sets.

    A compound coordinate such as ``2부 1권`` is a distinct book position,
    not a duplicate of ``1부 1권``.  Every observed per-part volume run still
    has to be contiguous so a loose handful of unrelated files does not create
    a work folder automatically.
    """

    kinds = {row.get("coordinate_kind") for row in rows}
    if kinds == {"part"}:
        if any(int(row.get("part_den") or 1) != 1 for row in rows):
            return False
        return _contiguous_integer_values(int(row["part_num"]) for row in rows)
    if kinds != {"volume"}:
        return False

    has_part = {row.get("part_num") is not None for row in rows}
    if len(has_part) > 1:
        return False
    by_part = {}
    for row in rows:
        if int(row.get("volume_den") or 1) != 1:
            return False
        part = None
        if row.get("part_num") is not None:
            if int(row.get("part_den") or 1) != 1:
                return False
            part = int(row["part_num"])
        by_part.setdefault(part, []).append(int(row["volume_num"]))
    if any(not _contiguous_integer_values(values) for values in by_part.values()):
        return False
    part_numbers = [part for part in by_part if part is not None]
    return not part_numbers or _contiguous_integer_values(part_numbers)


def _coordinate_response(row: Mapping[str, object]) -> dict:
    kind = str(row["coordinate_kind"])
    if kind == "volume":
        number = int(row["volume_num"])
        denominator = int(row["volume_den"] or 1)
    else:
        number = int(row["part_num"])
        denominator = int(row["part_den"] or 1)
    return {
        "coordinate_kind": kind,
        "coordinate_num": number,
        "coordinate_den": denominator,
        "part_num": row.get("part_num"),
        "part_den": row.get("part_den"),
        "volume_num": row.get("volume_num"),
        "volume_den": row.get("volume_den"),
    }


def classify_folderling_volume_target(
    conn, *, source_file_id: str, house_root: Path,
    new_group_parent: Path | None = None,
) -> dict:
    """Classify one volume as targetable, coordinate-conflicting, or unrelated.

    A same-coordinate source is held before it can become a second house parent.
    Later non-overlapping volumes in the same intake batch can therefore still
    use the coherent existing work folder.
    """

    def no_target(reason):
        return {"status": "no_target", "reason": reason}

    house_root = Path(house_root).resolve()
    source_row = conn.execute(
        """
        SELECT f.*, fa.core_title, fa.readable_title, fa.catalog_query_title,
               fa.title_override_json, fa.author, fa.disambig,
               fa.normalizer_version AS analysis_normalizer_version,
               fa.analyzed_name, fa.analyzed_size,
               fa.analyzed_mtime_ns, fa.analyzed_ctime_ns
        FROM files AS f LEFT JOIN file_analysis AS fa ON fa.file_id = f.file_id
        WHERE f.file_id = ? AND f.active = 1 AND f.source = 'temp'
        """,
        (source_file_id,),
    ).fetchone()
    if source_row is None:
        return no_target("missing_active_temp_source")
    source = decision_store.resolve_current_file_analysis(
        source_row,
        analysis_name=context_name(Path(str(source_row["canonical_path"])).name),
    )
    if (
        source["coordinate_kind"] not in {"volume", "part"}
        or source["span_ambiguous"]
        or int(source["disambig"] or 1) > 1
    ):
        return no_target("unsupported_or_ambiguous_coordinate")
    source_coordinate = _coordinate_key(source)
    if source_coordinate is None:
        return no_target("missing_coordinate")

    def classify_new_batch():
        if new_group_parent is None:
            return no_target("no_existing_core")
        source_path = Path(str(source["canonical_path"]))
        if source_path.suffix.lower() not in {".epub", ".pdf"}:
            return no_target("new_group_requires_ebook")
        peers = []
        for row in conn.execute(
            """
            SELECT f.*, fa.core_title, fa.readable_title, fa.catalog_query_title,
                   fa.title_override_json, fa.author, fa.disambig,
                   fa.normalizer_version AS analysis_normalizer_version,
                   fa.analyzed_name, fa.analyzed_size,
                   fa.analyzed_mtime_ns, fa.analyzed_ctime_ns
            FROM files AS f
            LEFT JOIN file_analysis AS fa ON fa.file_id = f.file_id
            WHERE f.active = 1 AND f.source = 'temp'
            ORDER BY f.canonical_path
            """
        ):
            peer_path = Path(str(row["canonical_path"]))
            if peer_path.suffix.lower() not in {".epub", ".pdf"}:
                continue
            peer = decision_store.resolve_current_file_analysis(
                row, analysis_name=context_name(peer_path.name)
            )
            peer_core = peer.get("core_title")
            if peer_core != source["core_title"]:
                continue
            peers.append(peer)
        if len(peers) < 2:
            return no_target("new_group_requires_multiple_volumes")
        authors = {
            str(row.get("author") or "").strip()
            for row in peers if str(row.get("author") or "").strip()
        }
        if len(authors) > 1:
            return no_target("new_group_author_conflict")
        if any(
            row["coordinate_kind"] not in {"volume", "part"}
            or row["span_ambiguous"]
            or int(row.get("disambig") or 1) > 1
            for row in peers
        ):
            return no_target("new_group_coordinate_shape_conflict")
        peer_coordinates = [_coordinate_key(row) for row in peers]
        if None in peer_coordinates or len(peer_coordinates) != len(set(peer_coordinates)):
            return no_target("new_group_duplicate_coordinate")
        if not _coordinates_form_contiguous_batch(peers):
            return no_target("new_group_requires_contiguous_volumes")
        display_title = str(source["readable_title"] or source["core_title"]).strip()
        if not display_title or Path(display_title).name != display_title:
            return no_target("new_group_title_is_not_safe_folder_name")
        target = Path(new_group_parent).resolve() / display_title
        try:
            target.relative_to(house_root)
        except ValueError:
            return no_target("new_group_target_outside_house")
        if target.exists() or target.is_symlink():
            return no_target("new_group_destination_exists")
        return {
            "status": "target",
            "target_folder": str(target),
            "existing_file_ids": [],
            "display_title": display_title,
            "core_title": str(source["core_title"]),
            "new_batch": True,
            "batch_file_ids": [str(row["file_id"]) for row in peers],
        }

    candidate_rows = conn.execute(
        """
        SELECT f.*, fa.core_title, fa.readable_title, fa.catalog_query_title,
               fa.title_override_json, fa.author, fa.disambig,
               fa.normalizer_version AS analysis_normalizer_version,
               fa.analyzed_name, fa.analyzed_size,
               fa.analyzed_mtime_ns, fa.analyzed_ctime_ns,
               v.work_bucket_id
        FROM files AS f
        JOIN file_analysis AS fa ON fa.file_id = f.file_id
        LEFT JOIN variants AS v ON v.variant_id = f.variant_id
        WHERE f.active = 1 AND f.source = 'house'
          AND (
              fa.core_title = ?
              OR fa.normalizer_version != ?
              OR fa.analyzed_size != f.size
              OR fa.analyzed_mtime_ns != f.mtime_ns
              OR (
                  fa.analyzed_ctime_ns IS NOT NULL
                  AND (f.ctime_ns IS NULL OR fa.analyzed_ctime_ns != f.ctime_ns)
              )
              OR length(f.canonical_path) <= length(fa.analyzed_name)
              OR substr(f.canonical_path, -length(fa.analyzed_name)) != fa.analyzed_name
              OR substr(
                  f.canonical_path, -(length(fa.analyzed_name) + 1), 1
              ) != '/'
          )
        ORDER BY f.canonical_path
        """,
        (source["core_title"], NORMALIZER_VERSION),
    ).fetchall()
    # Stored-core matches stay eligible while identity-stale rows are added so
    # a rename or normalizer change cannot hide a newly matching work.  The
    # shared resolver is the final authority in either direction.
    existing = []
    for row in candidate_rows:
        resolved = decision_store.resolve_current_file_analysis(row)
        if resolved.get("core_title") == source["core_title"]:
            existing.append(resolved)
    if not existing:
        return classify_new_batch()

    explicit_authors = {
        str(row.get("author") or "").strip()
        for row in [source, *existing]
        if str(row.get("author") or "").strip()
    }
    if len(explicit_authors) > 1:
        return no_target("author_conflict")

    same_kind_existing = [
        row for row in existing
        if row["coordinate_kind"] == source["coordinate_kind"]
    ]
    if not same_kind_existing:
        # A same-title episode compilation is not a volume coordinate and must
        # not prevent a coherent loose ebook batch from getting its own folder.
        unrelated_book_coordinates = [
            row for row in existing
            if row["coordinate_kind"] not in {None, "episode"}
        ]
        if unrelated_book_coordinates:
            return no_target("existing_coordinate_shape_conflict")
        return classify_new_batch()
    # 같은 core에 과거 미관리 합본이 섞여 있어도 하나의 managed work가
    # 일관된 권수 폴더를 이루면 그 집합을 라우팅 기준으로 삼는다. 다만
    # 같은 좌표의 미관리 파일은 아래 필터 전에 충돌로 잡아 중복 검사를
    # 우회하지 못하게 한다.
    all_existing = [
        row for row in existing
        if row["coordinate_kind"] == source["coordinate_kind"]
        or (
            row["coordinate_kind"] == "symbol"
            and row["coordinate_symbol"] == "side_story"
        )
    ]
    coordinate_matches = [
        row for row in all_existing
        if row["coordinate_kind"] == source["coordinate_kind"]
        and _coordinate_key(row) == source_coordinate
    ]
    if coordinate_matches:
        return {
            "status": "coordinate_conflict",
            "reason": "existing_same_coordinate",
            "core_title": str(source["core_title"]),
            "display_title": str(source["readable_title"] or source["core_title"]),
            **_coordinate_response(source),
            "conflicting_file_ids": [str(row["file_id"]) for row in coordinate_matches],
            "conflicting_paths": [str(row["canonical_path"]) for row in coordinate_matches],
        }
    managed_existing = [
        row for row in all_existing
        if row["assignment_state"] == "managed"
        and row["work_bucket_id"] is not None
    ]
    managed_works = {
        int(row["work_bucket_id"]) for row in managed_existing
    }
    if managed_existing and len(managed_works) == 1:
        existing = managed_existing

    if any(
        (
            row["coordinate_kind"] != source["coordinate_kind"]
            and not (
                row["coordinate_kind"] == "symbol"
                and row["coordinate_symbol"] == "side_story"
            )
        )
        or row["span_ambiguous"]
        or int(row["disambig"] or 1) > 1
        for row in existing
    ):
        return no_target("existing_coordinate_shape_conflict")
    main_existing = [
        row for row in existing
        if row["coordinate_kind"] == source["coordinate_kind"]
    ]
    coordinates = [_coordinate_key(row) for row in main_existing]
    if None in coordinates:
        return no_target("existing_coordinate_missing")
    authors = {str(row["author"]) for row in existing if row["author"]}
    source_author = str(source.get("author") or "").strip()
    if source_author:
        authors.add(source_author)
    if len(authors) > 1:
        return no_target("author_conflict")
    works = {
        int(row["work_bucket_id"])
        for row in existing
        if row["work_bucket_id"] is not None
    }
    if len(works) > 1:
        return no_target("multiple_existing_works")
    if len(coordinates) != len(set(coordinates)) and not (
        len(works) == 1
        and all(
            row["work_bucket_id"] is not None
            and row["assignment_state"] == "managed"
            for row in existing
        )
    ):
        return no_target("duplicate_existing_coordinates")

    parents = {Path(str(row["canonical_path"])).resolve().parent for row in existing}
    if len(parents) != 1:
        return no_target("multiple_existing_parents")
    target = next(iter(parents))
    try:
        relative = target.relative_to(house_root)
    except ValueError:
        return no_target("target_outside_house")
    if len(relative.parts) <= 1 or target.is_symlink() or not target.is_dir():
        return no_target("existing_work_folder_required")
    return {
        "status": "target",
        "target_folder": str(target),
        "existing_file_ids": [str(row["file_id"]) for row in existing],
        "display_title": str(source["readable_title"] or source["core_title"]),
        "core_title": str(source["core_title"]),
    }


def suggest_folderling_volume_target(
    conn, *, source_file_id: str, house_root: Path,
    new_group_parent: Path | None = None,
) -> dict | None:
    """Return one fail-closed existing-folder target for a new volume intake."""
    decision = classify_folderling_volume_target(
        conn, source_file_id=source_file_id, house_root=house_root,
        new_group_parent=new_group_parent,
    )
    return decision if decision["status"] == "target" else None


def hold_folderling_volume_conflict(
    conn,
    *,
    source_file_id: str,
    temp_root: Path,
    run_id: str,
    conflict: Mapping[str, object],
) -> dict:
    """Journal a same-coordinate intake into a non-destructive warning queue."""
    if conflict.get("status") != "coordinate_conflict":
        raise ValueError("volume hold requires a coordinate conflict decision")

    temp_root = Path(temp_root).resolve()
    with mutation_lock(conn, f"volume-coordinate-hold:{run_id}", run_id=run_id):
        actual_run = decision_store.assert_active_actual_run(conn, run_id)
        source = _ensure_intake_fingerprint(conn, _file_state(conn, source_file_id))
        if source["source"] != "temp":
            raise RuntimeError("volume coordinate hold source must be temp")
        if (
            source["variant_id"] is not None
            or source["protected"]
            or source["representative"]
            or source["assignment_state"] == "managed"
        ):
            raise RuntimeError(
                "managed volume conflict requires relationship-preserving review"
            )
        source_path = _preflight(source)
        decision_store.assert_actual_run_path(actual_run, source_path, "temp_root")
        source_evidence = inspect_regular_file(source_path)
        decision_store.assert_manifest_source(
            actual_run, source_path, "temp_root", source_evidence
        )

        destination_dir = (
            temp_root / "trash_bin" / "warning" / "volume_coordinate_conflicts"
        )
        ensure_directory_nofollow(destination_dir)
        destination = destination_dir / source_path.name
        counter = 1
        while destination.exists() or destination.is_symlink():
            destination = destination_dir / (
                f"{source_path.stem}_conflict_{counter}{source_path.suffix}"
            )
            counter += 1
        decision_store.assert_actual_run_path(actual_run, destination, "temp_root")

        with decision_store.transaction(conn):
            operation_id = decision_store.create_operation(
                conn,
                run_id=run_id,
                action="volume_coordinate_hold",
                source_path=str(source_path),
                dest_path=str(destination),
                file_id=source_file_id,
                expected_size=source["size"],
                expected_mtime_ns=source["mtime_ns"],
                expected_fingerprint_id=source["current_fingerprint_id"],
                source_dev=source_evidence.dev,
                source_ino=source_evidence.ino,
                source_ctime_ns=source_evidence.ctime_ns,
                source_sha256=source_evidence.sha256,
            )

        def guard():
            decision_store.assert_active_actual_run(conn, run_id)
            current = _file_state(conn, source_file_id)
            if current["current_fingerprint_id"] != source["current_fingerprint_id"]:
                raise RuntimeError("volume conflict source changed before consume")

        destination_evidence = decision_store.copy_record_consume_operation(
            conn,
            operation_id,
            source_path,
            destination,
            source_evidence,
            guard=guard,
        )
        with decision_store.transaction(conn):
            conn.execute(
                """
                UPDATE files
                SET canonical_path = ?, source = 'queue',
                    assignment_state = 'decision_required', assignment_origin = NULL,
                    variant_id = NULL, protected = 0,
                    dev = ?, ino = ?, ctime_ns = ?, size = ?, mtime_ns = ?,
                    last_seen_at = CURRENT_TIMESTAMP
                WHERE file_id = ?
                """,
                (
                    str(destination),
                    destination_evidence.dev,
                    destination_evidence.ino,
                    destination_evidence.ctime_ns,
                    destination_evidence.size,
                    destination_evidence.mtime_ns,
                    source_file_id,
                ),
            )
            decision_store.transition_operation(conn, operation_id, "db_done")
        with decision_store.transaction(conn):
            decision_store.transition_operation(conn, operation_id, "committed")
        return {
            "operation_id": operation_id,
            "action": "volume_coordinate_hold",
            "file_id": source_file_id,
            "source_path": str(source_path),
            "dest_path": str(destination),
            "conflicting_file_ids": list(conflict.get("conflicting_file_ids") or ()),
            "conflicting_paths": list(conflict.get("conflicting_paths") or ()),
            "coordinate_kind": conflict.get("coordinate_kind"),
            "coordinate_num": conflict.get("coordinate_num"),
            "coordinate_den": conflict.get("coordinate_den"),
        }


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path = Path(path)
    ensure_directory_nofollow(path.parent)
    temporary = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(temporary), flags, 0o600)
    try:
        raw = json.dumps(
            payload, ensure_ascii=False, indent=2, sort_keys=True
        ).encode("utf-8")
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)


def cleanup_staging(
    records: Sequence[Mapping[str, object]],
    staging_root: Path,
    *,
    manifest_evidence=None,
    expected_names: set[str] | None = None,
) -> bool:
    """Remove only stage copies whose bound evidence still matches.

    Recovery callers pass the manifest evidence and complete directory entry
    set they validated.  A drift is rejected before any known stage copy is
    removed, and a case is successful only when its root is actually gone.
    """

    staging_root = Path(staging_root)
    manifest = staging_root / "stage-manifest.json"
    if expected_names is not None:
        entries = _directory_entries_nofollow(staging_root)
        if set(entries) != set(expected_names):
            raise RuntimeError("volume staging case changed before cleanup")
        if any(not stat.S_ISREG(entries[name]) for name in expected_names):
            raise RuntimeError("volume staging case gained a non-regular entry")
    if manifest_evidence is not None:
        manifest_now = inspect_regular_file(manifest)
        if not evidence_matches(manifest_now, manifest_evidence):
            raise RuntimeError("volume staging manifest changed before cleanup")

    for record in reversed(list(records)):
        stage_path = Path(record["stage_path"])
        evidence = record["stage_evidence"]
        try:
            unlink_owned(stage_path, expected=evidence)
        except FileNotFoundError:
            continue
    if manifest_evidence is None:
        try:
            current_manifest_evidence = inspect_regular_file(manifest)
            unlink_owned(manifest, expected=current_manifest_evidence)
        except FileNotFoundError:
            pass
    else:
        unlink_owned(manifest, expected=manifest_evidence)
    current = staging_root
    cleanup_boundary = next(
        (
            candidate
            for candidate in (current, *current.parents)
            if candidate.name in {STAGING_DIRECTORY_NAME, ".stage"}
        ),
        current,
    )
    while current.name and os.path.lexists(current):
        try:
            removed = _remove_empty_directory_owned(current)
        except OSError:
            break
        if not removed:
            break
        if current == cleanup_boundary:
            break
        current = current.parent
    return not os.path.lexists(staging_root)


def _directory_entries_nofollow(path: Path) -> dict[str, int]:
    with opened_directory_nofollow(path) as directory_fd:
        return {
            name: os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode
            for name in os.listdir(directory_fd)
        }


def _read_stage_manifest(path: Path):
    try:
        evidence, payload = read_json_with_evidence(
            path, max_bytes=_STAGE_MANIFEST_MAX_BYTES
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("volume staging manifest is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("volume staging manifest root is not an object")
    return evidence, payload


def _remove_empty_directory_owned(path: Path) -> bool:
    """Remove one empty directory after pinning and rechecking its identity."""
    path = canonical_absolute_path(path)
    try:
        with opened_directory_nofollow(path) as directory_fd:
            expected = os.fstat(directory_fd)
            if os.listdir(directory_fd):
                return False
    except FileNotFoundError:
        return True
    with opened_directory_nofollow(path.parent) as parent_fd:
        current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(current.st_mode) or (
            current.st_dev, current.st_ino, current.st_ctime_ns
        ) != (expected.st_dev, expected.st_ino, expected.st_ctime_ns):
            raise RuntimeError(f"volume staging directory identity changed: {path}")
        os.rmdir(path.name, dir_fd=parent_fd)
    return True


def _recover_staging_case(case_root: Path, *, run_id: str) -> int:
    entries = _directory_entries_nofollow(case_root)
    manifest_name = "stage-manifest.json"
    if entries.get(manifest_name) is None or not stat.S_ISREG(entries[manifest_name]):
        raise RuntimeError("volume staging case has no regular manifest")
    manifest_evidence, payload = _read_stage_manifest(case_root / manifest_name)
    if payload.get("action") != ACTION or payload.get("run_id") != run_id:
        raise RuntimeError("volume staging manifest action/run does not match its path")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError("volume staging manifest has no files")
    if len(files) > _STAGE_MANIFEST_MAX_FILES:
        raise RuntimeError("volume staging manifest file count exceeds the limit")

    records = []
    expected_names = {manifest_name}
    for item in files:
        if not isinstance(item, dict):
            raise RuntimeError("volume staging manifest file entry is invalid")
        stage_path = Path(str(item.get("stage_path") or ""))
        leaf = stage_path.name
        expected_path = case_root / leaf
        if (
            not leaf
            or leaf in expected_names
            or canonical_absolute_path(stage_path)
            != canonical_absolute_path(expected_path)
        ):
            raise RuntimeError("volume staging manifest path escapes or collides")
        expected_names.add(leaf)
        if entries.get(leaf) is None or not stat.S_ISREG(entries[leaf]):
            raise RuntimeError("volume staging file is missing or is not regular")
        evidence = inspect_regular_file(expected_path)
        if (
            evidence.size != int(item.get("size", -1))
            or evidence.sha256 != str(item.get("sha256") or "")
        ):
            raise RuntimeError("volume staging file evidence does not match manifest")
        records.append({"stage_path": str(expected_path), "stage_evidence": evidence})
    if set(entries) != expected_names:
        raise RuntimeError("volume staging case contains an unexpected file")
    removed = cleanup_staging(
        records,
        case_root,
        manifest_evidence=manifest_evidence,
        expected_names=expected_names,
    )
    if not removed:
        raise RuntimeError("volume staging case remained after cleanup")
    return len(records)


def recover_abandoned_volume_staging(
    state_db: Path, *, house_root: Path, temp_root: Path
) -> dict:
    """Remove only fully verified stage copies from terminal actual runs.

    Unknown paths, non-terminal runs, unfinished operations, symlinks, manifest
    drift, or extra files are preserved and reported so Folderling can stop
    before starting another mutation run.
    """
    state_db = Path(state_db).expanduser().resolve()
    house_root = Path(house_root).expanduser().resolve()
    temp_root = Path(temp_root).expanduser().resolve()
    staging_root = temp_root / STAGING_DIRECTORY_NAME
    result = {"recovered_case_count": 0, "recovered_file_count": 0, "issues": []}
    if not os.path.lexists(staging_root):
        return result

    with mutation_lock_for_roots(
        house_root, temp_root, "volume-staging-recovery"
    ):
        try:
            run_entries = _directory_entries_nofollow(staging_root)
        except (OSError, RuntimeError) as exc:
            result["issues"].append({"path": str(staging_root), "reason": str(exc)})
            return result
        conn = decision_store.connect_state_db_readonly(state_db)
        try:
            for run_id, mode in sorted(run_entries.items()):
                run_root = staging_root / run_id
                run_issue_start = len(result["issues"])
                run_recovered_cases = 0
                run_recovered_files = 0
                if not stat.S_ISDIR(mode):
                    result["issues"].append({
                        "path": str(run_root),
                        "reason": "unexpected non-directory staging entry",
                    })
                    continue
                run = conn.execute(
                    "SELECT state FROM actual_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if run is None or run["state"] not in {"finished", "failed", "cancelled"}:
                    result["issues"].append({
                        "path": str(run_root),
                        "reason": "staging run is missing or not terminal",
                    })
                    continue
                unfinished_operations = conn.execute(
                    "SELECT COUNT(*) FROM operations WHERE run_id = ? "
                    "AND state IN ('planned', 'fs_done', 'db_done')",
                    (run_id,),
                ).fetchone()[0]
                unfinished_groups = conn.execute(
                    "SELECT COUNT(*) FROM operation_groups WHERE run_id = ? "
                    "AND state IN ('planned', 'fs_done', 'db_done')",
                    (run_id,),
                ).fetchone()[0]
                if unfinished_operations or unfinished_groups:
                    result["issues"].append({
                        "path": str(run_root),
                        "reason": "staging run still has unfinished operations",
                    })
                    continue
                try:
                    case_entries = _directory_entries_nofollow(run_root)
                except (OSError, RuntimeError) as exc:
                    result["issues"].append({"path": str(run_root), "reason": str(exc)})
                    continue
                if not case_entries:
                    try:
                        removed = _remove_empty_directory_owned(run_root)
                    except (OSError, RuntimeError) as exc:
                        result["issues"].append({
                            "path": str(run_root), "reason": str(exc)
                        })
                    else:
                        if not removed:
                            result["issues"].append({
                                "path": str(run_root),
                                "reason": "empty staging run remained after cleanup",
                            })
                    continue
                for case_name, case_mode in sorted(case_entries.items()):
                    case_root = run_root / case_name
                    if not stat.S_ISDIR(case_mode):
                        result["issues"].append({
                            "path": str(case_root),
                            "reason": "unexpected non-directory case entry",
                        })
                        continue
                    try:
                        recovered = _recover_staging_case(case_root, run_id=run_id)
                    except (OSError, RuntimeError, ValueError) as exc:
                        result["issues"].append({
                            "path": str(case_root), "reason": str(exc)
                        })
                    else:
                        run_recovered_cases += 1
                        run_recovered_files += recovered
                try:
                    run_removed = _remove_empty_directory_owned(run_root)
                except (OSError, RuntimeError) as exc:
                    result["issues"].append({
                        "path": str(run_root), "reason": str(exc)
                    })
                else:
                    if (
                        not run_removed
                        and len(result["issues"]) == run_issue_start
                    ):
                        result["issues"].append({
                            "path": str(run_root),
                            "reason": "staging run remained non-empty after recovery",
                        })
                if len(result["issues"]) == run_issue_start:
                    result["recovered_case_count"] += run_recovered_cases
                    result["recovered_file_count"] += run_recovered_files
        finally:
            conn.close()
        if not result["issues"]:
            try:
                staging_removed = _remove_empty_directory_owned(staging_root)
            except (OSError, RuntimeError) as exc:
                result["issues"].append({
                    "path": str(staging_root), "reason": str(exc)
                })
            else:
                if not staging_removed:
                    result["issues"].append({
                        "path": str(staging_root),
                        "reason": "volume staging root remained non-empty after recovery",
                    })
    return result


def stage_volume_sources(
    conn,
    *,
    file_ids: Sequence[str],
    staging_root: Path,
    run_id: str,
) -> list[dict]:
    """Copy every source into temp and verify the complete group before moving any source."""

    staging_root = Path(staging_root).resolve()
    created: list[dict] = []
    with mutation_lock(conn, f"{ACTION}:stage:{run_id}", run_id=run_id):
        actual_run = decision_store.assert_active_actual_run(conn, run_id)
        decision_store.assert_actual_run_path(actual_run, staging_root, "temp_root")
        ensure_directory_nofollow(staging_root)
        try:
            for index, file_id in enumerate(file_ids, start=1):
                source = _ensure_intake_fingerprint(conn, _file_state(conn, file_id))
                if source["source"] != "house":
                    raise RuntimeError("volume group source must be an active house file")
                source_path = _preflight(source)
                decision_store.assert_actual_run_path(
                    actual_run, source_path, "house_root"
                )
                source_evidence = inspect_regular_file(source_path)
                decision_store.assert_manifest_or_same_run_house_source(
                    conn, actual_run, source_path, source_evidence
                )
                stage_path = staging_root / f"{index:04d}_{source_path.name}"
                copied = copy_no_clobber(
                    source_path, stage_path, expected=source_evidence
                )
                created.append(
                    {
                        "file_id": file_id,
                        "source_path": str(source_path),
                        "source": source,
                        "source_evidence": source_evidence,
                        "stage_path": str(stage_path),
                        "stage_evidence": copied.destination_evidence,
                    }
                )
            _atomic_json(
                staging_root / "stage-manifest.json",
                {
                    "action": ACTION,
                    "run_id": run_id,
                    "files": [
                        {
                            "file_id": item["file_id"],
                            "source_path": item["source_path"],
                            "stage_path": item["stage_path"],
                            "size": item["stage_evidence"].size,
                            "sha256": item["stage_evidence"].sha256,
                        }
                        for item in created
                    ],
                },
            )
            return created
        except BaseException:
            cleanup_staging(created, staging_root)
            raise


def _choose_work(conn, records: Sequence[Mapping[str, object]], display_title: str) -> int:
    existing = set()
    for record in records:
        variant_id = record["source"]["variant_id"]
        if variant_id is None:
            continue
        row = conn.execute(
            "SELECT work_bucket_id FROM variants WHERE variant_id = ?", (variant_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"volume source variant is missing: {variant_id}")
        existing.add(int(row[0]))
    if len(existing) > 1:
        raise RuntimeError("volume group contains conflicting managed works")
    if existing:
        work_id = next(iter(existing))
        conn.execute(
            "UPDATE works SET display_title = COALESCE(display_title, ?), "
            "updated_at = CURRENT_TIMESTAMP WHERE work_bucket_id = ?",
            (display_title, work_id),
        )
        return work_id
    return int(
        conn.execute(
            "INSERT INTO works(display_title) VALUES (?)", (display_title,)
        ).lastrowid
    )


def ensure_volume_fingerprints(conn, file_ids: Sequence[str]) -> list[dict]:
    """Prepare durable identities before the caller opens its relationship transaction."""

    return [
        _ensure_intake_fingerprint(conn, _file_state(conn, file_id))
        for file_id in file_ids
    ]


def _attach_volume_relationship(
    conn, file_id: str, work_id: int, *, origin: str = "human_decision"
) -> int:
    row = _file_state(conn, file_id)
    if row["current_fingerprint_id"] is None:
        raise RuntimeError("volume source fingerprint must be prepared before linking")
    if row["variant_id"] is not None:
        variant = conn.execute(
            "SELECT work_bucket_id FROM variants WHERE variant_id = ?",
            (row["variant_id"],),
        ).fetchone()
        if variant is None or int(variant[0]) != int(work_id):
            raise RuntimeError("volume source variant conflicts with selected work")
        return int(row["variant_id"])

    variant_id = int(
        conn.execute(
            "INSERT INTO variants(work_bucket_id, variant_kind, label) "
            "VALUES (?, 'base', ?)",
        (work_id, f"volume:{file_id}"),
        ).lastrowid
    )
    conn.execute(
        "UPDATE files SET variant_id = ?, assignment_state = 'managed', "
        "assignment_origin = ?, protected = 1 WHERE file_id = ?",
        (variant_id, origin, file_id),
    )
    conn.execute(
        "INSERT INTO representatives(variant_id, file_id) VALUES (?, ?)",
        (variant_id, file_id),
    )
    return variant_id


def link_volume_relationships(
    conn,
    *,
    file_ids: Sequence[str],
    display_title: str,
    origin: str,
) -> dict:
    """Attach distinct volume files to one work without declaring them same content."""

    if origin not in {"human_decision", "strong_match"}:
        raise ValueError(f"invalid volume relationship origin: {origin}")
    records = [{"source": _file_state(conn, file_id)} for file_id in file_ids]
    work_id = _choose_work(conn, records, display_title)
    variants = {
        file_id: _attach_volume_relationship(
            conn, file_id, work_id, origin=origin
        )
        for file_id in file_ids
    }
    return {"work_bucket_id": work_id, "variant_ids": variants}


def merge_staged_volume_group(
    conn,
    *,
    staged: Sequence[Mapping[str, object]],
    destination_root: Path,
    display_title: str,
    run_id: str,
    relationship_origin: str = "human_decision",
    progress=None,
) -> dict:
    """Move a fully staged group and commit all DB rows in one transaction."""

    destination_root = Path(destination_root).resolve()
    moved: list[dict] = []
    noops: list[dict] = []
    with mutation_lock(conn, f"{ACTION}:commit:{run_id}", run_id=run_id):
        actual_run = decision_store.assert_active_actual_run(conn, run_id)
        decision_store.assert_actual_run_path(
            actual_run, destination_root, "house_root"
        )
        ensure_directory_nofollow(destination_root)

        for index, record in enumerate(staged, start=1):
            stage_now = inspect_regular_file(record["stage_path"])
            if not evidence_matches(stage_now, record["stage_evidence"]):
                raise RuntimeError(f"volume staging copy changed: {record['stage_path']}")
            source = _file_state(conn, record["file_id"])
            if source["current_fingerprint_id"] != record["source"]["current_fingerprint_id"]:
                raise RuntimeError("volume source fingerprint changed after staging")
            source_path = Path(record["source_path"])
            destination = destination_root / source_path.name
            if source_path == destination:
                noops.append({**dict(record), "destination": str(destination)})
                continue
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(f"volume destination exists: {destination}")

            with decision_store.transaction(conn):
                operation_id = decision_store.create_operation(
                    conn,
                    run_id=run_id,
                    action=ACTION,
                    source_path=str(source_path),
                    dest_path=str(destination),
                    file_id=record["file_id"],
                    expected_size=source["size"],
                    expected_mtime_ns=source["mtime_ns"],
                    expected_fingerprint_id=source["current_fingerprint_id"],
                    source_dev=record["source_evidence"].dev,
                    source_ino=record["source_evidence"].ino,
                    source_ctime_ns=record["source_evidence"].ctime_ns,
                    source_sha256=record["source_evidence"].sha256,
                )

            def guard(file_id=record["file_id"], fingerprint=source["current_fingerprint_id"]):
                decision_store.assert_active_actual_run(conn, run_id)
                current = _file_state(conn, file_id)
                if current["current_fingerprint_id"] != fingerprint:
                    raise RuntimeError("volume source fingerprint changed before consume")

            destination_evidence = decision_store.copy_record_consume_operation(
                conn,
                operation_id,
                source_path,
                destination,
                record["source_evidence"],
                guard=guard,
            )
            moved.append(
                {
                    **dict(record),
                    "operation_id": operation_id,
                    "destination": str(destination),
                    "destination_evidence": destination_evidence,
                }
            )
            if progress is not None:
                progress(index, len(staged), source_path.name)

        if not moved:
            raise RuntimeError("volume group has no filesystem changes")

        ensure_volume_fingerprints(
            conn, [record["file_id"] for record in staged]
        )
        with decision_store.transaction(conn):
            relationship = link_volume_relationships(
                conn,
                file_ids=[record["file_id"] for record in staged],
                display_title=display_title,
                origin=relationship_origin,
            )
            work_id = relationship["work_bucket_id"]
            variants = relationship["variant_ids"]
            for record in moved:
                evidence = record["destination_evidence"]
                conn.execute(
                    "UPDATE files SET canonical_path = ?, source = 'house', "
                    "dev = ?, ino = ?, ctime_ns = ?, size = ?, mtime_ns = ?, "
                    "last_seen_at = CURRENT_TIMESTAMP WHERE file_id = ?",
                    (
                        record["destination"], evidence.dev, evidence.ino,
                        evidence.ctime_ns, evidence.size, evidence.mtime_ns,
                        record["file_id"],
                    ),
                )
                decision_store.upsert_file_analysis(
                    conn,
                    record["file_id"],
                    record["destination"],
                    stat_result=os.stat(record["destination"], follow_symlinks=False),
                )
                decision_store.transition_operation(
                    conn, record["operation_id"], "db_done"
                )
        with decision_store.transaction(conn):
            for record in moved:
                decision_store.transition_operation(
                    conn, record["operation_id"], "committed"
                )

        return {
            "work_bucket_id": work_id,
            "variant_ids": variants,
            "moved": [
                {
                    "operation_id": item["operation_id"],
                    "file_id": item["file_id"],
                    "source_path": item["source_path"],
                    "destination": item["destination"],
                }
                for item in moved
            ],
            "unchanged": [item["file_id"] for item in noops],
        }


def remove_empty_source_folders(
    source_paths: Sequence[str], *, house_root: Path, destination_root: Path
) -> list[str]:
    """Remove only empty work folders, never the house or chosung category roots."""

    house_root = Path(house_root).resolve()
    destination_root = Path(destination_root).resolve()
    removed = []
    parents = sorted(
        {Path(value).resolve().parent for value in source_paths},
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for parent in parents:
        if parent == destination_root or not _within(parent, house_root):
            continue
        try:
            relative = parent.relative_to(house_root)
        except ValueError:
            continue
        while len(relative.parts) > 1 and parent != destination_root:
            try:
                parent.rmdir()
            except OSError:
                break
            removed.append(str(parent))
            parent = parent.parent
            relative = parent.relative_to(house_root)
    return removed
