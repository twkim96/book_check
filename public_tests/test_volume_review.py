from pathlib import Path

import decision_store
from volume_review import list_volume_cases, preview_volume_group


def _add_file(conn, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(path.name, encoding="utf-8")
    with decision_store.transaction(conn):
        return decision_store.reconcile_file_metadata(conn, path, source="house")


def _fixture(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        for number in (1, 2, 3):
            _add_file(conn, house / "ㅇ" / f"우주 도서 {number}권.txt")
        for number in (1, 2):
            _add_file(conn, house / "ㄷ" / "도시 이야기 전권" / f"도시 이야기 {number}권.epub")
        _add_file(conn, house / "ㅈ" / "중복 작품 1권.txt")
        _add_file(conn, house / "ㅈ" / "중복 작품 1권.epub")
        _add_file(conn, house / "ㄴ" / "누락 작품 1권.txt")
        _add_file(conn, house / "ㄴ" / "누락 작품 3권.txt")
        _add_file(conn, house / "ㅎ" / "형식 작품 1-182 외전 완 [한작가].txt")
        _add_file(conn, house / "ㅎ" / "형식 작품 1-182 외전 완 [한작가].epub")
        numeric_rows = [
            _add_file(conn, house / "숫자" / "24 1권.txt"),
            _add_file(conn, house / "숫자" / "24 2권.txt"),
        ]
        with decision_store.transaction(conn):
            conn.executemany(
                "UPDATE file_analysis SET core_title = '24', readable_title = '24' "
                "WHERE file_id = ?",
                [(row["file_id"],) for row in numeric_rows],
            )
    finally:
        conn.close()
    return state_db, house


def _by_title(listing):
    return {case["core_title"]: case for case in listing["items"]}


def test_volume_inventory_classifies_without_mutating_files(tmp_path):
    state_db, house = _fixture(tmp_path)
    before = sorted(str(path.relative_to(house)) for path in house.rglob("*") if path.is_file())
    listing = list_volume_cases(state_db, house_dir=house, limit=50)
    cases = _by_title(listing)

    assert listing["readonly"] is False
    assert listing["total"] == 6
    assert listing["summary"] == {
        "already_grouped": 1,
        "auto_ready": 3,
        "excluded": 1,
        "review_required": 1,
    }
    assert cases["우주도서"]["classification"] == "auto_ready"
    assert cases["도시이야기"]["classification"] == "already_grouped"
    assert cases["중복작품"]["blocked_reasons"] == ["duplicate_coordinate"]
    assert all(
        item["issues"] == ["duplicate_coordinate"]
        and item["same_coordinate_count"] == 2
        for item in cases["중복작품"]["items"]
    )
    assert cases["누락작품"]["missing_coordinates"] == ["2권"]
    assert cases["누락작품"]["blocked_reasons"] == []
    assert cases["누락작품"]["plan_ready"] is True
    assert cases["형식작품"]["classification"] == "auto_ready"
    assert cases["형식작품"]["duplicate_coordinates"] == []
    assert cases["형식작품"]["parallel_format_coordinates"] == ["side_story"]
    assert cases["24"]["classification"] == "excluded"

    after = sorted(str(path.relative_to(house)) for path in house.rglob("*") if path.is_file())
    assert after == before


def test_volume_preview_is_revision_bound_and_confirmation_ready(tmp_path):
    state_db, house = _fixture(tmp_path)
    listing = list_volume_cases(
        state_db,
        house_dir=house,
        search="우주 도서",
        classification="auto_ready",
        limit=10,
    )
    [case] = listing["items"]
    preview = preview_volume_group(
        state_db,
        house_dir=house,
        case_id=case["case_id"],
        source_revision=case["source_revision"],
        target_folder_name="우주 도서 전권",
    )
    assert preview["plan_ready"] is True
    assert preview["apply_available"] is True
    assert preview["item_count"] == 3
    assert len(preview["plan_sha256"]) == 64
    assert all(path.startswith("우주 도서 전권/") for path in preview["tree"])

    stale = preview_volume_group(
        state_db,
        house_dir=house,
        case_id=case["case_id"],
        source_revision="stale",
    )
    assert stale["plan_ready"] is False
    assert "source_revision_stale" in stale["blocked_reasons"]


def test_volume_preview_allows_missing_coordinates(tmp_path):
    state_db, house = _fixture(tmp_path)
    listing = list_volume_cases(
        state_db,
        house_dir=house,
        search="누락 작품",
        classification="auto_ready",
        limit=10,
    )
    [case] = listing["items"]

    preview = preview_volume_group(
        state_db,
        house_dir=house,
        case_id=case["case_id"],
        source_revision=case["source_revision"],
    )

    assert case["missing_coordinates"] == ["2권"]
    assert preview["blocked_reasons"] == []
    assert preview["apply_available"] is True


def test_side_story_parallel_formats_conflict_when_coverage_differs(tmp_path):
    state_db, house = _fixture(tmp_path)
    conn = decision_store.connect_state_db(state_db)
    try:
        epub = conn.execute(
            "SELECT file_id FROM files WHERE canonical_path LIKE ?",
            ("%형식 작품%.epub",),
        ).fetchone()
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE file_analysis SET effective_max = 181 WHERE file_id = ?",
                (epub["file_id"],),
            )
    finally:
        conn.close()

    listing = list_volume_cases(
        state_db,
        house_dir=house,
        search="형식 작품",
        classification="review_required",
        limit=10,
    )
    [case] = listing["items"]

    assert case["duplicate_coordinates"] == ["side_story"]
    assert case["parallel_format_coordinates"] == []
    assert case["blocked_reasons"] == ["duplicate_coordinate"]


def test_volume_and_side_story_are_a_compatible_group(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        _add_file(conn, house / "ㄷ" / "다정한 작품 1권 [한작가].epub")
        _add_file(conn, house / "ㄷ" / "다정한 작품 2권 [한작가].epub")
        _add_file(conn, house / "ㄷ" / "다정한 작품 외전 [한작가].epub")
    finally:
        conn.close()

    listing = list_volume_cases(
        state_db,
        house_dir=house,
        search="다정한 작품",
        classification="auto_ready",
        limit=10,
    )
    [case] = listing["items"]

    assert case["coordinate_kinds"] == ["symbol", "volume"]
    assert case["blocked_reasons"] == []
    assert case["plan_ready"] is True
    assert {item["author"] for item in case["items"]} == {"한작가"}
    assert {item["coordinate"] for item in case["items"]} == {"1권", "2권", "side_story"}
    assert all(item["issues"] == [] for item in case["items"])


def test_volume_preview_reuses_existing_group_folder(tmp_path):
    state_db, house = _fixture(tmp_path)
    listing = list_volume_cases(
        state_db, house_dir=house, classification="already_grouped", limit=10
    )
    [case] = listing["items"]

    preview = preview_volume_group(
        state_db,
        house_dir=house,
        case_id=case["case_id"],
        source_revision=case["source_revision"],
    )

    assert Path(preview["destination_root"]) == Path(case["target_folder_path"])
    assert preview["target_folder_name"] == Path(case["target_folder_path"]).name


def test_volume_listing_search_filter_and_cursor(tmp_path):
    state_db, house = _fixture(tmp_path)
    result = list_volume_cases(
        state_db,
        house_dir=house,
        search="작품",
        classification="all",
        limit=1,
        sort="title",
    )
    assert result["total"] == 3
    assert len(result["items"]) == 1
    assert result["next_cursor"]
    second = list_volume_cases(
        state_db,
        house_dir=house,
        search="작품",
        classification="all",
        limit=1,
        sort="title",
        cursor=result["next_cursor"],
    )
    assert len(second["items"]) == 1
    assert second["items"][0]["case_id"] != result["items"][0]["case_id"]
