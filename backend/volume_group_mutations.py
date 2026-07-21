"""Journaled, relationship-preserving mutations for approved volume groups."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping, Sequence

import decision_store
from dedup_mutations import _ensure_intake_fingerprint, _file_state, _preflight
from mutation_io import (
    copy_no_clobber,
    ensure_directory_nofollow,
    evidence_matches,
    inspect_regular_file,
    mutation_lock,
    unlink_owned,
)


ACTION = "volume_group_merge"


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _coordinate_key(row: Mapping[str, object]):
    kind = row["coordinate_kind"]
    if kind == "volume":
        return kind, int(row["volume_num"]), int(row["volume_den"] or 1)
    if kind == "part":
        return kind, int(row["part_num"]), int(row["part_den"] or 1)
    return None


def classify_folderling_volume_target(
    conn, *, source_file_id: str, house_root: Path
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
        SELECT f.*, fa.core_title, fa.readable_title, fa.author, fa.disambig
        FROM files AS f LEFT JOIN file_analysis AS fa ON fa.file_id = f.file_id
        WHERE f.file_id = ? AND f.active = 1 AND f.source = 'temp'
        """,
        (source_file_id,),
    ).fetchone()
    if source_row is None:
        return no_target("missing_active_temp_source")
    source = dict(source_row)
    current_source_analysis = decision_store.build_file_analysis(
        Path(str(source["canonical_path"])).name
    )
    source["author"] = current_source_analysis["author"]
    if source["core_title"] is None:
        source.update(
            core_title=current_source_analysis["core_title"],
            readable_title=current_source_analysis["readable_title"],
            disambig=current_source_analysis["disambig"],
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

    existing = [
        {
            **dict(row),
            "author": decision_store.build_file_analysis(
                Path(str(row["canonical_path"])).name
            )["author"],
        }
        for row in conn.execute(
            """
            SELECT f.*, fa.readable_title, fa.author, fa.disambig, v.work_bucket_id
            FROM files AS f
            JOIN file_analysis AS fa ON fa.file_id = f.file_id
            LEFT JOIN variants AS v ON v.variant_id = f.variant_id
            WHERE f.active = 1 AND f.source = 'house' AND fa.core_title = ?
            ORDER BY f.canonical_path
            """,
            (source["core_title"],),
        ).fetchall()
    ]
    if not existing:
        return no_target("no_existing_core")
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
    coordinate_matches = [
        row for row in main_existing
        if _coordinate_key(row) == source_coordinate
    ]
    if coordinate_matches:
        return {
            "status": "coordinate_conflict",
            "reason": "existing_same_coordinate",
            "core_title": str(source["core_title"]),
            "display_title": str(source["readable_title"] or source["core_title"]),
            "coordinate_kind": source_coordinate[0],
            "coordinate_num": source_coordinate[1],
            "coordinate_den": source_coordinate[2],
            "conflicting_file_ids": [str(row["file_id"]) for row in coordinate_matches],
            "conflicting_paths": [str(row["canonical_path"]) for row in coordinate_matches],
        }

    authors = {str(row["author"]) for row in existing if row["author"]}
    if source["author"]:
        authors.add(str(source["author"]))
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
    conn, *, source_file_id: str, house_root: Path
) -> dict | None:
    """Return one fail-closed existing-folder target for a new volume intake."""
    decision = classify_folderling_volume_target(
        conn, source_file_id=source_file_id, house_root=house_root
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


def cleanup_staging(records: Sequence[Mapping[str, object]], staging_root: Path) -> None:
    """Remove only stage copies whose durable evidence still matches."""

    for record in reversed(list(records)):
        stage_path = Path(record["stage_path"])
        evidence = record["stage_evidence"]
        try:
            unlink_owned(stage_path, expected=evidence)
        except FileNotFoundError:
            continue
    manifest = Path(staging_root) / "stage-manifest.json"
    try:
        manifest_evidence = inspect_regular_file(manifest)
        unlink_owned(manifest, expected=manifest_evidence)
    except FileNotFoundError:
        pass
    current = Path(staging_root)
    while current.name and current.exists():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


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
                decision_store.assert_manifest_source(
                    actual_run, source_path, "house_root", source_evidence
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
                origin="human_decision",
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
