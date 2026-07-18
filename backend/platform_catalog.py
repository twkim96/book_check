"""Platform popularity catalog kept separately from deduplication decisions.

The catalog is deliberately a latest-value cache: it stores one current record
per normalized title and platform.  It never changes a library file, a dedup
decision, or the generated browser index.
"""

from __future__ import annotations

import html
import json
import math
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

import decision_store
from normalizer import (
    NORMALIZER_VERSION,
    extract_core_title,
)


PLATFORMS = ("series", "kakao", "novelpia")
GROWTH_METRICS = {
    "series": ("download_count",),
    "kakao": ("view_count",),
    "novelpia": ("view_count", "recommend_count"),
}
PLATFORM_LABELS = {
    "series": "네이버 시리즈",
    "kakao": "카카오페이지",
    "novelpia": "노벨피아",
}
DEFAULT_DELAY_SECONDS = 1.0
DEFAULT_LIMIT = 25
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_ERROR_RETRY_SECONDS = 6 * 60 * 60
RATING_SCALES = {
    "series": 10.0,
    "kakao": 10.0,
}
_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " \
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
_KAKAO_BFF_ORIGIN = "https://bff-page.kakao.com"
_NOVELPIA_ORIGIN = "https://novelpia.com"
NOVELPIA_EMAIL_ENV = "FILE_CHECK_NOVELPIA_EMAIL"
NOVELPIA_PASSWORD_ENV = "FILE_CHECK_NOVELPIA_PASSWORD"
NOVELPIA_AUTH_BATCH_SIZE = 20


class NovelpiaAuthenticationError(RuntimeError):
    """Raised when authenticated adult-title search cannot safely continue."""


class NovelpiaSessionExpiredError(NovelpiaAuthenticationError):
    """Raised only when the server explicitly reports an expired login."""


class AuthenticatedNovelpiaClient:
    """Cookie-scoped NovelPia client; credentials live only until login succeeds."""

    def __init__(
        self,
        email: str,
        password: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        opener=None,
        credential_loader=None,
    ):
        if not str(email or "").strip() or not str(password or ""):
            raise NovelpiaAuthenticationError(
                "NovelPia email/password environment variables are incomplete"
            )
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._email = str(email).strip()
        self._password = str(password)
        self.timeout = timeout
        self._opener = opener or build_opener(HTTPCookieProcessor(CookieJar()))
        self._credential_loader = credential_loader
        self._logged_in = False
        self._lookup_count = 0
        self.relogin_count = 0

    @classmethod
    def from_environment(
        cls,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        required: bool = False,
        environ=None,
    ):
        source = os.environ if environ is None else environ
        def load_credentials():
            return (
                str(source.get(NOVELPIA_EMAIL_ENV, "") or "").strip(),
                str(source.get(NOVELPIA_PASSWORD_ENV, "") or ""),
            )

        email, password = load_credentials()
        if not email and not password and not required:
            return None
        if not email or not password:
            raise NovelpiaAuthenticationError(
                f"{NOVELPIA_EMAIL_ENV} and {NOVELPIA_PASSWORD_ENV} must both be configured"
            )
        return cls(
            email,
            password,
            timeout=timeout,
            credential_loader=load_credentials,
        )

    @staticmethod
    def environment_configured(environ=None) -> bool:
        source = os.environ if environ is None else environ
        return bool(
            str(source.get(NOVELPIA_EMAIL_ENV, "") or "").strip()
            and str(source.get(NOVELPIA_PASSWORD_ENV, "") or "")
        )

    def _request_text(self, url: str, *, data: Optional[bytes] = None) -> str:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/json,text/plain,*/*",
            "User-Agent": _USER_AGENT,
            "Referer": _NOVELPIA_ORIGIN + "/",
        }
        if data is not None:
            headers.update({
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": _NOVELPIA_ORIGIN,
            })
        request = Request(url, data=data, headers=headers)
        with self._opener.open(request, timeout=self.timeout) as response:
            payload = response.read()
            response_headers = getattr(response, "headers", None)
            encoding = response_headers.get_content_charset() if response_headers else None
        return payload.decode(encoding or "utf-8", "replace")

    def _request_json(self, url: str, *, data: Optional[bytes] = None) -> object:
        return json.loads(self._request_text(url, data=data))

    def verify_session(self) -> None:
        try:
            result = self._request_text(
                _NOVELPIA_ORIGIN + "/proc/member_adt_mode",
                data=urlencode({"option": "on"}).encode("utf-8"),
            ).strip().strip('"')
        except NovelpiaAuthenticationError:
            raise
        except Exception as exc:
            raise NovelpiaAuthenticationError(
                "NovelPia session verification request failed"
            ) from exc
        if result == "OK":
            return
        if result == "login":
            self._logged_in = False
            raise NovelpiaSessionExpiredError(
                "NovelPia authenticated session expired"
            )
        if result == "auth":
            raise NovelpiaAuthenticationError(
                "NovelPia account requires adult identity verification"
            )
        raise NovelpiaAuthenticationError(
            "NovelPia adult mode/session verification returned an unexpected response"
        )

    def _credentials_for_login(self) -> Tuple[str, str]:
        email = self._email
        password = self._password
        if (not email or not password) and self._credential_loader is not None:
            email, password = self._credential_loader()
        if not str(email or "").strip() or not str(password or ""):
            raise NovelpiaAuthenticationError(
                f"{NOVELPIA_EMAIL_ENV} and {NOVELPIA_PASSWORD_ENV} must both be configured"
            )
        return str(email).strip(), str(password)

    def login(self) -> None:
        if self._logged_in:
            return
        try:
            email, password = self._credentials_for_login()
            self._request_text(_NOVELPIA_ORIGIN + "/login")
            captcha = self._request_json(
                _NOVELPIA_ORIGIN + "/proc/login_captcha?"
                + urlencode({"mode": "get_captcha"})
            )
            if (
                isinstance(captcha, dict)
                and str(captcha.get("status")) == "200"
                and captcha.get("result") is True
            ):
                raise NovelpiaAuthenticationError(
                    "NovelPia requires CAPTCHA; complete a manual login before retrying"
                )
            payload = urlencode({
                "redirectrurl": "",
                "email": email,
                "wd": password,
            }).encode("utf-8")
            self._request_text(_NOVELPIA_ORIGIN + "/proc/login", data=payload)
            try:
                self.verify_session()
            except NovelpiaSessionExpiredError as exc:
                raise NovelpiaAuthenticationError(
                    "NovelPia login was rejected"
                ) from exc
            self._logged_in = True
        finally:
            # Do not retain reusable plaintext credentials after the login attempt.
            self._email = ""
            self._password = ""

    def fetch_json(self, url: str, timeout: float) -> object:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout
        return self._request_json(url)

    def _lookup_once(
        self, title: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
    ) -> PlatformStat:
        try:
            return lookup_novelpia(title, self.fetch_json, timeout=timeout)
        except NovelpiaAuthenticationError:
            raise
        except Exception as exc:
            return _error("novelpia", exc)

    def lookup_batch(
        self,
        titles: Sequence[str],
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        delay_seconds: float = 0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> List[PlatformStat]:
        """Return only chunks whose authenticated session was verified afterward."""
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be non-negative")
        self.login()
        results: List[PlatformStat] = []
        for start in range(0, len(titles), NOVELPIA_AUTH_BATCH_SIZE):
            chunk = list(titles[start:start + NOVELPIA_AUTH_BATCH_SIZE])
            attempted = []
            for index, title in enumerate(chunk):
                attempted.append(self._lookup_once(title, timeout=timeout))
                if index + 1 < len(chunk) and delay_seconds:
                    sleep(delay_seconds)
            try:
                self.verify_session()
            except NovelpiaSessionExpiredError:
                # Nothing from this chunk has escaped to a DB writer yet.
                self._logged_in = False
                self.relogin_count += 1
                self.login()
                attempted = []
                for index, title in enumerate(chunk):
                    attempted.append(self._lookup_once(title, timeout=timeout))
                    if index + 1 < len(chunk) and delay_seconds:
                        sleep(delay_seconds)
                # A second expiry or verification error fails closed without
                # returning any unverified result from this chunk.
                self.verify_session()
            results.extend(attempted)
            self._lookup_count += len(chunk)
        return results

    def lookup(
        self, title: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
    ) -> PlatformStat:
        return self.lookup_batch([title], timeout=timeout)[0]


@dataclass(frozen=True)
class CatalogTitle:
    title_key: str
    display_title: str
    query_title: str


@dataclass(frozen=True)
class RefreshTarget:
    title: CatalogTitle
    platforms: Tuple[str, ...]


@dataclass(frozen=True)
class PlatformStat:
    platform: str
    status: str
    remote_id: Optional[str] = None
    remote_title: Optional[str] = None
    remote_url: Optional[str] = None
    download_count: Optional[int] = None
    view_count: Optional[int] = None
    recommend_count: Optional[int] = None
    rating: Optional[float] = None
    rating_count: Optional[int] = None
    message: str = ""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def titles_match(requested_title: str, candidate_title: str) -> bool:
    """Match normalized full titles and their dedup cores.

    Platform responses commonly append presentation-only tags such as
    ``[단행본]``/``[독점]`` and total episode text.  Remove only that narrow
    whitelist before the full-title comparison; the file normalizer is more
    aggressive and would incorrectly collapse a real ``외전`` title.
    """
    def normalized(value: str) -> Tuple[str, str]:
        text = html.unescape(str(value or "")).strip()
        text = re.sub(r"\s*:\s*네이버시리즈\s*$", "", text, flags=re.IGNORECASE)
        previous = None
        while text != previous:
            previous = text
            text = re.sub(
                r"\s*[\(（]\s*총\s*[\d,]+\s*(?:화|권|편)"
                r"(?:\s*/\s*[^\)）]+)?[\)）]\s*$",
                "",
                text,
                flags=re.IGNORECASE,
            ).strip()
            text = re.sub(
                r"\s*[\[［]\s*(?:단행본|독점|미니노블)\s*[\]］]\s*$",
                "",
                text,
                flags=re.IGNORECASE,
            ).strip()
        full_title = text
        exact = re.sub(
            r"[^a-z0-9가-힣\u3400-\u9fff\uf900-\ufaff]", "", full_title.lower()
        )
        core = extract_core_title(full_title)
        return exact, core

    requested_exact, requested_core = normalized(requested_title)
    candidate_exact, candidate_core = normalized(candidate_title)
    return bool(requested_exact) and (
        requested_exact == candidate_exact
        and bool(requested_core)
        and requested_core == candidate_core
    )


