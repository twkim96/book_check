"""Read-only SQLite projection and one-way Google Sheets snapshot writer."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

import decision_store


WORKS_TAB = "작품 현황"
ERRORS_TAB = "수집 오류"
TEMP_PREFIX = "__file_check_tmp_"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DEFAULT_BATCH_ROWS = 1000

WORK_HEADERS = (
    "work_bucket_id",
    "variant_ids",
    "variant_kinds",
    "assignment_states",
    "title_key",
    "display_title",
    "query_title",
    "file_count",
    "series_status",
    "series_download_count",
    "series_rating",
    "series_last_success_at",
    "series_last_attempt_at",
    "kakao_status",
    "kakao_view_count",
    "kakao_rating",
    "kakao_last_success_at",
    "kakao_last_attempt_at",
    "novelpia_status",
    "novelpia_view_count",
    "novelpia_recommend_count",
    "novelpia_last_success_at",
    "novelpia_last_attempt_at",
    "sheet_synced_at",
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


def _joined(values: Iterable[object]) -> str:
    return ", ".join(sorted({str(value) for value in values if value is not None}))


def build_sheet_snapshot(
    state_db_path: os.PathLike | str,
    *,
    synced_at: Optional[datetime] = None,
) -> SheetSnapshot:
    """Build both Sheet tabs through a query-only SQLite connection."""
    conn = decision_store.connect_state_db_readonly(state_db_path)
    try:
        decision_store.validate_schema(conn)
        file_rows = conn.execute(
            """
            SELECT
                f.file_id, f.variant_id, f.assignment_state,
                v.work_bucket_id, v.variant_kind,
                a.core_title, a.readable_title, a.catalog_query_title
            FROM files AS f
            JOIN file_analysis AS a ON a.file_id = f.file_id
            LEFT JOIN variants AS v ON v.variant_id = f.variant_id
            WHERE f.active = 1 AND f.source = 'house'
            ORDER BY a.core_title, v.work_bucket_id, f.canonical_path
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

        grouped: Dict[Tuple[str, Optional[int]], dict] = {}
        for row in file_rows:
            title_key = row["core_title"]
            if not title_key:
                continue
            group_key = (title_key, row["work_bucket_id"])
            group = grouped.setdefault(
                group_key,
                {
                    "variant_ids": set(),
                    "variant_kinds": set(),
                    "assignment_states": set(),
                    "file_count": 0,
                    "candidates": [],
                },
            )
            group["variant_ids"].add(row["variant_id"])
            group["variant_kinds"].add(row["variant_kind"])
            group["assignment_states"].add(row["assignment_state"])
            group["file_count"] += 1
            candidate = (
                row["catalog_query_title"] or row["readable_title"] or title_key
            )
            group["candidates"].append(str(candidate))

        sync_text = _utc_text(synced_at)
        works_rows: List[Tuple[object, ...]] = []
        for (title_key, bucket_id), group in sorted(
            grouped.items(),
            key=lambda item: (item[0][0], item[0][1] is None, item[0][1] or 0),
        ):
            catalog = catalog_titles.get(title_key)
            fallback_title = min(group["candidates"], key=lambda value: (len(value), value))
            display_title = catalog["display_title"] if catalog else fallback_title
            query_title = catalog["query_title"] if catalog else fallback_title
            by_platform = stats.get(title_key, {})
            series = by_platform.get("series")
            kakao = by_platform.get("kakao")
            novelpia = by_platform.get("novelpia")

            def field(row, name):
                return _empty(row[name]) if row is not None else ""

            works_rows.append((
                bucket_id if bucket_id is not None else "미배정",
                _joined(group["variant_ids"]),
                _joined(group["variant_kinds"]),
                _joined(group["assignment_states"]),
                title_key,
                display_title,
                query_title,
                group["file_count"],
                field(series, "status"),
                field(series, "download_count"),
                field(series, "rating"),
                field(series, "last_success_at"),
                field(series, "last_attempt_at"),
                field(kakao, "status"),
                field(kakao, "view_count"),
                field(kakao, "rating"),
                field(kakao, "last_success_at"),
                field(kakao, "last_attempt_at"),
                field(novelpia, "status"),
                field(novelpia, "view_count"),
                field(novelpia, "recommend_count"),
                field(novelpia, "last_success_at"),
                field(novelpia, "last_attempt_at"),
                sync_text,
            ))

        active_keys = {key[0] for key in grouped}
        error_rows = []
        for row in conn.execute(
            """
            SELECT
                s.title_key, t.display_title, s.platform, s.status,
                s.last_attempt_at, s.last_success_at, s.retry_after, s.error_message
            FROM catalog_platform_stats AS s
            JOIN catalog_titles AS t ON t.title_key = s.title_key
            WHERE s.status IN ('not_found', 'error')
            ORDER BY s.status, s.platform, t.display_title, s.title_key
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

    def values_batch_update(self, data: Sequence[dict]) -> dict:
        return self._request(
            "POST",
            self._base + "/values:batchUpdate",
            body={"valueInputOption": "RAW", "data": list(data)},
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


def _format_requests(sheet_id: int, row_count: int, column_count: int, *, errors: bool):
    requests = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
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

    final_requests = []
    for table in (snapshot.works, snapshot.errors):
        sheet_id = temp_ids[temp_titles[table.title]]
        final_requests.extend(_format_requests(
            sheet_id,
            len(table.rows) + 1,
            len(table.headers),
            errors=table.title == ERRORS_TAB,
        ))
    for title in (WORKS_TAB, ERRORS_TAB):
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
