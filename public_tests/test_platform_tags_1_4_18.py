from datetime import datetime, timezone

import decision_store
import library_catalog
import library_services
import platform_catalog


def _catalog_db(tmp_path):
    db_path = tmp_path / ".dedup_state" / "state.sqlite3"
    conn = decision_store.initialize_state_db(db_path)
    with decision_store.transaction(conn):
        conn.execute(
            """
            INSERT INTO catalog_titles(title_key, display_title, query_title, normalizer_version)
            VALUES ('작품', '작품', '작품', 'test')
            """
        )
    return db_path, conn


def _tags(conn, platform):
    return [
        row[0]
        for row in conn.execute(
            """
            SELECT tag FROM catalog_platform_tags
            WHERE title_key = '작품' AND platform = ?
            ORDER BY position
            """,
            (platform,),
        )
    ]


def test_schema_v16_has_normalized_platform_tag_table(tmp_path):
    _db_path, conn = _catalog_db(tmp_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 16
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(catalog_platform_stats)")
        }
        assert {"genre", "genre_collected_at", "tags_collected_at"} <= columns
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'catalog_platform_tags'"
        ).fetchone()[0] == 1
        fk_rows = conn.execute("PRAGMA foreign_key_list(catalog_platform_tags)").fetchall()
        assert {(row[3], row[4]) for row in fk_rows} == {
            ("title_key", "title_key"),
            ("platform", "platform"),
        }
    finally:
        conn.close()


def test_v15_migrates_tags_schema_without_changing_existing_stat(tmp_path):
    db_path, conn = _catalog_db(tmp_path)
    try:
        platform_catalog.record_platform_stats(
            conn,
            "작품",
            [platform_catalog.PlatformStat("kakao", "ok", view_count=123)],
        )
        conn.execute("DROP TABLE catalog_platform_tags")
        conn.execute("ALTER TABLE catalog_platform_stats DROP COLUMN genre")
        conn.execute("ALTER TABLE catalog_platform_stats DROP COLUMN genre_collected_at")
        conn.execute("ALTER TABLE catalog_platform_stats DROP COLUMN tags_collected_at")
        conn.execute("PRAGMA user_version = 15")
        conn.commit()
    finally:
        conn.close()

    migrated = decision_store.initialize_state_db(db_path, migrate=True)
    try:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 16
        assert migrated.execute(
            "SELECT view_count FROM catalog_platform_stats "
            "WHERE title_key = '작품' AND platform = 'kakao'"
        ).fetchone()[0] == 123
        assert migrated.execute(
            "SELECT COUNT(*) FROM catalog_platform_tags"
        ).fetchone()[0] == 0
        assert migrated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        migrated.close()


def test_novelpia_writer_tags_ignore_personal_tag_and_dedupe_mobile_copy():
    block = """
    <p class="writer-tag">
      <span class="tag">#판타지</span>
      <span class="tag">#라이트노벨</span>
      <span class="tag">#하렘</span>
      <span class="tag">#로맨스</span>
      <span class="tag">#집착</span>
      <span class="tag">#역키잡</span>
      <span class="tag">#육성</span>
      <span class="tag">#먼치킨</span>
    </p>
    """
    page = block + '<p class="my-tag"><span class="tag more">+나만의태그 추가</span></p>' + block
    assert platform_catalog._parse_novelpia_tags(page) == (
        "판타지",
        "라이트노벨",
        "하렘",
        "로맨스",
        "집착",
        "역키잡",
        "육성",
        "먼치킨",
    )


def test_series_genre_is_scoped_to_product_end_info_not_recommended_genres():
    page = """
    <nav>
      <a href="/novel/categoryProductList.series?categoryTypeCode=genre&amp;genreCode=201">로맨스</a>
      <a href="/novel/categoryProductList.series?categoryTypeCode=genre&amp;genreCode=202">판타지</a>
    </nav>
    <ul class="end_info NE=a:nvi">
      <li class="info_lst"><ul>
        <li class="ing"><span>연재중</span></li>
        <li><span><a href="/novel/categoryProductList.series?categoryTypeCode=genre&amp;genreCode=208">현판</a></span></li>
      </ul></li>
    </ul>
    <div class="end_dsc">소개</div>
    """
    assert platform_catalog._parse_series_genre(page) == "현판"


