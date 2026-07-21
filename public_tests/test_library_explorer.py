import json
from pathlib import Path

import decision_store
from dedup_mutations import _ensure_intake_fingerprint, _file_state
from library_explorer import (
    compare_files,
    file_detail,
    file_listing,
    folder_detail,
    folder_listing,
    quarantine_listing,
)


def _fixture(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    folder = house / "ㄱ" / "검사 작품"
    trash = temp / "trash_bin" / "user_discard_quarantine"
    folder.mkdir(parents=True)
    trash.mkdir(parents=True)
    first = folder / "검사 작품 1권.txt"
    second = folder / "검사 작품 1권 extra.txt"
    first.write_text("동일한 합성 본문", encoding="utf-8")
    second.write_text("동일한 합성 본문", encoding="utf-8")
    (folder / "cover.jpg").write_bytes(b"cover")
    quarantined = trash / "버린 판본.txt"
    quarantined.write_text("격리 본문", encoding="utf-8")
    untracked = trash / "이력 없는 격리.epub"
    untracked.write_bytes(b"untracked")

    conn = decision_store.initialize_state_db(state_db)
    try:
        ids = []
        for path in (first, second):
            with decision_store.transaction(conn):
                row = decision_store.reconcile_file_metadata(conn, path, source="house")
            ids.append(row["file_id"])
            _ensure_intake_fingerprint(conn, _file_state(conn, row["file_id"]))
        with decision_store.transaction(conn):
            qrow = decision_store.reconcile_file_metadata(
                conn, quarantined, source="quarantine"
            )
        _ensure_intake_fingerprint(conn, _file_state(conn, qrow["file_id"]))
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE files SET active = 0 WHERE file_id = ?", (qrow["file_id"],)
            )
            left_fp = conn.execute(
                "SELECT current_fingerprint_id FROM files WHERE file_id = ?", (ids[0],)
            ).fetchone()[0]
            right_fp = conn.execute(
                "SELECT current_fingerprint_id FROM files WHERE file_id = ?", (ids[1],)
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO review_items(
                    candidate_file_id, reference_file_id,
                    left_fingerprint_id, right_fingerprint_id,
                    classification, evidence_json
                ) VALUES (?, ?, ?, ?, 'metadata_only', ?)
                """,
                (ids[0], ids[1], left_fp, right_fp, json.dumps({"fixture": True})),
            )
            qfp = conn.execute(
                "SELECT current_fingerprint_id FROM files WHERE file_id = ?", (qrow["file_id"],)
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO operations(
                    run_id, action, source_path, quarantine_path, file_id, keep_file_id,
                    expected_size, expected_mtime_ns, expected_fingerprint_id,
                    expected_keep_fingerprint_id, state
                ) VALUES ('fixture-run', 'user_quarantine', ?, ?, ?, ?, ?, ?, ?, ?, 'committed')
                """,
                (
                    str(house / "ㄱ" / "버린 판본.txt"), str(quarantined),
                    qrow["file_id"], ids[0], quarantined.stat().st_size,
                    quarantined.stat().st_mtime_ns, qfp, left_fp,
                ),
            )
        return state_db, house, temp, ids, folder, quarantined, untracked
    finally:
        conn.close()


def _snapshot(state_db, house, temp):
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("files", "fingerprints", "review_items", "decisions", "operations")
        }
        active = [
            tuple(row)
            for row in conn.execute(
                "SELECT file_id, canonical_path, source, active FROM files ORDER BY file_id"
            )
        ]
    finally:
        conn.close()
    tree = sorted(
        (str(path.relative_to(root)), path.stat().st_size)
        for root in (house, temp)
        for path in root.rglob("*")
        if path.is_file()
    )
    return counts, active, tree


def test_file_explorer_detail_and_compare_are_readonly(tmp_path):
    state_db, house, temp, ids, *_ = _fixture(tmp_path)
    before = _snapshot(state_db, house, temp)

    listing = file_listing(state_db, search="검사 작품", source="house")
    assert listing["total"] == 2
    assert {item["file_id"] for item in listing["items"]} == set(ids)
    detail = file_detail(state_db, ids[0])
    assert detail["file"]["core_title"] == "검사작품"
    assert detail["reviews"][0]["evidence"] == {"fixture": True}
    assert detail["actions"]["quarantine"] is True
    comparison = compare_files(state_db, ids[0], ids[1])
    assert comparison["comparison"]["same_raw_sha256"] is True
    assert comparison["latest_review"]["classification"] == "metadata_only"
    assert comparison["relationship_preview"]["apply_available"] is False

    assert _snapshot(state_db, house, temp) == before


