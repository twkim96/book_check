import json
from pathlib import Path
from types import SimpleNamespace

import cleanup_quarantine_1_4_4 as cleanup
import decision_store
from dedup_mutations import (
    _ensure_intake_fingerprint,
    _file_state,
    user_quarantine,
)
from mutation_io import inspect_regular_file


def _register(conn, path, source):
    with decision_store.transaction(conn):
        row = decision_store.reconcile_file_metadata(
            conn, path, source=source
        )
    _ensure_intake_fingerprint(conn, _file_state(conn, row["file_id"]))
    return row["file_id"]


def test_plan_bound_cleanup_revalidates_then_purges_old_and_queue_copies(
    tmp_path, monkeypatch
):
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    queue = temp / "trash_bin" / "warning"
    house.mkdir()
    queue.mkdir(parents=True)
    keep = house / "감사 작품 대표.txt"
    old_discard = house / "감사 작품 옛 사본.txt"
    queued = queue / "감사 작품 큐 사본.txt"
    for path in (keep, old_discard, queued):
        path.write_text("같은 감사 본문", encoding="utf-8")

    conn = decision_store.initialize_state_db(state_db)
    try:
        keep_id = _register(conn, keep, "house")
        old_id = _register(conn, old_discard, "house")
        queue_id = _register(conn, queued, "queue")
        backup = decision_store.backup_state_db(
            conn, state_db.parent / "before-old-quarantine.sqlite3"
        )
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()
    run_id, _ = decision_store.prepare_actual_run(
        state_db, house, temp, manifest_paths=[old_discard, keep]
    )
    conn = decision_store.connect_state_db(state_db)
    try:
        old = user_quarantine(
            conn, source_file_id=old_id, keep_file_id=keep_id,
            quarantine_dir=temp / "trash_bin" / "user_discard_quarantine",
            run_id=run_id,
        )
        decision_store.finish_actual_run(conn, run_id, success=True)
    finally:
        conn.close()

    old_path = Path(old["dest_path"])
    plan = {
        "schema_version": 1,
        "kind": "quarantine_cleanup_1_4_4",
        "restore": [],
        "upgrade_restored": [],
        "queue_discard": [{
            "file_id": queue_id,
            "keep_file_id": keep_id,
            "expected_source_sha256": inspect_regular_file(queued).sha256,
            "expected_keep_sha256": inspect_regular_file(keep).sha256,
        }],
        "queue_upgrade": [],
        "untracked_queue_discard": [],
        "missing_quarantine_ack": [],
        "metadata_cleanup": [],
        "purge_revalidation": [{
            "operation_id": old["operation_id"],
            "keep_file_id": keep_id,
            "expected_source_sha256": inspect_regular_file(old_path).sha256,
            "expected_keep_sha256": inspect_regular_file(keep).sha256,
        }],
        "purge_operation_ids": [old["operation_id"]],
    }
    plan_path = tmp_path / "plan.json"
    cleanup._atomic_json(plan_path, plan)
    monkeypatch.setattr(cleanup, "FILE_LIST", tmp_path / "file_list.json")
    monkeypatch.setattr(cleanup, "FILE_INDEX", tmp_path / "file_index.json")
    monkeypatch.setattr(cleanup, "PROJECT_ROOT", tmp_path)
    report = temp / "dedup_logs" / "cleanup-result.json"
    args = SimpleNamespace(
        plan=str(plan_path), state_db=str(state_db), house=str(house),
        temp=str(temp), execute=True,
        confirm_plan_sha256=cleanup._hash(plan), report_path=str(report),
    )

    assert cleanup.run(args) == 0
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["purge"]["purged_count"] == 2
    assert result["verification"]["doctor_issue_count"] == 0
    assert keep.is_file()
    assert not old_path.exists()
    assert not queued.exists()
