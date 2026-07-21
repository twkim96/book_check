import json

import decision_store
import deduplicator
import duplicate_auditor
from dedup_mutations import _ensure_intake_fingerprint, _file_state, ingest_to_house
from mutation_io import inspect_regular_file
from scanner import generate_file_list
from title_review import (
    apply_title_plan,
    build_title_plan,
    list_title_cases,
    preview_title_change,
)


def _add_case(conn, house, name, statuses):
    path = house / name
    path.write_text(f"{name} 본문", encoding="utf-8")
    with decision_store.transaction(conn):
        row = decision_store.reconcile_file_metadata(conn, path, source="house")
        analysis = conn.execute(
            "SELECT * FROM file_analysis WHERE file_id = ?", (row["file_id"],)
        ).fetchone()
        conn.execute(
            """
            INSERT INTO catalog_titles(
                title_key, display_title, query_title, normalizer_version
            ) VALUES (?, ?, ?, ?)
            """,
            (
                analysis["core_title"],
                analysis["readable_title"],
                analysis["catalog_query_title"],
                analysis["normalizer_version"],
            ),
        )
        for platform, status in statuses.items():
            conn.execute(
                "INSERT INTO catalog_platform_stats(title_key, platform, status) "
                "VALUES (?, ?, ?)",
                (analysis["core_title"], platform, status),
            )
    return row["file_id"], path


