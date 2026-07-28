import json
from pathlib import Path

import decision_store
import dedup_mutations
import pytest
import resolve_house_review_batch


def _bound_plan(conn, *, quarantine=(), restore_distinct=()):
    rows = resolve_house_review_batch._active_files(conn)
    title_index = resolve_house_review_batch._exact_title_index(rows)

    def bind(item, prefixes):
        bound = dict(item)
        for prefix in prefixes:
            row = resolve_house_review_batch._one_selector(
                rows, title_index, bound, prefix, f"{prefix} test selector"
            )
            bound[f"{prefix}_expected_sha256"] = \
                resolve_house_review_batch._file_snapshot(row)["raw_sha256"]
        return bound

    return {
        "schema_version": 2,
        "kind": "manual_house_cleanup_1_4_0",
        "quarantine": [bind(item, ("delete", "keep")) for item in quarantine],
        "restore_distinct": [
            bind(item, ("restore", "reference")) for item in restore_distinct
        ],
    }


def _empty_plan():
    return {
        "schema_version": 2,
        "kind": "manual_house_cleanup_1_4_0",
        "quarantine": [],
        "restore_distinct": [],
    }


def _active_pair(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    left = house / "수동 삭제 후보.txt"
    right = house / "보존할 도서.txt"
    left.write_text("같은 본문", encoding="utf-8")
    right.write_text("같은 본문", encoding="utf-8")
    state_db = tmp_path / "state.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    with decision_store.transaction(conn):
        decision_store.reconcile_file_metadata(conn, left, source="house")
        decision_store.reconcile_file_metadata(conn, right, source="house")
    return conn, state_db


def _restore_resume_fixture(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    queue = temp / "trash_bin" / "warning"
    (house / "ㅇ").mkdir(parents=True)
    (house / "ㅂ").mkdir(parents=True)
    queue.mkdir(parents=True)
    restore_path = queue / "오탐 후보.txt"
    reference_path = house / "ㅂ" / "참조 작품.txt"
    restore_path.write_text("완전히 다른 후보 본문", encoding="utf-8")
    reference_path.write_text("서로 무관한 참조 본문", encoding="utf-8")
    state_db = tmp_path / "state.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    with decision_store.transaction(conn):
        restore = decision_store.reconcile_file_metadata(
            conn, restore_path, source="queue"
        )
        reference = decision_store.reconcile_file_metadata(
            conn, reference_path, source="house"
        )
    dedup_mutations.refresh_user_approved_snapshot(conn, restore["file_id"])
    dedup_mutations.refresh_user_approved_snapshot(conn, reference["file_id"])
    review_id = decision_store.add_review_item(
        conn,
        candidate_file_id=restore["file_id"],
        reference_file_id=reference["file_id"],
        classification="manual_false_positive_restore",
        evidence_json=json.dumps({"fixture": True}),
    )
    extra_plan = _bound_plan(conn, restore_distinct=[{
        "restore_file_id": restore["file_id"],
        "reference_file_id": reference["file_id"],
        "destination_rel": "ㅇ/오탐 후보.txt",
        "reason": "confirmed_false_positive",
        "note": "분산 본문 비교 결과 서로 다른 작품",
    }])
    plan = resolve_house_review_batch.build_plan(conn, [], extra_plan)
    conn.close()
    return {
        "house": house,
        "temp": temp,
        "state_db": state_db,
        "restore": restore,
        "reference": reference,
        "review_id": review_id,
        "restore_path": restore_path,
        "destination": house / "ㅇ" / "오탐 후보.txt",
        "recent": house / "_최근" / "오탐 후보.txt",
        "extra_plan": extra_plan,
        "plan": plan,
    }


def _patch_restore_indexes(monkeypatch):
    monkeypatch.setattr(
        resolve_house_review_batch.folderling,
        "generate_file_list",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        resolve_house_review_batch.folderling,
        "sync_house_index",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        resolve_house_review_batch.folderling,
        "sync_extension_index",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        resolve_house_review_batch,
        "_prune_folderling_backups",
        lambda *args, **kwargs: None,
    )


def test_manual_plan_preserves_evidence_and_selection_policy(tmp_path):
    conn, _state_db = _active_pair(tmp_path)
    try:
        extra_plan = _bound_plan(conn, quarantine=[{
            "delete": "수동 삭제 후보",
            "keep": "보존할 도서",
            "reason": "v1_4_0_manual_anchor_duplicate",
            "classification": "manual_strong_anchor",
            "selection_policy": "prefer_complete_longer",
            "evidence": {"sample_anchor_matches": 7},
        }])
        plan = resolve_house_review_batch.build_plan(conn, [], extra_plan)
    finally:
        conn.close()

    assert plan["queue_restore"] == []
    assert plan["queue_delete"] == []
    assert plan["explicit_delete"][0]["classification"] == "manual_strong_anchor"
    assert plan["explicit_delete"][0]["selection_policy"] == "prefer_complete_longer"
    assert plan["explicit_delete"][0]["evidence"] == {
        "sample_anchor_matches": 7,
    }


def test_manual_plan_rel_path_selector_disambiguates_same_stem(tmp_path):
    conn, _state_db = _active_pair(tmp_path)
    duplicate_stem = tmp_path / "house" / "보존할 도서.epub"
    duplicate_stem.write_bytes(b"epub fixture")
    with decision_store.transaction(conn):
        decision_store.reconcile_file_metadata(
            conn, duplicate_stem, source="house"
        )
    try:
        with pytest.raises(RuntimeError, match="exactly one"):
            resolve_house_review_batch.build_plan(conn, [], {
                "schema_version": 2,
                "quarantine": [{
                    "delete": "수동 삭제 후보",
                    "keep": "보존할 도서",
                    "delete_expected_sha256": "0" * 64,
                    "keep_expected_sha256": "0" * 64,
                }],
                "restore_distinct": [],
            })
        extra_plan = _bound_plan(conn, quarantine=[{
            "delete_rel_path": "수동 삭제 후보.txt",
            "keep_rel_path": "보존할 도서.txt",
        }])
        plan = resolve_house_review_batch.build_plan(conn, [], extra_plan)
    finally:
        conn.close()

    assert plan["explicit_delete"][0]["keep_path"].endswith(
        "/보존할 도서.txt"
    )


def test_omitted_delete_list_does_not_restore_legacy_review_queue(tmp_path):
    conn, _state_db = _active_pair(tmp_path)
    try:
        file_id = conn.execute(
            "SELECT file_id FROM files WHERE canonical_path LIKE '%수동 삭제 후보.txt'"
        ).fetchone()["file_id"]
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE files SET source='queue', canonical_path=? WHERE file_id=?",
                (
                    str(tmp_path / "temp" / "trash_bin" / "house_human_review" /
                        "수동 삭제 후보.txt"),
                    file_id,
                ),
            )
        manual_only = resolve_house_review_batch.build_plan(
            conn, None, _empty_plan()
        )
        legacy_empty_list = resolve_house_review_batch.build_plan(
            conn, [], _empty_plan()
        )
    finally:
        conn.close()

    assert manual_only["queue_total"] == 1
    assert manual_only["queue_restore"] == []
    assert len(legacy_empty_list["queue_restore"]) == 1


def test_execution_report_is_atomic_and_contains_final_snapshot(tmp_path):
    conn, state_db = _active_pair(tmp_path)
    conn.close()
    temp = tmp_path / "temp"
    report = resolve_house_review_batch._report_path(temp)
    index = tmp_path / "file_index.json"
    index.write_text(json.dumps({
        "generation_id": "generation-1",
        "generated_at": "2026-07-28T00:00:00+09:00",
        "entries": [{"type": "file"}, {"type": "file"}],
    }), encoding="utf-8")

    written = resolve_house_review_batch.write_execution_report(
        report,
        plan={"explicit_delete": []},
        result={"quarantined": []},
        state_db=state_db,
        index_path=index,
    )

    payload = json.loads(Path(written).read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["kind"] == "manual_house_cleanup_1_4_0"
    assert payload["final_snapshot"]["counts"]["active_house"] == 2
    assert payload["final_snapshot"]["index"] == {
        "entries": 2,
        "generated_at": "2026-07-28T00:00:00+09:00",
        "generation_id": "generation-1",
    }
    assert list(report.parent.glob(report.name + ".*.tmp")) == []


def test_report_path_cannot_escape_temp_dedup_logs(tmp_path):
    with pytest.raises(ValueError, match="dedup_logs"):
        resolve_house_review_batch._report_path(
            tmp_path / "temp", tmp_path / "outside.json"
        )


def test_restore_distinct_plan_requires_queue_source_and_keeps_evidence(tmp_path):
    conn, _state_db = _active_pair(tmp_path)
    try:
        restore = conn.execute(
            "SELECT file_id FROM files WHERE canonical_path LIKE '%수동 삭제 후보.txt'"
        ).fetchone()["file_id"]
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE files SET source = 'queue' WHERE file_id = ?", (restore,)
            )
        extra_plan = _bound_plan(conn, restore_distinct=[{
            "restore": "수동 삭제 후보",
            "reference": "보존할 도서",
            "destination_rel": "ㅅ/수동 삭제 후보.txt",
            "reason": "confirmed_false_positive",
            "note": "본문 비교 결과 서로 다른 작품",
            "evidence": {"front_similarity": 0.05},
        }])
        extra_plan.update({
            "kind": "manual_house_cleanup_1_4_0",
            "source_audit": {"sha256": "audit-digest"},
        })
        plan = resolve_house_review_batch.build_plan(conn, [], extra_plan)
    finally:
        conn.close()

    assert plan["blocked"] == []
    assert plan["plan_metadata"]["source_audit"] == {
        "sha256": "audit-digest",
    }
    assert plan["explicit_delete"] == []
    assert plan["explicit_restore"][0]["verdict"] == "distinct_work"
    assert plan["explicit_restore"][0]["destination_rel"] == \
        "ㅅ/수동 삭제 후보.txt"
    assert plan["explicit_restore"][0]["evidence"] == {
        "front_similarity": 0.05,
    }


def test_normal_house_source_cannot_masquerade_as_restore_resume(tmp_path):
    conn, _state_db = _active_pair(tmp_path)
    try:
        extra_plan = _bound_plan(conn, restore_distinct=[{
            "restore": "수동 삭제 후보",
            "reference": "보존할 도서",
            "destination_rel": "ㅅ/수동 삭제 후보.txt",
        }])
        plan = resolve_house_review_batch.build_plan(conn, None, extra_plan)
    finally:
        conn.close()

    assert plan["explicit_restore"] == []
    assert plan["blocked"][0]["blocked_reason"] == "restore_source_not_queue"


def test_explicit_restore_destination_cannot_escape_house(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    with pytest.raises(RuntimeError, match="unsafe"):
        resolve_house_review_batch._explicit_restore_destination(
            house, {"path": "queue.txt", "destination_rel": "../escape.txt"}
        )


def test_recent_link_cleanup_only_removes_the_created_symlink(tmp_path):
    recent = tmp_path / "house" / "_최근"
    destination = tmp_path / "house" / "ㅇ" / "복원.txt"
    other = tmp_path / "house" / "ㅇ" / "다른.txt"
    link, evidence = resolve_house_review_batch._create_owned_recent_link(
        destination, recent
    )
    assert link.is_symlink()
    resolve_house_review_batch._remove_owned_recent_link(link, evidence)
    assert not link.is_symlink()

    link, evidence = resolve_house_review_batch._create_owned_recent_link(
        destination, recent
    )
    link.unlink()
    link.symlink_to(other)
    with pytest.raises(RuntimeError, match="refusing cleanup"):
        resolve_house_review_batch._remove_owned_recent_link(link, evidence)
    assert link.is_symlink()
    assert link.resolve(strict=False) == other


def test_execute_restores_false_positive_and_records_distinct_work(
    tmp_path, monkeypatch,
):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    queue = temp / "trash_bin" / "warning"
    (house / "ㅇ").mkdir(parents=True)
    (house / "ㅂ").mkdir(parents=True)
    queue.mkdir(parents=True)
    restore_path = queue / "오탐 후보.txt"
    reference_path = house / "ㅂ" / "참조 작품.txt"
    restore_path.write_text("완전히 다른 후보 본문", encoding="utf-8")
    reference_path.write_text("서로 무관한 참조 본문", encoding="utf-8")
    state_db = tmp_path / "state.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    with decision_store.transaction(conn):
        restore = decision_store.reconcile_file_metadata(
            conn, restore_path, source="queue"
        )
        reference = decision_store.reconcile_file_metadata(
            conn, reference_path, source="house"
        )
    dedup_mutations.refresh_user_approved_snapshot(conn, restore["file_id"])
    dedup_mutations.refresh_user_approved_snapshot(conn, reference["file_id"])
    review_id = decision_store.add_review_item(
        conn,
        candidate_file_id=restore["file_id"],
        reference_file_id=reference["file_id"],
        classification="manual_false_positive_restore",
        evidence_json=json.dumps({"fixture": True}),
    )
    conn.close()

    monkeypatch.setattr(
        resolve_house_review_batch.folderling, "generate_file_list", lambda *a, **k: True
    )
    monkeypatch.setattr(
        resolve_house_review_batch.folderling, "sync_house_index", lambda *a, **k: None
    )
    monkeypatch.setattr(
        resolve_house_review_batch.folderling, "sync_extension_index", lambda *a, **k: None
    )
    monkeypatch.setattr(
        resolve_house_review_batch, "_prune_folderling_backups", lambda *a, **k: None
    )

    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        extra_plan = _bound_plan(conn, restore_distinct=[{
                "restore": "오탐 후보",
                "reference": "참조 작품",
                "destination_rel": "ㅇ/오탐 후보.txt",
                "reason": "confirmed_false_positive",
                "note": "분산 본문 비교 결과 서로 다른 작품",
        }])
        plan = resolve_house_review_batch.build_plan(conn, [], extra_plan)
    finally:
        conn.close()

    result = resolve_house_review_batch.execute(
        plan, state_db=state_db, house=house, temp=temp
    )

    assert (house / "ㅇ" / "오탐 후보.txt").is_file()
    assert not restore_path.exists()
    recent = house / "_최근" / "오탐 후보.txt"
    assert recent.is_symlink()
    assert recent.resolve() == house / "ㅇ" / "오탐 후보.txt"
    assert len(result["restore_decisions"]) == 1
    conn = decision_store.connect_state_db(state_db)
    try:
        restored = conn.execute(
            "SELECT source, active FROM files WHERE file_id = ?",
            (restore["file_id"],),
        ).fetchone()
        assert restored[:] == ("house", 1)
        verdict = conn.execute(
            "SELECT verdict FROM decisions WHERE active = 1"
        ).fetchone()[0]
        assert verdict == "distinct_work"
        decided_review = conn.execute(
            "SELECT state FROM review_items WHERE review_id = ?", (review_id,)
        ).fetchone()[0]
        assert decided_review == "decided"
        assert conn.execute(
            "SELECT COUNT(*) FROM representatives"
        ).fetchone()[0] == 2
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


@pytest.mark.parametrize("crash_stage", ["after_move", "after_decision", "after_disposition"])
def test_explicit_restore_retry_resumes_only_missing_steps(
    tmp_path, monkeypatch, crash_stage,
):
    fixture = _restore_resume_fixture(tmp_path)
    _patch_restore_indexes(monkeypatch)
    if crash_stage == "after_move":
        original = decision_store.apply_decision
        monkeypatch.setattr(
            decision_store,
            "apply_decision",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("injected after move")
            ),
        )
    elif crash_stage == "after_decision":
        original = decision_store.record_human_restore_disposition
        monkeypatch.setattr(
            decision_store,
            "record_human_restore_disposition",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("injected after decision")
            ),
        )
    else:
        original = resolve_house_review_batch._stamp_explicit_restore_disposition

        def fail_after_disposition(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected after disposition")

        monkeypatch.setattr(
            resolve_house_review_batch,
            "_stamp_explicit_restore_disposition",
            fail_after_disposition,
        )

    with pytest.raises(RuntimeError, match="injected"):
        resolve_house_review_batch.execute(
            fixture["plan"],
            state_db=fixture["state_db"],
            house=fixture["house"],
            temp=fixture["temp"],
        )
    assert fixture["destination"].is_file()
    assert not fixture["restore_path"].exists()
    assert not fixture["recent"].is_symlink()

    if crash_stage == "after_move":
        monkeypatch.setattr(decision_store, "apply_decision", original)
    elif crash_stage == "after_decision":
        monkeypatch.setattr(
            decision_store, "record_human_restore_disposition", original
        )
    else:
        monkeypatch.setattr(
            resolve_house_review_batch,
            "_stamp_explicit_restore_disposition",
            original,
        )

    conn = decision_store.connect_state_db_readonly(fixture["state_db"])
    try:
        retry_plan = resolve_house_review_batch.build_plan(
            conn, [], fixture["extra_plan"]
        )
    finally:
        conn.close()
    assert retry_plan["explicit_restore"][0]["resume_operation_id"] is not None
    result = resolve_house_review_batch.execute(
        retry_plan,
        state_db=fixture["state_db"],
        house=fixture["house"],
        temp=fixture["temp"],
    )

    assert fixture["recent"].is_symlink()
    assert fixture["recent"].resolve() == fixture["destination"]
    assert result["restore_decisions"][0]["resumed"] is True
    conn = decision_store.connect_state_db_readonly(fixture["state_db"])
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM decisions "
            "WHERE active=1 AND verdict='distinct_work'"
        ).fetchone()[0] == 1
        operation = conn.execute(
            "SELECT state FROM operations WHERE action='user_queue_accept'"
        ).fetchall()
        assert len(operation) == 1
        assert operation[0]["state"] == "committed"
        review = conn.execute(
            "SELECT state, evidence_json FROM review_items WHERE review_id = ?",
            (fixture["review_id"],),
        ).fetchone()
        assert review["state"] == "decided"
        evidence = json.loads(review["evidence_json"])
        disposition = evidence["explicit_restore_disposition"]
        assert disposition["operation_id"] == \
            retry_plan["explicit_restore"][0]["resume_operation_id"]
        assert disposition["recent_link"]["target"] == str(fixture["destination"])
        assert [row["state"] for row in conn.execute(
            "SELECT state FROM actual_runs ORDER BY rowid"
        )] == ["failed", "finished", "finished"]
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_restore_resume_rejects_journal_without_matching_plan_provenance(
    tmp_path, monkeypatch,
):
    fixture = _restore_resume_fixture(tmp_path)
    _patch_restore_indexes(monkeypatch)
    original = decision_store.apply_decision
    monkeypatch.setattr(
        decision_store,
        "apply_decision",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("injected after move")
        ),
    )
    with pytest.raises(RuntimeError, match="injected after move"):
        resolve_house_review_batch.execute(
            fixture["plan"],
            state_db=fixture["state_db"],
            house=fixture["house"],
            temp=fixture["temp"],
        )
    monkeypatch.setattr(decision_store, "apply_decision", original)
    conn = decision_store.connect_state_db(fixture["state_db"])
    try:
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE operation_groups SET plan_sha256 = ?",
                ("0" * 64,),
            )
    finally:
        conn.close()
    conn = decision_store.connect_state_db_readonly(fixture["state_db"])
    try:
        retry_plan = resolve_house_review_batch.build_plan(
            conn, [], fixture["extra_plan"]
        )
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="immutable intent"):
        resolve_house_review_batch.execute(
            retry_plan,
            state_db=fixture["state_db"],
            house=fixture["house"],
            temp=fixture["temp"],
        )

    assert fixture["destination"].is_file()
    assert not fixture["recent"].exists()
    conn = decision_store.connect_state_db_readonly(fixture["state_db"])
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE active=1"
        ).fetchone()[0] == 0
    finally:
        conn.close()

