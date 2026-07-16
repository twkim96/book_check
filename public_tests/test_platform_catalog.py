from datetime import datetime, timezone
from pathlib import Path

import decision_store
import platform_catalog


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
        return platform_catalog.PlatformStat(platform, "ok", interest_count=123, rating=9.1)
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
        assert row["series_interest_count"] == 123
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

    def lookup(_title, platforms, *, timeout):
        calls.append(tuple(platforms))
        return [_stat(platform) for platform in platforms]

    result = platform_catalog.refresh_catalog(
        str(state_db),
        limit=2,
        delay_seconds=3,
        lookup=lookup,
        sleep=waits.append,
        now=lambda: datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    assert result["selected_titles"] == 2
    assert calls == [("series", "kakao", "novelpia"), ("series", "kakao", "novelpia")]
    assert waits == [3]

    second = platform_catalog.refresh_catalog(
        str(state_db),
        limit=2,
        lookup=lookup,
        sleep=waits.append,
    )
    assert second["selected_titles"] == 0
    assert len(calls) == 2


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
            [platform_catalog.PlatformStat("series", "ok", interest_count=10)],
        )
        platform_catalog.record_platform_stats(
            conn,
            keys[1],
            [platform_catalog.PlatformStat("series", "ok", interest_count=20)],
        )
    finally:
        conn.close()

    rows = platform_catalog.top_catalog_metrics(
        str(state_db), order_by="series-interest", limit=2
    )
    assert [row["series_interest_count"] for row in rows] == [20, 10]

    conn = decision_store.connect_state_db(state_db)
    try:
        conn.execute("UPDATE files SET active = 0 WHERE canonical_path LIKE '%합성작품나%'")
        conn.commit()
    finally:
        conn.close()
    active_only = platform_catalog.top_catalog_metrics(
        str(state_db), order_by="series-interest", limit=2
    )
    assert [row["series_interest_count"] for row in active_only] == [10]


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
            conn, key, [platform_catalog.PlatformStat("series", "ok", interest_count=123, rating=9.8)]
        )
        platform_catalog.record_platform_stats(
            conn, key, [platform_catalog.PlatformStat("series", "not_found")]
        )
        row = conn.execute(
            "SELECT status, interest_count, rating FROM catalog_platform_stats "
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