def _fixture(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    conn = decision_store.initialize_state_db(state_db)
    try:
        editable_id, editable_path = _add_case(
            conn,
            house,
            "미친년은 아니야 146.txt",
            {"series": "not_found", "kakao": "not_found", "novelpia": "not_found"},
        )
        _add_case(
            conn,
            house,
            "보호 메타데이터 작품 1-100.txt",
            {"series": "ok", "kakao": "not_found", "novelpia": "not_found"},
        )
    finally:
        conn.close()
    return state_db, house, temp, editable_id, editable_path


def test_list_only_exposes_titles_without_any_ok_metadata(tmp_path):
    state_db, house, temp, editable_id, _ = _fixture(tmp_path)
    result = list_title_cases(state_db, limit=10)
    assert result["total"] == 1
    [item] = result["items"]
    assert item["file_id"] == editable_id
    assert item["current_name"] == "미친년은 아니야 146.txt"
    assert item["platforms"] == {
        "series": "not_found",
        "kakao": "not_found",
        "novelpia": "not_found",
    }
    assert item["editable"] is True


def test_preview_preserves_extension_and_detects_stale_revision(tmp_path):
    state_db, house, temp, editable_id, _ = _fixture(tmp_path)
    [case] = list_title_cases(state_db)["items"]
    preview = preview_title_change(
        state_db,
        house_dir=house,
        temp_dir=temp,
        file_id=editable_id,
        new_body="미친년은 아니야 1-146",
        source_revision=case["source_revision"],
    )
    assert preview["runnable"] is True
    assert preview["candidate_name"] == "미친년은 아니야 1-146.txt"
    assert preview["after_core_title"] == "미친년은아니야"

    stale = preview_title_change(
        state_db,
        house_dir=house,
        temp_dir=temp,
        file_id=editable_id,
        new_body="미친년은 아니야 1-146",
        source_revision="stale",
    )
    assert stale["runnable"] is False
    assert "source_revision_stale" in stale["blocked_reasons"]


def test_preview_title_literal_shows_clean_final_name_and_platform_query(tmp_path):
    state_db, house, temp, editable_id, _ = _fixture(tmp_path)
    [case] = list_title_cases(state_db)["items"]
    preview = preview_title_change(
        state_db,
        house_dir=house,
        temp_dir=temp,
        file_id=editable_id,
        new_body="[[19금]] 떡타지의 주인공 친구가 되었다 [스투피르] 0-631 완",
        source_revision=case["source_revision"],
    )
    assert preview["runnable"] is True
    assert preview["candidate_name"].startswith("[[19금]]")
    assert preview["materialized_candidate_name"] == (
        "19금 떡타지의 주인공 친구가 되었다 [스투피르] 0-631 완.txt"
    )
    assert preview["after_core_title"] == "19금떡타지의주인공친구가되었다"
    assert preview["after_query_title"] == "19금 떡타지의 주인공 친구가 되었다"
    assert preview["after_author"] == "스투피르"
    assert preview["after_effective_max"] == 631
    assert preview["title_literal_tokens"] == ["19금"]

    malformed = preview_title_change(
        state_db,
        house_dir=house,
        temp_dir=temp,
        file_id=editable_id,
        new_body="[[19금] 잘못된 표시",
        source_revision=case["source_revision"],
    )
    assert malformed["runnable"] is False
    assert any(reason.startswith("invalid_new_name:") for reason in malformed["blocked_reasons"])


def test_preview_structure_hint_and_decimal_volume(tmp_path):
    state_db, house, temp, editable_id, _ = _fixture(tmp_path)
    [case] = list_title_cases(state_db)["items"]
    preview = preview_title_change(
        state_db,
        house_dir=house,
        temp_dir=temp,
        file_id=editable_id,
        new_body="가끔씩 툭하고 러시아어로 부끄러워하는 옆자리의 아랴 양 {{04.5권}}",
        source_revision=case["source_revision"],
    )

    assert preview["runnable"] is True
    assert preview["materialized_candidate_name"].endswith("아랴 양 04.5권.txt")
    assert preview["after_core_title"] == "가끔씩툭하고러시아어로부끄러워하는옆자리의아랴양"
    assert preview["after_volume_coordinate"] == "4.5"
    assert preview["after_unit"] == "권"
    assert preview["structure_hint_tokens"] == ["04.5권"]

    malformed = preview_title_change(
        state_db,
        house_dir=house,
        temp_dir=temp,
        file_id=editable_id,
        new_body="작품 {{04.5권}",
        source_revision=case["source_revision"],
    )
    assert malformed["runnable"] is False
    assert any(reason.startswith("invalid_new_name:") for reason in malformed["blocked_reasons"])


def test_structure_hint_requeue_can_return_to_same_clean_house_path(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    original = house / "합성 작품 04.5권.txt"
    conn = decision_store.initialize_state_db(state_db)
    try:
        old_file_id, _ = _add_case(
            conn,
            house,
            original.name,
            {"series": "not_found", "kakao": "not_found", "novelpia": "not_found"},
        )
    finally:
        conn.close()

    [case] = list_title_cases(state_db)["items"]
    changes = [{
        "file_id": old_file_id,
        "source_revision": case["source_revision"],
        "new_body": "합성 작품 {{04.5권}}",
    }]
    plan = build_title_plan(
        state_db, house_dir=house, temp_dir=temp, changes=changes
    )
    result = apply_title_plan(
        state_db,
        house_dir=house,
        temp_dir=temp,
        changes=changes,
        confirm_count=plan["item_count"],
        confirm_plan_sha256=plan["plan_sha256"],
    )
    marked = temp / "합성 작품 {{04.5권}}.txt"
    assert result["completed"] == 1
    assert marked.is_file()
    assert not original.exists()

    conn = decision_store.connect_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            new_row = decision_store.reconcile_file_metadata(
                conn, marked, source="temp"
            )
        new_file_id = new_row["file_id"]
        assert new_file_id != old_file_id
        backup = state_db.parent / "backups" / "before-same-path-ingest.sqlite3"
        decision_store.backup_state_db(conn, backup)
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()

    run_id, _ = decision_store.prepare_actual_run(state_db, house, temp)
    conn = decision_store.connect_state_db(state_db)
    try:
        ingest_to_house(
            conn,
            source_file_id=new_file_id,
            destination=original,
            run_id=run_id,
        )
        decision_store.finish_actual_run(conn, run_id, success=True)
        active = conn.execute(
            "SELECT file_id, canonical_path, source, active FROM files "
            "WHERE canonical_path = ?",
            (str(original.resolve()),),
        ).fetchone()
        assert tuple(active) == (new_file_id, str(original.resolve()), "house", 1)
        retired = conn.execute(
            "SELECT canonical_path, active FROM files WHERE file_id = ?",
            (old_file_id,),
        ).fetchone()
        assert retired["canonical_path"] == decision_store.retired_canonical_path(
            conn, old_file_id, original
        )
        assert retired["active"] == 0
        assert not decision_store.doctor_issues(conn)
    finally:
        conn.close()

    assert original.is_file()
    assert not marked.exists()


def test_title_literal_override_survives_clean_house_name_and_scanner(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    marked = temp / "[[19금]] 떡타지의 주인공 친구가 되었다 [스투피르] 0-631 완.txt"
    marked.write_text("보호 제목 합성 본문 " * 100, encoding="utf-8")

    conn = decision_store.initialize_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            source = decision_store.reconcile_file_metadata(conn, marked, source="temp")
        file_id = source["file_id"]
        saved = conn.execute(
            "SELECT * FROM file_analysis WHERE file_id = ?", (file_id,)
        ).fetchone()
        assert saved["core_title"] == "19금떡타지의주인공친구가되었다"
        assert json.loads(saved["title_override_json"]) == ["19금"]
        _ensure_intake_fingerprint(conn, _file_state(conn, file_id))
        backup = state_db.parent / "backups" / "before-title-literal-ingest.sqlite3"
        decision_store.backup_state_db(conn, backup)
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()

    run_id, _ = decision_store.prepare_actual_run(state_db, house, temp)
    destination_dir = house / "숫자"
    destination_dir.mkdir()
    destination = destination_dir / (
        "19금 떡타지의 주인공 친구가 되었다 [스투피르] 0-631 완.txt"
    )
    conn = decision_store.connect_state_db(state_db)
    try:
        ingest_to_house(
            conn,
            source_file_id=file_id,
            destination=destination,
            run_id=run_id,
        )
        decision_store.finish_actual_run(conn, run_id, success=True)
    finally:
        conn.close()

    assert destination.is_file()
    assert not marked.exists()
    index_path = tmp_path / "file_index.json"
    assert generate_file_list(
        [str(house)],
        str(tmp_path / "file_list.json"),
        str(index_path),
        state_db_path=str(state_db),
    ) is True
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    [entry] = [item for item in payload["entries"] if item["type"] == "file"]
    assert entry["name"] == destination.name
    assert entry["core_title"] == "19금떡타지의주인공친구가되었다"
    assert entry["title_override"] is True
    dedup_entries = deduplicator.load_index_entries(
        str(house), str(index_path), allow_write=False
    )
    assert dedup_entries[0]["core_title"] == "19금떡타지의주인공친구가되었다"
    audit_entries, invalid = duplicate_auditor.load_house_entries(
        str(index_path), str(house)
    )
    assert invalid == []
    assert audit_entries[0].core_title == "19금떡타지의주인공친구가되었다"

    conn = decision_store.connect_state_db(state_db)
    try:
        saved = conn.execute(
            "SELECT * FROM file_analysis WHERE file_id = ?", (file_id,)
        ).fetchone()
        assert saved["analyzed_name"] == destination.name
        assert saved["catalog_query_title"] == "19금 떡타지의 주인공 친구가 되었다"
        assert saved["author"] == "스투피르"
        assert saved["effective_max"] == 631
        assert json.loads(saved["title_override_json"]) == ["19금"]
    finally:
        conn.close()


def test_user_title_plan_moves_only_selected_file_and_uses_new_identity(tmp_path):
    state_db, house, temp, editable_id, editable_path = _fixture(tmp_path)
    [case] = list_title_cases(state_db)["items"]
    changes = [
        {
            "file_id": editable_id,
            "source_revision": case["source_revision"],
            "new_body": "미친년은 아니야 1-146",
        }
    ]
    plan = build_title_plan(
        state_db, house_dir=house, temp_dir=temp, changes=changes
    )
    assert plan["runnable"] is True
    assert plan["item_count"] == 1

    result = apply_title_plan(
        state_db,
        house_dir=house,
        temp_dir=temp,
        changes=changes,
        confirm_count=plan["item_count"],
        confirm_plan_sha256=plan["plan_sha256"],
    )
    destination = temp / "미친년은 아니야 1-146.txt"
    assert result["completed"] == 1
    assert not editable_path.exists()
    assert destination.is_file()

    conn = decision_store.connect_state_db(state_db)
    try:
        retired = conn.execute(
            "SELECT canonical_path, source, active FROM files WHERE file_id = ?",
            (editable_id,),
        ).fetchone()
        assert tuple(retired) == (
            decision_store.retired_canonical_path(conn, editable_id, editable_path),
            "house",
            0,
        )
        operation = conn.execute(
            "SELECT action, state FROM operations WHERE file_id = ?",
            (editable_id,),
        ).fetchone()
        assert tuple(operation) == ("user_title_requeue", "committed")
        assert not decision_store.doctor_issues(conn)
        with decision_store.transaction(conn):
            new_row = decision_store.reconcile_file_metadata(conn, destination, source="temp")
        assert new_row["file_id"] != editable_id
    finally:
        conn.close()


def test_interrupted_user_title_requeue_recovers_original_house_file(tmp_path):
    state_db, house, temp, editable_id, source = _fixture(tmp_path)
    backup = state_db.parent / "backups" / "before-user-title-recovery.sqlite3"
    conn = decision_store.connect_state_db(state_db)
    try:
        current = _ensure_intake_fingerprint(conn, _file_state(conn, editable_id))
        decision_store.backup_state_db(conn, backup)
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()
    run_id, _ = decision_store.prepare_actual_run(state_db, house, temp)
    destination = temp / "복구될 새 이름.txt"
    conn = decision_store.connect_state_db(state_db)
    try:
        evidence = inspect_regular_file(source)
        with decision_store.transaction(conn):
            operation_id = decision_store.create_operation(
                conn,
                run_id=run_id,
                action="user_title_requeue",
                source_path=str(source.resolve()),
                dest_path=str(destination.resolve()),
                file_id=editable_id,
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
        assert decision_store.recover_interrupted_operation(conn, operation_id) == "rolled_back"
        assert source.is_file()
        assert not destination.exists()
    finally:
        conn.close()