def _safe_message(error: BaseException) -> str:
    text = re.sub(r"\s+", " ", str(error or "조회 실패")).strip()
    return text[:300] or "조회 실패"


def _number(value: object) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) and parsed >= 0 else None
    text = html.unescape(str(value)).replace(",", "").strip()
    unit_values = {
        "억": 100_000_000,
        "천만": 10_000_000,
        "만": 10_000,
        "천": 1_000,
    }
    unit_matches = re.findall(
        r"(\d+(?:\.\d+)?)\s*(천만|억|만|천)",
        text,
    )
    if unit_matches:
        parsed = sum(
            float(number) * unit_values[unit]
            for number, unit in unit_matches
        )
        return parsed if math.isfinite(parsed) and parsed >= 0 else None
    matched = re.search(r"\d+(?:\.\d+)?", text)
    if not matched:
        return None
    parsed = float(matched.group(0))
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _count(value: object) -> Optional[int]:
    parsed = _number(value)
    return int(parsed) if parsed is not None else None


def _rating(value: object, *, maximum: Optional[float] = None) -> Optional[float]:
    parsed = _number(value)
    if parsed is None:
        return None
    if maximum is not None and parsed > maximum:
        raise ValueError(f"rating out of range: value={parsed}, maximum={maximum}")
    return round(parsed, 4)


def _first_value(record: object, keys: Sequence[str]) -> object:
    if not isinstance(record, dict):
        return None
    for key in keys:
        value = record.get(key)
        if value is not None and str(value).strip():
            return value
    return None


