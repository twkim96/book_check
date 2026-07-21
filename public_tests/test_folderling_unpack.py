import decision_store
import folderling
from folderling import cleanup_unpack_sources, iter_process_items
from scanner import generate_file_list


def test_unpack_and_legacy_wrappers_expand_supported_files_only(tmp_path):
    temp = tmp_path / "temp"
    unpack = temp / "unpack" / "20260701 완결"
    legacy = temp / "___예전 묶음" / "20260702 완결"
    normal = temp / "일반 폴더"
    unpack.mkdir(parents=True)
    legacy.mkdir(parents=True)
    normal.mkdir(parents=True)
    unpack_book = unpack / "첫 작품.txt"
    legacy_book = legacy / "둘째 작품.epub"
    unpack_book.write_text("book", encoding="utf-8")
    legacy_book.write_text("book", encoding="utf-8")
    (unpack / "표지.jpg").write_bytes(b"cover")
    (legacy / "지도.zip").write_bytes(b"map")

    items = iter_process_items(str(temp), str(temp / "pass"))

    assert ("일반 폴더", str(normal), False) in items
    assert (unpack_book.name, str(unpack_book), False) in items
    assert (legacy_book.name, str(legacy_book), False) in items
    assert all("표지.jpg" not in item[1] and "지도.zip" not in item[1] for item in items)
    assert all(item[0] not in {"unpack", "___예전 묶음"} for item in items)


def test_unpack_cleanup_waits_for_supported_files_then_discards_assets(tmp_path):
    temp = tmp_path / "temp"
    unpack = temp / "unpack" / "20260701 완결"
    legacy = temp / "___예전 묶음" / "20260702 완결"
    unpack.mkdir(parents=True)
    legacy.mkdir(parents=True)
    unpack_book = unpack / "첫 작품.txt"
    legacy_book = legacy / "둘째 작품.epub"
    unpack_book.write_text("book", encoding="utf-8")
    legacy_book.write_text("book", encoding="utf-8")
    (unpack / "표지.jpg").write_bytes(b"cover")
    (legacy / "지도.zip").write_bytes(b"map")

    pending = cleanup_unpack_sources(str(temp))
    assert {item["status"] for item in pending} == {"pending_supported_files"}
    assert unpack.is_dir()
    assert legacy.is_dir()

    unpack_book.unlink()
    legacy_book.unlink()
    cleaned = cleanup_unpack_sources(str(temp))

    assert {item["status"] for item in cleaned} == {"cleaned"}
    assert sum(item["discarded_files"] for item in cleaned) == 2
    assert (temp / "unpack").is_dir()
    assert list((temp / "unpack").iterdir()) == []
    assert not (temp / "___예전 묶음").exists()


def test_folderling_journals_unpacked_book_then_discards_wrapper_assets(tmp_path):
    script_dir = tmp_path / "project"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    state_db = script_dir / ".dedup_state" / "dedup_decisions.sqlite3"
    script_dir.mkdir()
    house.mkdir()
    unpack = temp / "unpack" / "20260701 완결"
    unpack.mkdir(parents=True)
    (script_dir / "extension").mkdir()
    incoming = unpack / "해체 입고 작품 1-10 완.txt"
    cover = unpack / "해체 입고 작품 표지.jpg"
    incoming.write_text("신규 본문", encoding="utf-8")
    cover.write_bytes(b"cover")

    conn = decision_store.initialize_state_db(state_db)
    try:
        with decision_store.transaction(conn):
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
            conn, state_db.parent / "backups" / "before-unpack.sqlite3"
        )
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()

    result = folderling._process_items_with_lock_held(
        str(temp), str(house), str(script_dir), state_db_path=str(state_db)
    )

    assert result["failure_count"] == 0
    assert result["move_count"] == 1
    assert result["unpack_discarded_file_count"] == 1
    assert (house / "ㅎ" / incoming.name).is_file()
    assert (temp / "unpack").is_dir()
    assert list((temp / "unpack").iterdir()) == []
    assert not cover.exists()
    conn = decision_store.connect_state_db(state_db)
    try:
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
