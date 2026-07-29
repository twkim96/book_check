"""Targeted, journaled rollback for false series folders created by v1.4.3."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional, Sequence

import decision_store
import volume_review
from folderling import retarget_owned_recent_link
from mutation_io import (
    FileEvidence,
    ensure_directory_nofollow,
    evidence_matches,
    inspect_regular_file,
    mutation_lock_for_roots,
)
from project_paths import FILE_INDEX, HOUSE_DIR, STATE_DB, TEMP_DIR


SOURCE_ACTION = "volume_group_merge"
MOVE_ACTION = "library_file_relocate"
RESTORE_GROUP_ACTION = "volume_false_series_restore"


def _json_hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"file:{Path(path).expanduser().resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=5,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _chunked(values: Sequence[str], size: int = 400):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _all_candidate_rows(conn) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT f.*, fa.core_title, fa.readable_title, fa.author,
                   fa.effective_max, fa.unit, fa.complete,
                   v.work_bucket_id,
                   CASE WHEN rep.file_id IS NULL THEN 0 ELSE 1 END AS representative
            FROM files AS f
            JOIN file_analysis AS fa ON fa.file_id = f.file_id
            LEFT JOIN variants AS v ON v.variant_id = f.variant_id
            LEFT JOIN representatives AS rep ON rep.file_id = f.file_id
            WHERE f.active = 1 AND f.source = 'house'
              AND f.coordinate_kind IN ('volume', 'part', 'episode', 'symbol')
            ORDER BY f.canonical_path
            """
        ).fetchall()
    ]


