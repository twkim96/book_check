from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading

import pytest

import decision_store
import platform_catalog
import run_platform_catalog


def _make_db(tmp_path, *names):
    house = tmp_path / "house"
    house.mkdir()
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            for name in names:
                path = house / name
                path.write_text("synthetic catalog fixture", encoding="utf-8")
                decision_store.reconcile_file_metadata(conn, path, source="house")
    finally:
        conn.close()
    return state_db


def _identified_stat(platform, **values):
    remote_ids = {"series": "11", "kakao": "22", "novelpia": "33"}
    remote_id = str(values.pop("remote_id", remote_ids[platform]))
    remote_title = values.pop("remote_title", "합성작품")
    return platform_catalog.PlatformStat(
        platform,
        "ok",
        remote_id=remote_id,
        remote_title=remote_title,
        remote_url=platform_catalog._canonical_remote_url(platform, remote_id),
        **values,
    )


def _stat(platform):
    if platform == "series":
        return platform_catalog.PlatformStat(
            platform, "ok", remote_id="11", remote_title="합성작품",
            remote_url="https://series.naver.com/novel/detail.series?productNo=11",
            download_count=123, rating=9.1, genre="현판",
        )
    if platform == "kakao":
        return platform_catalog.PlatformStat(
            platform, "ok", remote_id="22", remote_title="합성작품",
            remote_url="https://page.kakao.com/content/22",
            view_count=456, rating=8.2, genre="판타지", tags=("성장",),
        )
    return platform_catalog.PlatformStat(
        platform, "ok", remote_id="33", remote_title="합성작품",
        remote_url="https://novelpia.com/novel/33",
        view_count=789, recommend_count=22, genre="판타지", tags=("성장",),
    )


