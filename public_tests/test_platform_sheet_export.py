import hashlib
from datetime import datetime, timezone

import pytest
from requests.exceptions import ReadTimeout

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
        key = conn.execute(
            "SELECT core_title FROM file_analysis ORDER BY core_title LIMIT 1"
        ).fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            key,
            [
                platform_catalog.PlatformStat(
                    "series", "ok", remote_title="합성 작품",
                    remote_url="https://series.example/1",
                    download_count=1234, rating=9.2
                ),
                platform_catalog.PlatformStat("kakao", "not_found"),
                platform_catalog.PlatformStat(
                    "novelpia", "ok", remote_title="합성 작품",
                    remote_url="https://novelpia.example/1",
                    view_count=4567, recommend_count=88
                ),
            ],
            now=datetime(2026, 7, 17, tzinfo=timezone.utc),
        )
    finally:
        conn.close()
    return state_db


def test_sheet_projection_is_read_only_groups_titles_and_blanks_not_found(tmp_path):
    state_db = _sheet_db(tmp_path)
    before = _digest(state_db)
    snapshot = platform_sheet_export.build_sheet_snapshot(
        state_db, synced_at=datetime(2026, 7, 17, 1, 2, 3, tzinfo=timezone.utc)
    )
    after = _digest(state_db)

    assert before == after
    assert snapshot.works.title == "도서 목록"
    assert snapshot.works.headers == (
        "원본 도서명", "보유 범위", "작가",
        "작품명", "다운로드 수", "평점", "링크",
        "작품명", "조회 수", "평점", "링크",
        "작품명", "조회 수", "좋아요 수", "링크",
    )
    assert snapshot.works.group_headers == (
        "메타데이터", None, None,
        "시리즈", None, None, None,
        "카카오", None, None, None,
        "노벨피아", None, None, None,
    )
    assert len(snapshot.works.rows) == 1
    work = snapshot.works.rows[0]
    assert work[0] == "합성 작품"
    assert work[1] == "30화"
    assert work[3] == "합성 작품"
    assert work[6] == "https://series.example/1"
    assert work[4] == 1234
    assert work[7] == ""
    assert work[10] == ""
    assert work[8] == ""
    assert work[9] == ""
    assert work[13] == 88
    assert snapshot.errors.rows == ()


def test_sheet_error_tab_contains_only_real_errors(tmp_path):
    state_db = _sheet_db(tmp_path)
    conn = decision_store.connect_state_db(state_db)
    try:
        key = conn.execute(
            "SELECT core_title FROM file_analysis ORDER BY core_title LIMIT 1"
        ).fetchone()[0]
        platform_catalog.record_platform_stats(
            conn,
            key,
            [platform_catalog.PlatformStat("kakao", "error", message="temporary")],
            now=datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc),
        )
    finally:
        conn.close()

    snapshot = platform_sheet_export.build_sheet_snapshot(state_db)
    assert len(snapshot.errors.rows) == 1
    error = dict(zip(snapshot.errors.headers, snapshot.errors.rows[0]))
    assert error["platform"] == "kakao"
    assert error["status"] == "error"
    assert error["error_message"] == "temporary"


