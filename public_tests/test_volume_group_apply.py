from pathlib import Path

import pytest

import decision_store
from dedup_mutations import _ensure_intake_fingerprint, _file_state
from mutation_io import inspect_regular_file
from volume_review import apply_volume_plan, list_volume_cases, preview_volume_group


def _add(conn, path, content=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content or path.name, encoding="utf-8")
    with decision_store.transaction(conn):
        return decision_store.reconcile_file_metadata(conn, path, source="house")


def _fixture(tmp_path, names):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        rows = [_add(conn, house / relative) for relative in names]
    finally:
        conn.close()
    return state_db, house, temp, rows


def _case(state_db, house, classification="auto_ready"):
    listing = list_volume_cases(
        state_db, house_dir=house, classification=classification, limit=50
    )
    [case] = listing["items"]
    return case


def test_volume_group_apply_stages_moves_and_links_one_work(tmp_path):
    state_db, house, temp, _ = _fixture(
        tmp_path,
        ["ㅂ/별빛 연대기 1권.txt", "ㅂ/별빛 연대기 2권.txt"],
    )
    case = _case(state_db, house)
    plan = preview_volume_group(
        state_db,
        house_dir=house,
        case_id=case["case_id"],
        source_revision=case["source_revision"],
        target_folder_name="별빛 연대기",
    )
    assert plan["apply_available"] is True

    result = apply_volume_plan(
        state_db,
        house_dir=house,
        temp_dir=temp,
        case_id=case["case_id"],
        source_revision=case["source_revision"],
        selected_file_ids=plan["selected_file_ids"],
        target_folder_name="별빛 연대기",
        confirm_count=plan["item_count"],
        confirm_plan_sha256=plan["plan_sha256"],
    )

    destination = house / "ㅂ" / "별빛 연대기"
    assert sorted(path.name for path in destination.iterdir()) == [
        "별빛 연대기 1권.txt",
        "별빛 연대기 2권.txt",
    ]
    assert not (temp / ".volume_group_staging").exists()
    assert Path(result["backup_path"]).is_file()
    conn = decision_store.connect_state_db(state_db)
    try:
        files = conn.execute(
            "SELECT canonical_path, assignment_state, protected, variant_id "
            "FROM files WHERE active = 1 ORDER BY canonical_path"
        ).fetchall()
        assert {Path(row["canonical_path"]).parent for row in files} == {destination}
        assert all(row["assignment_state"] == "managed" for row in files)
        assert all(row["protected"] == 1 for row in files)
        assert len({row["variant_id"] for row in files}) == 2
        work_ids = conn.execute(
            "SELECT DISTINCT work_bucket_id FROM variants WHERE variant_id IN (?, ?)",
            (files[0]["variant_id"], files[1]["variant_id"]),
        ).fetchall()
        assert len(work_ids) == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM operations WHERE action = 'volume_group_merge' "
            "AND state = 'committed'"
        ).fetchone()[0] == 2
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_volume_group_apply_merges_two_existing_volume_folders(tmp_path):
    state_db, house, temp, _ = _fixture(
        tmp_path,
        [
            "ㅅ/서사시 1-10권/서사시 1권.txt",
            "ㅅ/서사시 11-20권/서사시 2권.txt",
        ],
    )
    case = _case(state_db, house)
    plan = preview_volume_group(
        state_db,
        house_dir=house,
        case_id=case["case_id"],
        source_revision=case["source_revision"],
        target_folder_name="서사시 전권",
    )
    result = apply_volume_plan(
        state_db,
        house_dir=house,
        temp_dir=temp,
        case_id=case["case_id"],
        source_revision=case["source_revision"],
        selected_file_ids=plan["selected_file_ids"],
        target_folder_name="서사시 전권",
        confirm_count=plan["item_count"],
        confirm_plan_sha256=plan["plan_sha256"],
    )

    destination = house / "ㅅ" / "서사시 전권"
    assert sorted(path.name for path in destination.iterdir()) == [
        "서사시 1권.txt",
        "서사시 2권.txt",
    ]
    assert not (house / "ㅅ" / "서사시 1-10권").exists()
    assert not (house / "ㅅ" / "서사시 11-20권").exists()
    assert set(result["removed_empty_folders"]) == {
        str(house / "ㅅ" / "서사시 1-10권"),
        str(house / "ㅅ" / "서사시 11-20권"),
    }