def test_catalog_preview_keeps_legacy_schema_read_only(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    conn = decision_store.connect_state_db(state_db)
    try:
        conn.execute("DROP VIEW catalog_title_metrics")
        conn.execute("DROP TABLE catalog_platform_stats")
        conn.execute("DROP TABLE catalog_titles")
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
    finally:
        conn.close()

    preview = platform_catalog.preview_catalog_refresh(str(state_db), limit=1)

    assert preview["dry_run"] is True
    assert preview["selected_titles"] == 1
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'catalog_titles'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_catalog_keeps_six_platform_metrics_without_touching_files(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        sync = platform_catalog.sync_catalog_titles(conn)
        assert sync["discovered"] == 1
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn, key, [_stat("series"), _stat("kakao"), _stat("novelpia")]
        )
        row = conn.execute("SELECT * FROM catalog_title_metrics").fetchone()
        assert row["series_download_count"] == 123
        assert row["series_rating"] == 9.1
        assert row["kakao_view_count"] == 456
        assert row["kakao_rating"] == 8.2
        assert row["novelpia_view_count"] == 789
        assert row["novelpia_recommend_count"] == 22
    finally:
        conn.close()


def test_catalog_search_uses_readable_title_not_compact_core_for_tagged_post(tmp_path):
    state_db = _make_db(
        tmp_path,
        "[19禁완) 야설(근친) 작가로 살아가는 법 1-155 완 [ txt + epub ].txt",
    )
    conn = decision_store.initialize_state_db(state_db)
    try:
        [title] = platform_catalog.discover_catalog_titles(conn)
        assert title.title_key == "야설근친작가로살아가는법"
        assert title.display_title == "야설(근친) 작가로 살아가는 법"
        assert title.query_title == "야설(근친) 작가로 살아가는 법"
        assert platform_catalog.titles_match(
            title.query_title, "야설(근친) 작가로 살아가는 법"
        )
    finally:
        conn.close()


def test_file_metadata_rekey_preserves_success_and_drops_failed_lookup(tmp_path):
    state_db = _make_db(tmp_path, "최강 헌터의 자화상  1 125 완.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        file_id = conn.execute("SELECT file_id FROM file_analysis").fetchone()[0]
        old_key = "최강헌터의자화상1"
        new_key = "최강헌터의자화상"
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE file_analysis SET core_title = ?, normalizer_version = '1.2.1' "
                "WHERE file_id = ?",
                (old_key, file_id),
            )
            conn.execute(
                "INSERT INTO catalog_titles(title_key, display_title, query_title, normalizer_version) "
                "VALUES (?, '최강 헌터의 자화상 1', '최강 헌터의 자화상 1', '1.2.1')",
                (old_key,),
            )
        platform_catalog.record_platform_stats(
            conn,
            old_key,
            [
                _identified_stat(
                    "series", remote_title="최강 헌터의 자화상 1",
                    download_count=125_000, rating=9.8,
                ),
                platform_catalog.PlatformStat("kakao", "not_found"),
            ],
        )

        with decision_store.transaction(conn):
            result = decision_store.sync_active_file_analysis(conn)

        assert result["title_rekeys"] == {
            "requested": 1,
            "migrated": 1,
            "blocked_active_source": 0,
            "blocked_keys": [],
            "successful_rows_preserved": 1,
            "failed_rows_discarded": 1,
        }
        analysis = conn.execute(
            "SELECT a.core_title, a.readable_title, f.episode_start, f.episode_end "
            "FROM file_analysis AS a JOIN files AS f ON f.file_id = a.file_id "
            "WHERE a.file_id = ?",
            (file_id,),
        ).fetchone()
        assert tuple(analysis) == (new_key, "최강 헌터의 자화상", 1, 125)
        assert conn.execute(
            "SELECT COUNT(*) FROM catalog_titles WHERE title_key = ?", (old_key,)
        ).fetchone()[0] == 0
        series = conn.execute(
            "SELECT status, download_count, rating FROM catalog_platform_stats "
            "WHERE title_key = ? AND platform = 'series'",
            (new_key,),
        ).fetchone()
        assert tuple(series) == ("ok", 125_000, 9.8)
        assert conn.execute(
            "SELECT COUNT(*) FROM catalog_platform_stats "
            "WHERE title_key = ? AND platform = 'kakao'",
            (new_key,),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_catalog_refresh_only_requests_missing_platforms_and_waits_between_titles(tmp_path):
    state_db = _make_db(
        tmp_path,
        "합성작품가 1-20화.txt",
        "합성작품나 1-20화.txt",
    )
    calls = []
    waits = []
    progress = []

    def lookup(_title, platforms, *, timeout):
        calls.append(tuple(platforms))
        return [_stat(platform) for platform in platforms]

    result = platform_catalog.refresh_catalog(
        str(state_db),
        limit=2,
        delay_seconds=3,
        lookup=lookup,
        sleep=waits.append,
        progress=progress.append,
        now=lambda: datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    assert result["selected_titles"] == 2
    assert calls == [("series", "kakao", "novelpia"), ("series", "kakao", "novelpia")]
    assert waits == [3]
    assert [event["phase"] for event in progress] == [
        "sync_start", "start", "progress", "progress"
    ]
    assert progress[-1]["completed_titles"] == 2
    assert progress[-1]["status_counts"] == {
        "ok": 6, "not_found": 0, "error": 0, "skipped": 0
    }

    second = platform_catalog.refresh_catalog(
        str(state_db),
        limit=2,
        lookup=lookup,
        sleep=waits.append,
    )
    assert second["selected_titles"] == 0
    assert len(calls) == 2


def test_catalog_refresh_completes_metadata_only_for_rows_touched_in_same_run(tmp_path):
    state_db = _make_db(tmp_path, "통합 수집 작품 1-20화.txt")
    public_calls = []
    metadata_calls = []

    def public_lookup(_title, platforms, *, timeout):
        public_calls.append(tuple(platforms))
        values = {
            "series": platform_catalog.PlatformStat(
                "series", "ok", remote_id="11", remote_title="통합 수집 작품",
                remote_url="https://series.naver.com/novel/detail.series?productNo=11",
                download_count=123,
            ),
            "kakao": platform_catalog.PlatformStat("kakao", "not_found"),
            "novelpia": platform_catalog.PlatformStat("novelpia", "not_found"),
        }
        return [values[platform] for platform in platforms]

    def metadata_lookup(_title, platforms, *, timeout):
        metadata_calls.append(tuple(platforms))
        return [platform_catalog.PlatformStat(
            "series", "ok", remote_id="11", remote_title="통합 수집 작품",
            remote_url="https://series.naver.com/novel/detail.series?productNo=11",
            genre="현판",
        ) for _platform in platforms]

    result = platform_catalog.refresh_catalog(
        str(state_db),
        limit=None,
        delay_seconds=0,
        timeout=1,
        lookup=public_lookup,
        metadata_lookup=metadata_lookup,
    )

    assert public_calls == [("series", "kakao", "novelpia")]
    assert metadata_calls == [("series",)]
    assert result["metadata_completion"]["selected_platforms"] == 1
    assert result["metadata_completion"]["outcome_counts"]["updated"] == 1
    conn = decision_store.connect_state_db(state_db)
    try:
        row = conn.execute(
            "SELECT status, download_count, genre, genre_collected_at "
            "FROM catalog_platform_stats WHERE platform = 'series'"
        ).fetchone()
        assert tuple(row[:3]) == ("ok", 123, "현판")
        assert row["genre_collected_at"] is not None
    finally:
        conn.close()

    second = platform_catalog.refresh_catalog(
        str(state_db),
        limit=None,
        delay_seconds=0,
        timeout=1,
        lookup=public_lookup,
        metadata_lookup=metadata_lookup,
    )
    assert second["selected_titles"] == 0
    assert len(metadata_calls) == 1


def test_catalog_refresh_does_not_sweep_historical_metadata_residuals(tmp_path):
    state_db = _make_db(tmp_path, "기존 누락 작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            key,
            [platform_catalog.PlatformStat(
                "series", "ok", remote_id="11", remote_title="기존 누락 작품",
                remote_url="https://series.naver.com/novel/detail.series?productNo=11",
                download_count=100,
            )],
        )
    finally:
        conn.close()

    metadata_calls = []

    def public_lookup(_title, platforms, *, timeout):
        return [platform_catalog.PlatformStat(platform, "not_found") for platform in platforms]

    def metadata_lookup(_title, platforms, *, timeout):
        metadata_calls.append(tuple(platforms))
        raise AssertionError("historical metadata residual must not be swept")

    result = platform_catalog.refresh_catalog(
        str(state_db),
        limit=None,
        delay_seconds=0,
        timeout=1,
        lookup=public_lookup,
        metadata_lookup=metadata_lookup,
    )
    assert result["selected_platforms"] == 2
    assert result["metadata_completion"]["selected_platforms"] == 0
    assert metadata_calls == []


def test_invalidate_platform_identity_clears_wrong_remote_object_with_cas(tmp_path):
    state_db = _make_db(tmp_path, "오매칭 작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            key,
            [platform_catalog.PlatformStat(
                "kakao", "ok", remote_id="57589258", remote_title="다른 작품",
                remote_url="https://page.kakao.com/content/57589258",
                view_count=839048, rating=9.4, rating_count=3431,
                genre="판타지", tags=("오매칭",),
            )],
        )
        result = platform_catalog.invalidate_platform_identity(
            conn,
            key,
            "kakao",
            expected_remote_id="57589258",
            expected_remote_title="다른 작품",
            reason="verified wrong remote object",
            now=datetime(2026, 8, 16, tzinfo=timezone.utc),
        )
        assert result["before"]["view_count"] == 839048
        row = conn.execute(
            "SELECT * FROM catalog_platform_stats "
            "WHERE title_key = ? AND platform = 'kakao'",
            (key,),
        ).fetchone()
        assert row["status"] == "not_found"
        assert row["remote_id"] is None
        assert row["remote_title"] is None
        assert row["view_count"] is None
        assert row["rating"] is None
        assert row["genre"] is None
        assert row["error_message"] == "verified wrong remote object"
        assert conn.execute(
            "SELECT COUNT(*) FROM catalog_platform_tags "
            "WHERE title_key = ? AND platform = 'kakao'",
            (key,),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_invalidate_platform_identity_rejects_stale_remote_id(tmp_path):
    state_db = _make_db(tmp_path, "CAS 오매칭 작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            key,
            [platform_catalog.PlatformStat(
                "kakao", "ok", remote_id="current", remote_title="현재 작품",
                remote_url="https://page.kakao.com/content/current",
                view_count=10,
            )],
        )
        with pytest.raises(RuntimeError, match="changed before invalidation"):
            platform_catalog.invalidate_platform_identity(
                conn,
                key,
                "kakao",
                expected_remote_id="stale",
                reason="must not apply",
            )
        row = conn.execute(
            "SELECT status, remote_id, view_count FROM catalog_platform_stats "
            "WHERE title_key = ? AND platform = 'kakao'",
            (key,),
        ).fetchone()
        assert tuple(row) == ("ok", "current", 10)
    finally:
        conn.close()


def test_existing_metric_refresh_selects_only_successful_platforms_with_counts(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            key,
            [
                _identified_stat("series", rating=9.0),
                _identified_stat("kakao", view_count=456, rating=8.2),
                platform_catalog.PlatformStat(
                    "novelpia", "not_found"
                ),
            ],
        )
        targets = platform_catalog.select_existing_metric_targets(conn)
        assert len(targets) == 1
        assert targets[0].platforms == ("kakao",)
    finally:
        conn.close()


def test_existing_metric_update_is_monotonic_and_rating_follows_growth(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            key,
            [
                _identified_stat(
                    "series", download_count=100, rating=9.0, rating_count=20,
                ),
                _identified_stat(
                    "novelpia", view_count=1000, recommend_count=100,
                ),
            ],
        )
        outcomes = platform_catalog.record_increased_platform_stats(
            conn,
            key,
            [
                _identified_stat(
                    "series", download_count=120, rating=8.7, rating_count=18,
                ),
                _identified_stat(
                    "novelpia", view_count=1100, recommend_count=95,
                ),
            ],
        )
        assert outcomes == {"series": "updated", "novelpia": "updated"}
        rows = {
            row["platform"]: row
            for row in conn.execute("SELECT * FROM catalog_platform_stats")
        }
        assert rows["series"]["download_count"] == 120
        assert rows["series"]["rating"] == 8.7
        assert rows["series"]["rating_count"] == 20
        assert rows["novelpia"]["view_count"] == 1100
        assert rows["novelpia"]["recommend_count"] == 100

        outcomes = platform_catalog.record_increased_platform_stats(
            conn,
            key,
            [
                _identified_stat("series", download_count=119, rating=9.9),
                platform_catalog.PlatformStat(
                    "novelpia", "error", message="temporary"
                ),
            ],
        )
        assert outcomes == {"series": "unchanged", "novelpia": "error"}
        rows = {
            row["platform"]: row
            for row in conn.execute("SELECT * FROM catalog_platform_stats")
        }
        assert rows["series"]["download_count"] == 120
        assert rows["series"]["rating"] == 8.7
        assert rows["novelpia"]["status"] == "ok"
    finally:
        conn.close()


def test_existing_metric_update_serializes_concurrent_writers(tmp_path):
    state_db = _make_db(tmp_path, "동시 갱신 작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            key,
            [_identified_stat("series", download_count=100)],
        )
    finally:
        conn.close()

    barrier = threading.Barrier(3)
    errors = []

    def update(value):
        worker = decision_store.connect_state_db(state_db)
        try:
            barrier.wait()
            platform_catalog.record_increased_platform_stats(
                worker,
                key,
                [_identified_stat("series", download_count=value)],
            )
        except Exception as exc:
            errors.append(exc)
        finally:
            worker.close()

    threads = [
        threading.Thread(target=update, args=(110,)),
        threading.Thread(target=update, args=(120,)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)
    assert not errors
    assert all(not thread.is_alive() for thread in threads)

    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute(
            "SELECT download_count FROM catalog_platform_stats "
            "WHERE title_key = ? AND platform = 'series'",
            (key,),
        ).fetchone()[0] == 120
    finally:
        conn.close()


def test_existing_metric_refresh_queries_only_present_platforms_and_auth_fallback(tmp_path):
    state_db = _make_db(tmp_path, "성인 합성작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            key,
            [
                _identified_stat("kakao", view_count=100, rating=8.0),
                _identified_stat("novelpia", view_count=200, recommend_count=20),
            ],
        )
    finally:
        conn.close()

    calls = []
    auth_calls = []

    def lookup(_title, platforms, *, timeout):
        calls.append(tuple(platforms))
        return [
            _identified_stat(
                "kakao", remote_title="성인 합성작품", view_count=150, rating=8.3
            ),
            platform_catalog.PlatformStat("novelpia", "not_found"),
        ]

    def authenticated(title, *, timeout):
        auth_calls.append(title)
        return _identified_stat(
            "novelpia", remote_title="성인 합성작품",
            view_count=250, recommend_count=25,
        )

    result = platform_catalog.refresh_existing_metrics(
        str(state_db),
        delay_seconds=0,
        lookup=lookup,
        authenticated_novelpia_lookup=authenticated,
    )
    assert calls == [("kakao", "novelpia")]
    assert auth_calls == ["성인 합성작품"]
    assert result["outcome_counts"]["updated"] == 2


def test_refresh_uses_authenticated_novelpia_only_after_three_public_misses(tmp_path):
    state_db = _make_db(tmp_path, "성인 합성작품 1-20화.txt")
    authenticated_calls = []

    def public_lookup(_title, platforms, *, timeout):
        return [
            platform_catalog.PlatformStat(platform, "not_found")
            for platform in platforms
        ]

    def authenticated_lookup(title, *, timeout):
        authenticated_calls.append((title, timeout))
        return platform_catalog.PlatformStat(
            "novelpia", "ok", remote_id="64741",
            remote_title=title, view_count=1869045, recommend_count=95029,
        )

    result = platform_catalog.refresh_catalog(
        str(state_db),
        limit=None,
        delay_seconds=0,
        lookup=public_lookup,
        authenticated_novelpia_lookup=authenticated_lookup,
        now=lambda: datetime(2026, 7, 17, tzinfo=timezone.utc),
    )
    assert authenticated_calls == [("성인 합성작품", 10.0)]
    assert result["authenticated_novelpia_attempts"] == 1
    assert result["authenticated_novelpia_status_counts"]["ok"] == 1
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        statuses = {
            row["platform"]: row["status"]
            for row in conn.execute("SELECT platform, status FROM catalog_platform_stats")
        }
        assert statuses == {
            "series": "not_found",
            "kakao": "not_found",
            "novelpia": "ok",
        }
    finally:
        conn.close()


def test_refresh_skips_authenticated_novelpia_when_public_pair_has_a_match(tmp_path):
    state_db = _make_db(tmp_path, "일반 합성작품 1-20화.txt")
    authenticated_calls = []

    def public_lookup(_title, platforms, *, timeout):
        return [
            _stat(platform) if platform == "series"
            else platform_catalog.PlatformStat(platform, "not_found")
            for platform in platforms
        ]

    platform_catalog.refresh_catalog(
        str(state_db),
        limit=None,
        delay_seconds=0,
        lookup=public_lookup,
        authenticated_novelpia_lookup=lambda *args, **kwargs: authenticated_calls.append(args),
    )
    assert authenticated_calls == []


def test_authenticated_novelpia_target_cutoff_makes_retry_resumable(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    first = datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc)
    second = first + timedelta(minutes=1)
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            key,
            [
                platform_catalog.PlatformStat(platform, "not_found")
                for platform in platform_catalog.PLATFORMS
            ],
            now=first,
        )
        assert len(platform_catalog.select_authenticated_novelpia_targets(
            conn, attempted_before=first
        )) == 1
        platform_catalog.record_platform_stats(
            conn,
            key,
            [platform_catalog.PlatformStat("novelpia", "not_found")],
            now=second,
        )
        assert platform_catalog.select_authenticated_novelpia_targets(
            conn, attempted_before=first
        ) == []
    finally:
        conn.close()


def test_authenticated_novelpia_environment_is_all_or_nothing():
    assert platform_catalog.AuthenticatedNovelpiaClient.from_environment(
        environ={}, required=False
    ) is None
    with pytest.raises(platform_catalog.NovelpiaAuthenticationError, match="must both"):
        platform_catalog.AuthenticatedNovelpiaClient.from_environment(
            environ={platform_catalog.NOVELPIA_EMAIL_ENV: "reader@example.com"}
        )


def test_authenticated_novelpia_login_enables_adult_mode_and_drops_password(monkeypatch):
    client = platform_catalog.AuthenticatedNovelpiaClient(
        "reader@example.com", "secret-password"
    )
    requests = []

    def request_text(url, *, data=None):
        requests.append((url, data))
        if url == "https://novelpia.com/":
            return 'mem_no : "12345"'
        if url.endswith("/proc/member_adt_mode"):
            return "OK"
        return ""

    monkeypatch.setattr(client, "_request_text", request_text)
    monkeypatch.setattr(
        client,
        "_request_json",
        lambda _url, **_kwargs: {"status": 200, "result": False},
    )
    client.login()
    assert client._logged_in is True
    assert client._email == ""
    assert client._password == ""
    login_request = next(item for item in requests if item[0].endswith("/proc/login"))
    assert b"reader%40example.com" in login_request[1]
    assert b"secret-password" in login_request[1]
    assert any(item[0].endswith("/proc/member_adt_mode") for item in requests)


def test_authenticated_novelpia_captcha_fails_closed_and_drops_password(monkeypatch):
    client = platform_catalog.AuthenticatedNovelpiaClient(
        "reader@example.com", "secret-password"
    )
    monkeypatch.setattr(client, "_request_text", lambda _url, **_kwargs: "")
    monkeypatch.setattr(
        client,
        "_request_json",
        lambda _url, **_kwargs: {"status": 200, "result": True},
    )
    with pytest.raises(platform_catalog.NovelpiaAuthenticationError, match="CAPTCHA"):
        client.login()
    assert client._email == ""
    assert client._password == ""


def test_authenticated_novelpia_not_found_verifies_once_per_twenty(monkeypatch):
    client = platform_catalog.AuthenticatedNovelpiaClient(
        "reader@example.com", "secret-password"
    )
    client._logged_in = True
    checks = []
    monkeypatch.setattr(
        client,
        "_lookup_once",
        lambda _title, **_kwargs: platform_catalog.PlatformStat(
            "novelpia", "not_found"
        ),
    )
    monkeypatch.setattr(client, "verify_session", lambda: checks.append("check"))

    results = client.lookup_batch([f"작품 {index}" for index in range(45)])
    assert len(results) == 45
    assert all(result.status == "not_found" for result in results)
    assert checks == ["check", "check", "check"]


def test_authenticated_novelpia_metadata_batch_reads_stored_ids_without_search(
    monkeypatch,
):
    client = platform_catalog.AuthenticatedNovelpiaClient(
        "reader@example.com", "secret-password"
    )
    client._logged_in = True
    urls = []
    checks = []

    def fetch_text(url, timeout):
        urls.append((url, timeout))
        return """
        <p class="writer-tag">
          <span class="tag">#현대판타지</span>
          <span class="tag">#범죄</span>
        </p>
        """

    monkeypatch.setattr(client, "fetch_text", fetch_text)
    monkeypatch.setattr(client, "verify_session", lambda: checks.append("check"))
    monkeypatch.setattr(
        client,
        "_lookup_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("title search must not be used for stored-ID metadata")
        ),
    )

    [result] = client.lookup_metadata_batch([
        ("암흑가 보스요？ 제가요？", "384111", "암흑가 보스요? 제가요?"),
    ], timeout=3)

    assert urls == [("https://novelpia.com/novel/384111", 3)]
    assert checks == ["check"]
    assert result.status == "ok"
    assert result.remote_id == "384111"
    assert result.remote_title == "암흑가 보스요? 제가요?"
    assert result.genre == "현대판타지"
    assert result.tags == ("현대판타지", "범죄")
    assert result.metadata_lookup_mode == "authenticated"


def test_authenticated_novelpia_expired_chunk_relogs_and_retries(monkeypatch):
    environ = {
        platform_catalog.NOVELPIA_EMAIL_ENV: "reader@example.com",
        platform_catalog.NOVELPIA_PASSWORD_ENV: "secret-password",
    }
    client = platform_catalog.AuthenticatedNovelpiaClient.from_environment(
        environ=environ,
        required=True,
    )
    login_payloads = []
    session_results = iter(["OK", "login", "OK", "OK"])

    def request_text(url, *, data=None):
        if url.endswith("/proc/login"):
            login_payloads.append(data)
            return ""
        if url.endswith("/proc/member_adt_mode"):
            return next(session_results)
        return ""

    attempts = []
    monkeypatch.setattr(client, "_request_text", request_text)
    monkeypatch.setattr(
        client,
        "_request_json",
        lambda _url, **_kwargs: {"status": 200, "result": False},
    )
    monkeypatch.setattr(
        client,
        "_lookup_once",
        lambda title, **_kwargs: (
            attempts.append(title)
            or platform_catalog.PlatformStat("novelpia", "not_found")
        ),
    )

    results = client.lookup_batch(["작품 하나", "작품 둘"])
    assert [result.status for result in results] == ["not_found", "not_found"]
    assert attempts == ["작품 하나", "작품 둘", "작품 하나", "작품 둘"]
    assert len(login_payloads) == 2
    assert all(b"secret-password" in payload for payload in login_payloads)
    assert client.relogin_count == 1


def test_authenticated_chunk_failure_writes_no_unverified_result(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    attempted_at = datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc)
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            key,
            [
                platform_catalog.PlatformStat(platform, "not_found")
                for platform in platform_catalog.PLATFORMS
            ],
            now=attempted_at,
        )
    finally:
        conn.close()

    class ExpiredClient:
        def login(self):
            return None

        def lookup_batch(self, *_args, **_kwargs):
            raise platform_catalog.NovelpiaAuthenticationError(
                "session verification failed"
            )

    with pytest.raises(platform_catalog.NovelpiaAuthenticationError):
        platform_catalog.refresh_authenticated_novelpia(
            str(state_db),
            ExpiredClient(),
            attempted_before=attempted_at,
            delay_seconds=0,
        )
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        row = conn.execute(
            "SELECT last_attempt_at FROM catalog_platform_stats "
            "WHERE title_key = ? AND platform = 'novelpia'",
            (key,),
        ).fetchone()
        assert platform_catalog._parse_time(row["last_attempt_at"]) == attempted_at
    finally:
        conn.close()


def test_regular_refresh_buffers_public_misses_until_auth_batch_is_verified(tmp_path):
    state_db = _make_db(
        tmp_path,
        "합성작품가 1-20화.txt",
        "합성작품나 1-20화.txt",
    )

    def public_lookup(_title, platforms, *, timeout):
        return [
            platform_catalog.PlatformStat(platform, "not_found")
            for platform in platforms
        ]

    class UnverifiedBatch:
        def lookup(self, *_args, **_kwargs):
            raise AssertionError("per-title authenticated lookup must not be used")

        def lookup_batch(self, titles, **_kwargs):
            assert len(titles) == 2
            raise platform_catalog.NovelpiaAuthenticationError(
                "session verification failed"
            )

    client = UnverifiedBatch()
    with pytest.raises(platform_catalog.NovelpiaAuthenticationError):
        platform_catalog.refresh_catalog(
            str(state_db),
            limit=None,
            delay_seconds=0,
            lookup=public_lookup,
            authenticated_novelpia_lookup=client.lookup,
        )
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM catalog_platform_stats"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_control_entry_progress_reporter_prints_start_and_periodic_updates(capsys):
    report = run_platform_catalog._progress_reporter()
    report({"phase": "sync_start"})
    report({
        "phase": "start",
        "discovered_titles": 100,
        "selected_titles": 30,
        "selected_platforms": 90,
    })
    for completed in (1, 2, 10, 30):
        report({
            "phase": "progress",
            "completed_titles": completed,
            "selected_titles": 30,
            "completed_platforms": completed * 3,
            "selected_platforms": 90,
            "status_counts": {
                "ok": completed * 2,
                "not_found": completed,
                "error": 0,
                "skipped": 0,
            },
        })
    output = capsys.readouterr().out
    assert "제목 동기화 시작" in output
    assert "이번 대상 30개 / 플랫폼 90건" in output
    assert "진행 1/30" in output
    assert "진행 10/30" in output
    assert "진행 30/30" in output
    assert "진행 2/30" not in output


def test_catalog_query_keeps_readable_title_instead_of_compact_key(tmp_path):
    state_db = _make_db(tmp_path, "합성 띄어쓰기 작품 1-20화 완 @가상작가.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        title = platform_catalog.discover_catalog_titles(conn)[0]
        assert title.title_key == "합성띄어쓰기작품"
        assert title.query_title == "합성 띄어쓰기 작품"
    finally:
        conn.close()


def test_catalog_query_preserves_main_and_subtitle_while_bucket_key_stays_compatible(tmp_path):
    state_db = _make_db(tmp_path, "합성 메인 제목: 충분히 긴 부제목 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        title = platform_catalog.discover_catalog_titles(conn)[0]
        assert title.title_key == "충분히긴부제목"
        assert title.query_title == "합성 메인 제목: 충분히 긴 부제목"
        assert platform_catalog.titles_match(
            title.query_title, "합성 메인 제목: 충분히 긴 부제목"
        )
    finally:
        conn.close()


def test_platform_title_match_strips_only_presentation_suffixes():
    title = "합성 메인 제목: 충분히 긴 부제목"
    assert platform_catalog.titles_match(
        title,
        f"{title} [단행본] (총 55권/미완결)",
    )
    assert platform_catalog.titles_match(title, f"{title} [독점] (총 100화/완결)")
    assert platform_catalog.titles_match(title, f"{title} [미니노블]")
    assert platform_catalog.titles_match(
        "9이닝 야구의 찬가", "9이닝 : 야구의 찬가"
    )
    assert platform_catalog.titles_match(
        "기프티드 GIFTED", "기프티드 (GIFTED)"
    )
    assert platform_catalog.titles_match(
        "각성수선전覺醒修仙傳", "각성수선전(覺醒修仙傳)"
    )
    assert not platform_catalog.titles_match(title, f"{title} 외전")
    assert not platform_catalog.titles_match("어게인1997", "어게인")
    assert not platform_catalog.titles_match(
        "합성 메인 A: 같은 부제목",
        "합성 메인 B: 같은 부제목 [독점]",
    )


def test_series_discontinued_detail_is_unavailable_not_identity_conflict():
    page = "<html><head><title>네이버 시리즈2 : 판매중지상품안내</title></head></html>"
    [stat] = platform_catalog.lookup_platform_identities(
        "판매중지 작품",
        ("series",),
        remote_ids={"series": "11"},
        fetch_text=lambda _url, _timeout: page,
        timeout=1,
    )
    assert stat.status == "error"
    assert stat.metadata_lookup_mode == "direct_unavailable"
    assert "unavailable" in stat.message.lower()


def test_changed_catalog_query_retries_not_found_but_preserves_success(tmp_path):
    state_db = _make_db(tmp_path, "합성 메인 제목: 충분히 긴 부제목 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            key,
            [
                _identified_stat(
                    "series", remote_title="합성 메인 제목: 충분히 긴 부제목",
                    rating=9.0,
                ),
                platform_catalog.PlatformStat("kakao", "not_found"),
                platform_catalog.PlatformStat("novelpia", "not_found"),
            ],
        )
        conn.execute(
            "UPDATE catalog_titles SET query_title = ? WHERE title_key = ?",
            ("충분히 긴 부제목", key),
        )
        conn.commit()

        platform_catalog.sync_catalog_titles(conn)
        rows = conn.execute(
            "SELECT platform, status FROM catalog_platform_stats ORDER BY platform"
        ).fetchall()
        assert [tuple(row) for row in rows] == [("series", "ok")]
        target = platform_catalog.select_refresh_targets(conn)[0]
        assert target.platforms == ("kakao", "novelpia")
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("stats", "expected"),
    [
        (("ok", "not_found", "error"), ("kakao",)),
        (("not_found", "ok", "error"), ("series",)),
        (("not_found", "error", "ok"), ()),
        (("not_found", "error", "not_found"), platform_catalog.PLATFORMS),
    ],
)
def test_failed_retry_obeys_commercial_pair_and_novelpia_only_rule(
    tmp_path, stats, expected
):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            key,
            [
                _stat(platform)
                if status == "ok"
                else platform_catalog.PlatformStat(platform, status, message="failed")
                for platform, status in zip(platform_catalog.PLATFORMS, stats)
            ],
        )
        targets = platform_catalog.select_refresh_targets(
            conn,
            limit=None,
            failed_retry=True,
        )
        assert [target.platforms for target in targets] == ([expected] if expected else [])
    finally:
        conn.close()


def test_regular_refresh_never_retries_recorded_failures_but_failed_action_can(tmp_path):
    state_db = _make_db(
        tmp_path,
        "합성작품가 1-20화.txt",
        "합성작품나 1-20화.txt",
    )
    recorded_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    much_later = recorded_at + timedelta(days=365)
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        keys = [row[0] for row in conn.execute("SELECT title_key FROM catalog_titles ORDER BY title_key")]
        platform_catalog.record_platform_stats(
            conn,
            keys[0],
            [
                _stat("series"),
                platform_catalog.PlatformStat("kakao", "not_found"),
                platform_catalog.PlatformStat("novelpia", "error", message="temporary"),
            ],
            now=recorded_at,
            error_retry_seconds=1,
        )
        regular_targets = platform_catalog.select_refresh_targets(
            conn,
            now=much_later,
        )
        assert len(regular_targets) == 1
        assert regular_targets[0].title.title_key == keys[1]
        assert regular_targets[0].platforms == platform_catalog.PLATFORMS
        targets = platform_catalog.select_refresh_targets(
            conn,
            limit=None,
            now=much_later,
            failed_retry=True,
        )
        assert len(targets) == 1
        assert targets[0].title.title_key == keys[0]
        assert targets[0].platforms == ("kakao",)
    finally:
        conn.close()


def test_failed_retry_is_reusable_when_a_platform_still_fails(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            key,
            [platform_catalog.PlatformStat("series", "not_found")],
            now=datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc),
        )
        cutoff = datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc)
        first = platform_catalog.select_refresh_targets(
            conn,
            limit=None,
            failed_retry=True,
            failure_retry_cutoff=cutoff,
        )
        assert first[0].platforms == ("series",)
        platform_catalog.record_platform_stats(
            conn,
            key,
            [platform_catalog.PlatformStat("series", "not_found")],
            now=cutoff + timedelta(minutes=1),
        )
        assert platform_catalog.select_refresh_targets(
            conn,
            limit=None,
            failed_retry=True,
            failure_retry_cutoff=cutoff,
        ) == []
        second = platform_catalog.select_refresh_targets(
            conn, limit=None, failed_retry=True
        )
        assert second[0].platforms == ("series",)
    finally:
        conn.close()