def test_explicit_plan_snapshot_rejects_changed_bytes_before_actual_run(tmp_path):
    conn, state_db = _active_pair(tmp_path)
    temp = tmp_path / "temp"
    temp.mkdir()
    try:
        with pytest.raises(RuntimeError, match="expected snapshot mismatch"):
            resolve_house_review_batch.build_plan(conn, None, {
                "schema_version": 2,
                "quarantine": [{
                    "delete": "수동 삭제 후보",
                    "keep": "보존할 도서",
                    "delete_expected_sha256": "0" * 64,
                    "keep_expected_sha256": "0" * 64,
                }],
                "restore_distinct": [],
            })
        extra_plan = _bound_plan(conn, quarantine=[{
            "delete": "수동 삭제 후보",
            "keep": "보존할 도서",
        }])
        plan = resolve_house_review_batch.build_plan(conn, None, extra_plan)
        source = Path(plan["explicit_delete"][0]["path"])
    finally:
        conn.close()

    source.write_text("승인 뒤 바뀐 본문", encoding="utf-8")
    with pytest.raises(RuntimeError, match="approval snapshot changed"):
        resolve_house_review_batch.execute(
            plan,
            state_db=state_db,
            house=tmp_path / "house",
            temp=temp,
        )
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM actual_runs").fetchone()[0] == 0
    finally:
        conn.close()


