"""Journaled house -> temp mutation for user-approved filename corrections."""

from __future__ import annotations

from pathlib import Path

import decision_store
from dedup_mutations import _assert_row_identity, _ensure_intake_fingerprint, _file_state
from mutation_io import ensure_directory_nofollow, inspect_regular_file, mutation_lock


ACTION = "user_title_requeue"
EDITABLE_ASSIGNMENT_STATES = frozenset(
    {"unassigned", "decision_required", "legacy_unresolved"}
)


def requeue_user_title_file(conn, *, file_id: str, destination, run_id: str) -> dict:
    """Move one approved house file to temp and retire its old stable identity.

    The destination is intentionally not attached to the old file row.  The next
    Folderling run creates a fresh intake identity and applies the normal dedup
    pipeline.  Managed/protected/representative files are blocked until a later
    workflow can preserve their variant relationship explicitly.
    """

    destination = Path(destination).resolve()
    with mutation_lock(conn, f"{ACTION}:{run_id}", run_id=run_id):
        actual_run = decision_store.assert_active_actual_run(conn, run_id)
        source = _file_state(conn, file_id)
        if source["source"] != "house":
            raise RuntimeError("title correction source must be an active house file")
        if (
            source["assignment_state"] not in EDITABLE_ASSIGNMENT_STATES
            or source["variant_id"] is not None
            or source["protected"]
            or source["representative"]
        ):
            raise RuntimeError(
                "managed, protected, or representative files require a dedicated "
                "relationship-preserving title workflow"
            )

        source_path = Path(source["canonical_path"])
        decision_store.assert_actual_run_path(actual_run, source_path, "house_root")
        decision_store.assert_actual_run_path(actual_run, destination, "temp_root")
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"title correction destination exists: {destination}")
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
                raise RuntimeError("title correction fingerprint changed before consume")

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
                raise RuntimeError("title correction fingerprint changed before DB retire")
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
