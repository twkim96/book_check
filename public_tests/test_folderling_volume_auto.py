from io import StringIO
from pathlib import Path

import decision_store
from folderling import move_to_house
from volume_group_mutations import suggest_folderling_volume_target


def _add(conn, path, source):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(path.name, encoding="utf-8")
    with decision_store.transaction(conn):
        return decision_store.reconcile_file_metadata(conn, path, source=source)


def _fixture(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        existing = [
            _add(conn, house / "ㅂ" / "별빛 연대기" / f"별빛 연대기 {number}권.txt", "house")
            for number in (1, 2)
        ]
        incoming = _add(conn, temp / "별빛 연대기 3권.txt", "temp")
    finally:
        conn.close()
    return state_db, house, temp, existing, incoming


def _approve(state_db, house, temp):
    conn = decision_store.connect_state_db(state_db)
    try:
        backup = decision_store.backup_state_db(
            conn, state_db.parent / "backups" / "before-folderling-volume.sqlite3"
        )
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()
    return decision_store.prepare_actual_run(state_db, house, temp)[0]


def test_folderling_auto_adds_non_overlapping_volume_to_existing_group(tmp_path):
    state_db, house, temp, existing, incoming = _fixture(tmp_path)
    run_id = _approve(state_db, house, temp)
    log = StringIO()
    destination = move_to_house(
        str(temp / "별빛 연대기 3권.txt"),
        str(house),
        str(house / "_최근"),
        "별빛 연대기 3권.txt",
        log,
        "",
        state_db_path=str(state_db),
        run_id=run_id,
    )
    conn = decision_store.connect_state_db(state_db)
    try:
        decision_store.finish_actual_run(conn, run_id, success=True)
        rows = conn.execute(
            """
            SELECT f.file_id, f.canonical_path, f.assignment_state,
                   f.assignment_origin, f.variant_id, v.work_bucket_id
            FROM files AS f LEFT JOIN variants AS v ON v.variant_id = f.variant_id
            WHERE f.active = 1 AND f.source = 'house'
            ORDER BY f.canonical_path
            """
        ).fetchall()
        assert len(rows) == 3
        assert len({row["work_bucket_id"] for row in rows}) == 1
        assert len({row["variant_id"] for row in rows}) == 3
        assert all(row["assignment_state"] == "managed" for row in rows)
        assert all(row["assignment_origin"] == "strong_match" for row in rows)
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
    assert Path(destination).parent == house / "ㅂ" / "별빛 연대기"
    assert "volume-auto" in log.getvalue()
    assert all(row["file_id"] for row in existing + [incoming])


def test_folderling_volume_target_rejects_duplicate_coordinate(tmp_path):
    state_db, house, temp, _, _ = _fixture(tmp_path)
    conn = decision_store.connect_state_db(state_db)
    try:
        duplicate = _add(conn, temp / "별빛 연대기 2권.epub", "temp")
        assert suggest_folderling_volume_target(
            conn,
            source_file_id=duplicate["file_id"],
            house_root=house,
        ) is None
    finally:
        conn.close()


def test_folderling_volume_target_requires_existing_work_folder(tmp_path):
    state_db, house, temp, _, incoming = _fixture(tmp_path)
    conn = decision_store.connect_state_db(state_db)
    try:
        rows = conn.execute(
            "SELECT file_id, canonical_path FROM files WHERE source = 'house'"
        ).fetchall()
        with decision_store.transaction(conn):
            for row in rows:
                old = Path(row["canonical_path"])
                new = house / "ㅂ" / old.name
                old.replace(new)
                stat = new.stat()
                conn.execute(
                    "UPDATE files SET canonical_path = ?, dev = ?, ino = ?, ctime_ns = ?, "
                    "size = ?, mtime_ns = ? WHERE file_id = ?",
                    (
                        str(new), stat.st_dev, stat.st_ino, stat.st_ctime_ns,
                        stat.st_size, stat.st_mtime_ns, row["file_id"],
                    ),
                )
        assert suggest_folderling_volume_target(
            conn,
            source_file_id=incoming["file_id"],
            house_root=house,
        ) is None
    finally:
        conn.close()
