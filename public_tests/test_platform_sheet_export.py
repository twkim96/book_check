import hashlib
from datetime import datetime, timezone

import pytest

import decision_store
import platform_catalog
import platform_sheet_export
import run_platform_catalog
import scanner


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sheet_db(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    paths = [
        house / "합성 작품 1-20화.txt",
        house / "합성 작품 1-30화 개정판.txt",
    ]
    for path in paths:
        path.write_text("합성 본문", encoding="utf-8")
    conn = decision_store.initialize_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            file_ids = []
            for path in paths:
                row = decision_store.reconcile_file_metadata(conn, path, source="house")
                file_ids.append(row["file_id"])
            for work_id, variant_id, file_id in (
                (1, 11, file_ids[0]),
                (2, 22, file_ids[1]),
            ):
                conn.execute(
                    "INSERT INTO works(work_bucket_id, display_title) VALUES (?, ?)",
                    (work_id, "합성 작품"),
                )
                conn.execute(
                    "INSERT INTO variants(variant_id, work_bucket_id) VALUES (?, ?)",
                    (variant_id, work_id),
                )
                conn.execute(
                    """
                    UPDATE files
                    SET variant_id = ?, assignment_state = 'managed',
                        assignment_origin = 'strong_match'
                    WHERE file_id = ?
                    """,
                    (variant_id, file_id),
                )
        platform_catalog.sync_catalog_titles(conn)
        key = conn.execute("SELECT title_key FROM catalog_titles").fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            key,
            [
                platform_catalog.PlatformStat(
                    "series", "ok", download_count=1234, rating=9.2
                ),
                platform_catalog.PlatformStat("kakao", "not_found"),
                platform_catalog.PlatformStat(
                    "novelpia", "ok", view_count=4567, recommend_count=88
                ),
            ],
            now=datetime(2026, 7, 17, tzinfo=timezone.utc),
        )
    finally:
        conn.close()
    return state_db


def test_sheet_projection_is_read_only_and_repeats_metrics_for_split_buckets(tmp_path):
    state_db = _sheet_db(tmp_path)
    before = _digest(state_db)
    snapshot = platform_sheet_export.build_sheet_snapshot(
        state_db, synced_at=datetime(2026, 7, 17, 1, 2, 3, tzinfo=timezone.utc)
    )
    after = _digest(state_db)

    assert before == after
    assert len(snapshot.works.rows) == 2
    work = [dict(zip(snapshot.works.headers, row)) for row in snapshot.works.rows]
    assert {row["work_bucket_id"] for row in work} == {1, 2}
    assert {row["series_download_count"] for row in work} == {1234}
    assert {row["novelpia_recommend_count"] for row in work} == {88}
    assert len(snapshot.errors.rows) == 1
    error = dict(zip(snapshot.errors.headers, snapshot.errors.rows[0]))
    assert error["platform"] == "kakao"
    assert error["status"] == "not_found"


def test_sheet_sync_dry_run_never_loads_google_credentials(tmp_path, monkeypatch):
    state_db = _sheet_db(tmp_path)
    monkeypatch.delenv("FILE_CHECK_GOOGLE_CREDENTIALS", raising=False)
    monkeypatch.delenv("FILE_CHECK_GOOGLE_SPREADSHEET_ID", raising=False)
    result = run_platform_catalog.sync_google_sheet(str(state_db), dry_run=True)
    assert result["dry_run"] is True
    assert result["works_rows"] == 2
    assert result["error_rows"] == 1


def test_scanner_prunes_only_analysis_projection_for_excluded_house_paths(tmp_path):
    house = tmp_path / "house"
    warning = house / "warning"
    warning.mkdir(parents=True)
    included = house / "합성 작품 1-20화.txt"
    excluded = warning / "검토 제외 작품 1-20화.txt"
    included.write_text("합성", encoding="utf-8")
    excluded.write_text("합성", encoding="utf-8")
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            decision_store.reconcile_file_metadata(conn, included, source="house")
            decision_store.reconcile_file_metadata(conn, excluded, source="house")
        assert conn.execute("SELECT COUNT(*) FROM file_analysis").fetchone()[0] == 2
    finally:
        conn.close()

    entries = scanner.get_file_entries([str(house)], state_db_path=str(state_db))
    assert [entry["name"] for entry in entries if entry["type"] == "file"] == [included.name]
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM files WHERE active = 1").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM file_analysis").fetchone()[0] == 1
    finally:
        conn.close()


class _FakeSheetsClient:
    def __init__(self, *, fail_values=False):
        self.fail_values = fail_values
        self.batch_calls = []
        self.value_calls = []
        self._next_id = 100

    def get_sheets(self):
        return [
            {"sheetId": 1, "title": platform_sheet_export.WORKS_TAB, "index": 0},
            {"sheetId": 2, "title": platform_sheet_export.ERRORS_TAB, "index": 1},
            {"sheetId": 3, "title": "사용자 메모", "index": 2},
        ]

    def batch_update(self, requests):
        self.batch_calls.append(list(requests))
        replies = []
        for request in requests:
            if "addSheet" not in request:
                replies.append({})
                continue
            properties = dict(request["addSheet"]["properties"])
            properties["sheetId"] = self._next_id
            self._next_id += 1
            replies.append({"addSheet": {"properties": properties}})
        return {"replies": replies}

    def values_batch_update(self, data):
        if self.fail_values:
            raise RuntimeError("synthetic values failure")
        self.value_calls.append(list(data))
        return {"totalUpdatedCells": 1}


def test_sheet_writer_uses_temporary_tabs_then_atomically_swaps_targets():
    rows = tuple((index,) for index in range(1600))
    snapshot = platform_sheet_export.SheetSnapshot(
        works=platform_sheet_export.SheetTable("작품 현황", ("n",), rows),
        errors=platform_sheet_export.SheetTable("수집 오류", ("n",), ()),
        synced_at="2026-07-17T00:00:00+00:00",
    )
    client = _FakeSheetsClient()
    result = platform_sheet_export.sync_snapshot_to_google(
        snapshot, client, batch_rows=1000
    )

    assert result["works_rows"] == 1600
    assert len(client.batch_calls) == 2
    create_call, final_call = client.batch_calls
    assert sum("addSheet" in request for request in create_call) == 2
    assert not any(
        request.get("deleteSheet", {}).get("sheetId") in {1, 2}
        for request in create_call
    )
    ranges = [item for call in client.value_calls for item in call]
    assert len(ranges) == 3  # works header+1600 rows in two chunks, errors header once
    assert any(request.get("deleteSheet", {}).get("sheetId") == 1 for request in final_call)
    renamed = {
        request["updateSheetProperties"]["properties"]["title"]
        for request in final_call
        if "updateSheetProperties" in request
        and request["updateSheetProperties"].get("fields") == "title"
    }
    assert renamed == {"작품 현황", "수집 오류"}


def test_sheet_writer_failure_does_not_delete_existing_target_tabs():
    snapshot = platform_sheet_export.SheetSnapshot(
        works=platform_sheet_export.SheetTable("작품 현황", ("n",), ((1,),)),
        errors=platform_sheet_export.SheetTable("수집 오류", ("n",), ()),
        synced_at="2026-07-17T00:00:00+00:00",
    )
    client = _FakeSheetsClient(fail_values=True)
    with pytest.raises(RuntimeError, match="synthetic values failure"):
        platform_sheet_export.sync_snapshot_to_google(snapshot, client)
    assert len(client.batch_calls) == 1
    assert not any(
        request.get("deleteSheet", {}).get("sheetId") in {1, 2}
        for request in client.batch_calls[0]
    )
