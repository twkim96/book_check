"""Read-only SQLite projection and one-way Google Sheets snapshot writer."""

from __future__ import annotations

import json
import os
import secrets
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

import decision_store
from requests.exceptions import Timeout as RequestTimeout


WORKS_TAB = "도서 목록"
ERRORS_TAB = "수집 오류"
LEGACY_SYNC_TABS = ("작품 현황",)
TEMP_PREFIX = "__file_check_tmp_"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DEFAULT_BATCH_ROWS = 1000
MAX_RAW_RANGES_PER_REQUEST = 2
MAX_FORMULA_RANGES_PER_REQUEST = 4
GOOGLE_VALUE_WRITE_ATTEMPTS = 3
GOOGLE_RATE_LIMIT_ATTEMPTS = 3
GOOGLE_RATE_LIMIT_DELAY_SECONDS = 30
GOOGLE_REQUEST_TIMEOUT = (10, 180)
GOOGLE_CONFIG_ENV = "FILE_CHECK_GOOGLE_CONFIG"
DEFAULT_GOOGLE_CONFIG = Path("~/.config/book_check/google-sheet.json")
MAX_GOOGLE_CONFIG_BYTES = 64 * 1024
COMMA_NUMBER_HEADERS = frozenset({"다운로드 수", "조회 수", "좋아요 수"})

WORK_HEADERS = (
    "원본 도서명",
    "보유 범위",
    "작가",
    "작품명",
    "장르",
    "다운로드 수",
    "평점",
    "링크",
    "작품명",
    "장르",
    "조회 수",
    "평점",
    "태그",
    "링크",
    "작품명",
    "장르",
    "조회 수",
    "좋아요 수",
    "태그",
    "링크",
)

