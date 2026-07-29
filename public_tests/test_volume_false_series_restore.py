from pathlib import Path

import decision_store
from volume_false_series_restore import apply_restore_plan, build_restore_plan
from volume_group_mutations import (
    cleanup_staging,
    merge_staged_volume_group,
    stage_volume_sources,
)


def _add(conn, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(path.name, encoding="utf-8")
    with decision_store.transaction(conn):
        return decision_store.reconcile_file_metadata(conn, path, source="house")


def _approve(state_db, house, temp, label):
    conn = decision_store.connect_state_db(state_db)
    try:
        backup = decision_store.backup_state_db(
            conn, state_db.parent / "backups" / f"before-{label}.sqlite3"
        )
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()
    return decision_store.prepare_actual_run(state_db, house, temp)


def test_false_parallel_series_restore_uses_source_run_backup(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        rows = [
            _add(conn, house / "ㄷ" / "동일 작품 1-20 완결.txt"),
            _add(conn, house / "ㄷ" / "동일 작품 1-20 완결.epub"),
        ]
    finally:
        conn.close()

    source_run_id, _ = _approve(state_db, house, temp, "false-series")
    conn = decision_store.connect_state_db(state_db)
    staged = []
    try:
        staged = stage_volume_sources(
            conn,
            file_ids=[row["file_id"] for row in rows],
            staging_root=temp / ".stage" / source_run_id,
            run_id=source_run_id,
        )
        merge_staged_volume_group(
            conn,
            staged=staged,
            destination_root=house / "ㄷ" / "동일 작품",
            display_title="동일 작품",
            run_id=source_run_id,
            relationship_origin="strong_match",
        )
        cleanup_staging(staged, temp / ".stage" / source_run_id)
        decision_store.finish_actual_run(conn, source_run_id, success=True)
    finally:
        conn.close()

    plan = build_restore_plan(
        state_db, house_dir=house, source_run_id=source_run_id
    )
    assert plan["apply_available"] is True
    assert plan["false_series_group_count"] == 1
    assert plan["true_series_group_count"] == 0
    assert plan["pending_move_count"] == 2
    assert plan["relationship_file_count"] == 2

    result = apply_restore_plan(
        state_db,
        house_dir=house,
        temp_dir=temp,
        source_run_id=source_run_id,
        confirm_plan_sha256=plan["plan_sha256"],
    )

    assert result["moved_count"] == 2
    assert result["relationship_rows_restored"] == 2
    assert result["orphan_variants_removed"] == 2
    assert result["orphan_works_removed"] == 1
    assert result["doctor_issue_count"] == 0
    assert not (house / "ㄷ" / "동일 작품").exists()
    assert (house / "ㄷ" / "동일 작품 1-20 완결.txt").is_file()
    assert (house / "ㄷ" / "동일 작품 1-20 완결.epub").is_file()

    conn = decision_store.connect_state_db(state_db)
    try:
        restored = conn.execute(
            "SELECT variant_id, assignment_state, assignment_origin, protected "
            "FROM files WHERE file_id IN (?, ?) ORDER BY file_id",
            (rows[0]["file_id"], rows[1]["file_id"]),
        ).fetchall()
        assert all(row[:] == (None, "unassigned", None, 0) for row in restored)
        group = conn.execute(
            "SELECT action, state, item_count FROM operation_groups "
            "WHERE group_id = ?",
            (result["operation_group_id"],),
        ).fetchone()
        assert group[:] == ("volume_false_series_restore", "committed", 2)
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