def test_kakao_about_theme_keyword_list_is_authoritative_order():
    data = {
        "result": {
            "theme_keyword_list": [
                {"uid": 170, "title": "게임"},
                {"uid": 199, "title": "방송"},
                {"uid": 21, "title": "#먼치킨"},
                {"uid": 53, "title": "천재"},
                {"uid": 213, "title": "성장"},
                {"uid": 218, "title": "검사"},
                {"uid": 440, "title": "궁사"},
                {"uid": 441, "title": "총"},
                {"uid": 248, "title": "만능"},
            ]
        }
    }
    assert platform_catalog._parse_kakao_tags(data) == (
        "게임",
        "방송",
        "먼치킨",
        "천재",
        "성장",
        "검사",
        "궁사",
        "총",
        "만능",
    )


def test_platform_lookups_collect_tags_without_making_tag_failure_fatal():
    def kakao(url, _timeout):
        if "/v2/search/series" in url:
            return {"result": {"list": [{
                "series_id": "56505778",
                "title": "천재 궁수의 스트리밍",
                "service_property": {"view_count": 100},
            }]}}
        if "/content/overview" in url:
            return {"result": {"content": {
                "title": "천재 궁수의 스트리밍",
                "sub_category": "판타지",
                "service_property": {"viewCount": 200},
            }}}
        if "/content/about" in url:
            return {"result": {"theme_keyword_list": [
                {"title": "게임"}, {"title": "방송"}, {"title": "먼치킨"}
            ]}}
        raise AssertionError(url)

    stat = platform_catalog.lookup_kakao(
        "천재 궁수의 스트리밍", fetch_json=kakao, timeout=1
    )
    assert stat.status == "ok"
    assert stat.genre == "판타지"
    assert stat.tags == ("게임", "방송", "먼치킨")

    def novelpia_json(_url, _timeout):
        return {"list": [{
            "novel_no": "415410",
            "novel_name": "망나니 검성을 너무 잘 키워버렸다",
            "count_view": 10,
            "count_good": 2,
            "novel_genre_arr": ["판타지", "라이트노벨", "먼치킨"],
        }]}

    def novelpia_html(_url, _timeout):
        raise AssertionError("search JSON genre array should avoid a detail request")

    stat = platform_catalog.lookup_novelpia(
        "망나니 검성을 너무 잘 키워버렸다",
        novelpia_json,
        novelpia_html,
        timeout=1,
    )
    assert stat.status == "ok"
    assert stat.genre == "판타지"
    assert stat.tags == ("판타지", "라이트노벨", "먼치킨")

    def failed_about(url, timeout):
        if "/content/about" in url:
            raise TimeoutError("about timeout")
        return kakao(url, timeout)

    stat = platform_catalog.lookup_kakao(
        "천재 궁수의 스트리밍", fetch_json=failed_about, timeout=1
    )
    assert stat.status == "ok"
    assert stat.view_count == 200
    assert stat.genre == "판타지"
    assert stat.tags is None