def test_failed_retry_cycle_resumes_active_then_starts_a_new_cycle(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    first = run_platform_catalog._failed_retry_state(str(state_db), create=True)
    assert first["state"] == "active"
    assert run_platform_catalog._failed_retry_state(
        str(state_db), create=True
    ) == first

    run_platform_catalog._complete_failed_retry(
        str(state_db),
        first,
        {"selected_titles": 1, "selected_platforms": 2},
    )
    completed = run_platform_catalog._failed_retry_state(
        str(state_db), create=False
    )
    assert completed["state"] == "completed"
    second = run_platform_catalog._failed_retry_state(str(state_db), create=True)
    assert second["state"] == "active"
    assert second["cycle"] == first["cycle"] + 1


def test_authenticated_novelpia_retry_state_is_resumable_then_completed(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    active = run_platform_catalog._novelpia_auth_retry_state(
        str(state_db), create=True
    )
    assert active["state"] == "active"
    assert run_platform_catalog._novelpia_auth_retry_state(
        str(state_db), create=False
    )["cutoff"] == active["cutoff"]
    run_platform_catalog._complete_novelpia_auth_retry(
        str(state_db),
        active["cutoff"],
        {"selected_titles": 3, "selected_platforms": 3},
    )
    completed = run_platform_catalog._novelpia_auth_retry_state(
        str(state_db), create=False
    )
    assert completed["state"] == "completed"
    assert completed["selected_titles"] == 3


def test_authenticated_novelpia_retry_dry_run_needs_no_credentials(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            key,
            [
                platform_catalog.PlatformStat(platform, "not_found")
                for platform in platform_catalog.PLATFORMS
            ],
        )
    finally:
        conn.close()
    args = run_platform_catalog.build_parser().parse_args([
        "--state-db", str(state_db), "retry-novelpia-auth", "--dry-run"
    ])
    _backup, result = run_platform_catalog.retry_novelpia_auth(args)
    assert result["dry_run"] is True
    assert result["selected_titles"] == 1


def test_metadata_consistency_cli_dry_run_selects_successful_series_kakao(tmp_path):
    state_db = _make_db(tmp_path, "ID 감사 작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            key,
            [
                platform_catalog.PlatformStat(
                    "series", "ok", remote_id="11", remote_title="ID 감사 작품",
                    remote_url="https://series.naver.com/novel/detail.series?productNo=11",
                    download_count=10,
                ),
                platform_catalog.PlatformStat(
                    "kakao", "ok", remote_id="22", remote_title="ID 감사 작품",
                    remote_url="https://page.kakao.com/content/22", view_count=20,
                ),
                platform_catalog.PlatformStat(
                    "novelpia", "ok", remote_id="33", remote_title="ID 감사 작품",
                    remote_url="https://novelpia.com/novel/33", view_count=30,
                ),
            ],
        )
    finally:
        conn.close()

    args = run_platform_catalog.build_parser().parse_args([
        "--state-db", str(state_db),
        "revalidate-metadata-consistency", "--all", "--dry-run",
    ])
    result = run_platform_catalog.run(args)
    assert result["dry_run"] is True
    assert result["selected_titles"] == 1
    assert result["selected_platforms"] == 2


def test_plain_initializer_refuses_to_migrate_an_existing_old_schema(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    conn = decision_store.connect_state_db(state_db)
    try:
        conn.execute("DROP VIEW catalog_title_metrics")
        conn.execute("DROP TABLE catalog_platform_stats")
        conn.execute("DROP TABLE catalog_titles")
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="migration required"):
        decision_store.initialize_state_db(state_db)

    readonly = decision_store.connect_state_db_readonly(state_db)
    try:
        assert readonly.execute("PRAGMA user_version").fetchone()[0] == 7
        assert readonly.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'catalog_titles'"
        ).fetchone()[0] == 0
    finally:
        readonly.close()


def test_platform_entry_backs_up_before_explicit_schema_migration(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    conn = decision_store.connect_state_db(state_db)
    try:
        conn.execute("DROP VIEW catalog_title_metrics")
        conn.execute("DROP TABLE catalog_platform_stats")
        conn.execute("DROP TABLE catalog_titles")
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
    finally:
        conn.close()

    backup = run_platform_catalog.ensure_catalog_schema(str(state_db))
    assert backup is not None and backup.is_file()
    before = decision_store.connect_state_db_readonly(backup)
    try:
        assert before.execute("PRAGMA user_version").fetchone()[0] == 7
        assert before.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        before.close()
    current = decision_store.initialize_state_db(state_db)
    try:
        assert current.execute("PRAGMA user_version").fetchone()[0] == decision_store.SCHEMA_VERSION
        assert current.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'file_analysis'"
        ).fetchone()[0] == 1
    finally:
        current.close()


def test_v9_migration_and_file_metadata_sync_backfill_active_house_files(tmp_path):
    state_db = _make_db(tmp_path, "합성 메인 제목: 부제목 1-20화.txt")
    conn = decision_store.connect_state_db(state_db)
    try:
        conn.execute("DROP TABLE file_analysis")
        conn.execute("PRAGMA user_version = 9")
        conn.commit()
    finally:
        conn.close()

    backup, result = run_platform_catalog.sync_file_metadata(str(state_db))
    assert backup is not None and backup.is_file()
    assert result == {"total": 1, "changed": 1, "unchanged": 0}
    before = decision_store.connect_state_db_readonly(backup)
    try:
        assert before.execute("PRAGMA user_version").fetchone()[0] == 9
        assert before.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'file_analysis'"
        ).fetchone()[0] == 0
    finally:
        before.close()
    current = decision_store.connect_state_db_readonly(state_db)
    try:
        row = current.execute("SELECT * FROM file_analysis").fetchone()
        assert row["core_title"] == "부제목"
        assert row["catalog_query_title"] == "합성 메인 제목: 부제목"
        assert current.execute("PRAGMA user_version").fetchone()[0] == decision_store.SCHEMA_VERSION
    finally:
        current.close()