def test_manual_action_requires_schema_v2_and_expected_hashes(tmp_path):
    conn, _state_db = _active_pair(tmp_path)
    try:
        with pytest.raises(ValueError, match="schema_version"):
            resolve_house_review_batch.build_plan(conn, None, {
                "schema_version": 1,
                "quarantine": [],
                "restore_distinct": [],
            })
        with pytest.raises(RuntimeError, match="approval requires"):
            resolve_house_review_batch.build_plan(conn, None, {
                "schema_version": 2,
                "quarantine": [{
                    "delete": "수동 삭제 후보",
                    "keep": "보존할 도서",
                }],
                "restore_distinct": [],
            })
    finally:
        conn.close()


def test_index_failure_marks_discard_run_failed(tmp_path, monkeypatch):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / "state.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    plan = resolve_house_review_batch.build_plan(conn, None, _empty_plan())
    conn.close()
    monkeypatch.setattr(
        resolve_house_review_batch.folderling,
        "generate_file_list",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("index publish failed")
        ),
    )

    with pytest.raises(RuntimeError, match="index publish failed"):
        resolve_house_review_batch.execute(
            plan, state_db=state_db, house=house, temp=temp
        )

    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        runs = conn.execute(
            "SELECT state, error FROM actual_runs ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()
    assert [row["state"] for row in runs] == ["finished", "failed"]
    assert "index publish failed" in runs[-1]["error"]


def test_restore_plan_rejects_duplicate_action_file(tmp_path):
    conn, _state_db = _active_pair(tmp_path)
    try:
        restore_id = conn.execute(
            "SELECT file_id FROM files WHERE canonical_path LIKE '%수동 삭제 후보.txt'"
        ).fetchone()[0]
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE files SET source = 'queue' WHERE file_id = ?", (restore_id,)
            )
        item = {
            "restore_file_id": restore_id,
            "reference": "보존할 도서",
            "destination_rel": "ㅅ/수동 삭제 후보.txt",
        }
        bound = _bound_plan(conn, restore_distinct=[item])["restore_distinct"][0]
        with pytest.raises(RuntimeError, match="duplicate file"):
            resolve_house_review_batch.build_plan(conn, None, {
                "schema_version": 2,
                "quarantine": [],
                "restore_distinct": [bound, dict(bound)],
            })
    finally:
        conn.close()


