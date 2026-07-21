"""Journaled house -> temp requeue for approved 1.2.7 title corrections."""

from __future__ import annotations

from pathlib import Path

import decision_store
from dedup_mutations import (
    _assert_row_identity,
    _ensure_intake_fingerprint,
    _file_state,
)
from mutation_io import ensure_directory_nofollow, inspect_regular_file, mutation_lock


ACTION = "title_cleanup_requeue"


def requeue_unassigned_title_file(
    conn,
    *,
    file_id: str,
    destination,
    run_id: str,
):
    """Move one unassigned house file to temp and retire its old identity.

    The old immutable fingerprint and file row stay as provenance.  The temp
    path is intentionally not assigned to that row, so Folderling creates a new
    intake identity and runs the normal dedup pipeline without special cases.
    """
    destination = Path(destination).resolve()
    with mutation_lock(conn, f"{ACTION}:{run_id}", run_id=run_id):
        actual_run = decision_store.assert_active_actual_run(conn, run_id)
        source = _file_state(conn, file_id)
        if source["source"] != "house":
            raise RuntimeError("title cleanup source must be an active house file")
        if (
            source["assignment_state"] not in {"unassigned", "decision_required"}
            or source["variant_id"] is not None
            or source["protected"]
            or source["representative"]
        ):
            raise RuntimeError(
                "title cleanup automatic requeue only supports unassigned or "
                "decision-required, "
                "unprotected, non-representative files"
            )
        source_path = Path(source["canonical_path"])
        decision_store.assert_actual_run_path(actual_run, source_path, "house_root")
        decision_store.assert_actual_run_path(actual_run, destination, "temp_root")
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"title cleanup destination exists: {destination}")
        ensure_directory_nofollow(destination.parent)

        source = _ensure_intake_fingerprint(conn, source)
        source_evidence = inspect_regular_file(source_path)
        _assert_row_identity(source, source_evidence, source_path)
        decision_store.assert_manifest_source(
            actual_run, source_path, "house_root", source_evidence
        )

        with decision_store.transaction(conn):
            operation_id = decision_store.create_operation(
                conn,
                run_id=run_id,
                action=ACTION,
                source_path=str(source_path),
                dest_path=str(destination),
                file_id=file_id,
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
            current = _file_state(conn, file_id)
            if current["current_fingerprint_id"] != source["current_fingerprint_id"]:
                raise RuntimeError("title cleanup fingerprint changed before consume")

        destination_evidence = decision_store.copy_record_consume_operation(
            conn,
            operation_id,
            source_path,
            destination,
            source_evidence,
            guard=guard,
        )
        with decision_store.transaction(conn):
            current = _file_state(conn, file_id)
            if current["current_fingerprint_id"] != source["current_fingerprint_id"]:
                raise RuntimeError("title cleanup fingerprint changed before DB retire")
            retired_path = decision_store.retired_canonical_path(
                conn, file_id, source_path
            )
            conn.execute(
                """
                UPDATE files
                SET canonical_path = ?, active = 0, protected = 0,
                    last_seen_at = CURRENT_TIMESTAMP
                WHERE file_id = ?
                """,
                (retired_path, file_id),
            )
            decision_store.transition_operation(conn, operation_id, "db_done")
        with decision_store.transaction(conn):
            decision_store.transition_operation(conn, operation_id, "committed")
        return {
            "operation_id": operation_id,
            "action": ACTION,
            "file_id": file_id,
            "source_path": str(source_path),
            "dest_path": str(destination),
            "destination_size": destination_evidence.size,
        }
