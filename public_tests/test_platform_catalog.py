from datetime import datetime, timezone
from pathlib import Path

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


def test_catalog_query_keeps_readable_title_instead_of_compact_key(tmp_path):
    state_db = _make_db(tmp_path, "합성 띄어쓰기 작품 1-20화 완 @가상작가.txt")
    conn = decision_store.initialize_state_db(state_db)
    try:
        title = platform_catalog.discover_catalog_titles(conn)[0]
        assert title.title_key == "합성띄어쓰기작품"
        assert title.query_title == "합성 띄어쓰기 작품"
    finally:
        conn.close()


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
        assert current.execute("PRAGMA user_version").fetchone()[0] == 9
    finally:
        current.close()


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