def test_preview_can_resolve_duplicate_coordinate_by_selection(tmp_path):
    state_db, house, _, _ = _fixture(
        tmp_path,
        [
            "ㅅ/선택 작품 1권.txt",
            "ㅅ/선택 작품 1권.epub",
            "ㅅ/선택 작품 2권.txt",
        ],
    )
    case = _case(state_db, house, classification="review_required")
    selected = [
        item["file_id"]
        for item in case["items"]
        if item["name"] in {"선택 작품 1권.txt", "선택 작품 2권.txt"}
    ]
    plan = preview_volume_group(
        state_db,
        house_dir=house,
        case_id=case["case_id"],
        source_revision=case["source_revision"],
        selected_file_ids=selected,
    )
    assert plan["blocked_reasons"] == []
    assert plan["apply_available"] is True

    all_files_plan = preview_volume_group(
        state_db,
        house_dir=house,
        case_id=case["case_id"],
        source_revision=case["source_revision"],
        allow_duplicate_coordinates=True,
    )
    assert all_files_plan["allow_duplicate_coordinates"] is True
    assert all_files_plan["item_count"] == 3
    assert all_files_plan["blocked_reasons"] == []
    assert all_files_plan["apply_available"] is True
    assert all_files_plan["plan_sha256"] != plan["plan_sha256"]


def test_volume_group_apply_keeps_human_approved_coordinate_variants(tmp_path):
    state_db, house, temp, _ = _fixture(
        tmp_path,
        [
            "ㄷ/대장간 작품 1권.epub",
            "ㄷ/대장간 작품 1권_dup_1.epub",
            "ㄷ/대장간 작품 2권.epub",
        ],
    )
    case = _case(state_db, house, classification="review_required")
    plan = preview_volume_group(
        state_db,
        house_dir=house,
        case_id=case["case_id"],
        source_revision=case["source_revision"],
        target_folder_name="대장간 작품",
        allow_duplicate_coordinates=True,
    )

    result = apply_volume_plan(
        state_db,
        house_dir=house,
        temp_dir=temp,
        case_id=case["case_id"],
        source_revision=case["source_revision"],
        selected_file_ids=plan["selected_file_ids"],
        target_folder_name=plan["target_folder_name"],
        allow_duplicate_coordinates=True,
        confirm_count=plan["item_count"],
        confirm_plan_sha256=plan["plan_sha256"],
    )

    destination = house / "ㄷ" / "대장간 작품"
    assert len(list(destination.glob("*.epub"))) == 3
    assert result["moved"]
    conn = decision_store.connect_state_db(state_db)
    try:
        work_ids = conn.execute(
            """
            SELECT DISTINCT v.work_bucket_id
            FROM files AS f JOIN variants AS v ON v.variant_id = f.variant_id
            WHERE f.active = 1 AND f.source = 'house'
            """
        ).fetchall()
        assert len(work_ids) == 1
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_preview_blocks_folder_with_unselected_companion(tmp_path):
    state_db, house, _, _ = _fixture(
        tmp_path,
        [
            "ㅍ/폴더 작품 1-2권/폴더 작품 1권.txt",
            "ㅍ/폴더 작품 1-2권/폴더 작품 2권.txt",
        ],
    )
    (house / "ㅍ" / "폴더 작품 1-2권" / "cover.jpg").write_bytes(b"image")
    case = _case(state_db, house, classification="already_grouped")
    plan = preview_volume_group(
        state_db,
        house_dir=house,
        case_id=case["case_id"],
        source_revision=case["source_revision"],
        target_folder_name="폴더 작품",
    )
    assert "source_folder_contains_unselected_files" in plan["blocked_reasons"]
    assert plan["apply_available"] is False


def test_interrupted_volume_move_recovers_original_house_file(tmp_path):
    state_db, house, temp, rows = _fixture(
        tmp_path, ["ㄹ/복구 작품 1권.txt", "ㄹ/복구 작품 2권.txt"]
    )
    source_path = Path(rows[0]["canonical_path"])
    destination = house / "ㄹ" / "복구 작품" / source_path.name
    conn = decision_store.connect_state_db(state_db)
    try:
        source = _ensure_intake_fingerprint(conn, _file_state(conn, rows[0]["file_id"]))
        backup = decision_store.backup_state_db(
            conn, state_db.parent / "backups" / "before-volume-recovery.sqlite3"
        )
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()
    run_id, _ = decision_store.prepare_actual_run(state_db, house, temp)
    conn = decision_store.connect_state_db(state_db)
    try:
        evidence = inspect_regular_file(source_path)
        with decision_store.transaction(conn):
            operation_id = decision_store.create_operation(
                conn,
                run_id=run_id,
                action="volume_group_merge",
                source_path=str(source_path),
                dest_path=str(destination),
                file_id=source["file_id"],
                expected_size=source["size"],
                expected_mtime_ns=source["mtime_ns"],
                expected_fingerprint_id=source["current_fingerprint_id"],
                source_dev=evidence.dev,
                source_ino=evidence.ino,
                source_ctime_ns=evidence.ctime_ns,
                source_sha256=evidence.sha256,
            )
        decision_store.copy_record_consume_operation(
            conn, operation_id, source_path, destination, evidence
        )
        decision_store.finish_actual_run(
            conn, run_id, success=False, error="synthetic interruption"
        )
        assert decision_store.recover_interrupted_operation(conn, operation_id) == "rolled_back"
        assert source_path.is_file()
        assert not destination.exists()
    finally:
        conn.close()