def test_sheet_sync_dry_run_never_loads_google_credentials(tmp_path, monkeypatch):
    state_db = _sheet_db(tmp_path)
    monkeypatch.delenv("FILE_CHECK_GOOGLE_CREDENTIALS", raising=False)
    monkeypatch.delenv("FILE_CHECK_GOOGLE_SPREADSHEET_ID", raising=False)
    result = run_platform_catalog.sync_google_sheet(str(state_db), dry_run=True)
    assert result["dry_run"] is True
    assert result["works_rows"] == 1
    assert result["error_rows"] == 0
    assert result["works_columns"] == 15
    assert result["error_columns"] == 8


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
        self.value_input_options = []
        self._next_id = 100

    def get_sheets(self):
        return [
            {"sheetId": 1, "title": platform_sheet_export.WORKS_TAB, "index": 0},
            {"sheetId": 2, "title": platform_sheet_export.ERRORS_TAB, "index": 1},
            {"sheetId": 3, "title": "사용자 메모", "index": 2},
            {"sheetId": 4, "title": "작품 현황", "index": 3},
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

    def values_batch_update(self, data, *, value_input_option="RAW"):
        if self.fail_values:
            raise RuntimeError("synthetic values failure")
        self.value_calls.append(list(data))
        self.value_input_options.append(value_input_option)
        return {"totalUpdatedCells": 1}


def test_sheet_writer_uses_temporary_tabs_then_atomically_swaps_targets():
    rows = tuple((index,) for index in range(1600))
    snapshot = platform_sheet_export.SheetSnapshot(
        works=platform_sheet_export.SheetTable(platform_sheet_export.WORKS_TAB, ("n",), rows),
        errors=platform_sheet_export.SheetTable("수집 오류", ("n",), ()),
        synced_at="2026-07-17T00:00:00+00:00",
    )
    client = _FakeSheetsClient()
    events = []
    result = platform_sheet_export.sync_snapshot_to_google(
        snapshot, client, batch_rows=1000, progress=events.append
    )

    assert result["works_rows"] == 1600
    assert [event["phase"] for event in events] == [
        "sheet_write_start",
        "sheet_temp_tabs_created",
        "sheet_values_written",
        "sheet_links_written",
        "sheet_swap_completed",
    ]
    assert len(client.batch_calls) == 2
    create_call, final_call = client.batch_calls
    assert sum("addSheet" in request for request in create_call) == 2
    assert not any(
        request.get("deleteSheet", {}).get("sheetId") in {1, 2}
        for request in create_call
    )
    ranges = [item for call in client.value_calls for item in call]
    assert len(ranges) == 3  # works header+1600 rows in two chunks, errors header once
    frozen = [
        request["updateSheetProperties"]
        for request in final_call
        if "updateSheetProperties" in request
        and "gridProperties" in request["updateSheetProperties"].get("fields", "")
    ]
    assert frozen
    frozen_by_sheet = {
        request["properties"]["sheetId"]:
        request["properties"]["gridProperties"]
        for request in frozen
    }
    assert frozen_by_sheet[100] == {
        "frozenRowCount": 1, "frozenColumnCount": 1
    }
    assert frozen_by_sheet[101] == {
        "frozenRowCount": 1, "frozenColumnCount": 1
    }
    assert any(request.get("deleteSheet", {}).get("sheetId") == 1 for request in final_call)
    assert any(request.get("deleteSheet", {}).get("sheetId") == 4 for request in final_call)
    renamed = {
        request["updateSheetProperties"]["properties"]["title"]
        for request in final_call
        if "updateSheetProperties" in request
        and request["updateSheetProperties"].get("fields") == "title"
    }
    assert renamed == {"도서 목록", "수집 오류"}


def test_sheet_writer_splits_large_value_writes_into_small_requests():
    rows = tuple((index,) for index in range(5000))
    snapshot = platform_sheet_export.SheetSnapshot(
        works=platform_sheet_export.SheetTable(
            platform_sheet_export.WORKS_TAB, ("n",), rows
        ),
        errors=platform_sheet_export.SheetTable("수집 오류", ("n",), ()),
        synced_at="2026-07-17T00:00:00+00:00",
    )
    client = _FakeSheetsClient()

    platform_sheet_export.sync_snapshot_to_google(
        snapshot, client, batch_rows=1000
    )

    assert len(client.value_calls) == 4
    assert [len(call) for call in client.value_calls] == [2, 2, 2, 1]
    assert all(
        len(call) <= platform_sheet_export.MAX_RAW_RANGES_PER_REQUEST
        for call in client.value_calls
    )


def test_google_value_write_retries_only_timeout_failures(monkeypatch):
    client = object.__new__(platform_sheet_export.GoogleSheetsRestClient)
    client._base = "https://sheets.example/test"
    attempts = []

    def request(method, url, *, body=None):
        attempts.append((method, url, body))
        if len(attempts) < 3:
            raise ReadTimeout("synthetic timeout")
        return {"totalUpdatedCells": 1}

    monkeypatch.setattr(client, "_request", request)

    result = client.values_batch_update([{"range": "A1", "values": [[1]]}])

    assert result == {"totalUpdatedCells": 1}
    assert len(attempts) == 3


def test_sheet_writer_replaces_link_urls_with_hyperlink_formulas():
    row_values = [""] * len(platform_sheet_export.WORK_HEADERS)
    row_values[6] = "https://series.example/1"
    row_values[14] = "https://novelpia.example/1"
    row = tuple(row_values)
    snapshot = platform_sheet_export.SheetSnapshot(
        works=platform_sheet_export.SheetTable(
            platform_sheet_export.WORKS_TAB,
            platform_sheet_export.WORK_HEADERS,
            (row,),
            group_headers=platform_sheet_export.WORK_GROUP_HEADERS,
        ),
        errors=platform_sheet_export.SheetTable("수집 오류", ("n",), ()),
        synced_at="2026-07-17T00:00:00+00:00",
    )
    client = _FakeSheetsClient()
    platform_sheet_export.sync_snapshot_to_google(snapshot, client)

    formula_ranges = [
        item
        for option, call in zip(client.value_input_options, client.value_calls)
        if option == "USER_ENTERED"
        for item in call
    ]
    assert [item["range"].rsplit("!", 1)[1] for item in formula_ranges] == [
        "G3", "K3", "O3"
    ]
    assert formula_ranges[0]["values"] == [
        ['=HYPERLINK("https://series.example/1","열기")']
    ]
    assert formula_ranges[1]["values"] == [[None]]
    assert formula_ranges[2]["values"] == [
        ['=HYPERLINK("https://novelpia.example/1","열기")']
    ]


def test_grouped_work_headers_are_merged_frozen_filtered_and_sized():
    row = tuple("" for _ in platform_sheet_export.WORK_HEADERS)
    snapshot = platform_sheet_export.SheetSnapshot(
        works=platform_sheet_export.SheetTable(
            platform_sheet_export.WORKS_TAB,
            platform_sheet_export.WORK_HEADERS,
            (row,),
            group_headers=platform_sheet_export.WORK_GROUP_HEADERS,
        ),
        errors=platform_sheet_export.SheetTable("수집 오류", ("n",), ()),
        synced_at="2026-07-17T00:00:00+00:00",
    )
    client = _FakeSheetsClient()

    platform_sheet_export.sync_snapshot_to_google(snapshot, client)

    final_call = client.batch_calls[-1]
    frozen = next(
        request["updateSheetProperties"]["properties"]["gridProperties"]
        for request in final_call
        if "updateSheetProperties" in request
        and request["updateSheetProperties"]["properties"]["sheetId"] == 100
        and "gridProperties" in request["updateSheetProperties"].get("fields", "")
    )
    assert frozen == {"frozenRowCount": 2, "frozenColumnCount": 3}

    work_filter = next(
        request["setBasicFilter"]["filter"]["range"]
        for request in final_call
        if request.get("setBasicFilter", {}).get("filter", {}).get("range", {}).get(
            "sheetId"
        ) == 100
    )
    assert work_filter["startRowIndex"] == 1

    merged_columns = [
        (
            request["mergeCells"]["range"]["startColumnIndex"],
            request["mergeCells"]["range"]["endColumnIndex"],
        )
        for request in final_call
        if "mergeCells" in request
    ]
    assert merged_columns == [
        (0, 3), (3, 7), (7, 11), (11, 15),
    ]

    widths = {
        request["updateDimensionProperties"]["range"]["startIndex"]:
        request["updateDimensionProperties"]["properties"]["pixelSize"]
        for request in final_call
        if "updateDimensionProperties" in request
    }
    assert widths == {
        0: 250, 1: 80, 2: 80,
        3: 250, 4: 90, 5: 80, 6: 80,
        7: 250, 8: 90, 9: 80, 10: 80,
        11: 250, 12: 90, 14: 80,
    }

    comma_formats = [
        request["repeatCell"]
        for request in final_call
        if request.get("repeatCell", {}).get("cell", {}).get(
            "userEnteredFormat", {}
        ).get("numberFormat", {}).get("pattern") == "#,##0"
    ]
    assert [
        request["range"]["startColumnIndex"] for request in comma_formats
    ] == [4, 8, 12, 13]
    assert all(request["range"]["startRowIndex"] == 2 for request in comma_formats)
    assert all(
        request["cell"]["userEnteredFormat"]["numberFormat"]
        == {"type": "NUMBER", "pattern": "#,##0"}
        for request in comma_formats
    )

    platform_fills = {
        (
            request["repeatCell"]["range"]["startRowIndex"],
            request["repeatCell"]["range"]["startColumnIndex"],
            request["repeatCell"]["range"]["endColumnIndex"],
        ): request["repeatCell"]["cell"]["userEnteredFormat"]["backgroundColor"]
        for request in final_call
        if "backgroundColor"
        in request.get("repeatCell", {}).get("cell", {}).get(
            "userEnteredFormat", {}
        )
    }
    assert platform_fills[(0, 0, 3)] == {
        "red": 56 / 255, "green": 118 / 255, "blue": 218 / 255,
    }
    assert platform_fills[(0, 3, 7)] == {
        "red": 1 / 255, "green": 228 / 255, "blue": 79 / 255,
    }
    assert platform_fills[(0, 7, 11)] == {
        "red": 1, "green": 214 / 255, "blue": 23 / 255,
    }
    assert platform_fills[(0, 11, 15)] == {
        "red": 118 / 255, "green": 50 / 255, "blue": 1,
    }
    assert platform_fills[(1, 0, 3)] == {
        "red": 195 / 255, "green": 214 / 255, "blue": 244 / 255,
    }
    assert platform_fills[(1, 3, 7)] == {
        "red": 179 / 255, "green": 247 / 255, "blue": 202 / 255,
    }
    assert platform_fills[(1, 7, 11)] == {
        "red": 1, "green": 243 / 255, "blue": 185 / 255,
    }
    assert platform_fills[(1, 11, 15)] == {
        "red": 214 / 255, "green": 194 / 255, "blue": 1,
    }


def test_sheet_writer_keeps_missing_values_as_truly_blank_cells():
    snapshot = platform_sheet_export.SheetSnapshot(
        works=platform_sheet_export.SheetTable(
            platform_sheet_export.WORKS_TAB,
            ("제목", "조회 수", "링크"),
            (("없는 정보", "", None),),
        ),
        errors=platform_sheet_export.SheetTable("수집 오류", ("n",), ()),
        synced_at="2026-07-17T00:00:00+00:00",
    )
    client = _FakeSheetsClient()

    platform_sheet_export.sync_snapshot_to_google(snapshot, client)

    raw_ranges = [
        item
        for option, call in zip(client.value_input_options, client.value_calls)
        if option == "RAW"
        for item in call
    ]
    works_values = next(
        item["values"] for item in raw_ranges if item["range"].endswith("!A1")
    )
    assert works_values == [
        ["제목", "조회 수", "링크"],
        ["없는 정보", None, None],
    ]


def test_sheet_writer_failure_does_not_delete_existing_target_tabs():
    snapshot = platform_sheet_export.SheetSnapshot(
        works=platform_sheet_export.SheetTable(platform_sheet_export.WORKS_TAB, ("n",), ((1,),)),
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