def test_v10_migration_adds_title_override_column_only_with_explicit_permission(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    conn = decision_store.connect_state_db(state_db)
    try:
        conn.execute("ALTER TABLE file_analysis DROP COLUMN title_override_json")
        conn.execute("PRAGMA user_version = 10")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="migration required"):
        decision_store.initialize_state_db(state_db)

    migrated = decision_store.initialize_state_db(state_db, migrate=True)
    try:
        columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(file_analysis)")
        }
        assert "title_override_json" in columns
        assert (
            migrated.execute("PRAGMA user_version").fetchone()[0]
            == decision_store.SCHEMA_VERSION
        )
    finally:
        migrated.close()


def test_catalog_title_discovery_reads_file_analysis_without_reparsing(tmp_path, monkeypatch):
    state_db = _make_db(tmp_path, "합성 띄어쓰기 작품 1-20화.txt")
    monkeypatch.setattr(
        "normalizer.analyze_name",
        lambda _name: (_ for _ in ()).throw(AssertionError("unexpected filename parse")),
    )
    conn = decision_store.initialize_state_db(state_db)
    try:
        title = platform_catalog.discover_catalog_titles(conn)[0]
        assert title.title_key == "합성띄어쓰기작품"
        assert title.query_title == "합성 띄어쓰기 작품"
    finally:
        conn.close()


