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


def _stat(platform):
    if platform == "series":
        return platform_catalog.PlatformStat(platform, "ok", download_count=123, rating=9.1)
    if platform == "kakao":
        return platform_catalog.PlatformStat(platform, "ok", view_count=456, rating=8.2)
    return platform_catalog.PlatformStat(platform, "ok", view_count=789, recommend_count=22)


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
    assert not platform_catalog.titles_match(title, f"{title} 외전")
    assert not platform_catalog.titles_match(
        "합성 메인 A: 같은 부제목",
        "합성 메인 B: 같은 부제목 [독점]",
    )


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
                platform_catalog.PlatformStat("series", "ok", rating=9.0),
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


def test_failed_only_cutoff_retries_each_failed_platform_once_and_skips_missing(tmp_path):
    state_db = _make_db(
        tmp_path,
        "합성작품가 1-20화.txt",
        "합성작품나 1-20화.txt",
    )
    recorded_at = datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc)
    retried_at = recorded_at + timedelta(minutes=1)
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
        )
        targets = platform_catalog.select_refresh_targets(
            conn,
            limit=None,
            now=retried_at,
            failed_only=True,
            failure_retry_cutoff=recorded_at,
        )
        assert len(targets) == 1
        assert targets[0].title.title_key == keys[0]
        assert targets[0].platforms == ("kakao", "novelpia")

        platform_catalog.record_platform_stats(
            conn,
            keys[0],
            [
                platform_catalog.PlatformStat("kakao", "not_found"),
                platform_catalog.PlatformStat("novelpia", "error", message="still temporary"),
            ],
            now=retried_at,
        )
        assert platform_catalog.select_refresh_targets(
            conn,
            limit=None,
            now=retried_at,
            failed_only=True,
            failure_retry_cutoff=recorded_at,
        ) == []
    finally:
        conn.close()


def test_failed_retry_state_is_resumable_then_permanently_completed(tmp_path):
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
    finally:
        conn.close()

    active = run_platform_catalog._failed_retry_state(str(state_db), create=True)
    assert active["state"] == "active"
    assert run_platform_catalog._failed_retry_state(
        str(state_db), create=False
    )["cutoff"] == active["cutoff"]
    run_platform_catalog._complete_failed_retry(
        str(state_db),
        active["cutoff"],
        {"selected_titles": 1, "selected_platforms": 1},
    )
    completed = run_platform_catalog._failed_retry_state(str(state_db), create=False)
    assert completed["state"] == "completed"
    assert completed["selected_platforms"] == 1


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
        assert current.execute("PRAGMA user_version").fetchone()[0] == 10
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
        assert current.execute("PRAGMA user_version").fetchone()[0] == 10
    finally:
        current.close()


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
            conn, key, [platform_catalog.PlatformStat("series", "ok", download_count=321)]
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
            [platform_catalog.PlatformStat("series", "ok", download_count=10)],
        )
        platform_catalog.record_platform_stats(
            conn,
            keys[1],
            [platform_catalog.PlatformStat("series", "ok", download_count=20)],
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


def test_catalog_top_excludes_last_good_metric_when_current_lookup_failed(tmp_path):
    state_db = _make_db(tmp_path, "합성작품 1-20화.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn, key, [platform_catalog.PlatformStat("series", "ok", rating=9.8)]
        )
        platform_catalog.record_platform_stats(
            conn, key, [platform_catalog.PlatformStat("series", "not_found")]
        )
    finally:
        conn.close()

    assert platform_catalog.top_catalog_metrics(
        str(state_db), order_by="series-rating", limit=10
    ) == []


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
            conn, key, [platform_catalog.PlatformStat("series", "ok", download_count=123, rating=9.8)]
        )
        platform_catalog.record_platform_stats(
            conn, key, [platform_catalog.PlatformStat("series", "not_found")]
        )
        row = conn.execute(
            "SELECT status, download_count, rating FROM catalog_platform_stats "
            "WHERE title_key = ? AND platform = 'series'",
            (key,),
        ).fetchone()
        assert tuple(row) == ("not_found", 123, 9.8)
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


def test_catalog_age_refresh_can_retry_old_not_found_rows(tmp_path):
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
        assert len(target) == 1
        assert target[0].platforms == ("series", "kakao", "novelpia")
    finally:
        conn.close()


def test_not_found_is_automatically_retried_after_thirty_days(tmp_path):
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
        target = platform_catalog.select_refresh_targets(
            conn, now=recorded_at + timedelta(days=30)
        )[0]
        assert target.platforms == platform_catalog.PLATFORMS
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
