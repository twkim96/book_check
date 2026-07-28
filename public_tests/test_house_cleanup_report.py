import json
import os
from pathlib import Path

import decision_store
import dedup_mutations
import pytest
import run_house_cleanup_once


def _protected_exact_pair(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    representative_path = house / "보호 대표.txt"
    candidate_path = house / "이동 후보.txt"
    representative_path.write_text("동일 본문", encoding="utf-8")
    candidate_path.write_text("동일 본문", encoding="utf-8")
    state_db = tmp_path / "state.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    with decision_store.transaction(conn):
        representative = decision_store.reconcile_file_metadata(
            conn, representative_path, source="house"
        )
        candidate = decision_store.reconcile_file_metadata(
            conn, candidate_path, source="house"
        )
    dedup_mutations.refresh_user_approved_snapshot(
        conn, representative["file_id"]
    )
    dedup_mutations.refresh_user_approved_snapshot(conn, candidate["file_id"])
    with decision_store.transaction(conn):
        work_id = conn.execute(
            "INSERT INTO works(display_title) VALUES ('보호 작품')"
        ).lastrowid
        variant_id = conn.execute(
            "INSERT INTO variants(work_bucket_id, variant_kind) VALUES (?, 'base')",
            (work_id,),
        ).lastrowid
        conn.execute(
            "UPDATE files SET variant_id=?, assignment_state='managed', "
            "assignment_origin='human_decision', protected=1 WHERE file_id=?",
            (variant_id, representative["file_id"]),
        )
        conn.execute(
            "INSERT INTO representatives(variant_id, file_id) VALUES (?, ?)",
            (variant_id, representative["file_id"]),
        )
    review_id = decision_store.add_review_item(
        conn,
        candidate_file_id=candidate["file_id"],
        reference_file_id=representative["file_id"],
        classification="text_equivalent",
        evidence_json=json.dumps({"raw_match": True}),
    )
    return {
        "conn": conn,
        "state_db": state_db,
        "house": house,
        "temp": temp,
        "representative": representative,
        "candidate": candidate,
        "review_id": review_id,
    }


def test_exact_plan_preserves_protected_representative_and_review_evidence(tmp_path):
    fixture = _protected_exact_pair(tmp_path)
    try:
        plans = run_house_cleanup_once.build_plan(fixture["conn"])
        review = fixture["conn"].execute(
            "SELECT left_fingerprint_id, right_fingerprint_id "
            "FROM review_items WHERE review_id = ?",
            (fixture["review_id"],),
        ).fetchone()
    finally:
        fixture["conn"].close()

    assert len(plans) == 1
    assert plans[0]["keep_file_id"] == fixture["representative"]["file_id"]
    assert plans[0]["move_file_id"] == fixture["candidate"]["file_id"]
    assert plans[0]["review_evidence"] == {"raw_match": True}
    assert plans[0]["left_fingerprint_id"] == review["left_fingerprint_id"]
    assert plans[0]["right_fingerprint_id"] == review["right_fingerprint_id"]


def test_protected_exact_component_does_not_absorb_weak_neighbor(tmp_path):
    fixture = _protected_exact_pair(tmp_path)
    weak_path = fixture["house"] / "약한 이웃.txt"
    weak_path.write_text("유사하지만 다른 본문", encoding="utf-8")
    with decision_store.transaction(fixture["conn"]):
        weak = decision_store.reconcile_file_metadata(
            fixture["conn"], weak_path, source="house"
        )
    dedup_mutations.refresh_user_approved_snapshot(
        fixture["conn"], weak["file_id"]
    )
    decision_store.add_review_item(
        fixture["conn"],
        candidate_file_id=weak["file_id"],
        reference_file_id=fixture["candidate"]["file_id"],
        classification="near_identical",
        evidence_json=json.dumps({"sampled": True}),
    )
    try:
        plans = run_house_cleanup_once.build_plan(fixture["conn"])
    finally:
        fixture["conn"].close()

    assert [plan["move_file_id"] for plan in plans] == [
        fixture["candidate"]["file_id"]
    ]


def test_index_failure_marks_actual_run_failed(tmp_path, monkeypatch):
    fixture = _protected_exact_pair(tmp_path)
    fixture["conn"].close()
    monkeypatch.setattr(
        run_house_cleanup_once,
        "house_review_move",
        lambda *args, **kwargs: {"operation_id": 1},
    )
    monkeypatch.setattr(
        run_house_cleanup_once.folderling,
        "generate_file_list",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("index publish failed")
        ),
    )

    with pytest.raises(RuntimeError, match="index publish failed"):
        run_house_cleanup_once.run(
            fixture["state_db"], fixture["house"], fixture["temp"], execute=True
        )

    conn = decision_store.connect_state_db_readonly(fixture["state_db"])
    try:
        actual = conn.execute(
            "SELECT state, error, backup_path FROM actual_runs"
        ).fetchone()
    finally:
        conn.close()
    assert actual["state"] == "failed"
    assert "index publish failed" in actual["error"]
    assert Path(actual["backup_path"]).is_file()