def _backup_relationship(conn, file_id: str) -> dict | None:
    row = conn.execute(
        """
        SELECT f.file_id, f.variant_id, f.assignment_state,
               f.assignment_origin, f.protected,
               rep.created_at AS representative_created_at,
               rep.updated_at AS representative_updated_at,
               CASE WHEN rep.file_id IS NULL THEN 0 ELSE 1 END AS representative
        FROM files AS f
        LEFT JOIN representatives AS rep ON rep.file_id = f.file_id
        WHERE f.file_id = ?
        """,
        (file_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _operation_destination_evidence(operation: Mapping[str, object]) -> FileEvidence:
    return FileEvidence(
        dev=int(operation["destination_dev"]),
        ino=int(operation["destination_ino"]),
        ctime_ns=int(operation["destination_ctime_ns"]),
        size=int(operation["destination_size"]),
        mtime_ns=int(operation["destination_mtime_ns"]),
        sha256=str(operation["destination_sha256"]),
    )


def _stat_identity(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if path.is_symlink() or not path.is_file():
        return None
    return (
        info.st_dev,
        info.st_ino,
        info.st_ctime_ns,
        info.st_size,
        info.st_mtime_ns,
    )


def build_restore_plan(
    state_db: Path,
    *,
    house_dir: Path,
    source_run_id: str,
) -> dict:
    """Find only source-run groups without two distinct series positions."""

    state_db = Path(state_db).expanduser().resolve()
    house_dir = Path(house_dir).expanduser().resolve()
    conn = _connect_readonly(state_db)
    backup_conn = None
    try:
        source_run = conn.execute(
            "SELECT * FROM actual_runs WHERE run_id = ?", (source_run_id,)
        ).fetchone()
        if source_run is None:
            raise KeyError(source_run_id)
        source_backup = Path(str(source_run["backup_path"])).expanduser().resolve()
        blockers = []
        if source_run["state"] != "finished":
            blockers.append("source_run_not_finished")
        if not source_backup.is_file():
            blockers.append("source_backup_missing")
        elif decision_store.sha256_file(source_backup) != source_run["backup_sha256"]:
            blockers.append("source_backup_sha256_mismatch")

        operations = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM operations
                WHERE run_id = ? AND action = ? AND state = 'committed'
                ORDER BY operation_id
                """,
                (source_run_id, SOURCE_ACTION),
            ).fetchall()
        ]
        if not operations:
            blockers.append("source_run_has_no_volume_moves")

        operations_by_parent: dict[str, list[dict]] = defaultdict(list)
        for operation in operations:
            if not operation["dest_path"]:
                blockers.append(f"source_operation_missing_destination:{operation['operation_id']}")
                continue
            operations_by_parent[
                str(Path(str(operation["dest_path"])).parent)
            ].append(operation)

        all_rows = _all_candidate_rows(conn)
        rows_by_id = {str(row["file_id"]): row for row in all_rows}
        rows_by_parent: dict[str, list[dict]] = defaultdict(list)
        for row in all_rows:
            rows_by_parent[str(Path(str(row["canonical_path"])).parent)].append(row)

        false_groups = []
        true_group_count = 0
        relation_file_ids = set()
        moves = []
        for destination_parent, group_operations in sorted(operations_by_parent.items()):
            operation_rows = []
            for operation in group_operations:
                row = rows_by_id.get(str(operation["file_id"]))
                if row is None:
                    blockers.append(
                        f"source_operation_file_not_active_house:{operation['operation_id']}"
                    )
                    continue
                operation_rows.append(row)
            cores = {str(row["core_title"]) for row in operation_rows}
            if len(cores) != 1:
                blockers.append(f"source_group_core_conflict:{destination_parent}")
                continue
            core_title = next(iter(cores))
            group_rows_by_id = {
                str(row["file_id"]): row for row in operation_rows
            }
            for row in rows_by_parent.get(destination_parent, []):
                if str(row["core_title"]) == core_title:
                    group_rows_by_id[str(row["file_id"])] = row
            group_rows = list(group_rows_by_id.values())
            selected = volume_review._select_distinct_series_rows(group_rows)
            if selected:
                true_group_count += 1
                continue

            group_move_ids = []
            for operation in group_operations:
                file_id = str(operation["file_id"])
                current = rows_by_id[file_id]
                source_path = Path(str(operation["source_path"])).resolve()
                destination_path = Path(str(operation["dest_path"])).resolve()
                expected = _operation_destination_evidence(operation)
                expected_identity = (
                    expected.dev,
                    expected.ino,
                    expected.ctime_ns,
                    expected.size,
                    expected.mtime_ns,
                )
                source_identity = _stat_identity(source_path)
                destination_identity = _stat_identity(destination_path)
                if (
                    current["canonical_path"] == str(destination_path)
                    and source_identity is None
                    and destination_identity == expected_identity
                ):
                    status = "pending"
                elif (
                    current["canonical_path"] == str(source_path)
                    and source_identity is not None
                    and destination_identity is None
                    and source_identity[3] == expected.size
                    and source_identity[4] == expected.mtime_ns
                ):
                    status = "restored"
                else:
                    status = "conflict"
                    blockers.append(
                        f"restore_path_or_identity_conflict:{operation['operation_id']}"
                    )
                move = {
                    "source_operation_id": int(operation["operation_id"]),
                    "file_id": file_id,
                    "current_path": str(destination_path),
                    "restore_path": str(source_path),
                    "expected_size": expected.size,
                    "expected_mtime_ns": expected.mtime_ns,
                    "expected_destination": {
                        "dev": expected.dev,
                        "ino": expected.ino,
                        "ctime_ns": expected.ctime_ns,
                        "size": expected.size,
                        "mtime_ns": expected.mtime_ns,
                        "sha256": expected.sha256,
                    },
                    "status": status,
                    "current_fingerprint_id": current["current_fingerprint_id"],
                }
                moves.append(move)
                group_move_ids.append(int(operation["operation_id"]))

            group_relation_ids = sorted(group_rows_by_id)
            relation_file_ids.update(group_relation_ids)
            false_groups.append(
                {
                    "core_title": core_title,
                    "destination_parent": destination_parent,
                    "source_operation_ids": group_move_ids,
                    "relation_file_ids": group_relation_ids,
                    "positions": sorted(
                        {
                            repr(position)
                            for row in group_rows
                            if (position := volume_review._series_position(row)) is not None
                        }
                    ),
                }
            )

        backup_relationships = []
        if source_backup.is_file():
            backup_conn = _connect_readonly(source_backup)
            for file_id in sorted(relation_file_ids):
                before = _backup_relationship(backup_conn, file_id)
                current = rows_by_id.get(file_id)
                if before is None:
                    blockers.append(f"source_backup_file_missing:{file_id}")
                    continue
                if current is None:
                    blockers.append(f"relationship_file_not_active_house:{file_id}")
                    continue
                current_relation = {
                    "variant_id": current["variant_id"],
                    "work_bucket_id": current["work_bucket_id"],
                    "assignment_state": current["assignment_state"],
                    "assignment_origin": current["assignment_origin"],
                    "protected": current["protected"],
                    "representative": current["representative"],
                }
                before_relation = {
                    "variant_id": before["variant_id"],
                    "assignment_state": before["assignment_state"],
                    "assignment_origin": before["assignment_origin"],
                    "protected": before["protected"],
                    "representative": before["representative"],
                    "representative_created_at": before["representative_created_at"],
                    "representative_updated_at": before["representative_updated_at"],
                }
                relation_changed = (
                    current_relation["variant_id"],
                    current_relation["assignment_state"],
                    current_relation["assignment_origin"],
                    current_relation["protected"],
                    current_relation["representative"],
                ) != (
                    before_relation["variant_id"],
                    before_relation["assignment_state"],
                    before_relation["assignment_origin"],
                    before_relation["protected"],
                    before_relation["representative"],
                )
                if relation_changed and not (
                    current_relation["assignment_origin"] == "strong_match"
                    and int(current_relation["protected"] or 0) == 1
                    and current_relation["variant_id"] is not None
                    and int(current_relation["representative"] or 0) == 1
                ):
                    blockers.append(f"relationship_changed_after_source_run:{file_id}")
                if before["variant_id"] is not None:
                    variant = conn.execute(
                        "SELECT variant_id FROM variants WHERE variant_id = ?",
                        (before["variant_id"],),
                    ).fetchone()
                    if variant is None:
                        blockers.append(f"source_variant_missing:{before['variant_id']}")
                backup_relationships.append(
                    {
                        "file_id": file_id,
                        "current": current_relation,
                        "before": before_relation,
                    }
                )

        if relation_file_ids:
            ids = sorted(relation_file_ids)
            source_finished = str(source_run["finished_at"] or "")
            for chunk in _chunked(ids):
                placeholders = ",".join("?" for _ in chunk)
                later = conn.execute(
                    f"""
                    SELECT op.operation_id
                    FROM operations AS op
                    LEFT JOIN operation_groups AS og
                      ON og.group_id = op.operation_group_id
                    WHERE op.file_id IN ({placeholders})
                      AND op.created_at > ?
                      AND COALESCE(og.action, '') != ?
                    LIMIT 1
                    """,
                    (*chunk, source_finished, RESTORE_GROUP_ACTION),
                ).fetchone()
                if later is not None:
                    blockers.append(
                        f"later_file_operation_exists:{later['operation_id']}"
                    )
                    break

        payload = {
            "version": "1.4.3-false-series-restore-v1",
            "source_run_id": source_run_id,
            "source_backup_path": str(source_backup),
            "false_groups": false_groups,
            "moves": moves,
            "backup_relationships": backup_relationships,
        }
        return {
            **payload,
            "plan_sha256": _json_hash(payload),
            "source_group_count": len(operations_by_parent),
            "true_series_group_count": true_group_count,
            "false_series_group_count": len(false_groups),
            "move_count": len(moves),
            "pending_move_count": sum(move["status"] == "pending" for move in moves),
            "restored_move_count": sum(move["status"] == "restored" for move in moves),
            "relationship_file_count": len(backup_relationships),
            "blocked_reasons": sorted(set(blockers)),
            "apply_available": not blockers and bool(false_groups),
            "readonly": True,
        }
    finally:
        if backup_conn is not None:
            backup_conn.close()
        conn.close()


def _restore_relationships(conn, relationships: Sequence[Mapping[str, object]]) -> dict:
    current_variant_ids = {
        int(item["current"]["variant_id"])
        for item in relationships
        if item["current"]["variant_id"] is not None
    }
    before_variant_ids = {
        int(item["before"]["variant_id"])
        for item in relationships
        if item["before"]["variant_id"] is not None
    }
    candidate_variant_ids = current_variant_ids - before_variant_ids
    candidate_work_ids = {
        int(row[0])
        for variant_id in candidate_variant_ids
        if (row := conn.execute(
            "SELECT work_bucket_id FROM variants WHERE variant_id = ?",
            (variant_id,),
        ).fetchone()) is not None
    }

    for item in relationships:
        conn.execute(
            "DELETE FROM representatives WHERE file_id = ?",
            (item["file_id"],),
        )
    for item in relationships:
        before = item["before"]
        conn.execute(
            """
            UPDATE files SET variant_id = ?, assignment_state = ?,
                assignment_origin = ?, protected = ?
            WHERE file_id = ?
            """,
            (
                before["variant_id"],
                before["assignment_state"],
                before["assignment_origin"],
                before["protected"],
                item["file_id"],
            ),
        )
    for item in relationships:
        before = item["before"]
        if before["representative"]:
            conn.execute(
                """
                INSERT INTO representatives(
                    variant_id, file_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    before["variant_id"],
                    item["file_id"],
                    before["representative_created_at"],
                    before["representative_updated_at"],
                ),
            )

    removed_variants = 0
    for variant_id in sorted(candidate_variant_ids):
        cursor = conn.execute(
            """
            DELETE FROM variants
            WHERE variant_id = ?
              AND NOT EXISTS (SELECT 1 FROM files WHERE variant_id = ?)
              AND NOT EXISTS (SELECT 1 FROM representatives WHERE variant_id = ?)
              AND NOT EXISTS (
                  SELECT 1 FROM decisions
                  WHERE left_variant_id = ? OR right_variant_id = ?
              )
            """,
            (variant_id, variant_id, variant_id, variant_id, variant_id),
        )
        removed_variants += cursor.rowcount

    removed_works = 0
    for work_id in sorted(candidate_work_ids):
        cursor = conn.execute(
            """
            DELETE FROM works
            WHERE work_bucket_id = ?
              AND NOT EXISTS (SELECT 1 FROM variants WHERE work_bucket_id = ?)
              AND NOT EXISTS (SELECT 1 FROM work_folders WHERE work_bucket_id = ?)
              AND NOT EXISTS (SELECT 1 FROM work_aliases WHERE work_bucket_id = ?)
              AND NOT EXISTS (
                  SELECT 1 FROM decisions
                  WHERE left_work_id = ? OR right_work_id = ?
              )
              AND NOT EXISTS (
                  SELECT 1 FROM work_management_events
                  WHERE source_work_id = ? OR target_work_id = ?
              )
            """,
            (
                work_id,
                work_id,
                work_id,
                work_id,
                work_id,
                work_id,
                work_id,
                work_id,
            ),
        )
        removed_works += cursor.rowcount
    return {
        "relationship_rows_restored": len(relationships),
        "orphan_variants_removed": removed_variants,
        "orphan_works_removed": removed_works,
    }


def _mark_group_failed(conn, group_id: int, error: str) -> None:
    row = conn.execute(
        "SELECT state FROM operation_groups WHERE group_id = ?", (group_id,)
    ).fetchone()
    if row is not None and row["state"] in {"planned", "fs_done", "db_done"}:
        with decision_store.transaction(conn):
            decision_store.transition_operation_group(
                conn, group_id, "failed", error=error
            )


def apply_restore_plan(
    state_db: Path,
    *,
    house_dir: Path,
    temp_dir: Path,
    source_run_id: str,
    confirm_plan_sha256: str,
    progress=None,
) -> dict:
    state_db = Path(state_db).expanduser().resolve()
    house_dir = Path(house_dir).expanduser().resolve()
    temp_dir = Path(temp_dir).expanduser().resolve()
    with mutation_lock_for_roots(
        house_dir, temp_dir, "volume-false-series-restore-1.4.3"
    ):
        plan = build_restore_plan(
            state_db, house_dir=house_dir, source_run_id=source_run_id
        )
        if not plan["apply_available"]:
            raise RuntimeError(
                "false series restore plan blocked: "
                + ",".join(plan["blocked_reasons"])
            )
        if plan["plan_sha256"] != confirm_plan_sha256:
            raise RuntimeError("false series restore confirmation is stale")

        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(
                    f"doctor failed before false series restore: {issues[0]['kind']}"
                )
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            backup = decision_store.backup_state_db(
                conn,
                state_db.parent / "backups" /
                f"before_false_series_restore_{stamp}.sqlite3",
            )
            decision_store.issue_actual_run_token(
                conn, str(backup), house_dir=house_dir, temp_dir=temp_dir
            )
        finally:
            conn.close()

        pending_paths = [
            move["current_path"]
            for move in plan["moves"]
            if move["status"] == "pending"
        ]
        run_id, manifest_path = decision_store.prepare_actual_run(
            state_db,
            house_dir,
            temp_dir,
            manifest_paths=pending_paths,
        )
        conn = decision_store.connect_state_db(state_db)
        group_id = None
        recent_counts = defaultdict(int)
        moved = []
        removed_folders = []
        try:
            decision_store.assert_active_actual_run(
                conn,
                run_id,
                house_dir=house_dir,
                temp_dir=temp_dir,
                full_evidence=True,
            )
            with decision_store.transaction(conn):
                group_id = decision_store.create_operation_group(
                    conn,
                    run_id=run_id,
                    action=RESTORE_GROUP_ACTION,
                    plan_sha256=plan["plan_sha256"],
                    source_path=source_run_id,
                    dest_path=str(house_dir),
                    item_count=plan["pending_move_count"],
                    manifest_path=manifest_path,
                    source_manifest_json=json.dumps(
                        {
                            "source_run_id": source_run_id,
                            "false_series_group_count": plan["false_series_group_count"],
                            "move_count": plan["move_count"],
                            "relationship_file_count": plan["relationship_file_count"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )

            pending = [move for move in plan["moves"] if move["status"] == "pending"]
            for index, move in enumerate(pending, start=1):
                source_path = Path(str(move["current_path"]))
                destination = Path(str(move["restore_path"]))
                expected = FileEvidence(**move["expected_destination"])
                source_evidence = inspect_regular_file(source_path)
                if not evidence_matches(source_evidence, expected):
                    raise RuntimeError(
                        f"restore source evidence changed: {source_path}"
                    )
                active_run = decision_store.assert_active_actual_run(conn, run_id)
                decision_store.assert_manifest_source(
                    active_run, source_path, "house_root", source_evidence
                )
                decision_store.assert_actual_run_path(
                    active_run, destination, "house_root"
                )
                if destination.exists() or destination.is_symlink():
                    raise FileExistsError(destination)
                ensure_directory_nofollow(destination.parent)
                file_row = conn.execute(
                    "SELECT * FROM files WHERE file_id = ? AND active = 1 AND source = 'house'",
                    (move["file_id"],),
                ).fetchone()
                if (
                    file_row is None
                    or file_row["canonical_path"] != str(source_path)
                    or file_row["current_fingerprint_id"]
                    != move["current_fingerprint_id"]
                ):
                    raise RuntimeError(
                        f"restore DB source changed: {move['file_id']}"
                    )
                with decision_store.transaction(conn):
                    operation_id = decision_store.create_operation(
                        conn,
                        run_id=run_id,
                        action=MOVE_ACTION,
                        source_path=str(source_path),
                        dest_path=str(destination),
                        file_id=str(move["file_id"]),
                        expected_size=source_evidence.size,
                        expected_mtime_ns=source_evidence.mtime_ns,
                        expected_fingerprint_id=file_row["current_fingerprint_id"],
                        operation_group_id=group_id,
                        source_dev=source_evidence.dev,
                        source_ino=source_evidence.ino,
                        source_ctime_ns=source_evidence.ctime_ns,
                        source_sha256=source_evidence.sha256,
                    )

                def guard() -> None:
                    current = conn.execute(
                        "SELECT canonical_path, current_fingerprint_id FROM files "
                        "WHERE file_id = ? AND active = 1 AND source = 'house'",
                        (move["file_id"],),
                    ).fetchone()
                    info = os.stat(source_path, follow_symlinks=False)
                    identity = (
                        info.st_dev,
                        info.st_ino,
                        info.st_ctime_ns,
                        info.st_size,
                        info.st_mtime_ns,
                    )
                    expected_identity = (
                        source_evidence.dev,
                        source_evidence.ino,
                        source_evidence.ctime_ns,
                        source_evidence.size,
                        source_evidence.mtime_ns,
                    )
                    if (
                        current is None
                        or current["canonical_path"] != str(source_path)
                        or current["current_fingerprint_id"]
                        != move["current_fingerprint_id"]
                        or identity != expected_identity
                    ):
                        raise RuntimeError(
                            f"restore source changed before consume: {source_path}"
                        )

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
                        UPDATE files SET canonical_path = ?, source = 'house',
                            dev = ?, ino = ?, ctime_ns = ?, size = ?, mtime_ns = ?,
                            last_seen_at = CURRENT_TIMESTAMP
                        WHERE file_id = ? AND active = 1
                        """,
                        (
                            str(destination),
                            destination_evidence.dev,
                            destination_evidence.ino,
                            destination_evidence.ctime_ns,
                            destination_evidence.size,
                            destination_evidence.mtime_ns,
                            move["file_id"],
                        ),
                    )
                    decision_store.upsert_file_analysis(
                        conn,
                        str(move["file_id"]),
                        destination,
                        stat_result=os.stat(destination, follow_symlinks=False),
                    )
                    decision_store.transition_operation(
                        conn, operation_id, "db_done"
                    )
                with decision_store.transaction(conn):
                    decision_store.transition_operation(
                        conn, operation_id, "committed"
                    )
                recent_status = retarget_owned_recent_link(
                    house_dir / "_최근", source_path, destination
                )
                recent_counts[recent_status] += 1
                moved.append(
                    {
                        "operation_id": operation_id,
                        "file_id": move["file_id"],
                        "source_path": str(source_path),
                        "destination_path": str(destination),
                        "recent_status": recent_status,
                    }
                )
                if progress is not None:
                    progress(index, len(pending), destination.name)

            with decision_store.transaction(conn):
                decision_store.transition_operation_group(conn, group_id, "fs_done")
            with decision_store.transaction(conn):
                relationship_result = _restore_relationships(
                    conn, plan["backup_relationships"]
                )
                decision_store.transition_operation_group(conn, group_id, "db_done")
            with decision_store.transaction(conn):
                decision_store.transition_operation_group(conn, group_id, "committed")

            for group in plan["false_groups"]:
                folder = Path(str(group["destination_parent"]))
                try:
                    folder.rmdir()
                except OSError:
                    continue
                removed_folders.append(str(folder))

            doctor_issues = decision_store.doctor_issues(
                conn, allowed_active_run_id=run_id
            )
            if doctor_issues:
                raise RuntimeError(
                    f"doctor failed after false series restore: {doctor_issues[0]['kind']}"
                )
            decision_store.finish_actual_run(conn, run_id, success=True)
        except BaseException as exc:
            try:
                unfinished = conn.execute(
                    "SELECT operation_id FROM operations WHERE run_id = ? "
                    "AND state IN ('planned', 'fs_done', 'db_done') "
                    "ORDER BY operation_id",
                    (run_id,),
                ).fetchall()
                for row in unfinished:
                    decision_store.recover_interrupted_operation(
                        conn, int(row["operation_id"])
                    )
                if group_id is not None:
                    _mark_group_failed(conn, group_id, str(exc))
                decision_store.finish_actual_run(
                    conn, run_id, success=False, error=str(exc)
                )
            finally:
                conn.close()
            raise
        else:
            conn.close()

        volume_review.invalidate_volume_case_cache(
            state_db, house_dir=house_dir
        )
        return {
            "source_run_id": source_run_id,
            "restore_run_id": run_id,
            "operation_group_id": group_id,
            "plan_sha256": plan["plan_sha256"],
            "backup_path": str(backup),
            "manifest_path": manifest_path,
            "false_series_group_count": plan["false_series_group_count"],
            "moved_count": len(moved),
            "already_restored_count": plan["restored_move_count"],
            "recent": dict(sorted(recent_counts.items())),
            "removed_empty_folder_count": len(removed_folders),
            "removed_empty_folders": removed_folders,
            **relationship_result,
            "doctor_issue_count": 0,
            "moved": moved,
        }


def _write_report(path: Path, payload: Mapping[str, object]) -> Path:
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as report:
        json.dump(payload, report, ensure_ascii=False, indent=2, sort_keys=True)
        report.write("\n")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="1.4.3 false series folder targeted restore"
    )
    parser.add_argument("--state-db", default=str(STATE_DB))
    parser.add_argument("--house", default=str(HOUSE_DIR))
    parser.add_argument("--temp", default=str(TEMP_DIR))
    parser.add_argument("--index", default=str(FILE_INDEX))
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-plan-sha256")
    parser.add_argument("--report")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    state_db = Path(args.state_db)
    house_dir = Path(args.house)
    temp_dir = Path(args.temp)
    plan = build_restore_plan(
        state_db,
        house_dir=house_dir,
        source_run_id=args.source_run_id,
    )
    if not args.apply:
        output = plan
    else:
        if not args.confirm_plan_sha256:
            raise SystemExit("--confirm-plan-sha256 is required with --apply")

        def progress(current, total, name):
            if current == 1 or current == total or current % 50 == 0:
                print(f"restore {current:,}/{total:,}: {name}", flush=True)

        output = apply_restore_plan(
            state_db,
            house_dir=house_dir,
            temp_dir=temp_dir,
            source_run_id=args.source_run_id,
            confirm_plan_sha256=args.confirm_plan_sha256,
            progress=progress,
        )
        try:
            from library_review import _refresh_review_index

            output["index"] = _refresh_review_index(
                state_db=state_db,
                house_dir=house_dir,
                temp_dir=temp_dir,
                index_path=Path(args.index),
            )
        except Exception as exc:
            output["index"] = {
                "index_updated": False,
                "warning": str(exc),
            }

    report_path = args.report
    if report_path:
        written = _write_report(Path(report_path), output)
        print(f"report={written}")
    summary = {
        key: output.get(key)
        for key in (
            "plan_sha256",
            "source_group_count",
            "true_series_group_count",
            "false_series_group_count",
            "move_count",
            "pending_move_count",
            "restored_move_count",
            "relationship_file_count",
            "apply_available",
            "blocked_reasons",
            "restore_run_id",
            "moved_count",
            "removed_empty_folder_count",
            "doctor_issue_count",
            "index",
        )
        if key in output
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
