import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import decision_store
import volume_review
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
    assert listing["total"] == 5
    assert listing["summary"] == {
        "already_grouped": 1,
        "auto_ready": 2,
        "excluded": 1,
        "review_required": 1,
    }
    assert cases["우주도서"]["classification"] == "auto_ready"
    assert cases["도시이야기"]["classification"] == "already_grouped"
    assert "중복작품" not in cases
    assert cases["누락작품"]["missing_coordinates"] == ["2권"]
    assert cases["누락작품"]["blocked_reasons"] == []
    assert cases["누락작품"]["plan_ready"] is True
    assert cases["형식작품"]["classification"] == "review_required"
    assert cases["형식작품"]["duplicate_coordinates"] == []
    assert cases["형식작품"]["parallel_format_coordinates"] == ["side_story"]
    assert cases["형식작품"]["blocked_reasons"] == [
        "side_story_requires_two_main_coordinates"
    ]
    assert cases["24"]["classification"] == "excluded"

    after = sorted(str(path.relative_to(house)) for path in house.rglob("*") if path.is_file())
    assert after == before


def test_current_file_analysis_does_not_reparse_every_listing_row(
    tmp_path, monkeypatch
):
    state_db, house = _fixture(tmp_path)

    def unexpected_reparse(*_args, **_kwargs):
        raise AssertionError("current analysis row was reparsed")

    monkeypatch.setattr(
        volume_review.decision_store,
        "coordinate_fields_from_name",
        unexpected_reparse,
    )
    monkeypatch.setattr(volume_review, "extract_author", unexpected_reparse)

    listing = list_volume_cases(state_db, house_dir=house, limit=50)

    assert listing["total"] == 5