def test_managed_same_work_restore_blocks_before_move(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    queue = temp / "trash_bin" / "warning"
    (house / "ㅇ").mkdir(parents=True)
    queue.mkdir(parents=True)
    candidate_path = queue / "오탐 후보.txt"
    reference_path = house / "ㅇ" / "기존 대표.txt"
    candidate_path.write_text("후보 본문", encoding="utf-8")
    reference_path.write_text("대표 본문", encoding="utf-8")
    state_db = tmp_path / "state.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    with decision_store.transaction(conn):
        candidate = decision_store.reconcile_file_metadata(
            conn, candidate_path, source="queue"
        )
        reference = decision_store.reconcile_file_metadata(
            conn, reference_path, source="house"
        )
    dedup_mutations.refresh_user_approved_snapshot(conn, candidate["file_id"])
    dedup_mutations.refresh_user_approved_snapshot(conn, reference["file_id"])
    with decision_store.transaction(conn):
        work_id = conn.execute(
            "INSERT INTO works(display_title) VALUES ('기존 작품')"
        ).lastrowid
        variant_id = conn.execute(
            "INSERT INTO variants(work_bucket_id, variant_kind) VALUES (?, 'base')",
            (work_id,),
        ).lastrowid
        conn.execute(
            "UPDATE files SET variant_id = ?, assignment_state = 'managed', "
            "assignment_origin = 'strong_match' WHERE file_id = ?",
            (variant_id, candidate["file_id"]),
        )
        conn.execute(
            "UPDATE files SET variant_id = ?, assignment_state = 'managed', "
            "assignment_origin = 'human_decision', protected = 1 WHERE file_id = ?",
            (variant_id, reference["file_id"]),
        )
        conn.execute(
            "INSERT INTO representatives(variant_id, file_id) VALUES (?, ?)",
            (variant_id, reference["file_id"]),
        )
    review_id = decision_store.add_review_item(
        conn,
        candidate_file_id=candidate["file_id"],
        reference_file_id=reference["file_id"],
        classification="text_equivalent",
        evidence_json="{}",
    )
    extra_plan = _bound_plan(conn, restore_distinct=[{
            "restore_file_id": candidate["file_id"],
            "reference_file_id": reference["file_id"],
            "destination_rel": "ㅇ/오탐 후보.txt",
    }])
    plan = resolve_house_review_batch.build_plan(conn, None, extra_plan)
    conn.close()

    with pytest.raises(RuntimeError, match="existing managed work"):
        resolve_house_review_batch.execute(
            plan, state_db=state_db, house=house, temp=temp
        )
    assert candidate_path.is_file()
    assert not (house / "ㅇ" / "오탐 후보.txt").exists()
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
        assert conn.execute(
            "SELECT state FROM review_items WHERE review_id = ?", (review_id,)
        ).fetchone()[0] == "pending"
    finally:
        conn.close()


def test_main_writes_recovery_report_when_execution_fails(tmp_path, monkeypatch):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / "state.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    conn.close()
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(_empty_plan()),
        encoding="utf-8",
    )
    report = temp / "dedup_logs" / "manual_house_cleanup_1_4_0_failure.json"
    monkeypatch.setattr(
        resolve_house_review_batch,
        "execute",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("partial failure")),
    )

    with pytest.raises(RuntimeError, match="partial failure"):
        resolve_house_review_batch.main([
            "--state-db", str(state_db),
            "--house", str(house),
            "--temp", str(temp),
            "--extra-plan", str(plan_path),
            "--report-path", str(report),
            "--run",
            "--ack-user-approved",
        ])

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["result"]["status"] == "failed"
    assert payload["result"]["error"] == "partial failure"