def test_db_done_volume_move_recovery_commits_completed_destination(tmp_path):
    state_db, house, temp, rows = _fixture(
        tmp_path, ["ㅇ/완료 복구 1권.txt", "ㅇ/완료 복구 2권.txt"]
    )
    source_path = Path(rows[0]["canonical_path"])
    destination = house / "ㅇ" / "완료 복구" / source_path.name
    conn = decision_store.connect_state_db(state_db)
    try:
        source = _ensure_intake_fingerprint(conn, _file_state(conn, rows[0]["file_id"]))
        backup = decision_store.backup_state_db(
            conn, state_db.parent / "backups" / "before-volume-db-done.sqlite3"
        )
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()
    run_id, _ = decision_store.prepare_actual_run(state_db, house, temp)
    conn = decision_store.connect_state_db(state_db)
    try:
        evidence = inspect_regular_file(source_path)
        with decision_store.transaction(conn):
            operation_id = decision_store.create_operation(
                conn,
                run_id=run_id,
                action="volume_group_merge",
                source_path=str(source_path),
                dest_path=str(destination),
                file_id=source["file_id"],
                expected_size=source["size"],
                expected_mtime_ns=source["mtime_ns"],
                expected_fingerprint_id=source["current_fingerprint_id"],
                source_dev=evidence.dev,
                source_ino=evidence.ino,
                source_ctime_ns=evidence.ctime_ns,
                source_sha256=evidence.sha256,
            )
        destination_evidence = decision_store.copy_record_consume_operation(
            conn, operation_id, source_path, destination, evidence
        )
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE files SET canonical_path = ?, source = 'house', dev = ?, "
                "ino = ?, ctime_ns = ?, size = ?, mtime_ns = ? WHERE file_id = ?",
                (
                    str(destination),
                    destination_evidence.dev,
                    destination_evidence.ino,
                    destination_evidence.ctime_ns,
                    destination_evidence.size,
                    destination_evidence.mtime_ns,
                    source["file_id"],
                ),
            )
            decision_store.transition_operation(conn, operation_id, "db_done")
        decision_store.finish_actual_run(
            conn, run_id, success=False, error="synthetic post-db interruption"
        )
        assert decision_store.recover_interrupted_operation(
            conn, operation_id
        ) == "committed"
        assert destination.is_file()
        assert not source_path.exists()
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_partial_volume_batch_failure_leaves_journaled_move_recoverable(
    tmp_path, monkeypatch
):
    state_db, house, temp, _ = _fixture(
        tmp_path, ["ㅈ/중단 작품 1권.txt", "ㅈ/중단 작품 2권.txt"]
    )
    case = _case(state_db, house)
    plan = preview_volume_group(
        state_db,
        house_dir=house,
        case_id=case["case_id"],
        source_revision=case["source_revision"],
    )
    original_copy = decision_store.copy_record_consume_operation
    calls = 0

    def fail_second_copy(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic second-volume failure")
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(
        decision_store, "copy_record_consume_operation", fail_second_copy
    )
    with pytest.raises(RuntimeError, match="second-volume failure"):
        apply_volume_plan(
            state_db,
            house_dir=house,
            temp_dir=temp,
            case_id=case["case_id"],
            source_revision=case["source_revision"],
            selected_file_ids=plan["selected_file_ids"],
            target_folder_name=plan["target_folder_name"],
            confirm_count=plan["item_count"],
            confirm_plan_sha256=plan["plan_sha256"],
        )

    conn = decision_store.connect_state_db(state_db)
    try:
        operations = conn.execute(
            "SELECT operation_id, state FROM operations "
            "WHERE action = 'volume_group_merge' ORDER BY operation_id"
        ).fetchall()
        assert [row["state"] for row in operations] == ["fs_done", "planned"]
        assert [
            decision_store.recover_interrupted_operation(
                conn, row["operation_id"]
            )
            for row in operations
        ] == ["rolled_back", "rolled_back"]
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
    assert (house / "ㅈ" / "중단 작품 1권.txt").is_file()
    assert (house / "ㅈ" / "중단 작품 2권.txt").is_file()
    assert not (temp / ".volume_group_staging").exists()


def test_volume_apply_rejects_stale_confirmation(tmp_path):
    state_db, house, temp, _ = _fixture(
        tmp_path, ["ㄱ/거절 작품 1권.txt", "ㄱ/거절 작품 2권.txt"]
    )
    case = _case(state_db, house)
    plan = preview_volume_group(
        state_db,
        house_dir=house,
        case_id=case["case_id"],
        source_revision=case["source_revision"],
    )
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        apply_volume_plan(
            state_db,
            house_dir=house,
            temp_dir=temp,
            case_id=case["case_id"],
            source_revision=case["source_revision"],
            selected_file_ids=plan["selected_file_ids"],
            target_folder_name=plan["target_folder_name"],
            confirm_count=plan["item_count"],
            confirm_plan_sha256="stale",
        )