def test_house_cleanup_report_is_atomic_and_records_final_state(tmp_path):
    state_db = tmp_path / "state.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    conn.close()
    temp = tmp_path / "temp"
    index = tmp_path / "file_index.json"
    index.write_text(json.dumps({
        "generation_id": "generation-clean",
        "generated_at": "2026-07-28T00:00:00+09:00",
        "entries": [],
    }), encoding="utf-8")
    target = run_house_cleanup_once._report_path(temp)

    written = run_house_cleanup_once.write_execution_report(
        target,
        result={"dry_run": False, "moved": []},
        state_db=state_db,
        index_path=index,
    )

    payload = json.loads(Path(written).read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["kind"] == "house_cleanup_1_4_0"
    assert payload["final_snapshot"]["doctor_issues"] == []
    assert payload["final_snapshot"]["active_runs"] == 0
    assert payload["final_snapshot"]["index"]["generation_id"] == \
        "generation-clean"
    assert list(target.parent.glob(target.name + ".*.tmp")) == []


def test_house_cleanup_report_path_cannot_escape_temp(tmp_path):
    with pytest.raises(ValueError, match="dedup_logs"):
        run_house_cleanup_once._report_path(
            tmp_path / "temp", tmp_path / "outside.json"
        )


def test_house_cleanup_report_rejects_symlink_log_directory(tmp_path):
    temp = tmp_path / "temp"
    outside = tmp_path / "outside"
    temp.mkdir()
    outside.mkdir()
    os.symlink(outside, temp / "dedup_logs")

    with pytest.raises(OSError):
        run_house_cleanup_once._report_path(temp)


def test_house_cleanup_main_writes_report_when_run_fails(tmp_path, monkeypatch):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / "state.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    conn.close()
    report = temp / "dedup_logs" / "house_cleanup_1_4_0_failure.json"
    monkeypatch.setattr(
        run_house_cleanup_once,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("move failed")),
    )

    with pytest.raises(RuntimeError, match="move failed"):
        run_house_cleanup_once.main([
            "--state-db", str(state_db),
            "--house", str(house),
            "--temp", str(temp),
            "--report-path", str(report),
            "--run",
            "--ack-user-approved",
        ])

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["result"]["status"] == "failed"
    assert payload["result"]["error"] == "move failed"


def test_house_cleanup_intent_failure_stops_before_run_and_move(
    tmp_path, monkeypatch,
):
    fixture = _protected_exact_pair(tmp_path)
    fixture["conn"].close()
    monkeypatch.setattr(
        run_house_cleanup_once,
        "write_intent_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("intent write failed")
        ),
    )

    with pytest.raises(RuntimeError, match="intent write failed"):
        run_house_cleanup_once.run(
            fixture["state_db"], fixture["house"], fixture["temp"], execute=True
        )

    assert (fixture["house"] / "이동 후보.txt").is_file()
    conn = decision_store.connect_state_db_readonly(fixture["state_db"])
    try:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM actual_runs").fetchone()[0] == 0
    finally:
        conn.close()


def test_house_cleanup_terminal_failure_keeps_intent(
    tmp_path, monkeypatch,
):
    fixture = _protected_exact_pair(tmp_path)
    fixture["conn"].close()
    calls = []
    monkeypatch.setattr(
        run_house_cleanup_once,
        "house_review_move",
        lambda *args, **kwargs: calls.append(kwargs) or {"operation_id": 7},
    )
    monkeypatch.setattr(
        run_house_cleanup_once.folderling, "generate_file_list", lambda *a, **k: True
    )
    monkeypatch.setattr(
        run_house_cleanup_once.folderling, "sync_house_index", lambda *a, **k: None
    )
    monkeypatch.setattr(
        run_house_cleanup_once.folderling, "sync_extension_index", lambda *a, **k: None
    )
    monkeypatch.setattr(
        run_house_cleanup_once, "_prune_folderling_backups", lambda *a, **k: None
    )
    monkeypatch.setattr(
        run_house_cleanup_once,
        "write_execution_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("terminal write failed")
        ),
    )
    intent = run_house_cleanup_once._intent_report_path(fixture["temp"])
    terminal = run_house_cleanup_once._report_path(fixture["temp"])

    with pytest.raises(RuntimeError, match="terminal write failed"):
        run_house_cleanup_once.run(
            fixture["state_db"],
            fixture["house"],
            fixture["temp"],
            execute=True,
            intent_report_path=intent,
            report_path=terminal,
        )

    payload = json.loads(intent.read_text(encoding="utf-8"))
    assert payload["kind"] == "house_cleanup_intent_1_4_0"
    assert payload["phase"] == "intent"
    assert len(calls) == 1
    conn = decision_store.connect_state_db_readonly(fixture["state_db"])
    try:
        run = conn.execute(
            "SELECT state, manifest_path FROM actual_runs"
        ).fetchone()
        assert run["state"] == "finished"
        manifest = json.loads(Path(run["manifest_path"]).read_text(encoding="utf-8"))
        assert any(
            record["source"] == "temp"
            and record["rel_path"].endswith(intent.name)
            for record in manifest["files"]
        )
    finally:
        conn.close()