def test_metadata_lookup_prefers_verified_remote_ids_and_falls_back_to_search():
    title = "테스트 작품"
    text_calls = []
    json_calls = []

    def direct_text(url, _timeout):
        text_calls.append(url)
        assert "search/search.series" not in url
        assert "productNo=11" in url
        return (
            '<meta property="og:title" content="테스트 작품">'
            '<button class="btn_download"><span>1,234</span></button>'
            '<ul class="end_info"><li><a href="/novel/categoryProductList.series?'
            'categoryTypeCode=genre&amp;genreCode=208">현판</a></li></ul>'
            '<div class="end_dsc">설명</div>'
        )

    def direct_json(url, _timeout):
        json_calls.append(url)
        assert "/v2/search/series" not in url
        if "/content/overview" in url:
            assert "series_id=22" in url
            return {"result": {"content": {
                "title": title,
                "sub_category": "판타지",
                "service_property": {"viewCount": 2_345},
            }}}
        if "/content/about" in url:
            return {"result": {"theme_keyword_list": [
                {"title": "먼치킨"}, {"title": "성장"},
            ]}}
        raise AssertionError(url)

    results = platform_catalog.lookup_platform_metadata(
        title,
        ("series", "kakao"),
        remote_ids={
            "series": "11",
            "kakao": "22",
        },
        fetch_text=direct_text,
        fetch_json=direct_json,
        timeout=1,
    )
    by_platform = {item.platform: item for item in results}
    assert by_platform["series"].remote_id == "11"
    assert by_platform["series"].genre == "현판"
    assert by_platform["kakao"].remote_id == "22"
    assert by_platform["kakao"].genre == "판타지"
    assert by_platform["kakao"].tags == ("먼치킨", "성장")
    assert len(text_calls) == 1
    assert len(json_calls) == 2

    fallback_calls = []

    def fallback_text(url, _timeout):
        fallback_calls.append(url)
        if "productNo=99" in url:
            return (
                '<meta property="og:title" content="다른 작품">'
                '<button class="btn_download"><span>10</span></button>'
            )
        if "search/search.series" in url:
            return (
                '<li><a class="N=a:nov.title" '
                'href="/novel/detail.series?productNo=11">테스트 작품</a></li>'
            )
        if "productNo=11" in url:
            return (
                '<meta property="og:title" content="테스트 작품">'
                '<button class="btn_download"><span>1,234</span></button>'
                '<ul class="end_info"><li><a href="/novel/categoryProductList.series?'
                'categoryTypeCode=genre&amp;genreCode=208">현판</a></li></ul>'
                '<div class="end_dsc">설명</div>'
            )
        raise AssertionError(url)

    [fallback] = platform_catalog.lookup_platform_metadata(
        title,
        ("series",),
        remote_ids={"series": "99"},
        fetch_text=fallback_text,
        timeout=1,
    )
    assert fallback.status == "ok"
    assert fallback.remote_id == "11"
    assert any("search/search.series" in url for url in fallback_calls)


def test_metadata_progress_persistence_is_throttled():
    assert library_services._should_emit_platform_progress("metadata_progress", 1, 25)
    assert not library_services._should_emit_platform_progress("metadata_progress", 2, 25)
    assert library_services._should_emit_platform_progress("metadata_progress", 10, 25)
    assert not library_services._should_emit_platform_progress("metadata_progress", 24, 25)
    assert library_services._should_emit_platform_progress("metadata_progress", 25, 25)
    assert library_services._should_emit_platform_progress("metadata_start", 0, 25)