def _strip_tags(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"<script\b[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<style\b[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _has_metrics(stat: PlatformStat) -> bool:
    return any(
        value is not None
        for value in (
            stat.download_count,
            stat.view_count,
            stat.recommend_count,
            stat.rating,
            stat.rating_count,
        )
    )


def _not_found(platform: str, message: str = "결과 없음") -> PlatformStat:
    return PlatformStat(platform=platform, status="not_found", message=message)


def _error(platform: str, error: BaseException) -> PlatformStat:
    return PlatformStat(platform=platform, status="error", message=_safe_message(error))


def _http_text(url: str, timeout: float) -> str:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/json,text/plain,*/*",
        "User-Agent": _USER_AGENT,
    }
    if url.startswith(f"{_KAKAO_BFF_ORIGIN}/api/gateway/"):
        headers.update({
            "Origin": "https://page.kakao.com",
            "Referer": "https://page.kakao.com/",
        })
    request = Request(
        url,
        headers=headers,
    )
    with urlopen(request, timeout=timeout) as response:  # nosec B310 - fixed public platform URLs
        payload = response.read()
        headers = getattr(response, "headers", None)
        encoding = headers.get_content_charset() if headers else None
    return payload.decode(encoding or "utf-8", "replace")


def _http_json(url: str, timeout: float) -> object:
    return json.loads(_http_text(url, timeout))


def discover_catalog_titles(conn: sqlite3.Connection) -> List[CatalogTitle]:
    """Read stable catalog keys from the versioned file-analysis projection."""
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "file_analysis" not in tables:
        raise RuntimeError("file metadata sync required: schema v10 file_analysis is missing")
    rows = conn.execute(
        """
        SELECT
            f.canonical_path, f.size, f.mtime_ns, f.ctime_ns,
            a.file_id AS analysis_file_id, a.normalizer_version, a.analyzed_name,
            a.analyzed_size, a.analyzed_mtime_ns, a.analyzed_ctime_ns,
            a.core_title, a.readable_title, a.catalog_query_title
        FROM files AS f
        JOIN file_analysis AS a ON a.file_id = f.file_id
        WHERE f.active = 1 AND f.source = 'house'
        ORDER BY f.canonical_path
        """
    ).fetchall()
    active_count = conn.execute(
        "SELECT COUNT(*) FROM files WHERE active = 1 AND source = 'house'"
    ).fetchone()[0]
    if active_count and not rows:
        raise RuntimeError("file metadata sync required before platform collection")
    titles: Dict[str, CatalogTitle] = {}
    stale = 0
    for row in rows:
        current_name = Path(row["canonical_path"]).name
        title_analysis_current = (
            row["normalizer_version"] == NORMALIZER_VERSION
            and row["analyzed_name"] == current_name
        )
        if not title_analysis_current:
            stale += 1
            continue
        title_key = str(row["core_title"] or "").strip()
        if not title_key:
            continue
        readable_title = str(row["readable_title"] or "").strip()
        query_title = str(row["catalog_query_title"] or "").strip()
        candidate = CatalogTitle(
            title_key=title_key,
            display_title=query_title or readable_title or title_key,
            query_title=query_title or readable_title or title_key,
        )
        current = titles.get(title_key)
        if current is None or len(candidate.display_title) < len(current.display_title):
            titles[title_key] = candidate
    if stale:
        raise RuntimeError(
            "file metadata sync required before platform collection: "
            f"stale={stale}"
        )
    return [titles[key] for key in sorted(titles)]


def sync_catalog_titles(conn: sqlite3.Connection) -> Dict[str, int]:
    """Persist title keys found in active house files; historical rows are retained."""
    titles = discover_catalog_titles(conn)
    existing = {
        row["title_key"]: (
            row["display_title"],
            row["query_title"],
            row["normalizer_version"],
        )
        for row in conn.execute(
            "SELECT title_key, display_title, query_title, normalizer_version "
            "FROM catalog_titles"
        )
    }
    changed = [
        title for title in titles
        if existing.get(title.title_key) != (
            title.display_title,
            title.query_title,
            NORMALIZER_VERSION,
        )
    ]
    query_changed_keys = {
        title.title_key
        for title in changed
        if title.title_key in existing
        and existing[title.title_key][1] != title.query_title
    }
    if changed:
        with decision_store.transaction(conn):
            for title in changed:
                conn.execute(
                    """
                    INSERT INTO catalog_titles(
                        title_key, display_title, query_title, normalizer_version
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(title_key) DO UPDATE SET
                        display_title = excluded.display_title,
                        query_title = excluded.query_title,
                        normalizer_version = excluded.normalizer_version,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        title.title_key,
                        title.display_title,
                        title.query_title,
                        NORMALIZER_VERSION,
                    ),
                )
            if query_changed_keys:
                conn.executemany(
                    "DELETE FROM catalog_platform_stats "
                    "WHERE title_key = ? AND status = 'not_found'",
                    ((title_key,) for title_key in sorted(query_changed_keys)),
                )
    created = sum(1 for title in changed if title.title_key not in existing)
    return {"discovered": len(titles), "created": created, "known": len(titles) - created}


def _needed_platforms(
    title_key: str,
    stats_by_title: Dict[str, Dict[str, sqlite3.Row]],
    *,
    now: datetime,
    retry_not_found: bool,
    refresh_before: Optional[datetime],
    force: bool,
    failed_retry: bool = False,
    failure_retry_cutoff: Optional[datetime] = None,
) -> Tuple[str, ...]:
    if force:
        return PLATFORMS
    rows = stats_by_title.get(title_key, {})
    if failed_retry:
        failed = {
            platform
            for platform, row in rows.items()
            if row["status"] in {"not_found", "error"}
            and (
                failure_retry_cutoff is None
                or _parse_time(row["last_attempt_at"]) is None
                or _parse_time(row["last_attempt_at"]) <= failure_retry_cutoff
            )
        }
        commercial_ok = any(
            rows.get(platform) is not None and rows[platform]["status"] == "ok"
            for platform in ("series", "kakao")
        )
        novelpia_ok = (
            rows.get("novelpia") is not None
            and rows["novelpia"]["status"] == "ok"
        )
        if commercial_ok:
            # Series and Kakao may carry the same title. Retry only the failed
            # side of that pair and do not probe NovelPia for a commercial work.
            return tuple(
                platform
                for platform in ("series", "kakao")
                if platform in failed
            )
        if novelpia_ok:
            # NovelPia-only titles almost never need commercial-platform probes.
            return ()
        # No platform has a success. Retry each recorded failure once in this run;
        # the normal refresh remains responsible for truly missing rows.
        return tuple(platform for platform in PLATFORMS if platform in failed)

    needed = []
    for platform in PLATFORMS:
        row = rows.get(platform)
        if row is None:
            needed.append(platform)
            continue
        status = row["status"]
        if status == "error":
            # Failure rows are deliberately sticky. They are retried only by the
            # explicit failed-results action, never by the regular update button.
            continue
        if status == "not_found":
            if retry_not_found:
                needed.append(platform)
            continue
        if refresh_before is not None:
            last_success = _parse_time(row["last_success_at"])
            if last_success is None or last_success <= refresh_before:
                needed.append(platform)
    return tuple(needed)


def _stats_by_title(conn: sqlite3.Connection) -> Dict[str, Dict[str, sqlite3.Row]]:
    values: Dict[str, Dict[str, sqlite3.Row]] = {}
    for row in conn.execute("SELECT * FROM catalog_platform_stats"):
        values.setdefault(row["title_key"], {})[row["platform"]] = row
    return values


def _refresh_targets(
    titles: Iterable[CatalogTitle],
    stats_by_title: Dict[str, Dict[str, sqlite3.Row]],
    *,
    limit: Optional[int],
    now: datetime,
    retry_not_found: bool = False,
    refresh_before: Optional[datetime] = None,
    force: bool = False,
    failed_retry: bool = False,
    failure_retry_cutoff: Optional[datetime] = None,
) -> List[RefreshTarget]:
    if limit == 0:
        return []
    targets = []
    for title in sorted(titles, key=lambda item: item.title_key):
        platforms = _needed_platforms(
            title.title_key,
            stats_by_title,
            now=now,
            retry_not_found=retry_not_found,
            refresh_before=refresh_before,
            force=force,
            failed_retry=failed_retry,
            failure_retry_cutoff=failure_retry_cutoff,
        )
        if platforms:
            targets.append(RefreshTarget(title=title, platforms=platforms))
            if limit is not None and len(targets) >= limit:
                break
    return targets


def preview_catalog_refresh(
    state_db_path: str,
    *,
    limit: Optional[int] = DEFAULT_LIMIT,
    retry_not_found: bool = False,
    refresh_after_days: Optional[float] = None,
    force: bool = False,
    failed_retry: bool = False,
    failure_retry_cutoff: Optional[datetime] = None,
    now: Callable[[], datetime] = utc_now,
) -> Dict[str, object]:
    """Read-only preview that also works before the v8 catalog migration."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    if refresh_after_days is not None and refresh_after_days < 0:
        raise ValueError("refresh_after_days must be non-negative")
    conn = decision_store.connect_state_db_readonly(state_db_path)
    try:
        current = now()
        refresh_before = (
            current - timedelta(days=refresh_after_days)
            if refresh_after_days is not None else None
        )
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        stats = _stats_by_title(conn) if "catalog_platform_stats" in tables else {}
        titles = discover_catalog_titles(conn)
        targets = _refresh_targets(
            titles,
            stats,
            limit=limit,
            now=current,
            retry_not_found=retry_not_found,
            refresh_before=refresh_before,
            force=force,
            failed_retry=failed_retry,
            failure_retry_cutoff=failure_retry_cutoff,
        )
        return {
            "dry_run": True,
            "discovered_titles": len(titles),
            "selected_titles": len(targets),
            "selected_platforms": sum(len(target.platforms) for target in targets),
            "titles": [target.title.display_title for target in targets],
        }
    finally:
        conn.close()


def select_refresh_targets(
    conn: sqlite3.Connection,
    *,
    limit: Optional[int] = DEFAULT_LIMIT,
    now: Optional[datetime] = None,
    retry_not_found: bool = False,
    refresh_before: Optional[datetime] = None,
    force: bool = False,
    failed_retry: bool = False,
    failure_retry_cutoff: Optional[datetime] = None,
) -> List[RefreshTarget]:
    return _refresh_targets(
        discover_catalog_titles(conn),
        _stats_by_title(conn),
        limit=limit,
        now=now or utc_now(),
        retry_not_found=retry_not_found,
        refresh_before=refresh_before,
        force=force,
        failed_retry=failed_retry,
        failure_retry_cutoff=failure_retry_cutoff,
    )


def _row_has_growth_metric(platform: str, row: sqlite3.Row) -> bool:
    return any(row[field] is not None for field in GROWTH_METRICS[platform])


def select_existing_metric_targets(
    conn: sqlite3.Connection,
    *,
    limit: Optional[int] = None,
) -> List[RefreshTarget]:
    """Select only active, successful platforms that already have a count."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    if limit == 0:
        return []
    stats = _stats_by_title(conn)
    targets = []
    for title in sorted(discover_catalog_titles(conn), key=lambda item: item.title_key):
        rows = stats.get(title.title_key, {})
        platforms = tuple(
            platform
            for platform in PLATFORMS
            if rows.get(platform) is not None
            and rows[platform]["status"] == "ok"
            and _row_has_growth_metric(platform, rows[platform])
        )
        if not platforms:
            continue
        targets.append(RefreshTarget(title=title, platforms=platforms))
        if limit is not None and len(targets) >= limit:
            break
    return targets


def preview_existing_metric_refresh(
    state_db_path: str,
    *,
    limit: Optional[int] = None,
) -> Dict[str, object]:
    conn = decision_store.connect_state_db_readonly(state_db_path)
    try:
        decision_store.validate_schema(conn)
        targets = select_existing_metric_targets(conn, limit=limit)
        return {
            "dry_run": True,
            "selected_titles": len(targets),
            "selected_platforms": sum(len(target.platforms) for target in targets),
            "titles": [target.title.display_title for target in targets],
        }
    finally:
        conn.close()


def _validate_stat(stat: PlatformStat) -> PlatformStat:
    if stat.platform not in PLATFORMS:
        raise ValueError(f"unknown platform: {stat.platform}")
    if stat.status not in {"ok", "not_found", "error", "skipped"}:
        raise ValueError(f"unknown platform status: {stat.status}")
    for label, value in (
        ("download_count", stat.download_count),
        ("view_count", stat.view_count),
        ("recommend_count", stat.recommend_count),
        ("rating_count", stat.rating_count),
    ):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise ValueError(f"invalid {label}: {value!r}")
    if stat.rating is not None:
        if (
            not isinstance(stat.rating, (int, float))
            or not math.isfinite(stat.rating)
            or stat.rating < 0
        ):
            raise ValueError(f"invalid rating: {stat.rating!r}")
        scale = RATING_SCALES.get(stat.platform)
        if scale is not None and stat.rating > scale:
            raise ValueError(
                f"invalid {stat.platform} rating: {stat.rating!r} exceeds {scale}"
            )
    if stat.status == "ok" and not _has_metrics(stat):
        raise ValueError("ok platform stat requires at least one metric")
    return stat


def record_platform_stats(
    conn: sqlite3.Connection,
    title_key: str,
    stats: Sequence[PlatformStat],
    *,
    now: Optional[datetime] = None,
    error_retry_seconds: int = DEFAULT_ERROR_RETRY_SECONDS,
) -> None:
    """Upsert one title's results atomically without erasing prior values on errors."""
    if error_retry_seconds < 0:
        raise ValueError("error_retry_seconds must be non-negative")
    moment = now or utc_now()
    attempted_at = _utc_text(moment)
    retry_after = _utc_text(moment + timedelta(seconds=error_retry_seconds))
    validated = [_validate_stat(stat) for stat in stats]
    if len({stat.platform for stat in validated}) != len(validated):
        raise ValueError("at most one stat per platform may be recorded per title")

    with decision_store.transaction(conn):
        exists = conn.execute(
            "SELECT 1 FROM catalog_titles WHERE title_key = ?", (title_key,)
        ).fetchone()
        if exists is None:
            raise KeyError(f"catalog title not found: {title_key}")
        for stat in validated:
            success_at = attempted_at if stat.status == "ok" else None
            next_retry = retry_after if stat.status == "error" else None
            conn.execute(
                """
                INSERT INTO catalog_platform_stats(
                    title_key, platform, status, remote_id, remote_title, remote_url,
                    download_count, view_count, recommend_count, rating, rating_count,
                    last_attempt_at, last_success_at, retry_after, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(title_key, platform) DO UPDATE SET
                    status = excluded.status,
                    remote_id = CASE WHEN excluded.status != 'ok'
                                     THEN catalog_platform_stats.remote_id ELSE excluded.remote_id END,
                    remote_title = CASE WHEN excluded.status != 'ok'
                                        THEN catalog_platform_stats.remote_title ELSE excluded.remote_title END,
                    remote_url = CASE WHEN excluded.status != 'ok'
                                      THEN catalog_platform_stats.remote_url ELSE excluded.remote_url END,
                    download_count = CASE WHEN excluded.status != 'ok'
                                          THEN catalog_platform_stats.download_count ELSE excluded.download_count END,
                    view_count = CASE WHEN excluded.status != 'ok'
                                      THEN catalog_platform_stats.view_count ELSE excluded.view_count END,
                    recommend_count = CASE WHEN excluded.status != 'ok'
                                           THEN catalog_platform_stats.recommend_count ELSE excluded.recommend_count END,
                    rating = CASE WHEN excluded.status != 'ok'
                                  THEN catalog_platform_stats.rating ELSE excluded.rating END,
                    rating_count = CASE WHEN excluded.status != 'ok'
                                        THEN catalog_platform_stats.rating_count ELSE excluded.rating_count END,
                    last_attempt_at = excluded.last_attempt_at,
                    last_success_at = CASE WHEN excluded.status = 'ok'
                                           THEN excluded.last_success_at ELSE catalog_platform_stats.last_success_at END,
                    retry_after = excluded.retry_after,
                    error_message = excluded.error_message,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    title_key,
                    stat.platform,
                    stat.status,
                    stat.remote_id,
                    stat.remote_title,
                    stat.remote_url,
                    stat.download_count,
                    stat.view_count,
                    stat.recommend_count,
                    stat.rating,
                    stat.rating_count,
                    attempted_at,
                    success_at,
                    next_retry,
                    stat.message or None,
                ),
            )


def _monotonic_count(old: Optional[int], new: Optional[int]) -> Optional[int]:
    if old is None:
        return new
    if new is None:
        return old
    return max(old, new)


def _growth_metric_increased(row: sqlite3.Row, stat: PlatformStat) -> bool:
    return any(
        getattr(stat, field) is not None
        and (row[field] is None or getattr(stat, field) > row[field])
        for field in GROWTH_METRICS[stat.platform]
    )


def record_increased_platform_stats(
    conn: sqlite3.Connection,
    title_key: str,
    stats: Sequence[PlatformStat],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, str]:
    """Apply successful results only when a platform's popularity count grew."""
    outcomes: Dict[str, str] = {}
    accepted = []
    for raw_stat in stats:
        stat = _validate_stat(raw_stat)
        row = conn.execute(
            "SELECT * FROM catalog_platform_stats "
            "WHERE title_key = ? AND platform = ?",
            (title_key, stat.platform),
        ).fetchone()
        if stat.status != "ok":
            outcomes[stat.platform] = stat.status
            continue
        if (
            row is None
            or row["status"] != "ok"
            or not _row_has_growth_metric(stat.platform, row)
        ):
            outcomes[stat.platform] = "skipped"
            continue
        if not _growth_metric_increased(row, stat):
            outcomes[stat.platform] = "unchanged"
            continue
        accepted.append(replace(
            stat,
            remote_id=stat.remote_id or row["remote_id"],
            remote_title=stat.remote_title or row["remote_title"],
            remote_url=stat.remote_url or row["remote_url"],
            download_count=_monotonic_count(
                row["download_count"], stat.download_count
            ),
            view_count=_monotonic_count(row["view_count"], stat.view_count),
            recommend_count=_monotonic_count(
                row["recommend_count"], stat.recommend_count
            ),
            rating=stat.rating if stat.rating is not None else row["rating"],
            rating_count=_monotonic_count(
                row["rating_count"], stat.rating_count
            ),
        ))
        outcomes[stat.platform] = "updated"
    if accepted:
        record_platform_stats(conn, title_key, accepted, now=now)
    return outcomes


def _parse_series_candidates(page: str) -> List[Dict[str, str]]:
    candidates = []
    seen = set()
    for item in re.findall(r"<li\b[\s\S]*?</li>", page, flags=re.IGNORECASE):
        product = re.search(r"/novel/detail\.series\?productNo=(\d+)", item, flags=re.IGNORECASE)
        if not product or product.group(1) in seen:
            continue
        title = (
            re.search(r"class=[\"'][^\"']*N=a:nov\.title[^\"']*[\"'][^>]*>([\s\S]*?)</a>", item, flags=re.IGNORECASE)
            or re.search(r"<h3[^>]*>[\s\S]*?<a[^>]+href=[\"'][^\"']*/novel/detail\.series\?productNo=\d+[^\"']*[\"'][^>]*>([\s\S]*?)</a>", item, flags=re.IGNORECASE)
            or re.search(r"<strong[^>]*>([\s\S]*?)</strong>", item, flags=re.IGNORECASE)
        )
        seen.add(product.group(1))
        candidates.append({
            "id": product.group(1),
            "title": _strip_tags(title.group(1)) if title else "",
        })
    if candidates:
        return candidates
    for product, title in re.findall(
        r"<a[^>]+href=[\"'][^\"']*/novel/detail\.series\?productNo=(\d+)[^\"']*[\"'][^>]*>([\s\S]*?)</a>",
        page,
        flags=re.IGNORECASE,
    ):
        if product not in seen:
            seen.add(product)
            candidates.append({"id": product, "title": _strip_tags(title)})
    return candidates


