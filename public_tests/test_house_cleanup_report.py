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


def _house_review_graph(tmp_path, names, relations):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / "state.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    rows = {}
    for name, body in names.items():
        path = house / name
        path.write_text(body, encoding="utf-8")
        with decision_store.transaction(conn):
            row = decision_store.reconcile_file_metadata(
                conn, path, source="house"
            )
        dedup_mutations.refresh_user_approved_snapshot(conn, row["file_id"])
        rows[name] = row
    review_ids = []
    for left, right, classification in relations:
        review_ids.append(decision_store.add_review_item(
            conn,
            candidate_file_id=rows[left]["file_id"],
            reference_file_id=rows[right]["file_id"],
            classification=classification,
            evidence_json=json.dumps({"fixture_relation": [left, right]}),
        ))
    return {
        "conn": conn, "state_db": state_db, "house": house, "temp": temp,
        "rows": rows, "review_ids": review_ids,
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


def test_all_pending_cannot_cross_explicit_sibling_volume_coordinates(tmp_path):
    fixture = _house_review_graph(
        tmp_path,
        {"형제 작품 1권.txt": "같은 본문", "형제 작품 2권.txt": "같은 본문"},
        [("형제 작품 1권.txt", "형제 작품 2권.txt", "text_equivalent")],
    )
    try:
        assert run_house_cleanup_once.build_plan(
            fixture["conn"], scope="queueable"
        ) == []
        assert run_house_cleanup_once.build_plan(
            fixture["conn"], scope="all-pending"
        ) == []
    finally:
        fixture["conn"].close()


def test_all_pending_cannot_cross_managed_variant_identity(tmp_path):
    fixture = _house_review_graph(
        tmp_path,
        {"관리 작품 A.txt": "같은 본문", "관리 작품 B.txt": "같은 본문"},
        [("관리 작품 A.txt", "관리 작품 B.txt", "text_equivalent")],
    )
    with decision_store.transaction(fixture["conn"]):
        work_id = fixture["conn"].execute(
            "INSERT INTO works(display_title) VALUES ('관리 작품')"
        ).lastrowid
        variant_ids = [
            fixture["conn"].execute(
                "INSERT INTO variants(work_bucket_id, variant_kind) "
                "VALUES (?, 'other')", (work_id,)
            ).lastrowid
            for _ in range(2)
        ]
        for row, variant_id in zip(fixture["rows"].values(), variant_ids):
            fixture["conn"].execute(
                "UPDATE files SET assignment_state='managed', "
                "assignment_origin='human_decision', variant_id=? "
                "WHERE file_id=?", (variant_id, row["file_id"])
            )
    try:
        assert run_house_cleanup_once.build_plan(
            fixture["conn"], scope="all-pending"
        ) == []
    finally:
        fixture["conn"].close()


def test_all_pending_strong_relation_uses_human_review_queue(tmp_path, monkeypatch):
    fixture = _house_review_graph(
        tmp_path,
        {
            "안전 작품 1-100.txt": "같은 본문",
            "안전 작품 1-100 완.txt": "같은 본문",
        },
        [("안전 작품 1-100.txt", "안전 작품 1-100 완.txt", "text_equivalent")],
    )
    fixture["conn"].close()
    calls = []
    monkeypatch.setattr(
        run_house_cleanup_once,
        "apply_strong_equivalent_quarantine",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("all-pending strong must not final-quarantine")
        ),
    )
    monkeypatch.setattr(
        run_house_cleanup_once,
        "house_review_move",
        lambda *args, **kwargs: calls.append(kwargs) or {
            "operation_id": 1, "destination": str(kwargs["queue_dir"] / "queued.txt")
        },
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

    run_house_cleanup_once.run(
        fixture["state_db"], fixture["house"], fixture["temp"],
        execute=True, scope="all-pending",
    )

    assert len(calls) == 1
    assert calls[0]["classification"] == "text_equivalent"
    assert calls[0]["queue_dir"].name == "house_human_review"


def test_mixed_component_rebinds_weak_edge_to_strong_representative(
    tmp_path, monkeypatch,
):
    names = {
        "연결 작품 1-100.txt": "짧은 다른 본문",
        "연결 작품 1-200.txt": "같은 강한 본문",
        "연결 작품 1-300.txt": "같은 강한 본문",
    }
    fixture = _house_review_graph(
        tmp_path,
        names,
        [
            ("연결 작품 1-100.txt", "연결 작품 1-200.txt", "near_identical"),
            ("연결 작품 1-200.txt", "연결 작품 1-300.txt", "text_equivalent"),
        ],
    )
    plans = run_house_cleanup_once.build_plan(fixture["conn"])
    assert [plan["phase"] for plan in plans] == ["strong", "weak"]
    assert plans[0]["move_file_id"] == fixture["rows"]["연결 작품 1-200.txt"]["file_id"]
    assert plans[0]["keep_file_id"] == fixture["rows"]["연결 작품 1-300.txt"]["file_id"]
    assert plans[1]["move_file_id"] == fixture["rows"]["연결 작품 1-100.txt"]["file_id"]
    assert plans[1]["keep_file_id"] == fixture["rows"]["연결 작품 1-300.txt"]["file_id"]
    assert plans[1]["review_rebind_required"] is True
    source_review_id = plans[1]["source_review_id"]
    fixture["conn"].close()

    calls = []

    def quarantine_and_supersede(conn, *args, **kwargs):
        calls.append(("strong", kwargs))
        with decision_store.transaction(conn):
            decision_store.supersede_open_reviews_for_file(
                conn,
                kwargs["discard_file_id"],
                reason="test_strong_quarantine",
            )
        return {"operation_id": 10, "dest_path": "strong"}

    monkeypatch.setattr(
        run_house_cleanup_once,
        "apply_strong_equivalent_quarantine",
        quarantine_and_supersede,
    )
    monkeypatch.setattr(
        run_house_cleanup_once,
        "house_review_move",
        lambda *args, **kwargs: calls.append(("weak", kwargs)) or {
            "operation_id": 11, "destination": "weak"
        },
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

    run_house_cleanup_once.run(
        fixture["state_db"], fixture["house"], fixture["temp"], execute=True
    )

    assert [kind for kind, _kwargs in calls] == ["strong", "weak"]
    assert calls[1][1]["keep_file_id"] == fixture["rows"]["연결 작품 1-300.txt"]["file_id"]
    assert calls[1][1]["review_id"] != source_review_id
    conn = decision_store.connect_state_db_readonly(fixture["state_db"])
    try:
        rebound = conn.execute(
            "SELECT candidate_file_id, reference_file_id, state, evidence_json "
            "FROM review_items WHERE review_id = ?",
            (calls[1][1]["review_id"],),
        ).fetchone()
        source_state = conn.execute(
            "SELECT state FROM review_items WHERE review_id = ?",
            (source_review_id,),
        ).fetchone()["state"]
    finally:
        conn.close()
    assert {
        rebound["candidate_file_id"], rebound["reference_file_id"]
    } == {
        fixture["rows"]["연결 작품 1-100.txt"]["file_id"],
        fixture["rows"]["연결 작품 1-300.txt"]["file_id"],
    }
    assert source_state == "superseded"
    assert rebound["state"] == "pending"
    assert json.loads(rebound["evidence_json"])["strong_component_rebind"][
        "source_review_id"
    ] == source_review_id


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
        "apply_strong_equivalent_quarantine",
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
        "apply_strong_equivalent_quarantine",
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