def test_tag_rows_replace_only_after_authoritative_success(tmp_path):
    _db_path, conn = _catalog_db(tmp_path)
    try:
        first = datetime(2026, 8, 15, 1, tzinfo=timezone.utc)
        platform_catalog.record_platform_stats(
            conn,
            "작품",
            [platform_catalog.PlatformStat(
                "kakao", "ok", view_count=100, tags=("게임", "방송")
            )],
            now=first,
        )
        assert _tags(conn, "kakao") == ["게임", "방송"]
        collected = conn.execute(
            "SELECT tags_collected_at FROM catalog_platform_stats "
            "WHERE title_key = '작품' AND platform = 'kakao'"
        ).fetchone()[0]
        assert collected == "2026-08-15T01:00:00+00:00"

        platform_catalog.record_platform_stats(
            conn,
            "작품",
            [platform_catalog.PlatformStat("kakao", "error", message="temporary")],
        )
        assert _tags(conn, "kakao") == ["게임", "방송"]
        assert conn.execute(
            "SELECT tags_collected_at FROM catalog_platform_stats "
            "WHERE title_key = '작품' AND platform = 'kakao'"
        ).fetchone()[0] == collected

        platform_catalog.record_platform_stats(
            conn,
            "작품",
            [platform_catalog.PlatformStat(
                "kakao", "ok", view_count=101, tags=("게임", "성장")
            )],
        )
        assert _tags(conn, "kakao") == ["게임", "성장"]

        platform_catalog.record_platform_stats(
            conn,
            "작품",
            [platform_catalog.PlatformStat("kakao", "ok", view_count=102, tags=())],
        )
        assert _tags(conn, "kakao") == []
        assert conn.execute(
            "SELECT tags_collected_at IS NOT NULL FROM catalog_platform_stats "
            "WHERE title_key = '작품' AND platform = 'kakao'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_tag_parent_delete_cascades_child_rows(tmp_path):
    _db_path, conn = _catalog_db(tmp_path)
    try:
        platform_catalog.record_platform_stats(
            conn,
            "작품",
            [platform_catalog.PlatformStat(
                "kakao", "ok", view_count=100, tags=("게임", "성장")
            )],
        )
        assert _tags(conn, "kakao") == ["게임", "성장"]
        with decision_store.transaction(conn):
            conn.execute(
                "DELETE FROM catalog_platform_stats "
                "WHERE title_key = '작품' AND platform = 'kakao'"
            )
        assert _tags(conn, "kakao") == []
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_metadata_backfill_reuses_stored_remote_ids(tmp_path, monkeypatch):
    house = tmp_path / "house"
    house.mkdir()
    path = house / "원격 ID 작품 1-10화.txt"
    path.write_text("fixture", encoding="utf-8")
    db_path = tmp_path / ".dedup_state" / "remote-hints.sqlite3"
    conn = decision_store.initialize_state_db(db_path)
    try:
        with decision_store.transaction(conn):
            decision_store.reconcile_file_metadata(conn, path, source="house")
        platform_catalog.sync_catalog_titles(conn)
        title_key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            title_key,
            [
                platform_catalog.PlatformStat(
                    "series", "ok", remote_id="9197875",
                    remote_title="원격 ID 작품", download_count=7,
                ),
                platform_catalog.PlatformStat(
                    "kakao", "ok", remote_id="69475645",
                    remote_title="원격 ID 작품", view_count=100,
                ),
            ],
        )
    finally:
        conn.close()

    captured = []

    def metadata_lookup(title, platforms, *, remote_ids, timeout):
        captured.append((title, tuple(platforms), dict(remote_ids or {}), timeout))
        return [
            platform_catalog.PlatformStat(
                platform,
                "ok",
                download_count=8 if platform == "series" else None,
                view_count=101 if platform == "kakao" else None,
                genre="현판" if platform == "series" else "무협",
                tags=("회귀", "지존") if platform == "kakao" else None,
            )
            for platform in platforms
        ]

    monkeypatch.setattr(platform_catalog, "lookup_platform_metadata", metadata_lookup)
    result = platform_catalog.refresh_missing_metadata(
        str(db_path),
        limit=None,
        delay_seconds=0,
        timeout=1,
    )
    assert result["selected_titles"] == 1
    assert len(captured) == 1
    _title, platforms, hints, timeout = captured[0]
    assert platforms == ("series", "kakao")
    assert timeout == 1
    assert hints == {
        "series": "9197875",
        "kakao": "69475645",
    }


def test_metadata_backfill_targets_missing_genre_or_tags_and_preserves_metrics(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    path = house / "태그 작품 1-10화.txt"
    path.write_text("fixture", encoding="utf-8")
    db_path = tmp_path / ".dedup_state" / "backfill.sqlite3"
    conn = decision_store.initialize_state_db(db_path)
    try:
        with decision_store.transaction(conn):
            decision_store.reconcile_file_metadata(conn, path, source="house")
        platform_catalog.sync_catalog_titles(conn)
        title_key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            title_key,
            [
                platform_catalog.PlatformStat("series", "ok", download_count=7),
                platform_catalog.PlatformStat("kakao", "ok", view_count=100),
                platform_catalog.PlatformStat(
                    "novelpia", "ok", view_count=200, recommend_count=20,
                    genre="판타지", tags=(),
                ),
            ],
        )
        targets = platform_catalog.select_metadata_backfill_targets(conn)
        assert len(targets) == 1
        assert targets[0].platforms == ("series", "kakao")
    finally:
        conn.close()

    calls = []

    def lookup(title, platforms, *, timeout):
        calls.append((title, tuple(platforms), timeout))
        values = {
            "series": platform_catalog.PlatformStat(
                "series", "ok", download_count=999, rating=9.9, genre="현판"
            ),
            "kakao": platform_catalog.PlatformStat(
                "kakao", "ok", view_count=999, rating=9.9,
                genre="판타지", tags=("게임", "성장", "게임"),
            ),
        }
        return [values[platform] for platform in platforms]

    result = platform_catalog.refresh_missing_metadata(
        str(db_path),
        limit=None,
        delay_seconds=0,
        timeout=1,
        lookup=lookup,
    )
    assert result["selected_titles"] == 1
    assert result["selected_platforms"] == 2
    assert result["outcome_counts"]["updated"] == 2
    assert len(calls) == 1

    conn = decision_store.connect_state_db(db_path)
    try:
        title_key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        series = conn.execute(
            "SELECT download_count, rating, genre, genre_collected_at "
            "FROM catalog_platform_stats WHERE title_key = ? AND platform = 'series'",
            (title_key,),
        ).fetchone()
        assert tuple(series[:3]) == (7, None, "현판")
        assert series["genre_collected_at"] is not None

        kakao = conn.execute(
            "SELECT view_count, rating, genre, genre_collected_at, tags_collected_at "
            "FROM catalog_platform_stats WHERE title_key = ? AND platform = 'kakao'",
            (title_key,),
        ).fetchone()
        assert tuple(kakao[:3]) == (100, None, "판타지")
        assert kakao["genre_collected_at"] is not None
        assert kakao["tags_collected_at"] is not None
        assert [
            item[0]
            for item in conn.execute(
                "SELECT tag FROM catalog_platform_tags "
                "WHERE title_key = ? AND platform = 'kakao' ORDER BY position",
                (title_key,),
            )
        ] == ["게임", "성장"]
        assert platform_catalog.select_metadata_backfill_targets(conn) == []
    finally:
        conn.close()

    listing = library_catalog.catalog_listing(db_path, search="태그 작품")
    [item] = listing["items"]
    assert item["platforms"]["series"]["genre"] == "현판"
    assert item["platforms"]["kakao"]["genre"] == "판타지"
    assert item["platforms"]["kakao"]["tags"] == ["게임", "성장"]
    assert item["platforms"]["novelpia"]["genre"] == "판타지"
    assert item["platforms"]["novelpia"]["tags"] == []
    assert item["platforms"]["series"].get("tags") is None