def test_intent_writer_failure_stops_before_any_actual_run_or_move(
    tmp_path, monkeypatch,
):
    conn, state_db = _active_pair(tmp_path)
    temp = tmp_path / "temp"
    temp.mkdir()
    try:
        extra_plan = _bound_plan(conn, quarantine=[{
            "delete": "수동 삭제 후보",
            "keep": "보존할 도서",
        }])
        plan = resolve_house_review_batch.build_plan(conn, None, extra_plan)
        source = Path(plan["explicit_delete"][0]["path"])
    finally:
        conn.close()
    monkeypatch.setattr(
        resolve_house_review_batch,
        "write_intent_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("intent write failed")
        ),
    )

    with pytest.raises(RuntimeError, match="intent write failed"):
        resolve_house_review_batch.execute(
            plan,
            state_db=state_db,
            house=tmp_path / "house",
            temp=temp,
        )

    assert source.is_file()
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM actual_runs").fetchone()[0] == 0
    finally:
        conn.close()


def test_terminal_report_failure_preserves_pre_mutation_intent(
    tmp_path, monkeypatch,
):
    fixture = _restore_resume_fixture(tmp_path)
    _patch_restore_indexes(monkeypatch)
    intent = resolve_house_review_batch._intent_report_path(fixture["temp"])
    terminal = resolve_house_review_batch._report_path(fixture["temp"])
    monkeypatch.setattr(
        resolve_house_review_batch,
        "write_execution_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("terminal write failed")
        ),
    )

    with pytest.raises(RuntimeError, match="terminal write failed"):
        resolve_house_review_batch.execute(
            fixture["plan"],
            state_db=fixture["state_db"],
            house=fixture["house"],
            temp=fixture["temp"],
            intent_report_path=intent,
            report_path=terminal,
        )

    payload = json.loads(intent.read_text(encoding="utf-8"))
    assert payload["kind"] == "manual_house_cleanup_intent_1_4_0"
    assert payload["phase"] == "intent"
    assert fixture["destination"].is_file()
    conn = decision_store.connect_state_db_readonly(fixture["state_db"])
    try:
        operation = conn.execute(
            "SELECT operation_group_id FROM operations "
            "WHERE action='user_queue_accept'"
        ).fetchone()
        assert operation["operation_group_id"] is not None
        group = conn.execute(
            "SELECT plan_sha256, manifest_path, source_manifest_json "
            "FROM operation_groups WHERE group_id = ?",
            (operation["operation_group_id"],),
        ).fetchone()
        provenance = json.loads(group["source_manifest_json"])
        assert group["manifest_path"] == str(intent)
        assert provenance["intent_sha256"] == \
            resolve_house_review_batch.inspect_regular_file(intent).sha256
        assert provenance["plan_sha256"] == payload["plan_sha256"]
    finally:
        conn.close()