def test_catalog_title_discovery_fails_closed_when_file_analysis_is_missing(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        conn.execute("DELETE FROM file_analysis")
        conn.commit()
        with pytest.raises(RuntimeError, match="file metadata sync required"):
            platform_catalog.discover_catalog_titles(conn)
    finally:
        conn.close()


def test_v8_download_values_are_preserved_by_v9_migration(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn, key, [_identified_stat("series", download_count=321)]
        )
        conn.execute("UPDATE catalog_platform_stats SET interest_count = download_count")
        conn.execute("DROP VIEW catalog_title_metrics")
        conn.execute("ALTER TABLE catalog_platform_stats DROP COLUMN download_count")
        conn.execute("PRAGMA user_version = 8")
        conn.commit()
    finally:
        conn.close()

    backup = run_platform_catalog.ensure_catalog_schema(str(state_db))
    assert backup is not None and backup.is_file()
    current = decision_store.initialize_state_db(state_db)
    try:
        row = current.execute("SELECT * FROM catalog_title_metrics").fetchone()
        assert row["series_download_count"] == 321
    finally:
        current.close()


def test_catalog_top_sorts_by_requested_platform_column(tmp_path):
    state_db = _make_db(
        tmp_path,
        "합성작품가 1-20화.txt",
        "합성작품나 1-20화.txt",
    )
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        keys = [
            row[0] for row in conn.execute(
                "SELECT title_key FROM catalog_titles ORDER BY title_key"
            )
        ]
        platform_catalog.record_platform_stats(
            conn,
            keys[0],
            [_identified_stat("series", download_count=10)],
        )
        platform_catalog.record_platform_stats(
            conn,
            keys[1],
            [_identified_stat("series", download_count=20)],
        )
    finally:
        conn.close()

    rows = platform_catalog.top_catalog_metrics(
        str(state_db), order_by="series-download", limit=2
    )
    assert [row["series_download_count"] for row in rows] == [20, 10]

    conn = decision_store.connect_state_db(state_db)
    try:
        conn.execute("UPDATE files SET active = 0 WHERE canonical_path LIKE '%합성작품나%'")
        conn.commit()
    finally:
        conn.close()
    active_only = platform_catalog.top_catalog_metrics(
        str(state_db), order_by="series-download", limit=2
    )
    assert [row["series_download_count"] for row in active_only] == [10]


def test_catalog_top_keeps_last_good_metric_when_current_lookup_failed(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn, key, [_identified_stat("series", rating=9.8)]
        )
        platform_catalog.record_platform_stats(
            conn, key, [platform_catalog.PlatformStat("series", "not_found")]
        )
    finally:
        conn.close()

    rows = platform_catalog.top_catalog_metrics(
        str(state_db), order_by="series-rating", limit=10
    )
    assert [row["series_rating"] for row in rows] == [9.8]


def test_catalog_status_is_read_only_and_uses_current_active_titles(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    conn = decision_store.connect_state_db(state_db)
    try:
        conn.execute("DROP VIEW catalog_title_metrics")
        conn.execute("DROP TABLE catalog_platform_stats")
        conn.execute("DROP TABLE catalog_titles")
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
    finally:
        conn.close()

    status = platform_catalog.catalog_status(str(state_db))
    assert status["catalog_schema_ready"] is False
    assert status["titles"] == 1
    assert status["pending_titles"] == 1
    assert status["pending_platforms"] == 3
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
    finally:
        conn.close()


def test_not_found_preserves_last_known_metrics(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn, key, [_identified_stat("series", download_count=123, rating=9.8)]
        )
        platform_catalog.record_platform_stats(
            conn, key, [platform_catalog.PlatformStat("series", "not_found")]
        )
        row = conn.execute(
            "SELECT status, download_count, rating FROM catalog_platform_stats "
            "WHERE title_key = ? AND platform = 'series'",
            (key,),
        ).fetchone()
        assert tuple(row) == ("ok", 123, 9.8)
    finally:
        conn.close()


def test_catalog_refresh_excludes_titles_without_an_active_house_file(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        conn.execute("UPDATE files SET active = 0")
        conn.commit()
        assert platform_catalog.select_refresh_targets(conn, limit=None) == []
    finally:
        conn.close()


def test_catalog_updates_display_title_when_a_cleaner_active_name_appears(tmp_path):
    state_db = _make_db(tmp_path, "긴작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        original = Path(
            conn.execute("SELECT canonical_path FROM files").fetchone()[0]
        )
        cleaner = original.parent / "긴작품.txt"
        cleaner.write_text("synthetic catalog fixture", encoding="utf-8")
        with decision_store.transaction(conn):
            decision_store.reconcile_file_metadata(conn, cleaner, source="house")
        platform_catalog.sync_catalog_titles(conn)
        assert conn.execute(
            "SELECT display_title FROM catalog_titles"
        ).fetchone()[0] == "긴작품"
    finally:
        conn.close()


def test_catalog_age_refresh_does_not_retry_old_not_found_rows(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        recorded_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        platform_catalog.record_platform_stats(
            conn,
            key,
            [
                platform_catalog.PlatformStat("series", "not_found"),
                platform_catalog.PlatformStat("kakao", "not_found"),
                platform_catalog.PlatformStat("novelpia", "not_found"),
            ],
            now=recorded_at,
        )
        target = platform_catalog.select_refresh_targets(
            conn,
            now=datetime(2026, 2, 1, tzinfo=timezone.utc),
            refresh_before=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        assert target == []
    finally:
        conn.close()


def test_not_found_is_not_automatically_retried_after_thirty_days(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        recorded_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        platform_catalog.record_platform_stats(
            conn,
            key,
            [platform_catalog.PlatformStat(platform, "not_found") for platform in platform_catalog.PLATFORMS],
            now=recorded_at,
        )
        assert platform_catalog.select_refresh_targets(
            conn, now=recorded_at + timedelta(days=29)
        ) == []
        assert platform_catalog.select_refresh_targets(
            conn, now=recorded_at + timedelta(days=300)
        ) == []
    finally:
        conn.close()


def test_public_platform_response_fixtures_cover_all_three_parsers():
    title = "합성 메인 제목: 충분히 긴 부제목"

    def fetch_text(url, _timeout):
        if "search/search.series" in url:
            return (
                '<li><a class="N=a:nov.title" '
                'href="/novel/detail.series?productNo=11">'
                f"{title} (총 20화/완결)</a></li>"
            )
        if "detail.series" in url:
            return (
                f'<meta property="og:title" content="{title}">'
                '<button class="btn_download"><span>1.2만</span></button>'
                '<div class="score_area"><em>9.8</em></div>'
            )
        raise AssertionError(url)

    def fetch_json(url, _timeout):
        if "/v2/search/series" in url:
            assert "category_uid=11" in url
            assert "is_complete=false" in url
            return {"result": {"list": [{
                "series_id": "22",
                "title": title,
                "on_issue": "N",
                "service_property": {"view_count": 23000},
            }]}}
        if "/v1/content/overview" in url:
            return {"result": {"content": {
                "title": title,
                "service_property": {
                    "view_count": 23000,
                    "rating_count": 20,
                    "rating_sum": 190,
                },
            }}}
        if "novelpia.com/proc/novel" in url:
            return {"status": 200, "list": [{
                "novel_no": "33",
                "novel_name": title,
                "count_view": 34000,
                "count_good": 450,
            }]}
        raise AssertionError(url)

    results = platform_catalog.lookup_platforms(
        title, fetch_text=fetch_text, fetch_json=fetch_json, timeout=1
    )
    by_platform = {result.platform: result for result in results}
    assert by_platform["series"].status == "ok"
    assert by_platform["series"].download_count == 12000
    assert by_platform["kakao"].status == "ok"
    assert by_platform["kakao"].rating == 9.5
    assert by_platform["novelpia"].status == "ok"
    assert by_platform["novelpia"].recommend_count == 450


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.2만", 12_000),
        ("91.2만", 912_000),
        ("2억 6,344만", 263_440_000),
        ("2억 6천만", 260_000_000),
        ("1,234", 1_234),
    ],
)
def test_count_parses_compound_korean_units(text, expected):
    assert platform_catalog._count(text) == expected


def test_one_titles_three_platforms_are_looked_up_in_parallel(monkeypatch):
    barrier = threading.Barrier(3, timeout=2)
    lock = threading.Lock()
    active = 0
    peak = 0

    def lookup(platform):
        def run(*_args, **_kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait()
                return platform_catalog.PlatformStat(platform, "ok", view_count=1)
            finally:
                with lock:
                    active -= 1

        return run

    monkeypatch.setattr(platform_catalog, "lookup_series", lookup("series"))
    monkeypatch.setattr(platform_catalog, "lookup_kakao", lookup("kakao"))
    monkeypatch.setattr(platform_catalog, "lookup_novelpia", lookup("novelpia"))

    results = platform_catalog.lookup_platforms("합성작품", timeout=1)
    assert [result.platform for result in results] == list(platform_catalog.PLATFORMS)
    assert peak == 3


@pytest.mark.parametrize("platform", ("series", "kakao"))
def test_known_ten_point_platforms_reject_out_of_range_ratings(platform):
    with pytest.raises(ValueError, match="rating"):
        platform_catalog._validate_stat(
            platform_catalog.PlatformStat(platform, "ok", rating=98)
        )


def test_changed_response_shapes_become_retryable_errors():
    series = platform_catalog.lookup_platforms(
        "합성작품",
        platforms=("series",),
        fetch_text=lambda _url, _timeout: "<html>unexpected</html>",
        timeout=1,
    )[0]
    kakao = platform_catalog.lookup_platforms(
        "합성작품",
        platforms=("kakao",),
        fetch_json=lambda _url, _timeout: {"unexpected": []},
        timeout=1,
    )[0]
    novelpia = platform_catalog.lookup_platforms(
        "합성작품",
        platforms=("novelpia",),
        fetch_json=lambda _url, _timeout: {"unexpected": []},
        timeout=1,
    )[0]
    assert [series.status, kakao.status, novelpia.status] == ["error", "error", "error"]


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("C++ 개발자", "C 개발자"),
        ("1+1", "11"),
        ("D&D", "DD"),
        ("C# 마스터", "C 마스터"),
    ],
)
def test_platform_title_match_preserves_identity_bearing_symbols(left, right):
    assert not platform_catalog.titles_match(left, right)


def test_series_duplicate_exact_title_candidates_fail_closed():
    search_page = (
        '<a href="/novel/detail.series?productNo=11">동일 제목</a>'
        '<a href="/novel/detail.series?productNo=22">동일 제목</a>'
    )
    stat = platform_catalog.lookup_series(
        "동일 제목",
        fetch_text=lambda url, _timeout: search_page
        if "search/search.series" in url else (_ for _ in ()).throw(AssertionError(url)),
        timeout=1,
    )
    assert stat.status == "error"
    assert "ambiguous" in stat.message


def test_kakao_author_evidence_mismatch_fails_closed_before_detail_lookup():
    def fetch_json(url, _timeout):
        if "/v2/search/series" in url:
            return {"result": {"list": [{
                "series_id": "22",
                "title": "동일 제목",
                "authors": [{"name": "작가 B"}],
                "service_property": {"view_count": 100},
            }]}}
        raise AssertionError(f"detail lookup must not run: {url}")

    stat = platform_catalog.lookup_kakao(
        "동일 제목", fetch_json=fetch_json, author="작가 A", timeout=1
    )
    assert stat.status == "not_found"
    assert stat.message == "remote author mismatch"


def test_ok_platform_stat_requires_remote_identity():
    with pytest.raises(ValueError, match="remote_id"):
        platform_catalog._validate_stat(
            platform_catalog.PlatformStat("series", "ok", download_count=1)
        )
    with pytest.raises(ValueError, match="remote_title"):
        platform_catalog._validate_stat(
            platform_catalog.PlatformStat(
                "series", "ok", remote_id="11", download_count=1
            )
        )


def test_primary_writer_rejects_changed_title_revision(tmp_path):
    state_db = _make_db(tmp_path, "CAS 작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        target = platform_catalog.select_refresh_targets(conn, limit=1)[0]
        conn.execute(
            "UPDATE catalog_titles SET query_title = ?, updated_at = ? WHERE title_key = ?",
            ("CAS 작품 변경", "2099-01-01 00:00:00", target.title.title_key),
        )
        conn.commit()
        outcomes = platform_catalog.record_platform_stats(
            conn,
            target.title.title_key,
            [_identified_stat("series", remote_title="CAS 작품", download_count=10)],
            expected_target=target,
        )
        assert outcomes == {"series": "stale_target"}
        assert conn.execute(
            "SELECT COUNT(*) FROM catalog_platform_stats WHERE title_key = ?",
            (target.title.title_key,),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_primary_writer_rejects_concurrent_expected_absence_violation(tmp_path):
    state_db = _make_db(tmp_path, "CAS 동시 작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        target = platform_catalog.select_refresh_targets(conn, limit=1)[0]
        worker = decision_store.connect_state_db(state_db)
        try:
            platform_catalog.record_platform_stats(
                worker,
                target.title.title_key,
                [_identified_stat("series", remote_id="11", download_count=20)],
            )
        finally:
            worker.close()
        outcomes = platform_catalog.record_platform_stats(
            conn,
            target.title.title_key,
            [_identified_stat("series", remote_id="22", download_count=99)],
            expected_target=target,
        )
        assert outcomes == {"series": "stale_target"}
        row = conn.execute(
            "SELECT remote_id, download_count FROM catalog_platform_stats "
            "WHERE title_key = ? AND platform = 'series'",
            (target.title.title_key,),
        ).fetchone()
        assert tuple(row) == ("11", 20)
    finally:
        conn.close()


def test_failed_retry_rejects_cross_id_success(tmp_path):
    state_db = _make_db(tmp_path, "재시도 ID 작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            key,
            [platform_catalog.PlatformStat(
                "kakao", "error", remote_id="old", remote_title="재시도 ID 작품",
                view_count=100, message="old failure",
            )],
        )
    finally:
        conn.close()

    def lookup(_title, platforms, *, timeout):
        assert platforms == ("kakao",)
        return [_identified_stat(
            "kakao", remote_id="new", remote_title="재시도 ID 작품", view_count=200
        )]

    result = platform_catalog.refresh_catalog(
        str(state_db), limit=None, delay_seconds=0, failed_retry=True, lookup=lookup
    )
    assert result["outcome_counts"]["identity_conflict"] == 1
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        row = conn.execute(
            "SELECT status, remote_id, view_count FROM catalog_platform_stats "
            "WHERE title_key = ? AND platform = 'kakao'",
            (key,),
        ).fetchone()
        assert tuple(row) == ("error", "old", 100)
    finally:
        conn.close()


def test_invalidated_identity_tombstone_blocks_retry_selectors(tmp_path):
    state_db = _make_db(tmp_path, "무효화 작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            key,
            [_identified_stat(
                "kakao", remote_id="bad", remote_title="다른 작품", view_count=10
            )],
        )
        platform_catalog.invalidate_platform_identity(
            conn,
            key,
            "kakao",
            expected_remote_id="bad",
            expected_remote_title="다른 작품",
            reason="verified wrong remote object",
        )
        failed = platform_catalog.select_refresh_targets(
            conn, limit=None, failed_retry=True
        )
        assert all("kakao" not in target.platforms for target in failed)
        retry_not_found = platform_catalog.select_refresh_targets(
            conn, limit=None, retry_not_found=True
        )
        assert all("kakao" not in target.platforms for target in retry_not_found)
    finally:
        conn.close()


def test_existing_metric_age_selector_is_stored_id_bound(tmp_path):
    state_db = _make_db(tmp_path, "기간 갱신 작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        recorded = datetime(2026, 1, 1, tzinfo=timezone.utc)
        platform_catalog.record_platform_stats(
            conn, key, [_identified_stat("series", download_count=100)], now=recorded
        )
        assert platform_catalog.select_existing_metric_targets(
            conn, refresh_before=datetime(2025, 12, 31, tzinfo=timezone.utc)
        ) == []
        [target] = platform_catalog.select_existing_metric_targets(
            conn, refresh_before=datetime(2026, 1, 2, tzinfo=timezone.utc)
        )
        assert target.platforms == ("series",)
        assert target.remote_hints[0][1] == "11"
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("flags", "force_expected", "age_expected"),
    [
        (("--force",), True, False),
        (("--refresh-after-days", "30"), False, True),
    ],
)
def test_refresh_force_and_age_route_existing_rows_to_stored_id_path(
    tmp_path, monkeypatch, flags, force_expected, age_expected
):
    state_db = tmp_path / "state.sqlite3"
    calls = {}
    monkeypatch.setattr(
        platform_catalog.AuthenticatedNovelpiaClient,
        "from_environment",
        classmethod(lambda cls, **_kwargs: None),
    )
    monkeypatch.setattr(
        run_platform_catalog,
        "sync_file_metadata",
        lambda *_args, **_kwargs: (None, {"total": 0}),
    )
    monkeypatch.setattr(
        run_platform_catalog,
        "_platform_refresh_lock",
        lambda *_args, **_kwargs: nullcontext(),
    )

    def generic(_path, **kwargs):
        calls["generic"] = kwargs
        return {"selected_titles": 0, "selected_platforms": 0}

    def existing(_path, **kwargs):
        calls["existing"] = kwargs
        return {
            "selected_titles": 1,
            "selected_platforms": 1,
            "outcome_counts": {"unchanged": 1},
        }

    monkeypatch.setattr(platform_catalog, "refresh_catalog", generic)
    monkeypatch.setattr(platform_catalog, "refresh_existing_metrics", existing)
    args = run_platform_catalog.build_parser().parse_args([
        "--state-db", str(state_db), "refresh", "--all", *flags,
    ])

    result = run_platform_catalog.run(args)

    assert calls["generic"]["refresh_after_days"] is None
    assert calls["generic"]["force"] is force_expected
    assert "existing_refresh" in result
    if age_expected:
        assert isinstance(calls["existing"]["refresh_before"], datetime)
    else:
        assert calls["existing"]["refresh_before"] is None


def test_metadata_completion_reconciles_pre_primary_crash_before_new_cycle(tmp_path):
    state_db = _make_db(tmp_path, "사전 재개 작품 1-20화.txt")
    public_calls = []
    metadata_calls = []

    def public_lookup(_title, platforms, *, timeout):
        public_calls.append(tuple(platforms))
        values = {
            "series": _identified_stat(
                "series", remote_title="사전 재개 작품", download_count=10
            ),
            "kakao": platform_catalog.PlatformStat("kakao", "not_found"),
            "novelpia": platform_catalog.PlatformStat("novelpia", "not_found"),
        }
        return [values[platform] for platform in platforms]

    def metadata_lookup(_title, platforms, *, timeout):
        metadata_calls.append(tuple(platforms))
        return [_identified_stat(
            "series", remote_title="사전 재개 작품", download_count=10, genre="현판"
        )]

    with pytest.raises(RuntimeError, match="pre-primary crash"):
        platform_catalog.refresh_catalog(
            str(state_db),
            limit=None,
            delay_seconds=0,
            lookup=public_lookup,
            metadata_lookup=metadata_lookup,
            _test_failpoint=lambda phase: (
                (_ for _ in ()).throw(RuntimeError("pre-primary crash"))
                if phase == "completion_cycle_created" else None
            ),
        )
    assert public_calls == []

    result = platform_catalog.refresh_catalog(
        str(state_db),
        limit=None,
        delay_seconds=0,
        lookup=public_lookup,
        metadata_lookup=metadata_lookup,
    )
    assert result["selected_titles"] == 1
    assert len(public_calls) == 1
    assert metadata_calls == [("series",)]
    assert result["metadata_completion"]["review_pairs"] == 0
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM settings WHERE key LIKE ?",
            (f"{platform_catalog.METADATA_COMPLETION_PREFIX}%",),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_metadata_completion_resumes_exact_pair_after_primary_crash(tmp_path):
    state_db = _make_db(tmp_path, "재개 작품 1-20화.txt")
    metadata_calls = []

    def public_lookup(_title, platforms, *, timeout):
        values = {
            "series": _identified_stat(
                "series", remote_title="재개 작품", download_count=10
            ),
            "kakao": platform_catalog.PlatformStat("kakao", "not_found"),
            "novelpia": platform_catalog.PlatformStat("novelpia", "not_found"),
        }
        return [values[platform] for platform in platforms]

    def metadata_lookup(_title, platforms, *, timeout):
        metadata_calls.append(tuple(platforms))
        assert platforms == ("series",)
        return [_identified_stat(
            "series", remote_title="재개 작품", download_count=10, genre="현판"
        )]

    def failpoint(phase):
        if phase == "primary_committed":
            raise RuntimeError("simulated crash after primary commit")

    with pytest.raises(RuntimeError, match="simulated crash"):
        platform_catalog.refresh_catalog(
            str(state_db),
            limit=None,
            delay_seconds=0,
            lookup=public_lookup,
            metadata_lookup=metadata_lookup,
            _test_failpoint=failpoint,
        )

    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM settings WHERE key LIKE ?",
            (f"{platform_catalog.METADATA_COMPLETION_PREFIX}%",),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT status FROM catalog_platform_stats WHERE platform = 'series'"
        ).fetchone()[0] == "ok"
    finally:
        conn.close()

    resumed = platform_catalog.refresh_catalog(
        str(state_db),
        limit=None,
        delay_seconds=0,
        lookup=public_lookup,
        metadata_lookup=metadata_lookup,
    )
    assert resumed["selected_titles"] == 0
    assert metadata_calls == [("series",)]
    assert resumed["metadata_completion"]["pending_pairs"] == 0
    assert resumed["metadata_completion"]["review_pairs"] == 0
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        row = conn.execute(
            "SELECT genre, genre_collected_at FROM catalog_platform_stats "
            "WHERE platform = 'series'"
        ).fetchone()
        assert row["genre"] == "현판"
        assert row["genre_collected_at"] is not None
        assert conn.execute(
            "SELECT COUNT(*) FROM settings WHERE key LIKE ?",
            (f"{platform_catalog.METADATA_COMPLETION_PREFIX}%",),
        ).fetchone()[0] == 0
    finally:
        conn.close()
