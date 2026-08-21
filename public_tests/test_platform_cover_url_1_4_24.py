from datetime import datetime, timezone

import pytest

import decision_store
import platform_catalog
import run_platform_catalog


def _catalog_db(tmp_path):
    db_path = tmp_path / ".dedup_state" / "state.sqlite3"
    conn = decision_store.initialize_state_db(db_path)
    with decision_store.transaction(conn):
        conn.execute(
            """
            INSERT INTO catalog_titles(
                title_key, display_title, query_title, normalizer_version
            ) VALUES ('작품', '작품', '작품', 'test')
            """
        )
    return db_path, conn


def _stat(platform, cover_url, **values):
    remote_ids = {"series": "11", "kakao": "22", "novelpia": "33"}
    remote_id = remote_ids[platform]
    return platform_catalog.PlatformStat(
        platform,
        "ok",
        remote_id=remote_id,
        remote_title="작품",
        remote_url=platform_catalog._canonical_remote_url(platform, remote_id),
        cover_url=cover_url,
        **values,
    )


def test_schema_v17_adds_nullable_https_cover_url_to_existing_v16_db(tmp_path):
    db_path, conn = _catalog_db(tmp_path)
    try:
        conn.execute("ALTER TABLE catalog_platform_stats DROP COLUMN cover_url")
        conn.execute("PRAGMA user_version = 16")
        conn.commit()
    finally:
        conn.close()
    migrated = decision_store.initialize_state_db(db_path, migrate=True)
    try:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 17
        columns = {
            row[1]
            for row in migrated.execute("PRAGMA table_info(catalog_platform_stats)")
        }
        assert "cover_url" in columns
        assert migrated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        migrated.close()


def test_platform_fixtures_collect_detail_cover_urls_for_all_three_sources():
    title = "합성 표지 작품"
    series_cover = "https://series.example/cover.jpg"
    novelpia_search_cover = "https://images.novelpia.com/search-cover.wimg"
    novelpia_detail_cover = "https://novelpia.com/imagebox/cover/detail.file"
    kakao_key = "abc/def/ghi"

    def fetch_text(url, _timeout):
        if "search/search.series" in url:
            return (
                '<a href="/novel/detail.series?productNo=11">'
                f"{title}</a>"
            )
        if "detail.series" in url:
            return (
                f'<meta property="og:title" content="{title}">'
                f'<meta property="og:image" content="{series_cover}">'
                '<button class="btn_download"><span>1.2만</span></button>'
            )
        if "novelpia.com/novel/33" in url:
            return (
                f'<meta property="og:image" content="{novelpia_detail_cover}">'
                '<p class="writer-tag"><span class="tag">#판타지</span></p>'
            )
        raise AssertionError(url)

    def fetch_json(url, _timeout):
        if "/v2/search/series" in url:
            return {"result": {"list": [{
                "series_id": "22",
                "title": title,
                "service_property": {"view_count": 23000},
            }]}}
        if "/v1/content/overview" in url:
            return {"result": {"content": {
                "title": title,
                "thumbnail": kakao_key,
                "service_property": {"view_count": 23000},
            }}}
        if "/v1/content/about" in url:
            return {"result": {"theme_keyword_list": []}}
        if "novelpia.com/proc/novel" in url:
            return {"status": 200, "list": [{
                "novel_no": "33",
                "novel_name": title,
                "count_view": 34000,
                "cover_url": "//images.novelpia.com/search-cover.wimg",
            }]}
        raise AssertionError(url)

    results = platform_catalog.lookup_platforms(
        title, fetch_text=fetch_text, fetch_json=fetch_json, timeout=1
    )
    by_platform = {result.platform: result for result in results}
    assert by_platform["series"].cover_url == series_cover
    assert by_platform["kakao"].cover_url == platform_catalog._kakao_cover_url(
        kakao_key
    )
    assert by_platform["novelpia"].cover_url == novelpia_detail_cover
    assert by_platform["novelpia"].cover_url != novelpia_search_cover


def test_cover_parser_drops_non_https_and_writer_rejects_it(tmp_path):
    assert platform_catalog._parse_open_graph_cover(
        '<meta property="og:image" content="http://example.test/cover.jpg">'
    ) is None
    _db_path, conn = _catalog_db(tmp_path)
    try:
        with pytest.raises(ValueError, match="direct https URL"):
            platform_catalog.record_platform_stats(
                conn,
                "작품",
                [_stat("series", "http://example.test/cover.jpg", download_count=1)],
            )
    finally:
        conn.close()