def test_parallel_complete_editions_are_not_series_cases(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        for name in (
            "동일 작품 1-200 완결.txt",
            "동일 작품 1-200 완결.epub",
            "같은 시작 작품 1-180 완결.txt",
            "같은 시작 작품 1-200 완결.epub",
            "영일 동일 작품 0-200 완결.txt",
            "영일 동일 작품 1-200 완결.epub",
            "동일 권 작품 1권.txt",
            "동일 권 작품 1권.epub",
        ):
            _add_file(conn, house / "ㄷ" / name)
    finally:
        conn.close()

    listing = list_volume_cases(state_db, house_dir=house, limit=20)

    assert listing["total"] == 0
    assert listing["items"] == []
    assert all(value == 0 for value in listing["summary"].values())


def test_episode_zero_and_one_are_the_same_series_start():
    zero = {"coordinate_kind": "episode", "episode_start": 0}
    one = {"coordinate_kind": "episode", "episode_start": 1}

    assert volume_review._series_position(zero) == ("episode", 1)
    assert volume_review._series_position(one) == ("episode", 1)


def test_parallel_edition_can_join_a_real_split_series(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        for name in (
            "분할 작품 1-100.txt",
            "분할 작품 1-100.epub",
            "분할 작품 101-200.txt",
        ):
            _add_file(conn, house / "ㅂ" / name)
    finally:
        conn.close()

    listing = list_volume_cases(state_db, house_dir=house, limit=20)

    assert listing["total"] == 1
    [case] = listing["items"]
    assert case["classification"] == "auto_ready"
    assert case["file_count"] == 3
    assert case["parallel_format_coordinates"] == ["1~100화"]


def test_same_start_editions_with_side_story_require_review(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        for name in (
            "별빛 관계 작품 1-180 완결.txt",
            "별빛 관계 작품 1-200 완결.epub",
            "별빛 관계 작품 외전 1-10.txt",
        ):
            _add_file(conn, house / "ㅇ" / name)
    finally:
        conn.close()

    listing = list_volume_cases(state_db, house_dir=house, limit=20)

    assert listing["total"] == 1
    [case] = listing["items"]
    assert case["classification"] == "review_required"
    assert case["main_coordinate_count"] == 1
    assert case["blocked_reasons"] == [
        "side_story_requires_two_main_coordinates"
    ]


def test_identical_concurrent_volume_listings_share_one_analysis(
    tmp_path, monkeypatch
):
    state_db, house = _fixture(tmp_path)
    volume_review.invalidate_volume_case_cache()
    original = volume_review._load_volume_rows
    calls = []
    calls_lock = threading.Lock()

    def slow_load(path):
        with calls_lock:
            calls.append(path)
        time.sleep(0.05)
        return original(path)

    monkeypatch.setattr(volume_review, "_load_volume_rows", slow_load)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _index: list_volume_cases(
                    state_db, house_dir=house, limit=50
                ),
                range(4),
            )
        )

    assert len(calls) == 1
    assert {result["total"] for result in results} == {5}


def test_volume_listing_cache_refreshes_only_after_database_revision(
    tmp_path, monkeypatch
):
    state_db, house = _fixture(tmp_path)
    volume_review.invalidate_volume_case_cache()
    original = volume_review._load_volume_rows
    calls = []

    def counted_load(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(volume_review, "_load_volume_rows", counted_load)

    assert list_volume_cases(state_db, house_dir=house, limit=1)["total"] == 5
    assert list_volume_cases(state_db, house_dir=house, limit=1)["total"] == 5
    assert len(calls) == 1

    conn = decision_store.connect_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                ("volume_listing_cache_probe", "changed"),
            )
    finally:
        conn.close()

    assert list_volume_cases(state_db, house_dir=house, limit=1)["total"] == 5
    assert len(calls) == 2


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
    assert case["blocked_reasons"] == [
        "duplicate_coordinate",
        "side_story_requires_two_main_coordinates",
    ]


def test_volume_and_side_story_are_a_compatible_group(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        _add_file(conn, house / "ㄷ" / "다정한 작품 1권 [한작가].txt")
        _add_file(conn, house / "ㄷ" / "다정한 작품 2권 [한작가].epub")
        _add_file(conn, house / "ㄷ" / "다정한 작품 외전 [한작가].txt")
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


def test_part_volume_coordinates_do_not_collapse_across_parts(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        for name in (
            "천마군림 1부 1권.epub",
            "천마군림 1부 2권.epub",
            "천마군림 2부 1권.epub",
            "천마군림 2부 3권.epub",
        ):
            _add_file(conn, house / "ㅊ" / name)
    finally:
        conn.close()

    listing = list_volume_cases(
        state_db, house_dir=house, search="천마군림", limit=10
    )
    [case] = listing["items"]

    assert case["classification"] == "auto_ready"
    assert case["duplicate_coordinates"] == []
    assert case["missing_coordinates"] == ["2부 2권"]
    assert {item["coordinate"] for item in case["items"]} == {
        "1부 1권", "1부 2권", "2부 1권", "2부 3권",
    }


def test_numbered_side_stories_are_distinct_coordinates(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        _add_file(conn, house / "ㅂ" / "블랙 라벨 외전 1.epub")
        _add_file(conn, house / "ㅂ" / "블랙 라벨 외전 2.epub")
    finally:
        conn.close()

    [case] = list_volume_cases(
        state_db, house_dir=house, search="블랙 라벨", limit=10
    )["items"]

    assert case["classification"] == "review_required"
    assert case["duplicate_coordinates"] == []
    assert case["blocked_reasons"] == [
        "side_story_requires_two_main_coordinates"
    ]
    assert {item["coordinate"] for item in case["items"]} == {"외전 1", "외전 2"}


def test_single_main_plus_side_story_requires_explicit_override(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        _add_file(conn, house / "ㄷ" / "다정한 작품 1권.txt")
        _add_file(conn, house / "ㄷ" / "다정한 작품 외전.epub")
    finally:
        conn.close()

    [case] = list_volume_cases(
        state_db, house_dir=house, search="다정한 작품", limit=10
    )["items"]
    assert case["classification"] == "review_required"
    assert case["main_coordinate_count"] == 1
    assert case["has_side_story"] is True

    blocked = preview_volume_group(
        state_db,
        house_dir=house,
        case_id=case["case_id"],
        source_revision=case["source_revision"],
    )
    assert blocked["apply_available"] is False
    assert "side_story_requires_two_main_coordinates" in blocked["blocked_reasons"]

    approved = preview_volume_group(
        state_db,
        house_dir=house,
        case_id=case["case_id"],
        source_revision=case["source_revision"],
        allow_side_story_without_two_main_coordinates=True,
    )
    assert approved["apply_available"] is True
    assert approved["allow_side_story_without_two_main_coordinates"] is True


def test_episode_split_files_are_automatic_even_when_ranges_overlap(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        for name in (
            "연속 작품 1-100화.txt",
            "연속 작품 105-200화.txt",
            "연속 작품 100-150화.txt",
        ):
            _add_file(conn, house / "ㅇ" / name)
    finally:
        conn.close()

    [case] = list_volume_cases(
        state_db, house_dir=house, search="연속 작품", limit=10
    )["items"]
    assert case["classification"] == "auto_ready"
    assert case["coordinate_kinds"] == ["episode"]
    assert case["duplicate_coordinates"] == []
    assert {item["coordinate"] for item in case["items"]} == {
        "1~100화", "100~150화", "105~200화",
    }


def test_epub_and_pdf_at_same_volume_are_parallel_formats(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        for volume in (1, 2):
            _add_file(conn, house / "ㅂ" / f"병행 작품 {volume}권.epub")
            _add_file(conn, house / "ㅂ" / f"병행 작품 {volume}권.pdf")
    finally:
        conn.close()

    [case] = list_volume_cases(
        state_db, house_dir=house, search="병행 작품", limit=10
    )["items"]

    assert case["classification"] == "auto_ready"
    assert case["duplicate_coordinates"] == []
    assert case["parallel_format_coordinates"] == ["1권", "2권"]


def test_volume_cohort_ignores_same_core_serial_compilation(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        for volume in (1, 2, 3):
            _add_file(
                conn,
                house / "ㄷ" / "두 번 사는 랭커" /
                f"두 번 사는 랭커 {volume}권.epub",
            )
        _add_file(conn, house / "ㄷ" / "두 번 사는 랭커 1-100화 2부.txt")
    finally:
        conn.close()

    [case] = list_volume_cases(
        state_db, house_dir=house, search="두 번 사는 랭커", limit=10
    )["items"]

    assert case["classification"] == "already_grouped"
    assert case["file_count"] == 3
    assert case["coordinate_kinds"] == ["volume"]


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
    assert result["total"] == 2
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