def _parse_series_detail(page: str) -> Tuple[str, Optional[int], Optional[float]]:
    title_match = (
        re.search(r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)[\"']", page, flags=re.IGNORECASE)
        or re.search(r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:title[\"']", page, flags=re.IGNORECASE)
        or re.search(r"<title[^>]*>([\s\S]*?)</title>", page, flags=re.IGNORECASE)
    )
    title = _strip_tags(title_match.group(1)) if title_match else ""
    title = re.sub(r"\s*:\s*네이버시리즈\s*$", "", title).strip()
    download = re.search(
        r"class=[\"'][^\"']*btn_download[^\"']*[\"'][\s\S]*?<span[^>]*>([\s\S]*?)</span>",
        page,
        flags=re.IGNORECASE,
    )
    rating = re.search(
        r"class=[\"'][^\"']*score_area[^\"']*[\"'][\s\S]*?<em[^>]*>\s*([0-9.]+)\s*</em>",
        page,
        flags=re.IGNORECASE,
    )
    return (
        title,
        _count(_strip_tags(download.group(1))) if download else None,
        _rating(rating.group(1), maximum=RATING_SCALES["series"]) if rating else None,
    )


def lookup_series(
    title: str,
    fetch_text: Callable[[str, float], str] = _http_text,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> PlatformStat:
    search_url = "https://series.naver.com/search/search.series?" + urlencode(
        {"t": "all", "fs": "novel", "q": title}
    )
    search_page = fetch_text(search_url, timeout)
    candidates = _parse_series_candidates(search_page)
    if not candidates and "검색결과가 없습니다" not in search_page:
        raise ValueError("Naver search response did not contain results or no-result marker")
    if candidates and not any(item["title"] for item in candidates):
        raise ValueError("Naver search result items have an unexpected title structure")
    candidate = next((item for item in candidates if titles_match(title, item["title"])), None)
    if candidate is None:
        return _not_found("series")
    detail_url = "https://series.naver.com/novel/detail.series?" + urlencode(
        {"productNo": candidate["id"]}
    )
    detail_title, download_count, rating = _parse_series_detail(fetch_text(detail_url, timeout))
    if not titles_match(title, detail_title or candidate["title"]):
        return _not_found("series")
    stat = PlatformStat(
        platform="series",
        status="ok",
        remote_id=candidate["id"],
        remote_title=detail_title or candidate["title"],
        remote_url=detail_url,
        download_count=download_count,
        rating=rating,
    )
    return stat if _has_metrics(stat) else _error("series", RuntimeError("지표를 찾지 못했습니다"))


def _kakao_api_candidates(data: object) -> List[Dict[str, object]]:
    if not isinstance(data, dict):
        raise ValueError("Kakao search response is not an object")
    result = data.get("result")
    if not isinstance(result, dict):
        raise ValueError("Kakao search response has no result object")
    items = result.get("list")
    if not isinstance(items, list):
        raise ValueError("Kakao search response has no result.list array")
    candidates = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content_id = _first_value(item, ("series_id", "seriesId", "id"))
        candidate_title = _first_value(item, ("title", "name"))
        if content_id and candidate_title:
            props = item.get("service_property") or item.get("serviceProperty") or {}
            candidates.append({
                "id": str(content_id),
                "title": str(candidate_title),
                "view_count": _count(_first_value(props, ("view_count", "viewCount"))),
            })
    if items and not candidates:
        raise ValueError("Kakao search result items have an unexpected structure")
    return candidates


def _parse_kakao_overview(
    data: object,
) -> Tuple[str, Optional[int], Optional[float], Optional[int]]:
    result = data.get("result") if isinstance(data, dict) else None
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, dict):
        raise ValueError("Kakao overview response has no result.content object")
    props = content.get("service_property") or content.get("serviceProperty") or {}
    rating_value = _first_value(props, ("ratingAverage", "ratingAvg", "rating"))
    rating_count = _count(_first_value(props, ("ratingCount", "rating_count")))
    if rating_value is None:
        rating_sum = _number(_first_value(props, ("ratingSum", "rating_sum")))
        if rating_sum is not None and rating_count:
            rating_value = rating_sum / rating_count
    return (
        str(_first_value(content, ("title", "name", "seoTitle")) or ""),
        _count(_first_value(props, ("viewCount", "view_count", "readCount", "read_count"))),
        _rating(rating_value, maximum=RATING_SCALES["kakao"]),
        rating_count,
    )


def lookup_kakao(
    title: str,
    fetch_json: Callable[[str, float], object] = _http_json,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> PlatformStat:
    params = {
        "keyword": title,
        # The library contains novels; category 0 can return a same-title webtoon first.
        "category_uid": "11",
        "is_complete": "false",
        "sort_type": "ACCURACY",
        "page": "0",
        "size": "5",
    }
    candidates = _kakao_api_candidates(fetch_json(
        f"{_KAKAO_BFF_ORIGIN}/api/gateway/api/v2/search/series?" + urlencode(params),
        timeout,
    ))
    matched = [item for item in candidates if titles_match(title, str(item["title"]))]
    for candidate in matched[:3]:
        content_id = str(candidate["id"])
        detail_url = f"https://page.kakao.com/content/{content_id}"
        overview_url = (
            f"{_KAKAO_BFF_ORIGIN}/api/gateway/api/v1/content/overview?"
            + urlencode({"series_id": content_id})
        )
        detail_title, views, rating, rating_count = _parse_kakao_overview(
            fetch_json(overview_url, timeout)
        )
        if not titles_match(title, detail_title):
            continue
        stat = PlatformStat(
            platform="kakao",
            status="ok",
            remote_id=content_id,
            remote_title=detail_title,
            remote_url=detail_url,
            view_count=views if views is not None else candidate.get("view_count"),
            rating=rating,
            rating_count=rating_count,
        )
        return stat if _has_metrics(stat) else _error("kakao", RuntimeError("지표를 찾지 못했습니다"))
    return _not_found("kakao")


def _novelpia_title(record: object) -> str:
    return str(_first_value(record, ("novel_name", "novelName", "title", "name", "subject")) or "")


def lookup_novelpia(
    title: str,
    fetch_json: Callable[[str, float], object] = _http_json,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> PlatformStat:
    params = {
        "cmd": "novel_search",
        "page": "1",
        "rows": "30",
        "search_type": "novel_name",
        "search_val": title,
        "novel_type": "",
        "start_count_book": "",
        "end_count_book": "",
        "novel_age": "",
        "start_days": "",
        "sort_col": "last_viewdate",
        "novel_genre": "",
        "block_out": "0",
        "block_stop": "0",
        "is_contest": "0",
        "is_complete": "",
        "is_challenge": "",
        "list_display": "list",
    }
    data = fetch_json("https://novelpia.com/proc/novel?" + urlencode(params), timeout)
    if not isinstance(data, dict) or not isinstance(data.get("list"), list):
        raise ValueError("Novelpia search response has no list array")
    items = data["list"]
    candidates = [item for item in items if isinstance(item, dict) and _novelpia_title(item)]
    if items and not candidates:
        raise ValueError("Novelpia search result items have an unexpected structure")
    candidate = next((item for item in candidates if titles_match(title, _novelpia_title(item))), None)
    if candidate is None:
        return _not_found("novelpia")
    remote_id = _first_value(candidate, ("novel_no", "novelNo", "novel_id", "novelId", "id"))
    remote_id_text = str(remote_id) if remote_id is not None else None
    stat = PlatformStat(
        platform="novelpia",
        status="ok",
        remote_id=remote_id_text,
        remote_title=_novelpia_title(candidate),
        remote_url=(f"https://novelpia.com/novel/{remote_id_text}" if remote_id_text else None),
        view_count=_count(_first_value(candidate, ("count_view", "view_count", "viewCount", "hit", "hits"))),
        recommend_count=_count(_first_value(candidate, ("count_good", "good_count", "goodCount", "recommend", "recommend_count"))),
        rating=_rating(_first_value(candidate, ("rating", "rating_average", "ratingAverage", "score"))),
        rating_count=_count(_first_value(candidate, ("rating_count", "ratingCount", "count_rating"))),
    )
    return stat if _has_metrics(stat) else _error("novelpia", RuntimeError("지표를 찾지 못했습니다"))


def lookup_platforms(
    title: str,
    platforms: Sequence[str] = PLATFORMS,
    *,
    fetch_text: Callable[[str, float], str] = _http_text,
    fetch_json: Callable[[str, float], object] = _http_json,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> List[PlatformStat]:
    if not platforms:
        return []
    if len(set(platforms)) != len(platforms):
        raise ValueError("platform lookup list must not contain duplicates")
    lookups = {
        "series": lambda: lookup_series(title, fetch_text, timeout=timeout),
        "kakao": lambda: lookup_kakao(title, fetch_json, timeout=timeout),
        "novelpia": lambda: lookup_novelpia(title, fetch_json, timeout=timeout),
    }
    for platform in platforms:
        if platform not in lookups:
            raise ValueError(f"unknown platform: {platform}")

    # 한 제목 안에서 서로 다른 플랫폼만 병렬 조회한다. 다음 제목은 이 세 작업이
    # 모두 끝난 뒤 시작하므로 같은 플랫폼에 동시 요청이 쌓이지 않는다.
    with ThreadPoolExecutor(
        max_workers=min(len(platforms), len(PLATFORMS)),
        thread_name_prefix="platform-catalog",
    ) as executor:
        futures = {platform: executor.submit(lookups[platform]) for platform in platforms}
        results = []
        for platform in platforms:
            try:
                results.append(futures[platform].result())
            except Exception as exc:
                results.append(_error(platform, exc))
        return results


def _all_platforms_not_found(conn: sqlite3.Connection, title_key: str) -> bool:
    rows = {
        row["platform"]: row["status"]
        for row in conn.execute(
            "SELECT platform, status FROM catalog_platform_stats WHERE title_key = ?",
            (title_key,),
        )
    }
    return all(rows.get(platform) == "not_found" for platform in PLATFORMS)


def _all_platforms_not_found_after(
    conn: sqlite3.Connection,
    title_key: str,
    results: Sequence[PlatformStat],
) -> bool:
    statuses = {
        row["platform"]: row["status"]
        for row in conn.execute(
            "SELECT platform, status FROM catalog_platform_stats WHERE title_key = ?",
            (title_key,),
        )
    }
    statuses.update({result.platform: result.status for result in results})
    return all(statuses.get(platform) == "not_found" for platform in PLATFORMS)


def _authenticated_lookup_batch(
    lookup: Callable[..., PlatformStat],
    titles: Sequence[str],
    *,
    timeout: float,
    delay_seconds: float,
    sleep: Callable[[float], None],
) -> List[PlatformStat]:
    owner = getattr(lookup, "__self__", None)
    batch_lookup = getattr(owner, "lookup_batch", None)
    if callable(batch_lookup):
        return batch_lookup(
            titles,
            timeout=timeout,
            delay_seconds=delay_seconds,
            sleep=sleep,
        )
    results = []
    for index, title in enumerate(titles):
        try:
            results.append(lookup(title, timeout=timeout))
        except NovelpiaAuthenticationError:
            raise
        except Exception as exc:
            results.append(_error("novelpia", exc))
        if index + 1 < len(titles) and delay_seconds:
            sleep(delay_seconds)
    return results


def select_authenticated_novelpia_targets(
    conn: sqlite3.Connection,
    *,
    limit: Optional[int] = None,
    attempted_before: Optional[datetime] = None,
) -> List[CatalogTitle]:
    """Select active titles whose three public platform searches all missed."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    if limit == 0:
        return []
    stats = _stats_by_title(conn)
    targets = []
    for title in discover_catalog_titles(conn):
        by_platform = stats.get(title.title_key, {})
        if not all(
            by_platform.get(platform) is not None
            and by_platform[platform]["status"] == "not_found"
            for platform in PLATFORMS
        ):
            continue
        novelpia_attempt = _parse_time(by_platform["novelpia"]["last_attempt_at"])
        if (
            attempted_before is not None
            and novelpia_attempt is not None
            and novelpia_attempt > attempted_before
        ):
            continue
        targets.append(title)
        if limit is not None and len(targets) >= limit:
            break
    return targets


def preview_authenticated_novelpia_refresh(
    state_db_path: str,
    *,
    limit: Optional[int] = None,
    attempted_before: Optional[datetime] = None,
) -> Dict[str, object]:
    conn = decision_store.connect_state_db_readonly(state_db_path)
    try:
        decision_store.validate_schema(conn)
        targets = select_authenticated_novelpia_targets(
            conn,
            limit=limit,
            attempted_before=attempted_before,
        )
        return {
            "dry_run": True,
            "selected_titles": len(targets),
            "selected_platforms": len(targets),
            "titles": [title.query_title for title in targets],
        }
    finally:
        conn.close()


def refresh_authenticated_novelpia(
    state_db_path: str,
    client: AuthenticatedNovelpiaClient,
    *,
    limit: Optional[int] = None,
    attempted_before: Optional[datetime] = None,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    error_retry_seconds: int = DEFAULT_ERROR_RETRY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = utc_now,
    progress: Optional[Callable[[Dict[str, object]], None]] = None,
) -> Dict[str, object]:
    """Retry triple-not-found titles through one authenticated NovelPia session."""
    if delay_seconds < 0:
        raise ValueError("delay_seconds must be non-negative")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    client.login()
    conn = decision_store.connect_state_db(state_db_path)
    try:
        decision_store.validate_schema(conn)
        synced = sync_catalog_titles(conn)
        targets = select_authenticated_novelpia_targets(
            conn,
            limit=limit,
            attempted_before=attempted_before,
        )
        status_counts = {"ok": 0, "not_found": 0, "error": 0, "skipped": 0}
        if progress is not None:
            progress({
                "phase": "auth_start",
                "selected_titles": len(targets),
                "selected_platforms": len(targets),
            })
        completed = 0
        for start in range(0, len(targets), NOVELPIA_AUTH_BATCH_SIZE):
            chunk = targets[start:start + NOVELPIA_AUTH_BATCH_SIZE]
            stats = client.lookup_batch(
                [title.query_title for title in chunk],
                timeout=timeout,
                delay_seconds=delay_seconds,
                sleep=sleep,
            )
            if len(stats) != len(chunk) or any(
                stat.platform != "novelpia" for stat in stats
            ):
                raise RuntimeError(
                    "authenticated lookup batch returned invalid NovelPia results"
                )
            # lookup_batch verifies the session after the whole chunk. Only now
            # may these results escape into persistent DB state.
            for title, stat in zip(chunk, stats):
                record_platform_stats(
                    conn,
                    title.title_key,
                    [stat],
                    now=now(),
                    error_retry_seconds=error_retry_seconds,
                )
                completed += 1
                status_counts[stat.status] = status_counts.get(stat.status, 0) + 1
                if progress is not None:
                    progress({
                        "phase": "auth_progress",
                        "completed_titles": completed,
                        "selected_titles": len(targets),
                        "completed_platforms": completed,
                        "selected_platforms": len(targets),
                        "status_counts": dict(status_counts),
                    })
            if start + len(chunk) < len(targets) and delay_seconds:
                sleep(delay_seconds)
        return {
            "dry_run": False,
            **synced,
            "selected_titles": len(targets),
            "selected_platforms": len(targets),
            "status_counts": status_counts,
            "authenticated_novelpia_relogins": client.relogin_count,
        }
    finally:
        conn.close()


def refresh_existing_metrics(
    state_db_path: str,
    *,
    limit: Optional[int] = None,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    dry_run: bool = False,
    authenticated_novelpia_lookup: Optional[Callable[..., PlatformStat]] = None,
    lookup: Callable[..., List[PlatformStat]] = lookup_platforms,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = utc_now,
    progress: Optional[Callable[[Dict[str, object]], None]] = None,
) -> Dict[str, object]:
    """Refresh existing successful metrics without allowing counters to fall."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    if delay_seconds < 0:
        raise ValueError("delay_seconds must be non-negative")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if dry_run:
        return preview_existing_metric_refresh(state_db_path, limit=limit)

    conn = decision_store.connect_state_db(state_db_path)
    try:
        decision_store.validate_schema(conn)
        if progress is not None:
            progress({"phase": "sync_start"})
        synced = sync_catalog_titles(conn)
        targets = select_existing_metric_targets(conn, limit=limit)
        selected_platforms = sum(len(target.platforms) for target in targets)
        outcome_counts = {
            "updated": 0,
            "unchanged": 0,
            "not_found": 0,
            "error": 0,
            "skipped": 0,
        }
        authenticated_novelpia_attempts = 0
        if progress is not None:
            progress({
                "phase": "existing_start",
                "discovered_titles": synced["discovered"],
                "selected_titles": len(targets),
                "selected_platforms": selected_platforms,
            })
        completed_titles = 0
        pending_authenticated = []

        def report_completed(target, results, authenticated=None):
            nonlocal completed_titles, authenticated_novelpia_attempts
            final_results = list(results)
            if authenticated is not None:
                final_results = [
                    result for result in final_results
                    if result.platform != "novelpia"
                ] + [authenticated]
                authenticated_novelpia_attempts += 1
            outcomes = record_increased_platform_stats(
                conn,
                target.title.title_key,
                final_results,
                now=now(),
            )
            for outcome in outcomes.values():
                outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
            completed_titles += 1
            if progress is not None:
                progress({
                    "phase": "existing_progress",
                    "completed_titles": completed_titles,
                    "selected_titles": len(targets),
                    "completed_platforms": sum(outcome_counts.values()),
                    "selected_platforms": selected_platforms,
                    "outcome_counts": dict(outcome_counts),
                    "authenticated_novelpia_attempts": (
                        authenticated_novelpia_attempts
                    ),
                })

        def flush_authenticated():
            if not pending_authenticated:
                return
            authenticated_results = _authenticated_lookup_batch(
                authenticated_novelpia_lookup,
                [target.title.query_title for target, _results in pending_authenticated],
                timeout=timeout,
                delay_seconds=delay_seconds,
                sleep=sleep,
            )
            if len(authenticated_results) != len(pending_authenticated) or any(
                result.platform != "novelpia" for result in authenticated_results
            ):
                raise RuntimeError(
                    "authenticated lookup batch returned invalid NovelPia results"
                )
            for (target, results), authenticated in zip(
                pending_authenticated, authenticated_results
            ):
                report_completed(target, results, authenticated)
            pending_authenticated.clear()

        for index, target in enumerate(targets):
            results = lookup(target.title.query_title, target.platforms, timeout=timeout)
            if {result.platform for result in results} != set(target.platforms):
                raise RuntimeError(
                    "platform lookup did not return exactly the requested platforms"
                )
            if (
                authenticated_novelpia_lookup is not None
                and "novelpia" in target.platforms
            ):
                novelpia_index = next(
                    i for i, result in enumerate(results)
                    if result.platform == "novelpia"
                )
                if results[novelpia_index].status == "not_found":
                    pending_authenticated.append((target, results))
                    if len(pending_authenticated) >= NOVELPIA_AUTH_BATCH_SIZE:
                        flush_authenticated()
                else:
                    report_completed(target, results)
            else:
                report_completed(target, results)
            if index + 1 < len(targets) and delay_seconds:
                sleep(delay_seconds)
        flush_authenticated()
        return {
            "dry_run": False,
            **synced,
            "selected_titles": len(targets),
            "selected_platforms": selected_platforms,
            "outcome_counts": outcome_counts,
            "authenticated_novelpia_attempts": authenticated_novelpia_attempts,
            "authenticated_novelpia_relogins": int(getattr(
                getattr(authenticated_novelpia_lookup, "__self__", None),
                "relogin_count",
                0,
            )),
        }
    finally:
        conn.close()


def refresh_catalog(
    state_db_path: str,
    *,
    limit: Optional[int] = DEFAULT_LIMIT,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retry_not_found: bool = False,
    refresh_after_days: Optional[float] = None,
    force: bool = False,
    failed_retry: bool = False,
    failure_retry_cutoff: Optional[datetime] = None,
    dry_run: bool = False,
    error_retry_seconds: int = DEFAULT_ERROR_RETRY_SECONDS,
    authenticated_novelpia_lookup: Optional[Callable[..., PlatformStat]] = None,
    lookup: Callable[..., List[PlatformStat]] = lookup_platforms,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = utc_now,
    progress: Optional[Callable[[Dict[str, object]], None]] = None,
) -> Dict[str, object]:
    """Fill only missing/due platform records in bounded, delayed batches."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    if delay_seconds < 0:
        raise ValueError("delay_seconds must be non-negative")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if refresh_after_days is not None and refresh_after_days < 0:
        raise ValueError("refresh_after_days must be non-negative")
    if dry_run:
        return preview_catalog_refresh(
            state_db_path,
            limit=limit,
            retry_not_found=retry_not_found,
            refresh_after_days=refresh_after_days,
            force=force,
            failed_retry=failed_retry,
            failure_retry_cutoff=failure_retry_cutoff,
            now=now,
        )

    conn = decision_store.connect_state_db(state_db_path)
    try:
        decision_store.validate_schema(conn)
        if progress is not None:
            progress({"phase": "sync_start"})
        current = now()
        refresh_before = (
            current - timedelta(days=refresh_after_days)
            if refresh_after_days is not None else None
        )
        synced = sync_catalog_titles(conn)
        targets = select_refresh_targets(
            conn,
            limit=limit,
            now=current,
            retry_not_found=retry_not_found,
            refresh_before=refresh_before,
            force=force,
            failed_retry=failed_retry,
            failure_retry_cutoff=failure_retry_cutoff,
        )
        status_counts: Dict[str, int] = {"ok": 0, "not_found": 0, "error": 0, "skipped": 0}
        authenticated_counts: Dict[str, int] = {
            "ok": 0, "not_found": 0, "error": 0, "skipped": 0
        }
        authenticated_attempts = 0
        selected_platforms = sum(len(target.platforms) for target in targets)
        if progress is not None:
            progress({
                "phase": "start",
                "discovered_titles": synced["discovered"],
                "selected_titles": len(targets),
                "selected_platforms": selected_platforms,
            })
        completed_titles = 0
        pending_authenticated = []

        def report_completed(target, results, authenticated=None):
            nonlocal completed_titles, authenticated_attempts
            final_results = list(results)
            if authenticated is not None:
                final_results = [
                    result for result in final_results
                    if result.platform != "novelpia"
                ] + [authenticated]
            record_platform_stats(
                conn,
                target.title.title_key,
                final_results,
                now=now(),
                error_retry_seconds=error_retry_seconds,
            )
            for result in results:
                status_counts[result.status] = status_counts.get(result.status, 0) + 1
            if authenticated is not None:
                authenticated_attempts += 1
                authenticated_counts[authenticated.status] = (
                    authenticated_counts.get(authenticated.status, 0) + 1
                )
            completed_titles += 1
            if progress is not None:
                progress({
                    "phase": "progress",
                    "completed_titles": completed_titles,
                    "selected_titles": len(targets),
                    "completed_platforms": sum(status_counts.values()),
                    "selected_platforms": selected_platforms,
                    "status_counts": dict(status_counts),
                    "authenticated_novelpia_attempts": authenticated_attempts,
                    "authenticated_novelpia_status_counts": dict(authenticated_counts),
                })

        def flush_authenticated():
            if not pending_authenticated:
                return
            authenticated_results = _authenticated_lookup_batch(
                authenticated_novelpia_lookup,
                [target.title.query_title for target, _results in pending_authenticated],
                timeout=timeout,
                delay_seconds=delay_seconds,
                sleep=sleep,
            )
            if len(authenticated_results) != len(pending_authenticated) or any(
                result.platform != "novelpia" for result in authenticated_results
            ):
                raise RuntimeError(
                    "authenticated lookup batch returned invalid NovelPia results"
                )
            for (target, results), authenticated in zip(
                pending_authenticated, authenticated_results
            ):
                report_completed(target, results, authenticated)
            pending_authenticated.clear()

        for index, target in enumerate(targets):
            results = lookup(target.title.query_title, target.platforms, timeout=timeout)
            if {result.platform for result in results} != set(target.platforms):
                raise RuntimeError("platform lookup did not return exactly the requested platforms")
            if (
                authenticated_novelpia_lookup is not None
                and _all_platforms_not_found_after(
                    conn, target.title.title_key, results
                )
            ):
                pending_authenticated.append((target, results))
                if len(pending_authenticated) >= NOVELPIA_AUTH_BATCH_SIZE:
                    flush_authenticated()
            else:
                report_completed(target, results)
            if index + 1 < len(targets) and delay_seconds:
                sleep(delay_seconds)
        flush_authenticated()
        return {
            "dry_run": False,
            **synced,
            "selected_titles": len(targets),
            "selected_platforms": selected_platforms,
            "status_counts": status_counts,
            "authenticated_novelpia_attempts": authenticated_attempts,
            "authenticated_novelpia_status_counts": authenticated_counts,
            "authenticated_novelpia_relogins": int(getattr(
                getattr(authenticated_novelpia_lookup, "__self__", None),
                "relogin_count",
                0,
            )),
        }
    finally:
        conn.close()


def catalog_status(state_db_path: str) -> Dict[str, object]:
    conn = decision_store.connect_state_db_readonly(state_db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        titles = discover_catalog_titles(conn)
        active_keys = {title.title_key for title in titles}
        stats = _stats_by_title(conn) if "catalog_platform_stats" in tables else {}
        by_status: Dict[str, int] = {}
        for title_key in active_keys:
            for row in stats.get(title_key, {}).values():
                by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        pending = _refresh_targets(
            titles,
            stats,
            limit=None,
            now=utc_now(),
        )
        return {
            "catalog_schema_ready": "catalog_platform_stats" in tables,
            "titles": len(titles),
            "platform_status": by_status,
            "pending_titles": len(pending),
            "pending_platforms": sum(len(target.platforms) for target in pending),
        }
    finally:
        conn.close()


_ORDER_COLUMNS = {
    "series-download": "series_download_count",
    # 1.2.4 CLI compatibility; the underlying Naver metric is download/use count.
    "series-interest": "series_download_count",
    "series-rating": "series_rating",
    "kakao-view": "kakao_view_count",
    "kakao-rating": "kakao_rating",
    "novelpia-view": "novelpia_view_count",
    "novelpia-recommend": "novelpia_recommend_count",
}
_ORDER_STATUS_COLUMNS = {
    order: f"{order.split('-', 1)[0]}_status" for order in _ORDER_COLUMNS
}


def top_catalog_metrics(
    state_db_path: str,
    *,
    order_by: str,
    limit: int = 20,
) -> List[Dict[str, object]]:
    if order_by not in _ORDER_COLUMNS:
        raise ValueError(f"unknown order: {order_by}")
    if limit <= 0:
        raise ValueError("limit must be positive")
    column = _ORDER_COLUMNS[order_by]
    status_column = _ORDER_STATUS_COLUMNS[order_by]
    conn = decision_store.connect_state_db_readonly(state_db_path)
    try:
        views = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")
        }
        if "catalog_title_metrics" not in views:
            return []
        active_keys = {
            title.title_key for title in discover_catalog_titles(conn)
        }
        if not active_keys:
            return []
        rows = conn.execute(
            f"""
            SELECT * FROM catalog_title_metrics
            WHERE {column} IS NOT NULL AND {status_column} = 'ok'
            ORDER BY {column} DESC, display_title ASC
            """,
        ).fetchall()
        return [
            dict(row) for row in rows
            if row["title_key"] in active_keys
        ][:limit]
    finally:
        conn.close()
