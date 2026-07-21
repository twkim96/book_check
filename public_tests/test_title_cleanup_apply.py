from pathlib import Path

import decision_store
import pytest
from dedup_mutations import _ensure_intake_fingerprint, _file_state
from deduplicator import scan_temp_files
from mutation_io import inspect_regular_file
from title_cleanup_apply import apply_requeue_plan, build_requeue_plan


def _legacy_candidate(conn, house, name, *, old_query, old_core):
    path = house / name
    path.write_text("제목 교정 재입고 본문", encoding="utf-8")
    with decision_store.transaction(conn):
        row = decision_store.reconcile_file_metadata(conn, path, source="house")
        conn.execute(
            """
            UPDATE file_analysis
            SET normalizer_version = '1.2.3', readable_title = ?,
                catalog_query_title = ?, core_title = ?
            WHERE file_id = ?
            """,
            (old_query, old_query, old_core, row["file_id"]),
        )
        conn.execute(
            """
            INSERT INTO catalog_titles(
                title_key, display_title, query_title, normalizer_version
            ) VALUES (?, ?, ?, '1.2.3')
            """,
            (old_core, old_query, old_query),
        )
        for platform in ("series", "kakao", "novelpia"):
            conn.execute(
                "INSERT INTO catalog_platform_stats(title_key, platform, status) "
                "VALUES (?, ?, 'not_found')",
                (old_core, platform),
            )
    return row["file_id"], path


def test_requeue_collision_keeps_clean_names_and_folderling_sees_both(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    conn = decision_store.initialize_state_db(state_db)
    try:
        _legacy_candidate(
            conn,
            house,
            "권본 1 (작가).epub",
            old_query="권본 1",
            old_core="권본1",
        )
        _legacy_candidate(
            conn,
            house,
            "권본 1 (작가)_dup_1.epub",
            old_query="권본 1 _dup_1",
            old_core="권본1dup1",
        )
    finally:
        conn.close()

    plan = build_requeue_plan(
        state_db, house_dir=house, temp_dir=temp, index_path=None
    )
    assert plan["read_only"] is True
    assert plan["item_count"] == 2
    assert plan["blocked_count"] == 0
    assert plan["runnable"] is True
    assert len({item["destination_path"] for item in plan["items"]}) == 2
    assert len({Path(item["destination_path"]).name for item in plan["items"]}) == 1
    [transport] = [item for item in plan["items"] if item["transport_subdir"]]
    assert transport["transport_name"] == "권본 1권 (작가).epub"
    assert transport["transport_subdir"] == "title_cleanup_collision_1"
    assert "_dup_" not in transport["transport_name"]
    assert all(path.is_file() for path in house.iterdir())
    assert not list(temp.iterdir())

    result = apply_requeue_plan(
        state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=None,
        confirm_count=plan["item_count"],
        confirm_plan_sha256=plan["plan_sha256"],
    )
    assert result["completed"] == 2
    loaded = scan_temp_files(temp)
    assert len(loaded) == 2
    assert {entry["name"] for entry in loaded} == {"권본 1권 (작가).epub"}
    assert {Path(entry["rel_path"]).parent.as_posix() for entry in loaded} == {
        ".",
        "title_cleanup_collision_1",
    }


def test_apply_requeue_requires_exact_count_and_uses_new_intake_identity(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    conn = decision_store.initialize_state_db(state_db)
    try:
        old_file_id, source = _legacy_candidate(
            conn,
            house,
            "작품명ⓒ작가 1-100 完.txt",
            old_query="작품명ⓒ작가",
            old_core="작품명작가",
        )
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="confirmation count mismatch"):
        plan = build_requeue_plan(
            state_db, house_dir=house, temp_dir=temp, index_path=None
        )
        apply_requeue_plan(
            state_db,
            house_dir=house,
            temp_dir=temp,
            index_path=None,
            confirm_count=2,
            confirm_plan_sha256=plan["plan_sha256"],
        )
    assert source.is_file()
    assert not list(temp.iterdir())

    plan = build_requeue_plan(
        state_db, house_dir=house, temp_dir=temp, index_path=None
    )
    result = apply_requeue_plan(
        state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=None,
        confirm_count=1,
        confirm_plan_sha256=plan["plan_sha256"],
    )
    destination = temp / "작품명 [ⓒ작가] 1-100 完.txt"
    assert result["planned"] == result["completed"] == 1
    assert result["next_action"] == "run Folderling one-button"
    assert not source.exists()
    assert destination.is_file()

    conn = decision_store.connect_state_db(state_db)
    try:
        retired = conn.execute(
            "SELECT canonical_path, source, active, current_fingerprint_id "
            "FROM files WHERE file_id = ?", (old_file_id,)
        ).fetchone()
        assert retired["canonical_path"] == decision_store.retired_canonical_path(
            conn, old_file_id, source
        )
        assert retired["source"] == "house"
        assert retired["active"] == 0
        assert retired["current_fingerprint_id"] is not None
        operation = conn.execute(
            "SELECT action, state, source_path, dest_path FROM operations"
        ).fetchone()
        assert tuple(operation) == (
            "title_cleanup_requeue", "committed",
            str(source.resolve()), str(destination.resolve()),
        )
        assert not decision_store.doctor_issues(conn)

        with decision_store.transaction(conn):
            new_row = decision_store.reconcile_file_metadata(
                conn, destination, source="temp"
            )
        assert new_row["file_id"] != old_file_id
    finally:
        conn.close()


def test_interrupted_title_requeue_recovers_temp_copy_to_house(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    conn = decision_store.initialize_state_db(state_db)
    source = house / "복구 작품ⓒ작가 1-100 完.txt"
    source.write_text("복구할 본문", encoding="utf-8")
    try:
        with decision_store.transaction(conn):
            file_row = decision_store.reconcile_file_metadata(conn, source, source="house")
        backup = decision_store.backup_state_db(
            conn, state_db.parent / "backups" / "before-recovery.sqlite3"
        )
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()

    run_id, _ = decision_store.prepare_actual_run(state_db, house, temp)
    destination = temp / "복구 작품 [ⓒ작가] 1-100 完.txt"
    conn = decision_store.connect_state_db(state_db)
    try:
        current = _ensure_intake_fingerprint(
            conn, _file_state(conn, file_row["file_id"])
        )
        evidence = inspect_regular_file(source)
        with decision_store.transaction(conn):
            operation_id = decision_store.create_operation(
                conn,
                run_id=run_id,
                action="title_cleanup_requeue",
                source_path=str(source.resolve()),
                dest_path=str(destination.resolve()),
                file_id=file_row["file_id"],
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
        assert not source.exists()
        assert destination.is_file()
        assert conn.execute(
            "SELECT state FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()[0] == "fs_done"

        decision_store.finish_actual_run(
            conn, run_id, success=False, error="synthetic interruption"
        )
        assert decision_store.recover_interrupted_operation(conn, operation_id) == "rolled_back"
        assert source.is_file()
        assert not destination.exists()
        restored = conn.execute(
            "SELECT canonical_path, source, active FROM files WHERE file_id = ?",
            (file_row["file_id"],),
        ).fetchone()
        assert tuple(restored) == (str(source.resolve()), "house", 1)
    finally:
        conn.close()