WORK_GROUP_HEADERS = (
    "메타데이터", None, None,
    "시리즈", None, None, None, None,
    "카카오", None, None, None, None, None,
    "노벨피아", None, None, None, None, None,
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
    group_headers: Optional[Tuple[object, ...]] = None


@dataclass(frozen=True)
class SheetSnapshot:
    works: SheetTable
    errors: SheetTable
    synced_at: str


@dataclass(frozen=True)
class GoogleSheetSettings:
    credentials_path: str
    spreadsheet_id: str
    source: str


def _read_private_google_config(path: Path) -> Mapping[str, object]:
    """Read one owner-only regular JSON file without following its final link."""
    path = Path(os.path.abspath(os.fspath(path.expanduser())))
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError("Google Sheet local config is not configured") from exc
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError("Google Sheet local config must be a regular file")
    if hasattr(os, "getuid") and before.st_uid != os.getuid():
        raise RuntimeError("Google Sheet local config must be owned by the current user")
    if stat.S_IMODE(before.st_mode) & 0o077:
        raise RuntimeError("Google Sheet local config permissions must be 0600")
    if before.st_size > MAX_GOOGLE_CONFIG_BYTES:
        raise RuntimeError("Google Sheet local config is too large")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError("Google Sheet local config could not be opened safely") from exc
    try:
        current = os.fstat(fd)
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError("Google Sheet local config changed before it was opened")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_GOOGLE_CONFIG_BYTES + 1)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (
        (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise RuntimeError("Google Sheet local config changed while it was read")
    if len(raw) > MAX_GOOGLE_CONFIG_BYTES:
        raise RuntimeError("Google Sheet local config is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Google Sheet local config is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Google Sheet local config must be a JSON object")
    return payload


def resolve_google_sheet_settings(
    environ: Optional[Mapping[str, str]] = None,
    *,
    config_path: Optional[os.PathLike | str] = None,
) -> GoogleSheetSettings:
    """Resolve an atomic credentials/Spreadsheet pair.

    Explicit environment variables retain precedence.  If either legacy
    variable is present, both must be present so a stale environment value is
    never mixed with the private file.  Otherwise an owner-only local JSON file
    keeps the settings independent from launchd/PM2 environment inheritance.
    """
    env = os.environ if environ is None else environ
    credentials = str(env.get("FILE_CHECK_GOOGLE_CREDENTIALS", "") or "").strip()
    spreadsheet_id = str(
        env.get("FILE_CHECK_GOOGLE_SPREADSHEET_ID", "") or ""
    ).strip()
    if credentials or spreadsheet_id:
        if not credentials or not spreadsheet_id:
            raise RuntimeError(
                "FILE_CHECK_GOOGLE_CREDENTIALS and FILE_CHECK_GOOGLE_SPREADSHEET_ID "
                "must be configured together"
            )
        source = "environment"
    else:
        selected = config_path
        if selected is None:
            selected = str(env.get(GOOGLE_CONFIG_ENV, "") or "").strip()
        selected = selected or DEFAULT_GOOGLE_CONFIG
        payload = _read_private_google_config(Path(selected))
        credentials = str(payload.get("credentials_path") or "").strip()
        spreadsheet_id = str(payload.get("spreadsheet_id") or "").strip()
        if not credentials or not spreadsheet_id:
            raise RuntimeError(
                "Google Sheet local config requires credentials_path and spreadsheet_id"
            )
        source = "local_config"

    credentials_file = Path(credentials).expanduser()
    if not credentials_file.is_file():
        raise RuntimeError("Google service-account credentials file is missing")
    return GoogleSheetSettings(
        credentials_path=str(credentials_file),
        spreadsheet_id=spreadsheet_id,
        source=source,
    )


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
        conn.execute("BEGIN")
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
        tags: Dict[Tuple[str, str], List[str]] = {}
        for row in conn.execute(
            """
            SELECT title_key, platform, tag
            FROM catalog_platform_tags
            ORDER BY title_key, platform, position
            """
        ):
            tags.setdefault((row["title_key"], row["platform"]), []).append(
                str(row["tag"])
            )

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

            def platform_tags(row, platform):
                if (
                    row is None
                    or row["status"] != "ok"
                    or row["tags_collected_at"] is None
                ):
                    return ""
                return " ".join(
                    f"#{tag}" for tag in tags.get((title_key, platform), [])
                )

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
                platform_field(series, "remote_title"),
                platform_field(series, "genre"),
                platform_field(series, "download_count"),
                platform_field(series, "rating"),
                platform_field(series, "remote_url"),
                platform_field(kakao, "remote_title"),
                platform_field(kakao, "genre"),
                platform_field(kakao, "view_count"),
                platform_field(kakao, "rating"),
                platform_tags(kakao, "kakao"),
                platform_field(kakao, "remote_url"),
                platform_field(novelpia, "remote_title"),
                platform_field(novelpia, "genre"),
                platform_field(novelpia, "view_count"),
                platform_field(novelpia, "recommend_count"),
                platform_tags(novelpia, "novelpia"),
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
            works=SheetTable(
                WORKS_TAB,
                WORK_HEADERS,
                tuple(works_rows),
                group_headers=WORK_GROUP_HEADERS,
            ),
            errors=SheetTable(ERRORS_TAB, ERROR_HEADERS, tuple(error_rows)),
            synced_at=sync_text,
        )
    finally:
        if conn.in_transaction:
            conn.rollback()
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
        settings = resolve_google_sheet_settings()
        return cls(settings.spreadsheet_id, settings.credentials_path)

    def _request(self, method: str, url: str, *, body=None):
        for attempt in range(GOOGLE_RATE_LIMIT_ATTEMPTS):
            response = self._session.request(
                method,
                url,
                json=body,
                timeout=GOOGLE_REQUEST_TIMEOUT,
            )
            if response.status_code != 429:
                break
            if attempt + 1 == GOOGLE_RATE_LIMIT_ATTEMPTS:
                break
            retry_after = response.headers.get("Retry-After", "")
            delay = (
                int(retry_after)
                if str(retry_after).isdigit()
                else GOOGLE_RATE_LIMIT_DELAY_SECONDS
            )
            print(
                f"⏳ Google Sheets 쓰기 한도 대기: {delay}초 후 재시도",
                flush=True,
            )
            time.sleep(delay)
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
        body = {"valueInputOption": value_input_option, "data": list(data)}
        for attempt in range(GOOGLE_VALUE_WRITE_ATTEMPTS):
            try:
                return self._request(
                    "POST",
                    self._base + "/values:batchUpdate",
                    body=body,
                )
            except RequestTimeout:
                if attempt + 1 == GOOGLE_VALUE_WRITE_ATTEMPTS:
                    raise
        raise AssertionError("unreachable")


def _a1_title(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _google_cell_value(value: object):
    """Keep missing values as untouched Sheet cells, not empty-string cells."""
    return None if value is None or value == "" else value


def _header_rows(table: SheetTable) -> List[Sequence[object]]:
    rows: List[Sequence[object]] = []
    if table.group_headers is not None:
        if len(table.group_headers) != len(table.headers):
            raise ValueError("group headers must match the column count")
        rows.append(table.group_headers)
    rows.append(table.headers)
    return rows


def _value_ranges(table: SheetTable, temp_title: str, batch_rows: int) -> List[dict]:
    all_rows: List[Sequence[object]] = [*_header_rows(table), *table.rows]
    ranges = []
    for offset in range(0, len(all_rows), batch_rows):
        chunk = all_rows[offset:offset + batch_rows]
        ranges.append({
            "range": f"{_a1_title(temp_title)}!A{offset + 1}",
            "majorDimension": "ROWS",
            "values": [
                [_google_cell_value(value) for value in row]
                for row in chunk
            ],
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


def _hyperlink_formula(value: object) -> Optional[str]:
    url = str(value or "").strip()
    if not url.startswith(("https://", "http://")):
        return None
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
        first_data_row = len(_header_rows(table)) + 1
        for offset in range(0, len(table.rows), batch_rows):
            rows = table.rows[offset:offset + batch_rows]
            ranges.append({
                "range": f"{_a1_title(temp_title)}!{column}{offset + first_data_row}",
                "majorDimension": "ROWS",
                "values": [
                    [_hyperlink_formula(row[column_index])]
                    for row in rows
                ],
            })
    return ranges


def _format_requests(
    sheet_id: int,
    row_count: int,
    column_count: int,
    *,
    headers: Sequence[str],
    group_headers: Optional[Sequence[object]],
    errors: bool,
):
    header_row_count = 2 if group_headers is not None else 1
    frozen_column_count = 1 if errors else min(3, column_count)
    requests = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {
                        "frozenRowCount": header_row_count,
                        "frozenColumnCount": frozen_column_count,
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
                        "startRowIndex": header_row_count - 1,
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
                    "endRowIndex": header_row_count,
                    "startColumnIndex": 0,
                    "endColumnIndex": column_count,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True},
                    }
                },
                "fields": "userEnteredFormat.textFormat.bold",
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
        if group_headers is not None:
            requests.append({
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
                            "horizontalAlignment": "CENTER",
                        }
                    },
                    "fields": "userEnteredFormat.horizontalAlignment",
                }
            })
            starts = [
                index for index, value in enumerate(group_headers)
                if value is not None and value != ""
            ]
            for position, start in enumerate(starts):
                end = starts[position + 1] if position + 1 < len(starts) else column_count
                spans = [(start, end)]
                frozen_boundary = frozen_column_count
                if start < frozen_boundary < end:
                    spans = [(start, frozen_boundary), (frozen_boundary, end)]
                for merge_start, merge_end in spans:
                    if merge_end - merge_start <= 1:
                        continue
                    requests.append({
                        "mergeCells": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 0,
                                "endRowIndex": 1,
                                "startColumnIndex": merge_start,
                                "endColumnIndex": merge_end,
                            },
                            "mergeType": "MERGE_ALL",
                        }
                    })

            group_colors = {
                "메타데이터": (
                    {"red": 56 / 255, "green": 118 / 255, "blue": 218 / 255},
                    {"red": 1, "green": 1, "blue": 1},
                    {"red": 195 / 255, "green": 214 / 255, "blue": 244 / 255},
                ),
                "시리즈": (
                    {"red": 1 / 255, "green": 228 / 255, "blue": 79 / 255},
                    {"red": 1, "green": 1, "blue": 1},
                    {"red": 179 / 255, "green": 247 / 255, "blue": 202 / 255},
                ),
                "카카오": (
                    {"red": 1, "green": 214 / 255, "blue": 23 / 255},
                    {"red": 0, "green": 0, "blue": 0},
                    {"red": 1, "green": 243 / 255, "blue": 185 / 255},
                ),
                "노벨피아": (
                    {"red": 118 / 255, "green": 50 / 255, "blue": 1},
                    {"red": 1, "green": 1, "blue": 1},
                    {"red": 214 / 255, "green": 194 / 255, "blue": 1},
                ),
            }
            for position, start in enumerate(starts):
                title = str(group_headers[start] or "")
                colors = group_colors.get(title)
                if colors is None:
                    continue
                end = starts[position + 1] if position + 1 < len(starts) else column_count
                background, foreground, content_background = colors
                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": start,
                            "endColumnIndex": end,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": background,
                                "textFormat": {"foregroundColor": foreground},
                            }
                        },
                        "fields": (
                            "userEnteredFormat(backgroundColor,"
                            "textFormat.foregroundColor)"
                        ),
                    }
                })
                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "endRowIndex": row_count,
                            "startColumnIndex": start,
                            "endColumnIndex": end,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": content_background,
                            }
                        },
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                })

        for index, header in enumerate(headers):
            pixel_size = 250 if header in {"원본 도서명", "작품명"} else None
            if header in {"보유 범위", "작가", "장르", "평점", "링크"}:
                pixel_size = 80
            if header in {"다운로드 수", "조회 수"}:
                pixel_size = 90
            if pixel_size is not None:
                requests.append({
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": index,
                            "endIndex": index + 1,
                        },
                        "properties": {"pixelSize": pixel_size},
                        "fields": "pixelSize",
                    }
                })
            if header in COMMA_NUMBER_HEADERS:
                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": header_row_count,
                            "endRowIndex": row_count,
                            "startColumnIndex": index,
                            "endColumnIndex": index + 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {
                                    "type": "NUMBER",
                                    "pattern": "#,##0",
                                }
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
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
    progress=None,
) -> dict:
    """Write temporary tabs, then atomically replace the two public view tabs."""
    if batch_rows <= 0:
        raise ValueError("batch_rows must be positive")
    existing = client.get_sheets()
    if progress is not None:
        progress({
            "phase": "sheet_write_start",
            "works_rows": len(snapshot.works.rows),
            "error_rows": len(snapshot.errors.rows),
            "existing_tabs": len(existing),
        })
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
                        "rowCount": max(
                            1000,
                            len(table.rows) + len(_header_rows(table)),
                        ),
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
    if progress is not None:
        progress({"phase": "sheet_temp_tabs_created", "temp_tab_count": 2})

    value_ranges = []
    for table in (snapshot.works, snapshot.errors):
        value_ranges.extend(_value_ranges(table, temp_titles[table.title], batch_rows))
    for offset in range(0, len(value_ranges), MAX_RAW_RANGES_PER_REQUEST):
        client.values_batch_update(
            value_ranges[offset:offset + MAX_RAW_RANGES_PER_REQUEST]
        )
    if progress is not None:
        progress({
            "phase": "sheet_values_written",
            "value_range_count": len(value_ranges),
            "works_rows": len(snapshot.works.rows),
            "error_rows": len(snapshot.errors.rows),
        })

    hyperlink_ranges = _hyperlink_ranges(
        snapshot.works,
        temp_titles[snapshot.works.title],
        batch_rows,
    )
    for offset in range(0, len(hyperlink_ranges), MAX_FORMULA_RANGES_PER_REQUEST):
        client.values_batch_update(
            hyperlink_ranges[offset:offset + MAX_FORMULA_RANGES_PER_REQUEST],
            value_input_option="USER_ENTERED",
        )
    if progress is not None:
        progress({
            "phase": "sheet_links_written",
            "hyperlink_range_count": len(hyperlink_ranges),
        })

    final_requests = []
    for table in (snapshot.works, snapshot.errors):
        sheet_id = temp_ids[temp_titles[table.title]]
        final_requests.extend(_format_requests(
            sheet_id,
            len(table.rows) + len(_header_rows(table)),
            len(table.headers),
            headers=table.headers,
            group_headers=table.group_headers,
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
    if progress is not None:
        progress({
            "phase": "sheet_swap_completed",
            "works_rows": len(snapshot.works.rows),
            "error_rows": len(snapshot.errors.rows),
            "replaced_tabs": [WORKS_TAB, ERRORS_TAB],
        })
    return {
        "works_rows": len(snapshot.works.rows),
        "error_rows": len(snapshot.errors.rows),
        "synced_at": snapshot.synced_at,
    }