def test_existing_refresh_writers_replace_and_clear_cover_url(tmp_path):
    _db_path, conn = _catalog_db(tmp_path)
    first = "https://images.example/first.jpg"
    second = "https://images.example/second.jpg"
    third = "https://images.example/third.jpg"
    now = datetime(2026, 8, 21, tzinfo=timezone.utc)
    try:
        platform_catalog.record_platform_stats(
            conn,
            "작품",
            [_stat("series", first, download_count=10, genre="판타지")],
            now=now,
        )
        outcome = platform_catalog.record_platform_stats(
            conn,
            "작품",
            [_stat("series", second, download_count=10, genre="판타지")],
            now=now,
        )
        assert outcome == {"series": "unchanged"}
        assert conn.execute(
            "SELECT cover_url FROM catalog_platform_stats WHERE title_key = '작품'"
        ).fetchone()[0] == second

        outcome = platform_catalog.record_increased_platform_stats(
            conn,
            "작품",
            [_stat("series", third, download_count=10)],
            now=now,
        )
        assert outcome == {"series": "updated"}

        outcome = platform_catalog.record_platform_metadata_results(
            conn,
            "작품",
            [_stat("series", None, genre="판타지")],
            now=now,
        )
        assert outcome == {"series": "updated"}
        assert conn.execute(
            "SELECT cover_url FROM catalog_platform_stats WHERE title_key = '작품'"
        ).fetchone()[0] is None
    finally:
        conn.close()


def test_cover_null_success_rows_are_metadata_backfill_targets(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    book = house / "표지 대상 1-10화.txt"
    book.write_text("fixture", encoding="utf-8")
    db_path = tmp_path / ".dedup_state" / "backfill.sqlite3"
    conn = decision_store.initialize_state_db(db_path)
    try:
        with decision_store.transaction(conn):
            decision_store.reconcile_file_metadata(conn, book, source="house")
        platform_catalog.sync_catalog_titles(conn)
        title_key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            title_key,
            [
                _stat("series", None, download_count=10, genre="판타지"),
                _stat(
                    "kakao", "https://images.example/kakao.jpg",
                    view_count=20, genre="판타지", tags=(),
                ),
                _stat(
                    "novelpia", None, view_count=30, genre="판타지", tags=(),
                ),
            ],
        )
        [target] = platform_catalog.select_metadata_backfill_targets(conn)
        assert target.platforms == ("series", "novelpia")
    finally:
        conn.close()


def test_novelpia_metadata_backfill_uses_stored_id_without_search():
    calls = []

    def fetch_text(url, _timeout):
        calls.append(url)
        assert url == "https://novelpia.com/novel/33"
        return (
            '<meta property="og:image" content="https://images.example/novelpia.jpg">'
            '<p class="writer-tag"><span class="tag">#판타지</span></p>'
        )

    [result] = platform_catalog.lookup_platform_metadata(
        "검색하면 안 되는 제목",
        ("novelpia",),
        remote_ids={"novelpia": "33"},
        remote_titles={"novelpia": "저장된 원격 제목"},
        fetch_text=fetch_text,
        fetch_json=lambda *_args: (_ for _ in ()).throw(
            AssertionError("NovelPia title search must not run")
        ),
        timeout=1,
    )

    assert calls == ["https://novelpia.com/novel/33"]
    assert result.status == "ok"
    assert result.remote_id == "33"
    assert result.remote_title == "저장된 원격 제목"
    assert result.cover_url == "https://images.example/novelpia.jpg"
    assert result.metadata_lookup_mode == "direct"


def test_metadata_backfill_reports_cover_missing_reason(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    book = house / "표지 없음 1-10화.txt"
    book.write_text("fixture", encoding="utf-8")
    db_path = tmp_path / ".dedup_state" / "failure-reason.sqlite3"
    conn = decision_store.initialize_state_db(db_path)
    try:
        with decision_store.transaction(conn):
            decision_store.reconcile_file_metadata(conn, book, source="house")
        platform_catalog.sync_catalog_titles(conn)
        title_key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            title_key,
            [_stat("series", None, download_count=10, genre="판타지")],
        )
    finally:
        conn.close()

    result = platform_catalog.refresh_missing_metadata(
        str(db_path),
        limit=None,
        delay_seconds=0,
        lookup=lambda _title, platforms, **_kwargs: [
            _stat(platform, None, genre="판타지") for platform in platforms
        ],
    )

    assert result["failure_reasons"] == [{
        "platform": "series",
        "outcome": "updated",
        "reason": "no https cover in metadata response",
        "count": 1,
    }]


def test_refresh_metadata_help_names_cover_genre_tag_backfill(capsys):
    parser = run_platform_catalog.build_parser()
    with pytest.raises(SystemExit) as raised:
        parser.parse_args(["refresh-metadata", "--help"])
    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    assert "표지/장르/태그" in help_text
