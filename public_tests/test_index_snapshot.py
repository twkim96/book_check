import json

import decision_store
import deduplicator
import folderling
from dedup_mutations import _ensure_intake_fingerprint, _file_state
from library_review import TitleCorrectionProvider
from mutation_io import inspect_regular_file
from scanner import (
    build_index_entries_from_state_db,
    generate_file_list,
    generate_file_list_from_state_db,
    validate_index_snapshot,
)
from title_review import list_title_cases


def _fixture(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    category = house / "ㄱ"
    category.mkdir(parents=True)
    temp.mkdir()
    first = category / "검증 작품 1-10.txt"
    first.write_text("본문", encoding="utf-8")
    grouped = category / "검증 작품"
    grouped.mkdir()
    second = grouped / "검증 작품 2권.epub"
    second.write_bytes(b"not-a-real-epub")
    conn = decision_store.initialize_state_db(state_db)
    conn.close()
    file_list = tmp_path / "file_list.json"
    index = tmp_path / "file_index.json"
    assert generate_file_list(
        [str(house)], str(file_list), str(index), state_db_path=str(state_db)
    )
    return state_db, house, temp, file_list, index, first


def test_state_db_projection_matches_full_scanner(tmp_path):
    state_db, house, _, file_list, index, _ = _fixture(tmp_path)
    full = json.loads(index.read_text(encoding="utf-8"))["entries"]

    projected, revision = build_index_entries_from_state_db(house, state_db)
    assert projected == full
    assert len(revision) == 64
    result = generate_file_list_from_state_db(
        house, file_list, index, state_db
    )
    assert result["index_mode"] == "state_db_projection"
    payload = json.loads(index.read_text(encoding="utf-8"))
    assert payload["entries"] == full
    assert payload["inventory_revision"] == revision
    assert validate_index_snapshot(house, index, state_db)["valid"] is True


def test_snapshot_validation_falls_back_for_external_changes(tmp_path):
    state_db, house, _, _, index, first = _fixture(tmp_path)
    (house / "ㄱ" / "외부 추가 1-2.txt").write_text("new", encoding="utf-8")
    added = validate_index_snapshot(house, index, state_db)
    assert added["valid"] is False
    assert "absent from state DB" in added["reason"]

    (house / "ㄱ" / "외부 추가 1-2.txt").unlink()
    first.write_text("변경된 본문", encoding="utf-8")
    changed = validate_index_snapshot(house, index, state_db)
    assert changed["valid"] is False
    assert "stale_snapshot" in changed["reason"] or "identity changed" in changed["reason"]


def test_title_requeue_updates_index_without_full_scan(tmp_path):
    state_db, house, temp, _, index, first = _fixture(tmp_path)
    provider = TitleCorrectionProvider(
        state_db=state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=index,
    )
    cases = list_title_cases(state_db)["items"]
    case = next(item for item in cases if item["current_name"] == first.name)
    preview = provider.preview({
        "file_id": case["file_id"],
        "source_revision": case["source_revision"],
        "new_body": "교정 작품 1-10",
    })
    result = provider.apply_plan(
        [{
            "file_id": case["file_id"],
            "source_revision": case["source_revision"],
            "new_body": "교정 작품 1-10",
        }],
        confirm_count=1,
        confirm_plan_sha256=provider.build_plan([{
            "file_id": case["file_id"],
            "source_revision": case["source_revision"],
            "new_body": "교정 작품 1-10",
        }])["plan_sha256"],
    )
    assert preview["runnable"] is True
    assert result["index_updated"] is True
    assert result["index_mode"] == "state_db_projection"
    indexed = {
        entry["rel_path"]
        for entry in json.loads(index.read_text(encoding="utf-8"))["entries"]
        if entry["type"] == "file"
    }
    assert "ㄱ/검증 작품 1-10.txt" not in indexed
    assert (temp / "교정 작품 1-10.txt").is_file()


def test_folderling_reuses_verified_index_and_projects_final_delta(tmp_path, monkeypatch):
    script_dir = tmp_path / "project"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    state_db = script_dir / ".dedup_state" / "dedup_decisions.sqlite3"
    script_dir.mkdir()
    house.mkdir()
    temp.mkdir()
    (script_dir / "extension").mkdir()
    existing = house / "ㄱ" / "기존 작품 1-10.txt"
    existing.parent.mkdir()
    existing.write_text("기존 본문", encoding="utf-8")
    incoming = temp / "신규 작품 1-20.txt"
    incoming.write_text("신규 본문", encoding="utf-8")

    conn = decision_store.initialize_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            decision_store.reconcile_file_metadata(conn, existing, source="house")
            decision_store.reconcile_file_metadata(conn, incoming, source="temp")
    finally:
        conn.close()
    assert generate_file_list(
        [str(house)],
        str(script_dir / "file_list.json"),
        str(script_dir / "file_index.json"),
        state_db_path=str(state_db),
    )

    conn = decision_store.connect_state_db(state_db)
    try:
        backup = decision_store.backup_state_db(
            conn, state_db.parent / "backups" / "before-folderling.sqlite3"
        )
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()

    def unexpected_full_scan(*args, **kwargs):
        raise AssertionError("verified snapshot must not invoke a full Scanner")

    monkeypatch.setattr(folderling, "generate_file_list", unexpected_full_scan)
    monkeypatch.setattr(deduplicator, "generate_file_list", unexpected_full_scan)
    events = []
    result = folderling._process_items_with_lock_held(
        str(temp), str(house), str(script_dir), state_db_path=str(state_db),
        event_callback=events.append,
    )
    assert result["failure_count"] == 0
    assert result["pre_index_mode"] == "verified_snapshot"
    assert result["index_mode"] == "state_db_projection"
    assert (house / "ㅅ" / incoming.name).is_file()
    assert not incoming.exists()
    assert validate_index_snapshot(
        house, script_dir / "file_index.json", state_db
    )["valid"] is True
    phases = [event["phase"] for event in events]
    assert phases[:3] == [
        "actual_run_started", "review_actions_result", "workflow_started"
    ]
    assert "snapshot_result" in phases
    assert "final_doctor_result" in phases
    assert phases[-1] == "actual_run_finished"
    intake = next(
        event for event in events
        if event["phase"] == "file_result"
        and event.get("stage") == "intake"
        and event.get("source_name") == incoming.name
    )
    assert intake["status"] == "ingested"
    assert intake["source_path"] == str(incoming)
    assert intake["destination_path"] == str(house / "ㅅ" / incoming.name)
    assert result["final_doctor_issue_count"] == 0


def test_folderling_holds_conflicting_volume_but_ingests_later_new_volume(
    tmp_path, monkeypatch
):
    script_dir = tmp_path / "project"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    state_db = script_dir / ".dedup_state" / "dedup_decisions.sqlite3"
    script_dir.mkdir()
    house.mkdir()
    temp.mkdir()
    (script_dir / "extension").mkdir()
    work = house / "ㅂ" / "별빛 연대기"
    work.mkdir(parents=True)

    conn = decision_store.initialize_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            for number in range(1, 6):
                path = work / f"별빛 연대기 {number}권.txt"
                path.write_text(f"기존 {number}권", encoding="utf-8")
                decision_store.reconcile_file_metadata(conn, path, source="house")
            conflict = temp / "별빛 연대기 3권.txt"
            conflict.write_text("다른 3권 판본", encoding="utf-8")
            latest = temp / "별빛 연대기 6권.txt"
            latest.write_text("신규 6권", encoding="utf-8")
            decision_store.reconcile_file_metadata(conn, conflict, source="temp")
            decision_store.reconcile_file_metadata(conn, latest, source="temp")
    finally:
        conn.close()

    assert generate_file_list(
        [str(house)],
        str(script_dir / "file_list.json"),
        str(script_dir / "file_index.json"),
        state_db_path=str(state_db),
    )
    conn = decision_store.connect_state_db(state_db)
    try:
        backup = decision_store.backup_state_db(
            conn, state_db.parent / "backups" / "before-coordinate-hold.sqlite3"
        )
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()

    monkeypatch.setattr(
        folderling,
        "clean_duplicates",
        lambda **kwargs: {
            "exact_count": 0,
            "suspect_move_count": 0,
            "review_queue_move_count": 0,
            "same_author_count": 0,
            "author_conflict_count": 0,
        },
    )
    events = []
    result = folderling._process_items_with_lock_held(
        str(temp), str(house), str(script_dir), state_db_path=str(state_db),
        event_callback=events.append,
    )
    held = (
        temp / "trash_bin" / "warning" / "volume_coordinate_conflicts" /
        "별빛 연대기 3권.txt"
    )
    assert result["failure_count"] == 0
    assert result["volume_conflict_hold_count"] == 1
    assert result["move_count"] == 1
    assert held.is_file()
    assert (work / "별빛 연대기 6권.txt").is_file()
    assert not (house / "ㅂ" / "별빛 연대기 3권.txt").exists()
    assert not (house / "ㅂ" / "별빛 연대기 6권.txt").exists()

    conn = decision_store.connect_state_db(state_db)
    try:
        actions = [
            row[0] for row in conn.execute(
                "SELECT action FROM operations ORDER BY operation_id"
            ).fetchall()
        ]
        assert actions == ["volume_coordinate_hold", "house_ingest"]
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
    warning = next(
        event for event in events
        if event["phase"] == "file_result"
        and event.get("reason") == "volume_coordinate_conflict"
    )
    assert warning["status"] == "warning"
    assert warning["source_path"] == str(conflict)
    assert warning["destination_path"] == str(held)
    assert warning["existing_paths"] == [str(work / "별빛 연대기 3권.txt")]


def test_interrupted_volume_coordinate_hold_restores_temp_source(tmp_path):
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    source = temp / "복구 작품 3권.txt"
    source.write_text("conflict", encoding="utf-8")
    conn = decision_store.initialize_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            row = decision_store.reconcile_file_metadata(conn, source, source="temp")
        current = _ensure_intake_fingerprint(conn, _file_state(conn, row["file_id"]))
        backup = decision_store.backup_state_db(
            conn, state_db.parent / "backups" / "before-hold-recovery.sqlite3"
        )
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()
    run_id, _ = decision_store.prepare_actual_run(state_db, house, temp)
    destination = temp / "trash_bin" / "warning" / "복구 작품 3권.txt"
    destination.parent.mkdir(parents=True)
    conn = decision_store.connect_state_db(state_db)
    try:
        evidence = inspect_regular_file(source)
        with decision_store.transaction(conn):
            operation_id = decision_store.create_operation(
                conn,
                run_id=run_id,
                action="volume_coordinate_hold",
                source_path=str(source),
                dest_path=str(destination),
                file_id=row["file_id"],
                expected_size=current["size"],
                expected_mtime_ns=current["mtime_ns"],
                expected_fingerprint_id=current["current_fingerprint_id"],
                source_dev=evidence.dev,
                source_ino=evidence.ino,
                source_ctime_ns=evidence.ctime_ns,
                source_sha256=evidence.sha256,
            )
        decision_store.copy_record_consume_operation(
            conn, operation_id, source, destination, evidence
        )
        decision_store.finish_actual_run(
            conn, run_id, success=False, error="synthetic interruption"
        )
        assert decision_store.recover_interrupted_operation(
            conn, operation_id
        ) == "rolled_back"
        assert source.is_file()
        assert not destination.exists()
    finally:
        conn.close()