def test_ambiguous_rekey_discards_source_tags_instead_of_blessing_one(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    path = house / "통합 작품 1-10화.txt"
    path.write_text("fixture", encoding="utf-8")
    db_path = tmp_path / ".dedup_state" / "rekey.sqlite3"
    conn = decision_store.initialize_state_db(db_path)
    try:
        with decision_store.transaction(conn):
            decision_store.reconcile_file_metadata(conn, path, source="house")
            for old_key in ("통합작품1", "통합작품2"):
                conn.execute(
                    """
                    INSERT INTO catalog_titles(
                        title_key, display_title, query_title, normalizer_version
                    ) VALUES (?, ?, ?, 'old')
                    """,
                    (old_key, old_key, old_key),
                )
        for old_key, tag in (("통합작품1", "게임"), ("통합작품2", "방송")):
            platform_catalog.record_platform_stats(
                conn,
                old_key,
                [platform_catalog.PlatformStat(
                    "kakao", "ok", view_count=100, tags=(tag,)
                )],
            )
        with decision_store.transaction(conn):
            result = decision_store.migrate_catalog_title_keys(
                conn,
                (("통합작품1", "통합작품"), ("통합작품2", "통합작품")),
            )
        assert result["ambiguous_success_rows_discarded"] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM catalog_platform_stats "
            "WHERE title_key = '통합작품' AND platform = 'kakao'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM catalog_platform_tags"
        ).fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_existing_metric_refresh_updates_metadata_even_when_count_is_unchanged(tmp_path):
    _db_path, conn = _catalog_db(tmp_path)
    try:
        platform_catalog.record_platform_stats(
            conn,
            "작품",
            [platform_catalog.PlatformStat(
                "novelpia", "ok", view_count=100, recommend_count=10,
                genre="판타지", tags=("판타지",),
            )],
        )
        outcomes = platform_catalog.record_increased_platform_stats(
            conn,
            "작품",
            [platform_catalog.PlatformStat(
                "novelpia", "ok", view_count=100, recommend_count=10,
                genre="무협", tags=("판타지", "먼치킨"),
            )],
        )
        assert outcomes == {"novelpia": "unchanged"}
        assert _tags(conn, "novelpia") == ["판타지", "먼치킨"]
        assert conn.execute(
            "SELECT view_count, recommend_count, genre FROM catalog_platform_stats "
            "WHERE title_key = '작품' AND platform = 'novelpia'"
        ).fetchone()[:] == (100, 10, "무협")
    finally:
        conn.close()
