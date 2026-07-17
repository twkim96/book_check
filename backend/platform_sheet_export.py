"""Read-only SQLite projection and one-way Google Sheets snapshot writer."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

import decision_store


WORKS_TAB = "도서 목록"
ERRORS_TAB = "수집 오류"
LEGACY_SYNC_TABS = ("작품 현황",)
TEMP_PREFIX = "__file_check_tmp_"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DEFAULT_BATCH_ROWS = 1000

WORK_HEADERS = (
    "원본 도서명",
    "보유 범위",
    "작가",
    "보유 파일 수",
    "시리즈 작품명",
    "시리즈 다운로드 수",
    "시리즈 평점",
    "시리즈 링크",
    "카카오 작품명",
    "카카오 조회 수",
    "카카오 평점",
    "카카오 링크",
    "노벨피아 작품명",
    "노벨피아 조회 수",
    "노벨피아 좋아요 수",
    "노벨피아 링크",
)

ERROR_HEADERS = (
    "title_key",
    "display_title",
    "platform",
    "status",
    "last_attempt_at",
    "last_success_at",
    "retry_after",
    "error_message",
)


@dataclass(frozen=True)
class SheetTable:
    title: str
    headers: Tuple[str, ...]
    rows: Tuple[Tuple[object, ...], ...]


@dataclass(frozen=True)
class SheetSnapshot:
    works: SheetTable
    errors: SheetTable
    synced_at: str


def _utc_text(value: Optional[datetime] = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(
        timespec="seconds"
    )


def _empty(value):
    return "" if value is None else value


def build_sheet_snapshot(
    state_db_path: os.PathLike | str,
    *,
    synced_at: Optional[datetime] = None,
) -> SheetSnapshot:
    """Build the public catalog and technical-error tabs read-only."""
    conn = decision_store.connect_state_db_readonly(state_db_path)
    try:
        decision_store.validate_schema(conn)
        file_rows = conn.execute(
            """
            SELECT
                f.file_id, a.core_title, a.readable_title,
                a.catalog_query_title, a.author, a.effective_max,
                a.unit, a.complete
            FROM files AS f
            JOIN file_analysis AS a ON a.file_id = f.file_id
            WHERE f.active = 1 AND f.source = 'house'
            ORDER BY a.core_title, f.canonical_path
            """
        ).fetchall()
        expected = conn.execute(
            """
            SELECT COUNT(*)
            FROM files AS f
            JOIN file_analysis AS a ON a.file_id = f.file_id
            WHERE f.active = 1 AND f.source = 'house'
            """
        ).fetchone()[0]
        if len(file_rows) != expected:
            raise RuntimeError(
                "Sheet projection requires current analysis for every active house file: "
                f"files={expected}, analyzed_titles={len(file_rows)}"
            )

        catalog_titles = {
            row["title_key"]: row
            for row in conn.execute(
                "SELECT title_key, display_title, query_title FROM catalog_titles"
            )
        }
        stats: Dict[str, Dict[str, object]] = {}
        for row in conn.execute(
            "SELECT * FROM catalog_platform_stats ORDER BY title_key, platform"
        ):
            stats.setdefault(row["title_key"], {})[row["platform"]] = row

        grouped: Dict[str, dict] = {}
        for row in file_rows:
            title_key = str(row["core_title"] or "").strip()
            if not title_key:
                continue
            group = grouped.setdefault(
                title_key,
                {
                    "candidates": [],
                    "authors": set(),
                    "ranges": [],
                    "file_count": 0,
                },
            )
            candidate = (
                row["catalog_query_title"] or row["readable_title"] or title_key
            )
            group["candidates"].append(str(candidate))
            author = str(row["author"] or "").strip()
            if author:
                group["authors"].add(author)
            group["ranges"].append((
                int(row["effective_max"]),
                str(row["unit"] or "미상"),
                bool(row["complete"]),
            ))
            group["file_count"] += 1

        sync_text = _utc_text(synced_at)
        works_rows: List[Tuple[object, ...]] = []
        for title_key, group in sorted(grouped.items()):
            catalog = catalog_titles.get(title_key)
            fallback_title = min(
                group["candidates"], key=lambda value: (len(value), value)
            )
            display_title = catalog["display_title"] if catalog else fallback_title
            by_platform = stats.get(title_key, {})
            series = by_platform.get("series")
            kakao = by_platform.get("kakao")
            novelpia = by_platform.get("novelpia")

            def platform_field(row, name):
                if row is None or row["status"] != "ok":
                    return ""
                return _empty(row[name])

            known_ranges = [
                item for item in group["ranges"] if item[0] > 0
            ]
            if known_ranges:
                effective_max, unit, _ = max(
                    known_ranges, key=lambda item: (item[0], item[1])
                )
                range_text = str(effective_max)
                if unit != "미상":
                    range_text += unit
                if any(item[2] for item in known_ranges):
                    range_text += " 완"
            elif any(item[2] for item in group["ranges"]):
                range_text = "완결"
            else:
                range_text = ""

            works_rows.append((
                display_title,
                range_text,
                ", ".join(sorted(group["authors"])),
                group["file_count"],
                platform_field(series, "remote_title"),
                platform_field(series, "download_count"),
                platform_field(series, "rating"),
                platform_field(series, "remote_url"),
                platform_field(kakao, "remote_title"),
                platform_field(kakao, "view_count"),
                platform_field(kakao, "rating"),
                platform_field(kakao, "remote_url"),
                platform_field(novelpia, "remote_title"),
                platform_field(novelpia, "view_count"),
                platform_field(novelpia, "recommend_count"),
                platform_field(novelpia, "remote_url"),
            ))

        active_keys = set(grouped)
        error_rows = []
        for row in conn.execute(
            """
            SELECT
                s.title_key, t.display_title, s.platform, s.status,
                s.last_attempt_at, s.last_success_at, s.retry_after, s.error_message
            FROM catalog_platform_stats AS s
            JOIN catalog_titles AS t ON t.title_key = s.title_key
            WHERE s.status = 'error'
            ORDER BY s.platform, t.display_title, s.title_key
            """
        ):
            if row["title_key"] not in active_keys:
                continue
            error_rows.append(tuple(_empty(row[name]) for name in ERROR_HEADERS))

        return SheetSnapshot(
            works=SheetTable(WORKS_TAB, WORK_HEADERS, tuple(works_rows)),
            errors=SheetTable(ERRORS_TAB, ERROR_HEADERS, tuple(error_rows)),
            synced_at=sync_text,
        )
    finally:
        conn.close()


class GoogleSheetsRestClient:
    """Small Google-authenticated REST adapter kept out of projection tests."""

    def __init__(self, spreadsheet_id: str, credentials_path: os.PathLike | str):
        if not spreadsheet_id or not str(spreadsheet_id).strip():
            raise ValueError("Google Spreadsheet ID is missing")
        credentials_file = Path(credentials_path).expanduser().resolve()
        if not credentials_file.is_file():
            raise FileNotFoundError("Google service-account credentials file is missing")
        try:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account
        except ImportError as exc:
            raise RuntimeError(
                "Google Sheet sync requires: pip install -r requirements.txt"
            ) from exc
        credentials = service_account.Credentials.from_service_account_file(
            str(credentials_file), scopes=[SHEETS_SCOPE]
        )
        self.spreadsheet_id = str(spreadsheet_id).strip()
        self._session = AuthorizedSession(credentials)
        self._base = "https://sheets.googleapis.com/v4/spreadsheets/" + quote(
            self.spreadsheet_id, safe=""
        )

    @classmethod
    def from_environment(cls):
        credentials = os.environ.get("FILE_CHECK_GOOGLE_CREDENTIALS", "").strip()
        spreadsheet_id = os.environ.get("FILE_CHECK_GOOGLE_SPREADSHEET_ID", "").strip()
        if not credentials:
            raise RuntimeError("FILE_CHECK_GOOGLE_CREDENTIALS is not configured")
        if not spreadsheet_id:
            raise RuntimeError("FILE_CHECK_GOOGLE_SPREADSHEET_ID is not configured")
        return cls(spreadsheet_id, credentials)

    def _request(self, method: str, url: str, *, body=None):
        response = self._session.request(method, url, json=body, timeout=60)
        if not response.ok:
            message = ""
            try:
                payload = response.json()
                message = str(payload.get("error", {}).get("message") or "")
            except (TypeError, ValueError):
                message = ""
            raise RuntimeError(
                f"Google Sheets API request failed: status={response.status_code}"
                + (f", message={message[:300]}" if message else "")
            )
        return response.json() if response.content else {}

    def get_sheets(self) -> List[dict]:
        payload = self._request(
            "GET", self._base + "?fields=sheets.properties(sheetId,title,index)"
        )
        return [dict(item["properties"]) for item in payload.get("sheets", [])]

    def batch_update(self, requests: Sequence[dict]) -> dict:
        return self._request(
            "POST", self._base + ":batchUpdate", body={"requests": list(requests)}
        )

    def values_batch_update(
        self,
        data: Sequence[dict],
        *,
        value_input_option: str = "RAW",
    ) -> dict:
        if value_input_option not in {"RAW", "USER_ENTERED"}:
            raise ValueError("invalid Google Sheets value input option")
        return self._request(
            "POST",
            self._base + "/values:batchUpdate",
            body={"valueInputOption": value_input_option, "data": list(data)},
        )


def _a1_title(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _value_ranges(table: SheetTable, temp_title: str, batch_rows: int) -> List[dict]:
    all_rows: List[Sequence[object]] = [table.headers, *table.rows]
    ranges = []
    for offset in range(0, len(all_rows), batch_rows):
        chunk = all_rows[offset:offset + batch_rows]
        ranges.append({
            "range": f"{_a1_title(temp_title)}!A{offset + 1}",
            "majorDimension": "ROWS",
            "values": [list(row) for row in chunk],
        })
    return ranges


def _column_name(index: int) -> str:
    if index < 0:
        raise ValueError("column index must be non-negative")
    value = index + 1
    letters = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _hyperlink_formula(value: object) -> str:
    url = str(value or "").strip()
    if not url.startswith(("https://", "http://")):
        return ""
    return '=HYPERLINK("' + url.replace('"', '""') + '","열기")'


def _hyperlink_ranges(
    table: SheetTable,
    temp_title: str,
    batch_rows: int,
) -> List[dict]:
    ranges = []
    link_columns = [
        index for index, header in enumerate(table.headers)
        if str(header).endswith("링크")
    ]
    for column_index in link_columns:
        column = _column_name(column_index)
        for offset in range(0, len(table.rows), batch_rows):
            rows = table.rows[offset:offset + batch_rows]
            ranges.append({
                "range": f"{_a1_title(temp_title)}!{column}{offset + 2}",
                "majorDimension": "ROWS",
                "values": [
                    [_hyperlink_formula(row[column_index])]
                    for row in rows
                ],
            })
    return ranges


def _format_requests(sheet_id: int, row_count: int, column_count: int, *, errors: bool):
    requests = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {
                        "frozenRowCount": 1,
                        "frozenColumnCount": 1 if errors else 2,
                    },
                },
                "fields": (
                    "gridProperties.frozenRowCount,"
                    "gridProperties.frozenColumnCount"
                ),
            }
        },
        {
            "setBasicFilter": {
                "filter": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": max(1, row_count),
                        "startColumnIndex": 0,
                        "endColumnIndex": column_count,
                    }
                }
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": column_count,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True},
                        "backgroundColor": {"red": 0.86, "green": 0.92, "blue": 0.98},
                    }
                },
                "fields": "userEnteredFormat(textFormat,backgroundColor)",
            }
        },
        {
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": column_count,
                }
            }
        },
    ]
    if not errors:
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": 1,
                },
                "properties": {"pixelSize": 360},
                "fields": "pixelSize",
            }
        })
    if errors and row_count > 1:
        status_range = {
            "sheetId": sheet_id,
            "startRowIndex": 1,
            "endRowIndex": row_count,
            "startColumnIndex": 3,
            "endColumnIndex": 4,
        }
        for value, color in (
            ("error", {"red": 0.96, "green": 0.70, "blue": 0.70}),
            ("not_found", {"red": 1.0, "green": 0.92, "blue": 0.62}),
        ):
            requests.append({
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [status_range],
                        "booleanRule": {
                            "condition": {
                                "type": "TEXT_EQ",
                                "values": [{"userEnteredValue": value}],
                            },
                            "format": {"backgroundColor": color},
                        },
                    },
                    "index": 0,
                }
            })
    return requests


def sync_snapshot_to_google(
    snapshot: SheetSnapshot,
    client,
    *,
    batch_rows: int = DEFAULT_BATCH_ROWS,
) -> dict:
    """Write temporary tabs, then atomically replace the two public view tabs."""
    if batch_rows <= 0:
        raise ValueError("batch_rows must be positive")
    existing = client.get_sheets()
    existing_by_title = {item["title"]: item for item in existing}
    token = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + secrets.token_hex(3)
    temp_titles = {
        WORKS_TAB: f"{TEMP_PREFIX}{token}_works",
        ERRORS_TAB: f"{TEMP_PREFIX}{token}_errors",
    }
    create_requests = [
        {"deleteSheet": {"sheetId": item["sheetId"]}}
        for item in existing
        if str(item["title"]).startswith(TEMP_PREFIX)
    ]
    for table in (snapshot.works, snapshot.errors):
        create_requests.append({
            "addSheet": {
                "properties": {
                    "title": temp_titles[table.title],
                    "gridProperties": {
                        "rowCount": max(1000, len(table.rows) + 1),
                        "columnCount": len(table.headers),
                    },
                }
            }
        })
    created = client.batch_update(create_requests)
    replies = created.get("replies", [])
    add_replies = [reply["addSheet"]["properties"] for reply in replies if "addSheet" in reply]
    if len(add_replies) != 2:
        raise RuntimeError("Google Sheets API did not return both temporary sheet IDs")
    temp_ids = {
        properties["title"]: properties["sheetId"] for properties in add_replies
    }

    value_ranges = []
    for table in (snapshot.works, snapshot.errors):
        value_ranges.extend(_value_ranges(table, temp_titles[table.title], batch_rows))
    for offset in range(0, len(value_ranges), 20):
        client.values_batch_update(value_ranges[offset:offset + 20])

    hyperlink_ranges = _hyperlink_ranges(
        snapshot.works,
        temp_titles[snapshot.works.title],
        batch_rows,
    )
    for offset in range(0, len(hyperlink_ranges), 20):
        client.values_batch_update(
            hyperlink_ranges[offset:offset + 20],
            value_input_option="USER_ENTERED",
        )

    final_requests = []
    for table in (snapshot.works, snapshot.errors):
        sheet_id = temp_ids[temp_titles[table.title]]
        final_requests.extend(_format_requests(
            sheet_id,
            len(table.rows) + 1,
            len(table.headers),
            errors=table.title == ERRORS_TAB,
        ))
    for title in (WORKS_TAB, ERRORS_TAB, *LEGACY_SYNC_TABS):
        old = existing_by_title.get(title)
        if old is not None:
            final_requests.append({"deleteSheet": {"sheetId": old["sheetId"]}})
    for title in (WORKS_TAB, ERRORS_TAB):
        final_requests.append({
            "updateSheetProperties": {
                "properties": {
                    "sheetId": temp_ids[temp_titles[title]],
                    "title": title,
                },
                "fields": "title",
            }
        })
    client.batch_update(final_requests)
    return {
        "works_rows": len(snapshot.works.rows),
        "error_rows": len(snapshot.errors.rows),
        "synced_at": snapshot.synced_at,
    }