def test_folder_explorer_distinguishes_registered_and_auxiliary_files(tmp_path):
    state_db, house, temp, _, folder, *_ = _fixture(tmp_path)
    before = _snapshot(state_db, house, temp)

    listing = folder_listing(state_db, house, search="검사 작품")
    assert listing["total"] == 1
    assert listing["items"][0]["file_count"] == 2
    detail = folder_detail(state_db, house, str(folder))
    assert detail["registered_count"] == 2
    assert detail["unregistered_count"] == 1
    cover = next(item for item in detail["entries"] if item["name"] == "cover.jpg")
    assert cover["registered"] is False
    assert detail["actions"]["move"] is False

    assert _snapshot(state_db, house, temp) == before


def test_quarantine_explorer_shows_tracked_and_untracked_bytes(tmp_path):
    state_db, house, temp, _, _, quarantined, untracked = _fixture(tmp_path)
    before = _snapshot(state_db, house, temp)

    listing = quarantine_listing(state_db, temp)
    by_path = {item["path"]: item for item in listing["items"]}
    tracked = by_path[str(quarantined.resolve())]
    assert tracked["tracked"] is True
    assert tracked["physical_state"] == "present"
    assert tracked["source_path"].endswith("버린 판본.txt")
    assert tracked["source_size"] == quarantined.stat().st_size
    assert tracked["keep_size"] == (house / "ㄱ" / "검사 작품" / "검사 작품 1권.txt").stat().st_size
    assert tracked["related_files"][0]["file_id"]
    assert tracked["related_files"][0]["bases"][0] == "keep"
    assert tracked["restore_available"] is True
    assert tracked["purge_available"] is True
    assert by_path[str(untracked.resolve())]["physical_state"] == "untracked"
    assert listing["summary"]["present"] == 1
    assert listing["summary"]["untracked"] == 1

    assert _snapshot(state_db, house, temp) == before


def test_quarantine_explorer_lists_actionable_quarantine_before_review_queue(tmp_path):
    state_db, _, temp, _, _, quarantined, _ = _fixture(tmp_path)
    warning = temp / "trash_bin" / "warning" / "최신 검토 큐.txt"
    warning.parent.mkdir(parents=True)
    warning.write_text("검토 큐 본문", encoding="utf-8")
    conn = decision_store.connect_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            row = decision_store.reconcile_file_metadata(conn, warning, source="queue")
        fingerprint = _ensure_intake_fingerprint(conn, _file_state(conn, row["file_id"]))
        with decision_store.transaction(conn):
            conn.execute(
                """
                INSERT INTO operations(
                    run_id, action, source_path, dest_path, file_id, state,
                    expected_size, expected_mtime_ns, expected_fingerprint_id,
                    created_at, updated_at
                ) VALUES ('newer-review-run', 'warning_move', ?, ?, ?, 'committed', ?, ?, ?,
                          '2099-01-01 00:00:00', '2099-01-01 00:00:00')
                """,
                (
                    str(temp / "최신 검토 큐.txt"), str(warning), row["file_id"],
                    warning.stat().st_size, warning.stat().st_mtime_ns,
                    fingerprint["current_fingerprint_id"],
                ),
            )
    finally:
        conn.close()

    listing = quarantine_listing(state_db, temp)

    assert listing["items"][0]["path"] == str(quarantined.resolve())
    assert listing["items"][0]["purge_available"] is True
    queued = next(item for item in listing["items"] if item["path"] == str(warning.resolve()))
    assert queued["purge_available"] is False


def test_file_explorer_marks_retired_virtual_paths_as_history_only(tmp_path):
    state_db, house, temp, ids, *_ = _fixture(tmp_path)
    conn = decision_store.connect_state_db(state_db)
    try:
        retired = decision_store.retired_canonical_path(
            conn, ids[0], house / "ㄱ" / "검사 작품" / "검사 작품 1권.txt"
        )
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE files SET canonical_path = ?, active = 0 WHERE file_id = ?",
                (retired, ids[0]),
            )
    finally:
        conn.close()

    inactive = file_listing(state_db, source="inactive")
    item = next(item for item in inactive["items"] if item["file_id"] == ids[0])
    assert item["retired_virtual_path"] is True
    folders = folder_listing(state_db, house, search="retired_paths")
    assert folders["total"] == 0


def test_compare_does_not_call_two_missing_coordinates_a_match(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    house.mkdir()
    paths = [house / "좌표 없는 첫 작품.txt", house / "좌표 없는 둘째 작품.txt"]
    for path in paths:
        path.write_text(path.stem, encoding="utf-8")
    conn = decision_store.initialize_state_db(state_db)
    try:
        ids = []
        for path in paths:
            with decision_store.transaction(conn):
                ids.append(decision_store.reconcile_file_metadata(conn, path, source="house")["file_id"])
    finally:
        conn.close()

    result = compare_files(state_db, *ids)

    assert result["comparison"]["same_coordinate"] is False
    assert result["comparison"]["same_core_title"] is False
