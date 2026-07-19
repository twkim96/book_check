import decision_store
from dedup_mutations import _ensure_intake_fingerprint, _file_state
from mutation_io import inspect_regular_file
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
        assert tuple(retired) == (str(editable_path.resolve()), "house", 0)
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
