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
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

import decision_store
from normalizer import NORMALIZER_VERSION


PLATFORMS = ("series", "kakao", "novelpia")
TAG_PLATFORMS = ("kakao", "novelpia")
IDENTITY_AUDIT_PLATFORMS = ("series", "kakao")
IDENTITY_TOMBSTONE_PREFIX = "platform_identity_tombstone_v1:"
METADATA_COMPLETION_PREFIX = "platform_metadata_completion_v1:"
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

    def fetch_text(self, url: str, timeout: float) -> str:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout
        return self._request_text(url)

    def fetch_json(self, url: str, timeout: float) -> object:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout
        return self._request_json(url)

    def _lookup_once(
        self, title: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
    ) -> PlatformStat:
        try:
            return lookup_novelpia(
                title,
                self.fetch_json,
                self.fetch_text,
                timeout=timeout,
            )
        except NovelpiaAuthenticationError:
            raise
        except Exception as exc:
            return _error("novelpia", exc)

    def _lookup_metadata_once(
        self,
        title: str,
        remote_id: str,
        remote_title: Optional[str] = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> PlatformStat:
        """Read metadata from one already-proven NovelPia remote object."""
        remote_id_text = str(remote_id or "").strip()
        if not remote_id_text:
            return _error("novelpia", RuntimeError("stored remote ID is missing"))
        try:
            detail_page = self.fetch_text(
                f"{_NOVELPIA_ORIGIN}/novel/{remote_id_text}", timeout
            )
        except NovelpiaAuthenticationError:
            raise
        except Exception as exc:
            return _error("novelpia", exc)
        cover_url = _parse_open_graph_cover(detail_page)
        try:
            tags = _parse_novelpia_tags(detail_page)
        except Exception:
            tags = None
        if cover_url is None and tags is None:
            return _error(
                "novelpia",
                RuntimeError("NovelPia detail response has no cover or tag metadata"),
            )
        return PlatformStat(
            platform="novelpia",
            status="ok",
            remote_id=remote_id_text,
            remote_title=str(remote_title or title).strip(),
            remote_url=f"{_NOVELPIA_ORIGIN}/novel/{remote_id_text}",
            cover_url=cover_url,
            genre=(tags[0] if tags else "") if tags is not None else None,
            tags=tags,
            message=("" if cover_url is not None else "no https cover in detail response"),
            metadata_lookup_mode="authenticated",
        )

    def lookup_metadata_batch(
        self,
        items: Sequence[Tuple[str, str, Optional[str]]],
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        delay_seconds: float = 0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> List[PlatformStat]:
        """Read stored-ID metadata in session-verified chunks without title search."""
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be non-negative")
        self.login()
        results: List[PlatformStat] = []
        for start in range(0, len(items), NOVELPIA_AUTH_BATCH_SIZE):
            chunk = list(items[start:start + NOVELPIA_AUTH_BATCH_SIZE])

            def read_chunk() -> List[PlatformStat]:
                attempted = []
                for index, (title, remote_id, remote_title) in enumerate(chunk):
                    attempted.append(self._lookup_metadata_once(
                        title,
                        remote_id,
                        remote_title,
                        timeout=timeout,
                    ))
                    if index + 1 < len(chunk) and delay_seconds:
                        sleep(delay_seconds)
                return attempted

            attempted = read_chunk()
            try:
                self.verify_session()
            except NovelpiaSessionExpiredError:
                self._logged_in = False
                self.relogin_count += 1
                self.login()
                attempted = read_chunk()
                self.verify_session()
            results.extend(attempted)
            self._lookup_count += len(chunk)
        return results

    def lookup_metadata(
        self,
        title: str,
        remote_id: str,
        remote_title: Optional[str] = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> PlatformStat:
        return self.lookup_metadata_batch(
            [(title, remote_id, remote_title)], timeout=timeout
        )[0]

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
    author: Optional[str] = None


@dataclass(frozen=True)
class RefreshTarget:
    title: CatalogTitle
    platforms: Tuple[str, ...]
    title_updated_at: Optional[str] = None
    remote_hints: Tuple[Tuple[str, Optional[str], Optional[str]], ...] = ()
    row_hints: Tuple[
        Tuple[str, Optional[str], Optional[str], Optional[str]], ...
    ] = ()


@dataclass(frozen=True)
class PlatformStat:
    platform: str
    status: str
    remote_id: Optional[str] = None
    remote_title: Optional[str] = None
    remote_url: Optional[str] = None
    cover_url: Optional[str] = None
    download_count: Optional[int] = None
    view_count: Optional[int] = None
    recommend_count: Optional[int] = None
    rating: Optional[float] = None
    rating_count: Optional[int] = None
    genre: Optional[str] = None
    tags: Optional[Tuple[str, ...]] = None
    message: str = ""
    metadata_lookup_mode: Optional[str] = None


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


_PRESENTATION_PUNCTUATION_RE = re.compile(
    r"[\s:()\[\]{},.\u3002\u2026?!\-\u2010-\u2015_\u00b7\u318d\u30fb"
    r"\u201c\u201d\u300c\u300d\u300e\u300f]+"
)


def _normalized_platform_title(value: str) -> str:
    """Normalize only presentation punctuation; preserve identity-bearing symbols."""
    text = unicodedata.normalize("NFKC", html.unescape(str(value or ""))).strip()
    text = re.sub(r"\s*:\s*네이버시리즈\s*$", "", text, flags=re.IGNORECASE)
    previous = None
    while text != previous:
        previous = text
        text = re.sub(
            r"\s*\(\s*총\s*[\d,]+\s*(?:화|권|편)"
            r"(?:\s*/\s*[^)]+)?\)\s*$",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        text = re.sub(
            r"\s*\[\s*(?:단행본|독점|미니노블)\s*\]\s*$",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
    return _PRESENTATION_PUNCTUATION_RE.sub("", text.casefold())


def titles_match(requested_title: str, candidate_title: str) -> bool:
    """Compare full titles while preserving symbols such as ``+``, ``&`` and ``#``."""
    requested_exact = _normalized_platform_title(requested_title)
    candidate_exact = _normalized_platform_title(candidate_title)
    return bool(requested_exact) and requested_exact == candidate_exact


def _normalized_author(value: object) -> str:
    return re.sub(
        r"\s+", "", unicodedata.normalize("NFKC", html.unescape(str(value or "")))
    ).casefold()


def _candidate_author(value: object) -> Optional[str]:
    """Best-effort author extraction from platform search result shapes."""
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, dict):
        for key in (
            "name", "author_name", "authorName", "writer_name", "writerName",
            "writer_nick", "writerNick", "nickname", "nick_name", "nickName",
        ):
            if value.get(key) is not None:
                extracted = _candidate_author(value.get(key))
                if extracted:
                    return extracted
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            extracted = _candidate_author(item)
            if extracted:
                return extracted
    return None


def _search_candidate_author(record: object) -> Optional[str]:
    if not isinstance(record, dict):
        return None
    for key in (
        "author", "authors", "writer", "writers", "creator", "creators",
        "writer_nick", "writerNick", "writer_name", "writerName",
    ):
        if key in record:
            extracted = _candidate_author(record.get(key))
            if extracted:
                return extracted
    return None


def _select_unique_title_candidate(
    platform: str,
    requested_title: str,
    candidates: Sequence[Dict[str, object]],
    *,
    requested_author: Optional[str] = None,
) -> Tuple[Optional[Dict[str, object]], Optional[PlatformStat]]:
    matched = [
        item for item in candidates
        if titles_match(requested_title, str(item.get("title") or ""))
    ]
    if requested_author:
        local_author = _normalized_author(requested_author)
        author_matches = [
            item for item in matched
            if item.get("author") and _normalized_author(item.get("author")) == local_author
        ]
        author_mismatches = [
            item for item in matched
            if item.get("author") and _normalized_author(item.get("author")) != local_author
        ]
        if author_matches:
            matched = author_matches
        elif author_mismatches and len(author_mismatches) == len(matched):
            return None, _not_found(platform, "remote author mismatch")
    distinct = {
        str(item.get("id") or "") for item in matched if item.get("id") is not None
    }
    if len(distinct) > 1 or len(matched) > 1:
        return None, _error(
            platform,
            RuntimeError("ambiguous exact-title search candidates"),
        )
    return (matched[0], None) if matched else (None, None)


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


def _normalize_genre(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"platform genre must be text: {value!r}")
    genre = re.sub(r"\s+", " ", html.unescape(value)).strip()
    return re.sub(r"^#+\s*", "", genre).strip()


def _normalize_tags(values: Iterable[str]) -> Tuple[str, ...]:
    tags = []
    seen = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"platform tag must be text: {value!r}")
        tag = re.sub(r"\s+", " ", html.unescape(value)).strip()
        tag = re.sub(r"^#+\s*", "", tag).strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
    return tuple(tags)


def _normalize_cover_url(value: object) -> Optional[str]:
    if value is None:
        return None
    url = html.unescape(str(value)).strip()
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        return None
    return url


def _parse_open_graph_cover(page: str) -> Optional[str]:
    for pattern in (
        r"<meta\b[^>]*(?:property|name)=[\"'](?:og:image|og:image:secure_url)"
        r"[\"'][^>]*content=[\"']([^\"']+)[\"']",
        r"<meta\b[^>]*content=[\"']([^\"']+)[\"'][^>]*"
        r"(?:property|name)=[\"'](?:og:image|og:image:secure_url)[\"']",
    ):
        match = re.search(pattern, page, flags=re.IGNORECASE)
        if match:
            cover_url = _normalize_cover_url(match.group(1))
            if cover_url is not None:
                return cover_url
    return None


def _kakao_cover_url(value: object) -> Optional[str]:
    direct = _normalize_cover_url(value)
    if direct is not None:
        return direct
    image_key = str(value or "").strip()
    if not image_key or "://" in image_key:
        return None
    return "https://dn-img-page.kakao.com/download/resource?" + urlencode({
        "kid": image_key,
        "filename": "o1",
    })


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
            a.core_title, a.readable_title, a.catalog_query_title, a.author
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
    author_conflicts: set[str] = set()
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
        author = str(row["author"] or "").strip() or None
        candidate = CatalogTitle(
            title_key=title_key,
            display_title=query_title or readable_title or title_key,
            query_title=query_title or readable_title or title_key,
            author=author,
        )
        current = titles.get(title_key)
        if (
            current is not None
            and current.author
            and candidate.author
            and _normalized_author(current.author) != _normalized_author(candidate.author)
        ):
            author_conflicts.add(title_key)
        if current is None or len(candidate.display_title) < len(current.display_title):
            selected = candidate
        else:
            selected = current
            if selected.author is None and candidate.author and title_key not in author_conflicts:
                selected = replace(selected, author=candidate.author)
        if title_key in author_conflicts and selected.author is not None:
            selected = replace(selected, author=None)
        titles[title_key] = selected
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
            # Failure rows are sticky during the ordinary update. Explicit retry
            # paths may select them, but existing successful rows never flow
            # through this search-first selector.
            if force:
                needed.append(platform)
            continue
        if status == "not_found":
            if retry_not_found or force:
                needed.append(platform)
            continue
        # Existing successes are refreshed only by the stored-ID monotonic path.
        # ``refresh_before`` is intentionally ignored here and is routed by the
        # CLI/service orchestrator to refresh_existing_metrics().
    return tuple(needed)


def _stats_by_title(conn: sqlite3.Connection) -> Dict[str, Dict[str, sqlite3.Row]]:
    values: Dict[str, Dict[str, sqlite3.Row]] = {}
    for row in conn.execute("SELECT * FROM catalog_platform_stats"):
        values.setdefault(row["title_key"], {})[row["platform"]] = row
    return values


def _catalog_title_rows(conn: sqlite3.Connection) -> Dict[str, sqlite3.Row]:
    return {
        row["title_key"]: row
        for row in conn.execute(
            "SELECT title_key, query_title, updated_at FROM catalog_titles"
        )
    }


def _identity_tombstone_key(title_key: str, platform: str) -> str:
    return f"{IDENTITY_TOMBSTONE_PREFIX}{platform}:{title_key}"


def _load_identity_tombstones(conn: sqlite3.Connection) -> Dict[Tuple[str, str], Dict[str, object]]:
    tombstones: Dict[Tuple[str, str], Dict[str, object]] = {}
    for row in conn.execute(
        "SELECT key, value FROM settings WHERE key LIKE ?",
        (f"{IDENTITY_TOMBSTONE_PREFIX}%",),
    ):
        try:
            payload = json.loads(row["value"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        title_key = str(payload.get("title_key") or "")
        platform = str(payload.get("platform") or "")
        if title_key and platform in PLATFORMS:
            tombstones[(title_key, platform)] = payload
    # 1.4.21 performed one verified manual invalidation before tombstones were
    # persisted. Preserve that fail-closed result across upgrades as well.
    for row in conn.execute(
        """
        SELECT s.title_key, s.platform, t.query_title, s.error_message
        FROM catalog_platform_stats AS s
        JOIN catalog_titles AS t ON t.title_key = s.title_key
        WHERE s.status = 'not_found'
          AND s.error_message LIKE 'verified wrong remote object:%'
        """
    ):
        tombstones.setdefault((row["title_key"], row["platform"]), {
            "title_key": row["title_key"],
            "query_title": row["query_title"],
            "platform": row["platform"],
            "rejected_remote_ids": [],
            "reason": row["error_message"],
            "legacy": True,
        })
    return tombstones


def _tombstone_applies(
    tombstones: Dict[Tuple[str, str], Dict[str, object]],
    title: CatalogTitle,
    platform: str,
) -> bool:
    payload = tombstones.get((title.title_key, platform))
    if payload is None:
        return False
    query_title = str(payload.get("query_title") or "")
    return not query_title or query_title == title.query_title


def _metadata_completion_key(cycle_id: str) -> str:
    return f"{METADATA_COMPLETION_PREFIX}{cycle_id}"


def _load_metadata_completion_cycle(
    conn: sqlite3.Connection, cycle_id: str
) -> Dict[str, object]:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?",
        (_metadata_completion_key(cycle_id),),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"metadata completion cycle disappeared: {cycle_id}")
    try:
        payload = json.loads(row["value"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("metadata completion cycle is invalid") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("pairs"), list):
        raise RuntimeError("metadata completion cycle is invalid")
    return payload


def _save_metadata_completion_cycle(
    conn: sqlite3.Connection, cycle_id: str, payload: Dict[str, object]
) -> None:
    conn.execute(
        "UPDATE settings SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?",
        (
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            _metadata_completion_key(cycle_id),
        ),
    )


def _create_metadata_completion_cycle(
    conn: sqlite3.Connection,
    targets: Sequence[RefreshTarget],
    *,
    now: datetime,
) -> Optional[str]:
    pairs = []
    for target in targets:
        row_hints = {
            platform: (status, remote_id, last_attempt_at)
            for platform, status, remote_id, last_attempt_at in target.row_hints
        }
        for platform in target.platforms:
            status, remote_id, last_attempt_at = row_hints.get(
                platform, (None, None, None)
            )
            pairs.append({
                "title_key": target.title.title_key,
                "platform": platform,
                "query_title": target.title.query_title,
                "title_updated_at": target.title_updated_at,
                "expected_status": status,
                "expected_remote_id": remote_id,
                "expected_last_attempt_at": last_attempt_at,
                "state": "awaiting_primary",
                "primary_remote_id": None,
                "last_outcome": None,
            })
    if not pairs:
        return None
    cycle_id = str(uuid.uuid4())
    payload = {
        "cycle_id": cycle_id,
        "created_at": _utc_text(now),
        "state": "active",
        "pairs": pairs,
    }
    with decision_store.transaction(conn):
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?)",
            (
                _metadata_completion_key(cycle_id),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
    return cycle_id


def _mark_metadata_completion_primary(
    conn: sqlite3.Connection,
    cycle_id: Optional[str],
    title_key: str,
    platform: str,
    *,
    outcome: str,
    stat: PlatformStat,
) -> None:
    if cycle_id is None:
        return
    payload = _load_metadata_completion_cycle(conn, cycle_id)
    for pair in payload["pairs"]:
        if pair.get("title_key") == title_key and pair.get("platform") == platform:
            pair["last_outcome"] = outcome
            if outcome in {"ok", "updated", "unchanged"} and stat.status == "ok":
                pair["state"] = "pending_metadata"
                pair["primary_remote_id"] = str(stat.remote_id)
            elif outcome in {
                "stale_target", "identity_conflict", "tombstoned", "unavailable"
            }:
                pair["state"] = "needs_review"
            else:
                pair["state"] = "primary_terminal"
            _save_metadata_completion_cycle(conn, cycle_id, payload)
            return
    raise RuntimeError("metadata completion pair disappeared")


def _metadata_snapshot_complete(row: sqlite3.Row, platform: str) -> bool:
    if row["genre_collected_at"] is None:
        return False
    return platform not in TAG_PLATFORMS or row["tags_collected_at"] is not None


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
    title_rows: Optional[Dict[str, sqlite3.Row]] = None,
    tombstones: Optional[Dict[Tuple[str, str], Dict[str, object]]] = None,
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
        if tombstones:
            platforms = tuple(
                platform for platform in platforms
                if not _tombstone_applies(tombstones, title, platform)
            )
        if platforms:
            rows = stats_by_title.get(title.title_key, {})
            title_row = (title_rows or {}).get(title.title_key)
            targets.append(RefreshTarget(
                title=title,
                platforms=platforms,
                title_updated_at=(title_row["updated_at"] if title_row is not None else None),
                remote_hints=tuple(
                    (
                        platform,
                        rows[platform]["remote_id"] if rows.get(platform) is not None else None,
                        rows[platform]["remote_title"] if rows.get(platform) is not None else None,
                    )
                    for platform in platforms
                ),
                row_hints=tuple(
                    (
                        platform,
                        rows[platform]["status"] if rows.get(platform) is not None else None,
                        rows[platform]["remote_id"] if rows.get(platform) is not None else None,
                        rows[platform]["last_attempt_at"] if rows.get(platform) is not None else None,
                    )
                    for platform in platforms
                ),
            ))
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
            title_rows=(
                _catalog_title_rows(conn) if "catalog_titles" in tables else {}
            ),
            tombstones=(
                _load_identity_tombstones(conn)
                if "catalog_platform_stats" in tables else {}
            ),
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
        title_rows=_catalog_title_rows(conn),
        tombstones=_load_identity_tombstones(conn),
    )


def _row_has_growth_metric(platform: str, row: sqlite3.Row) -> bool:
    return any(row[field] is not None for field in GROWTH_METRICS[platform])


def select_existing_metric_targets(
    conn: sqlite3.Connection,
    *,
    limit: Optional[int] = None,
    refresh_before: Optional[datetime] = None,
) -> List[RefreshTarget]:
    """Select active successful metrics together with their remote identity snapshot."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    if limit == 0:
        return []
    stats = _stats_by_title(conn)
    title_rows = {
        row["title_key"]: row
        for row in conn.execute(
            "SELECT title_key, query_title, updated_at FROM catalog_titles"
        )
    }
    targets = []
    for title in sorted(discover_catalog_titles(conn), key=lambda item: item.title_key):
        rows = stats.get(title.title_key, {})
        platforms = tuple(
            platform
            for platform in PLATFORMS
            if rows.get(platform) is not None
            and rows[platform]["status"] == "ok"
            and _row_has_growth_metric(platform, rows[platform])
            and rows[platform]["remote_id"] is not None
            and (
                refresh_before is None
                or _parse_time(rows[platform]["last_success_at"]) is None
                or _parse_time(rows[platform]["last_success_at"]) <= refresh_before
            )
        )
        if not platforms:
            continue
        title_row = title_rows.get(title.title_key)
        if title_row is None or title_row["query_title"] != title.query_title:
            continue
        targets.append(RefreshTarget(
            title=title,
            platforms=platforms,
            title_updated_at=title_row["updated_at"],
            remote_hints=tuple(
                (
                    platform,
                    rows[platform]["remote_id"],
                    rows[platform]["remote_title"],
                )
                for platform in platforms
            ),
        ))
        if limit is not None and len(targets) >= limit:
            break
    return targets


def select_metadata_backfill_targets(
    conn: sqlite3.Connection,
    *,
    limit: Optional[int] = None,
) -> List[RefreshTarget]:
    """Select successful rows missing a cover, genre, or supported tag snapshot."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    if limit == 0:
        return []
    stats = _stats_by_title(conn)
    title_rows = {
        row["title_key"]: row
        for row in conn.execute(
            "SELECT title_key, query_title, updated_at FROM catalog_titles"
        )
    }
    targets = []
    for title in sorted(discover_catalog_titles(conn), key=lambda item: item.title_key):
        rows = stats.get(title.title_key, {})
        platforms = tuple(
            platform
            for platform in PLATFORMS
            if rows.get(platform) is not None
            and rows[platform]["status"] == "ok"
            and (
                rows[platform]["cover_url"] is None
                or rows[platform]["genre_collected_at"] is None
                or (
                    platform in TAG_PLATFORMS
                    and rows[platform]["tags_collected_at"] is None
                )
            )
        )
        if not platforms:
            continue
        title_row = title_rows.get(title.title_key)
        if title_row is None or title_row["query_title"] != title.query_title:
            continue
        targets.append(RefreshTarget(
            title=title,
            platforms=platforms,
            title_updated_at=title_row["updated_at"],
            remote_hints=tuple(
                (
                    platform,
                    rows[platform]["remote_id"],
                    rows[platform]["remote_title"],
                )
                for platform in platforms
            ),
        ))
        if limit is not None and len(targets) >= limit:
            break
    return targets


def select_metadata_identity_targets(
    conn: sqlite3.Connection,
    *,
    limit: Optional[int] = None,
) -> List[RefreshTarget]:
    """Select active successful Series/Kakao rows for remote identity audit."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    if limit == 0:
        return []
    stats = _stats_by_title(conn)
    title_rows = {
        row["title_key"]: row
        for row in conn.execute(
            "SELECT title_key, query_title, updated_at FROM catalog_titles"
        )
    }
    targets = []
    for title in sorted(discover_catalog_titles(conn), key=lambda item: item.title_key):
        rows = stats.get(title.title_key, {})
        platforms = tuple(
            platform
            for platform in IDENTITY_AUDIT_PLATFORMS
            if rows.get(platform) is not None
            and rows[platform]["status"] == "ok"
            and rows[platform]["remote_id"] is not None
        )
        if not platforms:
            continue
        title_row = title_rows.get(title.title_key)
        if title_row is None or title_row["query_title"] != title.query_title:
            continue
        targets.append(RefreshTarget(
            title=title,
            platforms=platforms,
            title_updated_at=title_row["updated_at"],
            remote_hints=tuple(
                (
                    platform,
                    rows[platform]["remote_id"],
                    rows[platform]["remote_title"],
                )
                for platform in platforms
            ),
        ))
        if limit is not None and len(targets) >= limit:
            break
    return targets


def preview_metadata_identity_audit(
    state_db_path: str,
    *,
    limit: Optional[int] = None,
) -> Dict[str, object]:
    conn = decision_store.connect_state_db_readonly(state_db_path)
    try:
        decision_store.validate_schema(conn)
        targets = select_metadata_identity_targets(conn, limit=limit)
        return {
            "dry_run": True,
            "selected_titles": len(targets),
            "selected_platforms": sum(len(target.platforms) for target in targets),
            "titles": [target.title.display_title for target in targets],
        }
    finally:
        conn.close()


def preview_metadata_backfill(
    state_db_path: str,
    *,
    limit: Optional[int] = None,
) -> Dict[str, object]:
    conn = decision_store.connect_state_db_readonly(state_db_path)
    try:
        decision_store.validate_schema(conn)
        targets = select_metadata_backfill_targets(conn, limit=limit)
        return {
            "dry_run": True,
            "selected_titles": len(targets),
            "selected_platforms": sum(len(target.platforms) for target in targets),
            "titles": [target.title.display_title for target in targets],
        }
    finally:
        conn.close()


def preview_existing_metric_refresh(
    state_db_path: str,
    *,
    limit: Optional[int] = None,
    refresh_before: Optional[datetime] = None,
) -> Dict[str, object]:
    conn = decision_store.connect_state_db_readonly(state_db_path)
    try:
        decision_store.validate_schema(conn)
        targets = select_existing_metric_targets(
            conn, limit=limit, refresh_before=refresh_before
        )
        return {
            "dry_run": True,
            "selected_titles": len(targets),
            "selected_platforms": sum(len(target.platforms) for target in targets),
            "titles": [target.title.display_title for target in targets],
        }
    finally:
        conn.close()


def _canonical_remote_url(platform: str, remote_id: str) -> str:
    if platform == "series":
        return "https://series.naver.com/novel/detail.series?" + urlencode(
            {"productNo": remote_id}
        )
    if platform == "kakao":
        return f"https://page.kakao.com/content/{remote_id}"
    if platform == "novelpia":
        return f"https://novelpia.com/novel/{remote_id}"
    raise ValueError(f"unknown platform: {platform}")


def _validate_stat(
    stat: PlatformStat, *, require_metrics: bool = True
) -> PlatformStat:
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
    if stat.status == "ok":
        for label, value in (
            ("remote_id", stat.remote_id),
            ("remote_title", stat.remote_title),
        ):
            if value is None or not str(value).strip():
                raise ValueError(f"ok platform stat requires {label}")
        if stat.remote_url is None or not str(stat.remote_url).strip():
            stat = replace(
                stat,
                remote_url=_canonical_remote_url(stat.platform, str(stat.remote_id)),
            )
        if require_metrics and not _has_metrics(stat):
            raise ValueError("ok platform stat requires at least one metric")
    if stat.cover_url is not None:
        cover_url = _normalize_cover_url(stat.cover_url)
        if cover_url is None:
            raise ValueError("cover_url must be a direct https URL")
        stat = replace(stat, cover_url=cover_url)
    if stat.genre is not None:
        stat = replace(stat, genre=_normalize_genre(stat.genre))
    if stat.tags is not None:
        stat = replace(stat, tags=_normalize_tags(stat.tags))
    return stat


def _replace_platform_genre(
    conn: sqlite3.Connection,
    title_key: str,
    platform: str,
    genre: str,
    collected_at: str,
) -> None:
    conn.execute(
        """
        UPDATE catalog_platform_stats
        SET genre = ?, genre_collected_at = ?, updated_at = CURRENT_TIMESTAMP
        WHERE title_key = ? AND platform = ?
        """,
        (genre, collected_at, title_key, platform),
    )


def _replace_platform_tags(
    conn: sqlite3.Connection,
    title_key: str,
    platform: str,
    tags: Tuple[str, ...],
    collected_at: str,
) -> None:
    conn.execute(
        "DELETE FROM catalog_platform_tags WHERE title_key = ? AND platform = ?",
        (title_key, platform),
    )
    conn.executemany(
        """
        INSERT INTO catalog_platform_tags(title_key, platform, tag, position)
        VALUES (?, ?, ?, ?)
        """,
        (
            (title_key, platform, tag, position)
            for position, tag in enumerate(tags, start=1)
        ),
    )
    conn.execute(
        """
        UPDATE catalog_platform_stats
        SET tags_collected_at = ?, updated_at = CURRENT_TIMESTAMP
        WHERE title_key = ? AND platform = ?
        """,
        (collected_at, title_key, platform),
    )


def record_platform_metadata_results(
    conn: sqlite3.Connection,
    title_key: str,
    stats: Sequence[PlatformStat],
    *,
    now: Optional[datetime] = None,
    expected_target: Optional[RefreshTarget] = None,
) -> Dict[str, str]:
    """Persist metadata only when the current row still names the same remote object."""
    moment = now or utc_now()
    collected_at = _utc_text(moment)
    validated = [
        _validate_stat(stat, require_metrics=False) for stat in stats
    ]
    if len({stat.platform for stat in validated}) != len(validated):
        raise ValueError("at most one stat per platform may be recorded per title")
    outcomes: Dict[str, str] = {}
    expected_remote_ids = (
        {
            platform: remote_id
            for platform, remote_id, _remote_title in expected_target.remote_hints
        }
        if expected_target is not None else {}
    )
    with decision_store.transaction(conn):
        if expected_target is not None:
            current_title = conn.execute(
                "SELECT query_title, updated_at FROM catalog_titles WHERE title_key = ?",
                (title_key,),
            ).fetchone()
            if (
                current_title is None
                or current_title["query_title"] != expected_target.title.query_title
                or current_title["updated_at"] != expected_target.title_updated_at
            ):
                return {stat.platform: "stale_target" for stat in validated}
        for stat in validated:
            row = conn.execute(
                "SELECT status, remote_id, remote_title, remote_url, cover_url "
                "FROM catalog_platform_stats WHERE title_key = ? AND platform = ?",
                (title_key, stat.platform),
            ).fetchone()
            if row is None or row["status"] != "ok":
                outcomes[stat.platform] = "skipped"
                continue
            if (
                expected_target is not None
                and stat.platform in expected_remote_ids
                and row["remote_id"] != expected_remote_ids[stat.platform]
            ):
                outcomes[stat.platform] = "stale_target"
                continue
            if stat.status != "ok":
                if stat.metadata_lookup_mode == "direct_mismatch":
                    outcomes[stat.platform] = "identity_conflict"
                elif stat.metadata_lookup_mode == "direct_unavailable":
                    outcomes[stat.platform] = "unavailable"
                else:
                    outcomes[stat.platform] = stat.status
                continue
            if (
                row["remote_id"] is None
                or stat.remote_id is None
                or str(stat.remote_id) != str(row["remote_id"])
            ):
                outcomes[stat.platform] = "identity_conflict"
                continue

            identity_refreshed = bool(
                (stat.remote_title and stat.remote_title != row["remote_title"])
                or (stat.remote_url and stat.remote_url != row["remote_url"])
            )
            if identity_refreshed:
                conn.execute(
                    """
                    UPDATE catalog_platform_stats
                    SET remote_title = COALESCE(?, remote_title),
                        remote_url = COALESCE(?, remote_url),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE title_key = ? AND platform = ? AND remote_id = ?
                    """,
                    (
                        stat.remote_title,
                        stat.remote_url,
                        title_key,
                        stat.platform,
                        row["remote_id"],
                    ),
                )

            cover_changed = stat.cover_url != row["cover_url"]
            if cover_changed:
                conn.execute(
                    """
                    UPDATE catalog_platform_stats
                    SET cover_url = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE title_key = ? AND platform = ? AND remote_id = ?
                    """,
                    (stat.cover_url, title_key, stat.platform, row["remote_id"]),
                )

            updated = cover_changed
            if stat.genre is not None:
                _replace_platform_genre(
                    conn,
                    title_key,
                    stat.platform,
                    stat.genre,
                    collected_at,
                )
                updated = True
            if stat.platform in TAG_PLATFORMS and stat.tags is not None:
                _replace_platform_tags(
                    conn,
                    title_key,
                    stat.platform,
                    stat.tags,
                    collected_at,
                )
                updated = True
            if updated:
                outcomes[stat.platform] = "updated"
            elif identity_refreshed:
                outcomes[stat.platform] = "identity_refreshed"
            else:
                outcomes[stat.platform] = "unavailable"
    return outcomes


def record_platform_stats(
    conn: sqlite3.Connection,
    title_key: str,
    stats: Sequence[PlatformStat],
    *,
    now: Optional[datetime] = None,
    error_retry_seconds: int = DEFAULT_ERROR_RETRY_SECONDS,
    expected_target: Optional[RefreshTarget] = None,
    completion_cycle_id: Optional[str] = None,
) -> Dict[str, str]:
    """Persist search results without crossing an existing remote identity.

    New rows use expected-absence CAS when a refresh target is supplied. Existing
    success rows never lose ``ok`` status on lookup failure, never switch remote
    IDs, and never decrease growth counters. Retry rows are written only when the
    snapshotted status/remote-id/last-attempt tuple is still current.
    """
    if error_retry_seconds < 0:
        raise ValueError("error_retry_seconds must be non-negative")
    moment = now or utc_now()
    attempted_at = _utc_text(moment)
    retry_after = _utc_text(moment + timedelta(seconds=error_retry_seconds))
    validated = [_validate_stat(stat) for stat in stats]
    if len({stat.platform for stat in validated}) != len(validated):
        raise ValueError("at most one stat per platform may be recorded per title")
    expected_rows = {
        platform: (status, remote_id, last_attempt_at)
        for platform, status, remote_id, last_attempt_at in (
            expected_target.row_hints if expected_target is not None else ()
        )
    }
    outcomes: Dict[str, str] = {}

    with decision_store.transaction(conn):
        title_row = conn.execute(
            "SELECT query_title, updated_at FROM catalog_titles WHERE title_key = ?",
            (title_key,),
        ).fetchone()
        if title_row is None:
            raise KeyError(f"catalog title not found: {title_key}")
        if expected_target is not None and (
            title_row["query_title"] != expected_target.title.query_title
            or (
                expected_target.title_updated_at is not None
                and title_row["updated_at"] != expected_target.title_updated_at
            )
        ):
            for stat in validated:
                outcomes[stat.platform] = "stale_target"
                _mark_metadata_completion_primary(
                    conn, completion_cycle_id, title_key, stat.platform,
                    outcome="stale_target", stat=stat,
                )
            return outcomes

        tombstones = _load_identity_tombstones(conn)
        for stat in validated:
            if expected_target is not None and _tombstone_applies(
                tombstones, expected_target.title, stat.platform
            ):
                outcomes[stat.platform] = "tombstoned"
                _mark_metadata_completion_primary(
                    conn, completion_cycle_id, title_key, stat.platform,
                    outcome="tombstoned", stat=stat,
                )
                continue

            row = conn.execute(
                "SELECT * FROM catalog_platform_stats "
                "WHERE title_key = ? AND platform = ?",
                (title_key, stat.platform),
            ).fetchone()
            if expected_target is not None and stat.platform in expected_rows:
                expected_status, expected_id, expected_attempt = expected_rows[stat.platform]
                if expected_status is None:
                    if row is not None:
                        outcomes[stat.platform] = "stale_target"
                        _mark_metadata_completion_primary(
                            conn, completion_cycle_id, title_key, stat.platform,
                            outcome="stale_target", stat=stat,
                        )
                        continue
                elif row is None or (
                    row["status"] != expected_status
                    or row["remote_id"] != expected_id
                    or row["last_attempt_at"] != expected_attempt
                ):
                    outcomes[stat.platform] = "stale_target"
                    _mark_metadata_completion_primary(
                        conn, completion_cycle_id, title_key, stat.platform,
                        outcome="stale_target", stat=stat,
                    )
                    continue

            if stat.metadata_lookup_mode == "direct_mismatch":
                outcomes[stat.platform] = "identity_conflict"
                _mark_metadata_completion_primary(
                    conn, completion_cycle_id, title_key, stat.platform,
                    outcome="identity_conflict", stat=stat,
                )
                continue
            if stat.metadata_lookup_mode == "direct_unavailable":
                outcomes[stat.platform] = "unavailable"
                _mark_metadata_completion_primary(
                    conn, completion_cycle_id, title_key, stat.platform,
                    outcome="unavailable", stat=stat,
                )
                continue

            next_retry = retry_after if stat.status == "error" else None
            if row is None:
                success_at = attempted_at if stat.status == "ok" else None
                conn.execute(
                    """
                    INSERT INTO catalog_platform_stats(
                        title_key, platform, status, remote_id, remote_title, remote_url,
                        cover_url,
                        download_count, view_count, recommend_count, rating, rating_count,
                        last_attempt_at, last_success_at, retry_after, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        title_key, stat.platform, stat.status,
                        stat.remote_id, stat.remote_title, stat.remote_url,
                        stat.cover_url,
                        stat.download_count, stat.view_count, stat.recommend_count,
                        stat.rating, stat.rating_count, attempted_at, success_at,
                        next_retry, stat.message or None,
                    ),
                )
                outcomes[stat.platform] = stat.status
            elif row["status"] == "ok":
                if stat.status != "ok":
                    conn.execute(
                        """
                        UPDATE catalog_platform_stats
                        SET last_attempt_at = ?, retry_after = ?, error_message = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE title_key = ? AND platform = ? AND status = 'ok'
                    """,
                        (
                            attempted_at, next_retry, stat.message or None,
                            title_key, stat.platform,
                        ),
                    )
                    outcomes[stat.platform] = "preserved_success"
                    _mark_metadata_completion_primary(
                        conn, completion_cycle_id, title_key, stat.platform,
                        outcome="preserved_success", stat=stat,
                    )
                    continue
                if str(stat.remote_id) != str(row["remote_id"]):
                    outcomes[stat.platform] = "identity_conflict"
                    _mark_metadata_completion_primary(
                        conn, completion_cycle_id, title_key, stat.platform,
                        outcome="identity_conflict", stat=stat,
                    )
                    continue
                growth = _growth_metric_increased(row, stat)
                conn.execute(
                    """
                    UPDATE catalog_platform_stats
                    SET remote_title = COALESCE(?, remote_title),
                        remote_url = COALESCE(?, remote_url),
                        cover_url = ?,
                        download_count = ?, view_count = ?, recommend_count = ?,
                        rating = ?, rating_count = ?, last_attempt_at = ?,
                        last_success_at = ?, retry_after = NULL, error_message = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE title_key = ? AND platform = ? AND status = 'ok' AND remote_id = ?
                    """,
                    (
                        stat.remote_title, stat.remote_url, stat.cover_url,
                        _monotonic_count(row["download_count"], stat.download_count),
                        _monotonic_count(row["view_count"], stat.view_count),
                        _monotonic_count(row["recommend_count"], stat.recommend_count),
                        stat.rating if growth and stat.rating is not None else row["rating"],
                        _monotonic_count(row["rating_count"], stat.rating_count),
                        attempted_at, attempted_at, title_key, stat.platform, row["remote_id"],
                    ),
                )
                outcomes[stat.platform] = "updated" if growth else "unchanged"
            else:
                same_identity = (
                    row["remote_id"] is not None
                    and stat.status == "ok"
                    and str(row["remote_id"]) == str(stat.remote_id)
                )
                if (
                    stat.status == "ok"
                    and row["remote_id"] is not None
                    and not same_identity
                ):
                    outcomes[stat.platform] = "identity_conflict"
                    _mark_metadata_completion_primary(
                        conn, completion_cycle_id, title_key, stat.platform,
                        outcome="identity_conflict", stat=stat,
                    )
                    continue
                if stat.status == "ok":
                    preserve_metrics = same_identity
                    growth = _growth_metric_increased(row, stat) if preserve_metrics else True
                    conn.execute(
                        """
                        UPDATE catalog_platform_stats
                        SET status = 'ok', remote_id = ?, remote_title = ?, remote_url = ?,
                            cover_url = ?,
                            download_count = ?, view_count = ?, recommend_count = ?,
                            rating = ?, rating_count = ?, last_attempt_at = ?,
                            last_success_at = ?, retry_after = NULL, error_message = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE title_key = ? AND platform = ?
                        """,
                        (
                            stat.remote_id, stat.remote_title, stat.remote_url,
                            stat.cover_url,
                            _monotonic_count(row["download_count"], stat.download_count)
                            if preserve_metrics else stat.download_count,
                            _monotonic_count(row["view_count"], stat.view_count)
                            if preserve_metrics else stat.view_count,
                            _monotonic_count(row["recommend_count"], stat.recommend_count)
                            if preserve_metrics else stat.recommend_count,
                            stat.rating if growth and stat.rating is not None else row["rating"],
                            _monotonic_count(row["rating_count"], stat.rating_count)
                            if preserve_metrics else stat.rating_count,
                            attempted_at, attempted_at, title_key, stat.platform,
                        ),
                    )
                    outcomes[stat.platform] = "ok"
                else:
                    conn.execute(
                        """
                        UPDATE catalog_platform_stats
                        SET status = ?, last_attempt_at = ?, retry_after = ?,
                            error_message = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE title_key = ? AND platform = ?
                        """,
                        (
                            stat.status, attempted_at, next_retry, stat.message or None,
                            title_key, stat.platform,
                        ),
                    )
                    outcomes[stat.platform] = stat.status

            if stat.status == "ok" and outcomes.get(stat.platform) not in {
                "identity_conflict", "stale_target", "tombstoned",
            }:
                if stat.genre is not None:
                    _replace_platform_genre(
                        conn, title_key, stat.platform, stat.genre, attempted_at
                    )
                if stat.platform in TAG_PLATFORMS and stat.tags is not None:
                    _replace_platform_tags(
                        conn, title_key, stat.platform, stat.tags, attempted_at
                    )
            _mark_metadata_completion_primary(
                conn, completion_cycle_id, title_key, stat.platform,
                outcome=outcomes[stat.platform], stat=stat,
            )
    return outcomes


def invalidate_platform_identity(
    conn: sqlite3.Connection,
    title_key: str,
    platform: str,
    *,
    expected_remote_id: str,
    expected_remote_title: Optional[str] = None,
    reason: str,
    now: Optional[datetime] = None,
) -> Dict[str, object]:
    """CAS-invalidate one proven-wrong successful identity without installing a replacement."""
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform: {platform}")
    expected_id = str(expected_remote_id or "").strip()
    if not expected_id:
        raise ValueError("expected remote ID is required")
    message = str(reason or "").strip()
    if not message:
        raise ValueError("invalidation reason is required")
    attempted_at = _utc_text(now or utc_now())

    with decision_store.transaction(conn):
        title_row = conn.execute(
            "SELECT query_title FROM catalog_titles WHERE title_key = ?",
            (title_key,),
        ).fetchone()
        if title_row is None:
            raise KeyError(f"catalog title not found: {title_key}")
        row = conn.execute(
            "SELECT * FROM catalog_platform_stats "
            "WHERE title_key = ? AND platform = ?",
            (title_key, platform),
        ).fetchone()
        if row is None:
            raise KeyError(f"platform row not found: {title_key}/{platform}")
        if row["status"] != "ok":
            raise RuntimeError("platform identity is no longer an active success row")
        if str(row["remote_id"] or "") != expected_id:
            raise RuntimeError("platform identity changed before invalidation")
        if (
            expected_remote_title is not None
            and str(row["remote_title"] or "") != str(expected_remote_title)
        ):
            raise RuntimeError("platform remote title changed before invalidation")

        before = {
            key: row[key]
            for key in (
                "status",
                "remote_id",
                "remote_title",
                "remote_url",
                "cover_url",
                "download_count",
                "view_count",
                "recommend_count",
                "rating",
                "rating_count",
                "genre",
                "genre_collected_at",
                "tags_collected_at",
            )
        }
        deleted = conn.execute(
            "DELETE FROM catalog_platform_stats "
            "WHERE title_key = ? AND platform = ? AND status = 'ok' AND remote_id = ?",
            (title_key, platform, expected_id),
        )
        if deleted.rowcount != 1:
            raise RuntimeError("platform identity changed during invalidation")
        conn.execute(
            """
            INSERT INTO catalog_platform_stats(
                title_key, platform, status,
                last_attempt_at, error_message, created_at, updated_at
            ) VALUES (?, ?, 'not_found', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (title_key, platform, attempted_at, message),
        )
        tag_count = conn.execute(
            "SELECT COUNT(*) FROM catalog_platform_tags "
            "WHERE title_key = ? AND platform = ?",
            (title_key, platform),
        ).fetchone()[0]
        if tag_count:
            raise RuntimeError("platform tag rows survived identity invalidation")
        tombstone_key = _identity_tombstone_key(title_key, platform)
        existing_tombstone = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (tombstone_key,)
        ).fetchone()
        rejected_ids = []
        if existing_tombstone is not None:
            try:
                payload = json.loads(existing_tombstone["value"])
                rejected_ids = [str(value) for value in payload.get("rejected_remote_ids", [])]
            except (TypeError, ValueError, AttributeError):
                rejected_ids = []
        if expected_id not in rejected_ids:
            rejected_ids.append(expected_id)
        tombstone = {
            "title_key": title_key,
            "query_title": str(title_row["query_title"] or ""),
            "platform": platform,
            "rejected_remote_ids": rejected_ids,
            "reason": message,
            "invalidated_at": attempted_at,
        }
        conn.execute(
            """
            INSERT INTO settings(key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (tombstone_key, json.dumps(tombstone, ensure_ascii=False, sort_keys=True)),
        )

    return {
        "title_key": title_key,
        "platform": platform,
        "status": "not_found",
        "reason": message,
        "before": before,
    }


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
    expected_target: Optional[RefreshTarget] = None,
) -> Dict[str, str]:
    """Atomically increase metrics only for the same snapshotted remote object."""
    outcomes: Dict[str, str] = {}
    moment = now or utc_now()
    attempted_at = _utc_text(moment)
    validated = [_validate_stat(stat) for stat in stats]
    if len({stat.platform for stat in validated}) != len(validated):
        raise ValueError("at most one stat per platform may be recorded per title")
    expected_remote_ids = (
        {
            platform: remote_id
            for platform, remote_id, _remote_title in expected_target.remote_hints
        }
        if expected_target is not None else {}
    )

    with decision_store.transaction(conn):
        if expected_target is not None:
            current_title = conn.execute(
                "SELECT query_title, updated_at FROM catalog_titles WHERE title_key = ?",
                (title_key,),
            ).fetchone()
            if (
                current_title is None
                or current_title["query_title"] != expected_target.title.query_title
                or current_title["updated_at"] != expected_target.title_updated_at
            ):
                return {stat.platform: "stale_target" for stat in validated}
        for stat in validated:
            row = conn.execute(
                "SELECT * FROM catalog_platform_stats "
                "WHERE title_key = ? AND platform = ?",
                (title_key, stat.platform),
            ).fetchone()
            if row is None or row["status"] != "ok":
                outcomes[stat.platform] = "skipped"
                continue
            if (
                expected_target is not None
                and stat.platform in expected_remote_ids
                and row["remote_id"] != expected_remote_ids[stat.platform]
            ):
                outcomes[stat.platform] = "stale_target"
                continue
            if stat.status != "ok":
                if stat.metadata_lookup_mode == "direct_mismatch":
                    outcomes[stat.platform] = "identity_conflict"
                elif stat.metadata_lookup_mode == "direct_unavailable":
                    outcomes[stat.platform] = "unavailable"
                else:
                    outcomes[stat.platform] = stat.status
                continue
            if (
                row["remote_id"] is None
                or stat.remote_id is None
                or str(stat.remote_id) != str(row["remote_id"])
            ):
                outcomes[stat.platform] = "identity_conflict"
                continue

            has_growth_metric = _row_has_growth_metric(stat.platform, row)
            increased = (
                _growth_metric_increased(row, stat) if has_growth_metric else False
            )
            cover_changed = stat.cover_url != row["cover_url"]
            if increased or cover_changed:
                conn.execute(
                    """
                    UPDATE catalog_platform_stats
                    SET remote_title = COALESCE(?, remote_title),
                        remote_url = COALESCE(?, remote_url),
                        cover_url = ?,
                        download_count = ?, view_count = ?, recommend_count = ?,
                        rating = ?, rating_count = ?,
                        last_attempt_at = ?, last_success_at = ?, retry_after = NULL,
                        error_message = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE title_key = ? AND platform = ? AND status = 'ok'
                      AND remote_id = ?
                    """,
                    (
                        stat.remote_title,
                        stat.remote_url,
                        stat.cover_url,
                        _monotonic_count(row["download_count"], stat.download_count),
                        _monotonic_count(row["view_count"], stat.view_count),
                        _monotonic_count(
                            row["recommend_count"], stat.recommend_count
                        ),
                        stat.rating if stat.rating is not None else row["rating"],
                        _monotonic_count(row["rating_count"], stat.rating_count),
                        attempted_at,
                        attempted_at,
                        stat.message or None,
                        title_key,
                        stat.platform,
                        row["remote_id"],
                    ),
                )
                outcomes[stat.platform] = "updated"
            elif has_growth_metric:
                outcomes[stat.platform] = "unchanged"
            else:
                outcomes[stat.platform] = "skipped"

            if stat.genre is not None:
                _replace_platform_genre(
                    conn, title_key, stat.platform, stat.genre, attempted_at
                )
            if stat.platform in TAG_PLATFORMS and stat.tags is not None:
                _replace_platform_tags(
                    conn, title_key, stat.platform, stat.tags, attempted_at
                )
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


def _parse_series_genre(page: str) -> Optional[str]:
    info_start = re.search(
        r"<ul\b[^>]*class=[\"'][^\"']*\bend_info\b[^\"']*[\"'][^>]*>",
        page,
        flags=re.IGNORECASE,
    )
    if info_start is None:
        return None
    remainder = page[info_start.start():]
    description_start = re.search(
        r"<div\b[^>]*class=[\"'][^\"']*\bend_dsc\b[^\"']*[\"'][^>]*>",
        remainder,
        flags=re.IGNORECASE,
    )
    block = remainder[:description_start.start()] if description_start else remainder[:5000]
    genre = re.search(
        r"<a\b[^>]*href=[\"'][^\"']*categoryProductList\.series\?"
        r"[^\"']*categoryTypeCode=genre(?:&amp;|&)genreCode=[^\"']+[\"'][^>]*>"
        r"([\s\S]*?)</a>",
        block,
        flags=re.IGNORECASE,
    )
    return _normalize_genre(_strip_tags(genre.group(1))) if genre else None


def _parse_series_detail(
    page: str,
) -> Tuple[str, Optional[int], Optional[float], Optional[str], Optional[str]]:
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
        _parse_series_genre(page),
        _parse_open_graph_cover(page),
    )


def _series_detail_is_unavailable(title: str) -> bool:
    """Recognize Series system pages that are not positive identity evidence."""
    normalized = re.sub(r"\s+", "", str(title or ""))
    return "판매중지상품안내" in normalized


def _lookup_series_remote(
    title: str,
    remote_id: str,
    fetch_text: Callable[[str, float], str],
    *,
    timeout: float,
    fallback_title: Optional[str] = None,
) -> PlatformStat:
    detail_url = "https://series.naver.com/novel/detail.series?" + urlencode(
        {"productNo": remote_id}
    )
    detail_title, download_count, rating, genre, cover_url = _parse_series_detail(
        fetch_text(detail_url, timeout)
    )
    if _series_detail_is_unavailable(detail_title):
        raise ValueError("Naver Series product is unavailable")
    matched_title = detail_title or str(fallback_title or "")
    if not matched_title:
        raise ValueError("Naver detail response has no title")
    if not titles_match(title, matched_title):
        return _not_found("series", "stored remote title mismatch")
    stat = PlatformStat(
        platform="series",
        status="ok",
        remote_id=str(remote_id),
        remote_title=matched_title,
        remote_url=detail_url,
        cover_url=cover_url,
        download_count=download_count,
        rating=rating,
        genre=genre,
    )
    return stat if _has_metrics(stat) else _error("series", RuntimeError("지표를 찾지 못했습니다"))


def _lookup_series_metadata_remote(
    title: str,
    remote_id: str,
    fetch_text: Callable[[str, float], str],
    *,
    timeout: float,
) -> PlatformStat:
    """Read title/genre from one stored Series ID without requiring popularity fields."""
    detail_url = "https://series.naver.com/novel/detail.series?" + urlencode(
        {"productNo": remote_id}
    )
    detail_title, _download_count, _rating_value, genre, cover_url = _parse_series_detail(
        fetch_text(detail_url, timeout)
    )
    if not detail_title:
        raise ValueError("Naver detail response has no title")
    if _series_detail_is_unavailable(detail_title):
        raise ValueError("Naver Series product is unavailable")
    if not titles_match(title, detail_title):
        return _not_found("series", "stored remote title mismatch")
    return PlatformStat(
        platform="series",
        status="ok",
        remote_id=str(remote_id),
        remote_title=detail_title,
        remote_url=detail_url,
        cover_url=cover_url,
        genre=genre,
        message=("" if cover_url is not None else "no https cover in detail response"),
    )


def lookup_series(
    title: str,
    fetch_text: Callable[[str, float], str] = _http_text,
    *,
    author: Optional[str] = None,
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
    candidate, rejected = _select_unique_title_candidate(
        "series", title, candidates, requested_author=author
    )
    if rejected is not None:
        return rejected
    if candidate is None:
        return _not_found("series")
    return _lookup_series_remote(
        title,
        str(candidate["id"]),
        fetch_text,
        timeout=timeout,
        fallback_title=str(candidate["title"]),
    )


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
                "author": _search_candidate_author(item),
                "view_count": _count(_first_value(props, ("view_count", "viewCount"))),
            })
    if items and not candidates:
        raise ValueError("Kakao search result items have an unexpected structure")
    return candidates


def _parse_kakao_overview(
    data: object,
) -> Tuple[
    str, Optional[int], Optional[float], Optional[int], Optional[str], Optional[str]
]:
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
    genre_value = _first_value(content, ("sub_category", "subCategory"))
    return (
        str(_first_value(content, ("title", "name", "seoTitle")) or ""),
        _count(_first_value(props, ("viewCount", "view_count", "readCount", "read_count"))),
        _rating(rating_value, maximum=RATING_SCALES["kakao"]),
        rating_count,
        _normalize_genre(str(genre_value)) if genre_value is not None else None,
        _kakao_cover_url(_first_value(content, ("thumbnail", "coverImage", "image"))),
    )


def _parse_kakao_tags(data: object) -> Tuple[str, ...]:
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        raise ValueError("Kakao about response has no result object")
    if "theme_keyword_list" not in result:
        raise ValueError("Kakao about response has no theme_keyword_list")
    items = result.get("theme_keyword_list")
    if not isinstance(items, list):
        raise ValueError("Kakao about theme_keyword_list is not an array")
    values = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Kakao about theme keyword item is not an object")
        title = item.get("title")
        if title is None:
            raise ValueError("Kakao about theme keyword item has no title")
        values.append(str(title))
    return _normalize_tags(values)


def _lookup_kakao_remote(
    title: str,
    remote_id: str,
    fetch_json: Callable[[str, float], object],
    *,
    timeout: float,
    fallback_view_count: Optional[int] = None,
) -> PlatformStat:
    content_id = str(remote_id)
    detail_url = f"https://page.kakao.com/content/{content_id}"
    overview_url = (
        f"{_KAKAO_BFF_ORIGIN}/api/gateway/api/v1/content/overview?"
        + urlencode({"series_id": content_id})
    )
    detail_title, views, rating, rating_count, genre, cover_url = _parse_kakao_overview(
        fetch_json(overview_url, timeout)
    )
    matched_title = detail_title
    if not matched_title:
        raise ValueError("Kakao overview response has no title")
    if not titles_match(title, matched_title):
        return _not_found("kakao", "stored remote title mismatch")
    tags = None
    try:
        about_url = (
            f"{_KAKAO_BFF_ORIGIN}/api/gateway/api/v1/content/about?"
            + urlencode({"series_id": content_id})
        )
        tags = _parse_kakao_tags(fetch_json(about_url, timeout))
    except Exception:
        # Theme keywords are optional metadata. A transient about-response
        # failure must not discard otherwise valid popularity metrics.
        tags = None
    stat = PlatformStat(
        platform="kakao",
        status="ok",
        remote_id=content_id,
        remote_title=matched_title,
        remote_url=detail_url,
        cover_url=cover_url,
        view_count=views if views is not None else fallback_view_count,
        rating=rating,
        rating_count=rating_count,
        genre=genre,
        tags=tags,
    )
    return stat if _has_metrics(stat) else _error("kakao", RuntimeError("지표를 찾지 못했습니다"))


def _lookup_kakao_metadata_remote(
    title: str,
    remote_id: str,
    fetch_json: Callable[[str, float], object],
    *,
    timeout: float,
) -> PlatformStat:
    """Read authoritative genre/tags from one stored Kakao ID."""
    content_id = str(remote_id)
    overview_url = (
        f"{_KAKAO_BFF_ORIGIN}/api/gateway/api/v1/content/overview?"
        + urlencode({"series_id": content_id})
    )
    detail_title, _views, _rating, _rating_count, genre, cover_url = _parse_kakao_overview(
        fetch_json(overview_url, timeout)
    )
    if not detail_title:
        raise ValueError("Kakao overview response has no title")
    if not titles_match(title, detail_title):
        return _not_found("kakao", "stored remote title mismatch")
    tags = None
    try:
        about_url = (
            f"{_KAKAO_BFF_ORIGIN}/api/gateway/api/v1/content/about?"
            + urlencode({"series_id": content_id})
        )
        tags = _parse_kakao_tags(fetch_json(about_url, timeout))
    except Exception:
        # Cover/genre metadata from the authoritative overview remains usable.
        tags = None
    return PlatformStat(
        platform="kakao",
        status="ok",
        remote_id=content_id,
        remote_title=detail_title,
        remote_url=f"https://page.kakao.com/content/{content_id}",
        cover_url=cover_url,
        genre=genre,
        tags=tags,
        message=("" if cover_url is not None else "no https cover in overview response"),
    )


def lookup_kakao(
    title: str,
    fetch_json: Callable[[str, float], object] = _http_json,
    *,
    author: Optional[str] = None,
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
    candidate, rejected = _select_unique_title_candidate(
        "kakao", title, candidates, requested_author=author
    )
    if rejected is not None:
        return rejected
    if candidate is None:
        return _not_found("kakao")
    return _lookup_kakao_remote(
        title,
        str(candidate["id"]),
        fetch_json,
        timeout=timeout,
        fallback_view_count=candidate.get("view_count"),
    )


def _novelpia_title(record: object) -> str:
    return str(_first_value(record, ("novel_name", "novelName", "title", "name", "subject")) or "")


def _novelpia_genre_tags(record: object) -> Optional[Tuple[str, ...]]:
    if not isinstance(record, dict):
        return None
    if "novel_genre_arr" in record:
        values = record.get("novel_genre_arr")
        if not isinstance(values, list):
            raise ValueError("Novelpia novel_genre_arr is not an array")
        return _normalize_tags(values)
    raw = record.get("novel_genre")
    if raw is None:
        return None
    if isinstance(raw, list):
        return _normalize_tags(raw)
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Novelpia novel_genre is not valid JSON") from exc
        if not isinstance(decoded, list):
            raise ValueError("Novelpia novel_genre JSON is not an array")
        return _normalize_tags(decoded)
    raise ValueError("Novelpia novel_genre has an unexpected type")


def _parse_novelpia_tags(page: str) -> Tuple[str, ...]:
    blocks = re.findall(
        r"<p\b[^>]*class=[\"'][^\"']*\bwriter-tag\b[^\"']*[\"'][^>]*>"
        r"([\s\S]*?)</p>",
        page,
        flags=re.IGNORECASE,
    )
    if not blocks:
        raise ValueError("Novelpia detail response has no writer-tag block")
    values = []
    for block in blocks:
        for match in re.finditer(
            r"<span\b[^>]*class=[\"'][^\"']*\btag\b[^\"']*[\"'][^>]*>"
            r"([\s\S]*?)</span>",
            block,
            flags=re.IGNORECASE,
        ):
            values.append(_strip_tags(match.group(1)))
    return _normalize_tags(values)


def _novelpia_search_cover(record: object) -> Optional[str]:
    if not isinstance(record, dict):
        return None
    value = _first_value(record, (
        "cover_url", "coverUrl", "novel_thumb_all", "novelThumbAll",
        "novel_thumb", "novelThumb",
    ))
    if value is None:
        return None
    text = str(value).strip()
    if text.startswith("/") and not text.startswith("//"):
        text = urljoin(_NOVELPIA_ORIGIN + "/", text)
    return _normalize_cover_url(text)


def _lookup_novelpia_metadata_remote(
    title: str,
    remote_id: str,
    fetch_text: Callable[[str, float], str],
    *,
    timeout: float,
    fallback_title: Optional[str] = None,
) -> PlatformStat:
    """Read cover/tags from one stored NovelPia detail ID without title search."""
    remote_id_text = str(remote_id)
    detail_url = f"{_NOVELPIA_ORIGIN}/novel/{remote_id_text}"
    detail_page = fetch_text(detail_url, timeout)
    cover_url = _parse_open_graph_cover(detail_page)
    try:
        tags = _parse_novelpia_tags(detail_page)
    except Exception:
        tags = None
    if cover_url is None and tags is None:
        raise ValueError("NovelPia detail response has no cover or tag metadata")
    return PlatformStat(
        platform="novelpia",
        status="ok",
        remote_id=remote_id_text,
        remote_title=str(fallback_title or title).strip(),
        remote_url=detail_url,
        cover_url=cover_url,
        genre=(tags[0] if tags else "") if tags is not None else None,
        tags=tags,
        message=("" if cover_url is not None else "no https cover in detail response"),
    )


def lookup_novelpia(
    title: str,
    fetch_json: Callable[[str, float], object] = _http_json,
    fetch_text: Optional[Callable[[str, float], str]] = None,
    *,
    author: Optional[str] = None,
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
    raw_candidates = [item for item in items if isinstance(item, dict) and _novelpia_title(item)]
    if items and not raw_candidates:
        raise ValueError("Novelpia search result items have an unexpected structure")
    candidates = [
        {
            "id": _first_value(item, ("novel_no", "novelNo", "novel_id", "novelId", "id")),
            "title": _novelpia_title(item),
            "author": _search_candidate_author(item),
            "record": item,
        }
        for item in raw_candidates
    ]
    candidate_ref, rejected = _select_unique_title_candidate(
        "novelpia", title, candidates, requested_author=author
    )
    if rejected is not None:
        return rejected
    if candidate_ref is None:
        return _not_found("novelpia")
    candidate = candidate_ref["record"]
    remote_id = candidate_ref.get("id")
    if remote_id is None or not str(remote_id).strip():
        return _error("novelpia", RuntimeError("matched NovelPia result has no remote ID"))
    remote_id_text = str(remote_id) if remote_id is not None else None
    try:
        tags = _novelpia_genre_tags(candidate)
    except Exception:
        tags = None
    cover_url = _novelpia_search_cover(candidate)
    detail_fetch = fetch_text
    if detail_fetch is None and fetch_json is _http_json:
        detail_fetch = _http_text
    if remote_id_text and detail_fetch is not None:
        try:
            detail_page = detail_fetch(
                f"{_NOVELPIA_ORIGIN}/novel/{remote_id_text}", timeout
            )
        except Exception:
            # Search metrics and an authoritative search cover remain usable.
            detail_page = None
        if detail_page is not None:
            detail_cover = _parse_open_graph_cover(detail_page)
            if detail_cover is not None:
                cover_url = detail_cover
            if tags is None:
                try:
                    tags = _parse_novelpia_tags(detail_page)
                except Exception:
                    tags = None
    genre = tags[0] if tags else ("" if tags is not None else None)
    stat = PlatformStat(
        platform="novelpia",
        status="ok",
        remote_id=remote_id_text,
        remote_title=_novelpia_title(candidate),
        remote_url=(f"https://novelpia.com/novel/{remote_id_text}" if remote_id_text else None),
        cover_url=cover_url,
        view_count=_count(_first_value(candidate, ("count_view", "view_count", "viewCount", "hit", "hits"))),
        recommend_count=_count(_first_value(candidate, ("count_good", "good_count", "goodCount", "recommend", "recommend_count"))),
        rating=_rating(_first_value(candidate, ("rating", "rating_average", "ratingAverage", "score"))),
        rating_count=_count(_first_value(candidate, ("rating_count", "ratingCount", "count_rating"))),
        genre=genre,
        tags=tags,
    )
    return stat if _has_metrics(stat) else _error("novelpia", RuntimeError("지표를 찾지 못했습니다"))


def lookup_platforms(
    title: str,
    platforms: Sequence[str] = PLATFORMS,
    *,
    author: Optional[str] = None,
    fetch_text: Callable[[str, float], str] = _http_text,
    fetch_json: Callable[[str, float], object] = _http_json,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> List[PlatformStat]:
    if not platforms:
        return []
    if len(set(platforms)) != len(platforms):
        raise ValueError("platform lookup list must not contain duplicates")
    lookups = {
        "series": lambda: lookup_series(
            title, fetch_text, author=author, timeout=timeout
        ),
        "kakao": lambda: lookup_kakao(
            title, fetch_json, author=author, timeout=timeout
        ),
        "novelpia": lambda: lookup_novelpia(
            title,
            fetch_json,
            fetch_text,
            author=author,
            timeout=timeout,
        ),
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


def lookup_platform_metadata(
    title: str,
    platforms: Sequence[str],
    *,
    remote_ids: Optional[Dict[str, str]] = None,
    remote_titles: Optional[Dict[str, str]] = None,
    fetch_text: Callable[[str, float], str] = _http_text,
    fetch_json: Callable[[str, float], object] = _http_json,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> List[PlatformStat]:
    """Collect metadata without ever switching away from an existing remote ID."""
    if not platforms:
        return []
    if len(set(platforms)) != len(platforms):
        raise ValueError("platform lookup list must not contain duplicates")
    for platform in platforms:
        if platform not in PLATFORMS:
            raise ValueError(f"unknown platform: {platform}")

    ids = remote_ids or {}
    titles = remote_titles or {}

    def direct_result(platform: str, remote_id: str) -> PlatformStat:
        try:
            if platform == "series":
                stat = _lookup_series_metadata_remote(
                    title, remote_id, fetch_text, timeout=timeout
                )
            elif platform == "kakao":
                stat = _lookup_kakao_metadata_remote(
                    title, remote_id, fetch_json, timeout=timeout
                )
            else:
                stat = _lookup_novelpia_metadata_remote(
                    title,
                    remote_id,
                    fetch_text,
                    timeout=timeout,
                    fallback_title=titles.get(platform),
                )
        except Exception as exc:
            return replace(_error(platform, exc), metadata_lookup_mode="direct_unavailable")
        if stat.status == "not_found":
            return replace(stat, metadata_lookup_mode="direct_mismatch")
        return replace(stat, metadata_lookup_mode="direct")

    def lookup_one(platform: str) -> PlatformStat:
        remote_id = ids.get(platform)
        if remote_id:
            # A transport/parser failure is not evidence that the stored ID is
            # wrong. Likewise a positive title mismatch is reported for review;
            # neither condition may silently search and switch identities.
            return direct_result(platform, str(remote_id))
        if platform == "series":
            return replace(
                lookup_series(title, fetch_text, timeout=timeout),
                metadata_lookup_mode="search",
            )
        if platform == "kakao":
            return replace(
                lookup_kakao(title, fetch_json, timeout=timeout),
                metadata_lookup_mode="search",
            )
        return replace(
            _error(platform, RuntimeError("stored remote ID is missing")),
            metadata_lookup_mode="direct_unavailable",
        )

    with ThreadPoolExecutor(
        max_workers=min(len(platforms), len(PLATFORMS)),
        thread_name_prefix="platform-metadata",
    ) as executor:
        futures = {platform: executor.submit(lookup_one, platform) for platform in platforms}
        results = []
        for platform in platforms:
            try:
                results.append(futures[platform].result())
            except Exception as exc:
                results.append(
                    replace(_error(platform, exc), metadata_lookup_mode="direct_unavailable")
                )
        return results


def lookup_existing_platform_metrics(
    title: str,
    platforms: Sequence[str],
    *,
    remote_ids: Optional[Dict[str, str]] = None,
    fetch_text: Callable[[str, float], str] = _http_text,
    fetch_json: Callable[[str, float], object] = _http_json,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> List[PlatformStat]:
    """Refresh metrics without permitting a Series/Kakao identity search fallback."""
    if not platforms:
        return []
    if len(set(platforms)) != len(platforms):
        raise ValueError("platform metric lookup list must not contain duplicates")
    ids = remote_ids or {}

    def lookup_one(platform: str) -> PlatformStat:
        if platform not in PLATFORMS:
            raise ValueError(f"unknown platform: {platform}")
        remote_id = ids.get(platform)
        if platform in {"series", "kakao"}:
            if not remote_id:
                return replace(
                    _error(platform, RuntimeError("stored remote ID is missing")),
                    metadata_lookup_mode="direct_unavailable",
                )
            try:
                if platform == "series":
                    stat = _lookup_series_remote(
                        title, str(remote_id), fetch_text, timeout=timeout
                    )
                else:
                    stat = _lookup_kakao_remote(
                        title, str(remote_id), fetch_json, timeout=timeout
                    )
            except Exception as exc:
                return replace(
                    _error(platform, exc), metadata_lookup_mode="direct_unavailable"
                )
            if stat.status == "not_found":
                return replace(stat, metadata_lookup_mode="direct_mismatch")
            if stat.status != "ok":
                return replace(stat, metadata_lookup_mode="direct_unavailable")
            return replace(stat, metadata_lookup_mode="direct")
        try:
            return replace(
                lookup_novelpia(title, fetch_json, fetch_text, timeout=timeout),
                metadata_lookup_mode="search",
            )
        except Exception as exc:
            return replace(_error(platform, exc), metadata_lookup_mode="search")

    with ThreadPoolExecutor(
        max_workers=min(len(platforms), len(PLATFORMS)),
        thread_name_prefix="platform-existing",
    ) as executor:
        futures = {platform: executor.submit(lookup_one, platform) for platform in platforms}
        return [futures[platform].result() for platform in platforms]


def lookup_platform_identities(
    title: str,
    platforms: Sequence[str],
    *,
    remote_ids: Optional[Dict[str, str]] = None,
    fetch_text: Callable[[str, float], str] = _http_text,
    fetch_json: Callable[[str, float], object] = _http_json,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> List[PlatformStat]:
    """Revalidate stored Series/Kakao IDs and metadata from that exact object."""
    if not platforms:
        return []
    if len(set(platforms)) != len(platforms):
        raise ValueError("platform identity lookup list must not contain duplicates")
    for platform in platforms:
        if platform not in IDENTITY_AUDIT_PLATFORMS:
            raise ValueError(f"unsupported identity audit platform: {platform}")

    ids = remote_ids or {}

    def lookup_one(platform: str) -> PlatformStat:
        remote_id = ids.get(platform)
        if not remote_id:
            return replace(
                _error(platform, RuntimeError("stored remote ID is missing")),
                metadata_lookup_mode="direct_unavailable",
            )
        try:
            if platform == "series":
                stat = _lookup_series_metadata_remote(
                    title, str(remote_id), fetch_text, timeout=timeout
                )
            else:
                stat = _lookup_kakao_metadata_remote(
                    title, str(remote_id), fetch_json, timeout=timeout
                )
        except Exception as exc:
            return replace(_error(platform, exc), metadata_lookup_mode="direct_unavailable")
        if stat.status == "not_found":
            return replace(stat, metadata_lookup_mode="direct_mismatch")
        return replace(stat, metadata_lookup_mode="direct")

    with ThreadPoolExecutor(
        max_workers=min(len(platforms), len(IDENTITY_AUDIT_PLATFORMS)),
        thread_name_prefix="platform-identity",
    ) as executor:
        futures = {platform: executor.submit(lookup_one, platform) for platform in platforms}
        results = []
        for platform in platforms:
            try:
                results.append(futures[platform].result())
            except Exception as exc:
                results.append(
                    replace(_error(platform, exc), metadata_lookup_mode="direct_unavailable")
                )
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


def _authenticated_metadata_lookup_batch(
    lookup: Callable[..., PlatformStat],
    targets: Sequence[RefreshTarget],
    *,
    timeout: float,
    delay_seconds: float,
    sleep: Callable[[float], None],
) -> List[PlatformStat]:
    """Prefer stored-ID metadata reads when the authenticated client supports them."""
    owner = getattr(lookup, "__self__", None)
    batch_lookup = getattr(owner, "lookup_metadata_batch", None)
    if callable(batch_lookup):
        items = []
        for target in targets:
            hint = next(
                (
                    (remote_id, remote_title)
                    for platform, remote_id, remote_title in target.remote_hints
                    if platform == "novelpia"
                ),
                (None, None),
            )
            remote_id, remote_title = hint
            if remote_id is None or not str(remote_id).strip():
                raise RuntimeError(
                    "authenticated NovelPia metadata target has no stored remote ID"
                )
            items.append((
                target.title.query_title,
                str(remote_id),
                remote_title,
            ))
        return batch_lookup(
            items,
            timeout=timeout,
            delay_seconds=delay_seconds,
            sleep=sleep,
        )
    return _authenticated_lookup_batch(
        lookup,
        [target.title.query_title for target in targets],
        timeout=timeout,
        delay_seconds=delay_seconds,
        sleep=sleep,
    )


def select_authenticated_novelpia_targets(
    conn: sqlite3.Connection,
    *,
    limit: Optional[int] = None,
    attempted_before: Optional[datetime] = None,
) -> List[RefreshTarget]:
    """Select triple-miss NovelPia retries with a CAS snapshot."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    if limit == 0:
        return []
    stats = _stats_by_title(conn)
    title_rows = _catalog_title_rows(conn)
    tombstones = _load_identity_tombstones(conn)
    targets = []
    for title in discover_catalog_titles(conn):
        by_platform = stats.get(title.title_key, {})
        if not all(
            by_platform.get(platform) is not None
            and by_platform[platform]["status"] == "not_found"
            for platform in PLATFORMS
        ):
            continue
        if _tombstone_applies(tombstones, title, "novelpia"):
            continue
        novelpia_attempt = _parse_time(by_platform["novelpia"]["last_attempt_at"])
        if (
            attempted_before is not None
            and novelpia_attempt is not None
            and novelpia_attempt > attempted_before
        ):
            continue
        title_row = title_rows.get(title.title_key)
        if title_row is None or title_row["query_title"] != title.query_title:
            continue
        novelpia_row = by_platform["novelpia"]
        targets.append(RefreshTarget(
            title=title,
            platforms=("novelpia",),
            title_updated_at=title_row["updated_at"],
            remote_hints=((
                "novelpia", novelpia_row["remote_id"], novelpia_row["remote_title"]
            ),),
            row_hints=((
                "novelpia", novelpia_row["status"], novelpia_row["remote_id"],
                novelpia_row["last_attempt_at"]
            ),),
        ))
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
            "titles": [target.title.query_title for target in targets],
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
                [target.title.query_title for target in chunk],
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
            for target, stat in zip(chunk, stats):
                record_platform_stats(
                    conn,
                    target.title.title_key,
                    [stat],
                    now=now(),
                    error_retry_seconds=error_retry_seconds,
                    expected_target=target,
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


def repair_metadata_identities(
    state_db_path: str,
    *,
    limit: Optional[int] = DEFAULT_LIMIT,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    dry_run: bool = False,
    lookup: Callable[..., List[PlatformStat]] = lookup_platform_identities,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = utc_now,
    progress: Optional[Callable[[Dict[str, object]], None]] = None,
) -> Dict[str, object]:
    """Revalidate Series/Kakao metadata from the exact stored remote object.

    Despite the compatibility function name, this pass never switches remote IDs.
    A positive title mismatch is reported as an identity conflict, while transport
    or parser failures leave the existing snapshot untouched and retryable.
    """
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    if delay_seconds < 0:
        raise ValueError("delay_seconds must be non-negative")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if dry_run:
        return preview_metadata_identity_audit(state_db_path, limit=limit)

    conn = decision_store.connect_state_db(state_db_path)
    try:
        decision_store.validate_schema(conn)
        if progress is not None:
            progress({"phase": "sync_start"})
        synced = sync_catalog_titles(conn)
        targets = select_metadata_identity_targets(conn, limit=limit)
        selected_platforms = sum(len(target.platforms) for target in targets)
        outcome_counts = {
            "revalidated": 0,
            "identity_refreshed": 0,
            "identity_conflict": 0,
            "stale_target": 0,
            "unavailable": 0,
            "error": 0,
            "skipped": 0,
        }
        if progress is not None:
            progress({
                "phase": "identity_start",
                "discovered_titles": synced["discovered"],
                "selected_titles": len(targets),
                "selected_platforms": selected_platforms,
            })
        completed_titles = 0
        for index, target in enumerate(targets):
            expected_ids = {
                platform: str(remote_id)
                for platform, remote_id, _remote_title in target.remote_hints
                if remote_id is not None
            }
            results = lookup(
                target.title.query_title,
                target.platforms,
                remote_ids=expected_ids,
                timeout=timeout,
            )
            if {result.platform for result in results} != set(target.platforms):
                raise RuntimeError(
                    "identity lookup did not return exactly the requested platforms"
                )
            for result in results:
                candidate = result
                if result.status == "ok":
                    metadata_complete = (
                        result.genre is not None
                        and (
                            result.platform != "kakao"
                            or result.tags is not None
                        )
                    )
                    if not metadata_complete:
                        candidate = replace(
                            result,
                            status="error",
                            message="stored-ID metadata response is incomplete",
                            metadata_lookup_mode="direct_unavailable",
                        )
                outcome = record_platform_metadata_results(
                    conn,
                    target.title.title_key,
                    [candidate],
                    now=now(),
                    expected_target=target,
                )[result.platform]
                if outcome == "updated":
                    outcome = "revalidated"
                outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
            completed_titles += 1
            if progress is not None:
                progress({
                    "phase": "identity_progress",
                    "completed_titles": completed_titles,
                    "selected_titles": len(targets),
                    "completed_platforms": sum(outcome_counts.values()),
                    "selected_platforms": selected_platforms,
                    "outcome_counts": dict(outcome_counts),
                })
            if index + 1 < len(targets) and delay_seconds:
                sleep(delay_seconds)
        return {
            "dry_run": False,
            **synced,
            "selected_titles": len(targets),
            "selected_platforms": selected_platforms,
            "outcome_counts": outcome_counts,
        }
    finally:
        conn.close()


def refresh_missing_metadata(
    state_db_path: str,
    *,
    limit: Optional[int] = DEFAULT_LIMIT,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    dry_run: bool = False,
    authenticated_novelpia_lookup: Optional[Callable[..., PlatformStat]] = None,
    lookup: Callable[..., List[PlatformStat]] = lookup_platforms,
    target_pairs: Optional[set[Tuple[str, str]]] = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = utc_now,
    progress: Optional[Callable[[Dict[str, object]], None]] = None,
) -> Dict[str, object]:
    """Fill missing cover/genre/tag metadata without changing popularity metrics."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    if delay_seconds < 0:
        raise ValueError("delay_seconds must be non-negative")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if dry_run:
        return preview_metadata_backfill(state_db_path, limit=limit)

    conn = decision_store.connect_state_db(state_db_path)
    try:
        decision_store.validate_schema(conn)
        if progress is not None:
            progress({"phase": "sync_start"})
        synced = sync_catalog_titles(conn)
        targets = select_metadata_backfill_targets(
            conn,
            limit=None if target_pairs is not None else limit,
        )
        if target_pairs is not None:
            filtered_targets = []
            for target in targets:
                platforms = tuple(
                    platform for platform in target.platforms
                    if (target.title.title_key, platform) in target_pairs
                )
                if not platforms:
                    continue
                remote_hints = tuple(
                    hint for hint in target.remote_hints if hint[0] in platforms
                )
                filtered_targets.append(replace(
                    target,
                    platforms=platforms,
                    remote_hints=remote_hints,
                ))
                if limit is not None and len(filtered_targets) >= limit:
                    break
            targets = filtered_targets
        selected_platforms = sum(len(target.platforms) for target in targets)
        outcome_counts = {
            "updated": 0,
            "identity_refreshed": 0,
            "identity_conflict": 0,
            "stale_target": 0,
            "empty": 0,
            "unavailable": 0,
            "not_found": 0,
            "error": 0,
            "skipped": 0,
        }
        pair_outcomes: Dict[Tuple[str, str], str] = {}
        failure_reason_counts: Dict[Tuple[str, str, str], int] = {}
        lookup_mode_counts = {
            "direct": 0,
            "direct_mismatch": 0,
            "direct_unavailable": 0,
            "search": 0,
            "authenticated": 0,
        }
        if progress is not None:
            progress({
                "phase": "metadata_start",
                "discovered_titles": synced["discovered"],
                "selected_titles": len(targets),
                "selected_platforms": selected_platforms,
            })
        completed_titles = 0
        authenticated_novelpia_attempts = 0
        pending_authenticated = []

        def persist_results(target, results):
            outcomes = record_platform_metadata_results(
                conn,
                target.title.title_key,
                list(results),
                now=now(),
                expected_target=target,
            )
            for result in results:
                if result.metadata_lookup_mode in lookup_mode_counts:
                    lookup_mode_counts[result.metadata_lookup_mode] += 1
            for platform, outcome in outcomes.items():
                outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
                pair_outcomes[(target.title.title_key, platform)] = outcome
                result = next(item for item in results if item.platform == platform)
                reason = None
                if outcome not in {"updated", "identity_refreshed"}:
                    reason = result.message or outcome
                elif result.cover_url is None:
                    reason = result.message or "no https cover in metadata response"
                if reason:
                    key = (platform, outcome, str(reason)[:240])
                    failure_reason_counts[key] = failure_reason_counts.get(key, 0) + 1
            return outcomes

        def report_completed():
            nonlocal completed_titles
            completed_titles += 1
            if progress is not None:
                progress({
                    "phase": "metadata_progress",
                    "completed_titles": completed_titles,
                    "selected_titles": len(targets),
                    "completed_platforms": sum(outcome_counts.values()),
                    "selected_platforms": selected_platforms,
                    "outcome_counts": dict(outcome_counts),
                    "lookup_mode_counts": dict(lookup_mode_counts),
                    "authenticated_novelpia_attempts": (
                        authenticated_novelpia_attempts
                    ),
                })

        def flush_authenticated():
            nonlocal authenticated_novelpia_attempts
            if not pending_authenticated:
                return
            authenticated_results = _authenticated_metadata_lookup_batch(
                authenticated_novelpia_lookup,
                pending_authenticated,
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
            for target, authenticated in zip(
                pending_authenticated, authenticated_results
            ):
                authenticated_novelpia_attempts += 1
                persist_results(
                    target,
                    [replace(authenticated, metadata_lookup_mode="authenticated")],
                )
                report_completed()
            pending_authenticated.clear()

        for index, target in enumerate(targets):
            if lookup is lookup_platforms:
                results = lookup_platform_metadata(
                    target.title.query_title,
                    target.platforms,
                    remote_ids={
                        platform: str(remote_id)
                        for platform, remote_id, _remote_title in target.remote_hints
                        if remote_id is not None
                    },
                    remote_titles={
                        platform: str(remote_title)
                        for platform, _remote_id, remote_title in target.remote_hints
                        if remote_title is not None
                    },
                    timeout=timeout,
                )
            else:
                results = lookup(
                    target.title.query_title,
                    target.platforms,
                    timeout=timeout,
                )
            if {result.platform for result in results} != set(target.platforms):
                raise RuntimeError(
                    "platform lookup did not return exactly the requested platforms"
                )
            if (
                authenticated_novelpia_lookup is not None
                and "novelpia" in target.platforms
            ):
                novelpia_result = next(
                    result for result in results if result.platform == "novelpia"
                )
                if (
                    novelpia_result.status != "ok"
                    or novelpia_result.genre is None
                    or novelpia_result.tags is None
                ):
                    public_results = [
                        result for result in results if result.platform != "novelpia"
                    ]
                    if public_results:
                        persist_results(target, public_results)
                    pending_authenticated.append(target)
                    if len(pending_authenticated) >= NOVELPIA_AUTH_BATCH_SIZE:
                        flush_authenticated()
                else:
                    persist_results(target, results)
                    report_completed()
            else:
                persist_results(target, results)
                report_completed()
            if index + 1 < len(targets) and delay_seconds:
                sleep(delay_seconds)
        flush_authenticated()
        return {
            "dry_run": False,
            **synced,
            "selected_titles": len(targets),
            "selected_platforms": selected_platforms,
            "outcome_counts": outcome_counts,
            "lookup_mode_counts": lookup_mode_counts,
            "pair_outcomes": [
                {"title_key": title_key, "platform": platform, "outcome": outcome}
                for (title_key, platform), outcome in sorted(pair_outcomes.items())
            ],
            "failure_reasons": [
                {
                    "platform": platform,
                    "outcome": outcome,
                    "reason": reason,
                    "count": count,
                }
                for (platform, outcome, reason), count
                in sorted(failure_reason_counts.items())
            ],
            "authenticated_novelpia_attempts": authenticated_novelpia_attempts,
            "authenticated_novelpia_relogins": int(getattr(
                getattr(authenticated_novelpia_lookup, "__self__", None),
                "relogin_count",
                0,
            )),
        }
    finally:
        conn.close()


def resume_metadata_completion_cycles(
    state_db_path: str,
    *,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    authenticated_novelpia_lookup: Optional[Callable[..., PlatformStat]] = None,
    lookup: Callable[..., List[PlatformStat]] = lookup_platforms,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = utc_now,
    progress: Optional[Callable[[Dict[str, object]], None]] = None,
) -> Dict[str, object]:
    """Resume exact current-run metadata pairs recorded before network writes."""
    conn = decision_store.connect_state_db(state_db_path)
    try:
        decision_store.validate_schema(conn)
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE ? ORDER BY key",
            (f"{METADATA_COMPLETION_PREFIX}%",),
        ).fetchall()
        if not rows:
            return {
                "cycles": 0,
                "pending_pairs": 0,
                "review_pairs": 0,
                "completed_pairs": 0,
                "pair_outcomes": [],
            }
        target_pairs: set[Tuple[str, str]] = set()
        with decision_store.transaction(conn):
            for setting in rows:
                cycle_id = str(setting["key"])[len(METADATA_COMPLETION_PREFIX):]
                payload = _load_metadata_completion_cycle(conn, cycle_id)
                for pair in payload["pairs"]:
                    state = str(pair.get("state") or "")
                    if state == "awaiting_primary":
                        title_row = conn.execute(
                            "SELECT query_title, updated_at FROM catalog_titles WHERE title_key = ?",
                            (pair["title_key"],),
                        ).fetchone()
                        current = conn.execute(
                            "SELECT status, remote_id, last_attempt_at FROM catalog_platform_stats "
                            "WHERE title_key = ? AND platform = ?",
                            (pair["title_key"], pair["platform"]),
                        ).fetchone()
                        expected = (
                            pair.get("expected_status"),
                            pair.get("expected_remote_id"),
                            pair.get("expected_last_attempt_at"),
                        )
                        current_tuple = (
                            (current["status"], current["remote_id"], current["last_attempt_at"])
                            if current is not None else (None, None, None)
                        )
                        title_matches = title_row is not None and (
                            title_row["query_title"] == pair.get("query_title")
                            and (
                                pair.get("title_updated_at") is None
                                or title_row["updated_at"] == pair.get("title_updated_at")
                            )
                        )
                        if title_matches and current_tuple == expected:
                            pair["state"] = "primary_terminal"
                            pair["last_outcome"] = "primary_not_committed"
                        else:
                            pair["state"] = "needs_review"
                            pair["last_outcome"] = "primary_state_changed"
                    if pair.get("state") == "pending_metadata":
                        title_row = conn.execute(
                            "SELECT query_title, updated_at FROM catalog_titles WHERE title_key = ?",
                            (pair["title_key"],),
                        ).fetchone()
                        current = conn.execute(
                            "SELECT * FROM catalog_platform_stats WHERE title_key = ? AND platform = ?",
                            (pair["title_key"], pair["platform"]),
                        ).fetchone()
                        if (
                            title_row is None
                            or title_row["query_title"] != pair.get("query_title")
                            or (
                                pair.get("title_updated_at") is not None
                                and title_row["updated_at"] != pair.get("title_updated_at")
                            )
                            or current is None
                            or current["status"] != "ok"
                            or str(current["remote_id"] or "") != str(pair.get("primary_remote_id") or "")
                        ):
                            pair["state"] = "needs_review"
                            pair["last_outcome"] = "stale_target"
                        elif _metadata_snapshot_complete(current, str(pair["platform"])):
                            pair["state"] = "completed"
                            pair["last_outcome"] = "already_complete"
                        else:
                            target_pairs.add((str(pair["title_key"]), str(pair["platform"])))
                _save_metadata_completion_cycle(conn, cycle_id, payload)
    finally:
        conn.close()

    metadata_result = {
        "pair_outcomes": [],
        "outcome_counts": {},
        "selected_titles": 0,
        "selected_platforms": 0,
    }
    if target_pairs:
        metadata_result = refresh_missing_metadata(
            state_db_path,
            limit=None,
            delay_seconds=delay_seconds,
            timeout=timeout,
            authenticated_novelpia_lookup=authenticated_novelpia_lookup,
            lookup=lookup,
            target_pairs=target_pairs,
            sleep=sleep,
            now=now,
            progress=progress,
        )
    outcomes = {
        (str(item["title_key"]), str(item["platform"])): str(item["outcome"])
        for item in metadata_result.get("pair_outcomes", [])
    }

    conn = decision_store.connect_state_db(state_db_path)
    try:
        rows = conn.execute(
            "SELECT key FROM settings WHERE key LIKE ? ORDER BY key",
            (f"{METADATA_COMPLETION_PREFIX}%",),
        ).fetchall()
        completed_pairs = 0
        pending_pairs = 0
        review_pairs = 0
        final_outcomes = []
        with decision_store.transaction(conn):
            for setting in rows:
                cycle_id = str(setting["key"])[len(METADATA_COMPLETION_PREFIX):]
                payload = _load_metadata_completion_cycle(conn, cycle_id)
                for pair in payload["pairs"]:
                    key = (str(pair["title_key"]), str(pair["platform"]))
                    if pair.get("state") == "pending_metadata" and key in outcomes:
                        outcome = outcomes[key]
                        pair["last_outcome"] = outcome
                        if outcome in {"updated", "identity_refreshed"}:
                            pair["state"] = "completed"
                        elif outcome in {"identity_conflict", "stale_target", "skipped"}:
                            pair["state"] = "needs_review"
                    state = str(pair.get("state") or "")
                    final_outcomes.append({
                        "cycle_id": cycle_id,
                        "title_key": pair.get("title_key"),
                        "platform": pair.get("platform"),
                        "state": state,
                        "outcome": pair.get("last_outcome"),
                    })
                    if state in {"completed", "primary_terminal"}:
                        completed_pairs += 1
                    elif state == "needs_review":
                        review_pairs += 1
                    elif state == "pending_metadata":
                        pending_pairs += 1
                if all(
                    pair.get("state") in {"completed", "primary_terminal"}
                    for pair in payload["pairs"]
                ):
                    conn.execute(
                        "DELETE FROM settings WHERE key = ?",
                        (_metadata_completion_key(cycle_id),),
                    )
                else:
                    _save_metadata_completion_cycle(conn, cycle_id, payload)
        return {
            "cycles": len(rows),
            "pending_pairs": pending_pairs,
            "review_pairs": review_pairs,
            "completed_pairs": completed_pairs,
            "pair_outcomes": final_outcomes,
            "outcome_counts": dict(metadata_result.get("outcome_counts") or {}),
            "selected_titles": int(metadata_result.get("selected_titles") or 0),
            "selected_platforms": int(metadata_result.get("selected_platforms") or 0),
        }
    finally:
        conn.close()


def refresh_existing_metrics(
    state_db_path: str,
    *,
    limit: Optional[int] = None,
    refresh_before: Optional[datetime] = None,
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
        return preview_existing_metric_refresh(
            state_db_path, limit=limit, refresh_before=refresh_before
        )

    conn = decision_store.connect_state_db(state_db_path)
    try:
        decision_store.validate_schema(conn)
        if progress is not None:
            progress({"phase": "sync_start"})
        synced = sync_catalog_titles(conn)
        targets = select_existing_metric_targets(
            conn, limit=limit, refresh_before=refresh_before
        )
        selected_platforms = sum(len(target.platforms) for target in targets)
        outcome_counts = {
            "updated": 0,
            "unchanged": 0,
            "identity_conflict": 0,
            "stale_target": 0,
            "unavailable": 0,
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
                expected_target=target,
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
            if lookup is lookup_platforms:
                results = lookup_existing_platform_metrics(
                    target.title.query_title,
                    target.platforms,
                    remote_ids={
                        platform: str(remote_id)
                        for platform, remote_id, _remote_title in target.remote_hints
                        if remote_id is not None
                    },
                    timeout=timeout,
                )
            else:
                results = lookup(
                    target.title.query_title,
                    target.platforms,
                    timeout=timeout,
                )
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
    metadata_lookup: Optional[Callable[..., List[PlatformStat]]] = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = utc_now,
    progress: Optional[Callable[[Dict[str, object]], None]] = None,
    _test_failpoint: Optional[Callable[[str], None]] = None,
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
    if refresh_after_days is not None:
        raise ValueError(
            "age refresh must use the stored-ID existing-metric refresh path"
        )
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
        completion_enabled = metadata_lookup is not None or lookup is lookup_platforms
        resumed_before = (
            resume_metadata_completion_cycles(
                state_db_path,
                delay_seconds=delay_seconds,
                timeout=timeout,
                authenticated_novelpia_lookup=authenticated_novelpia_lookup,
                lookup=metadata_lookup or lookup_platforms,
                sleep=sleep,
                now=now,
                progress=progress,
            )
            if completion_enabled else {
                "cycles": 0,
                "pending_pairs": 0,
                "review_pairs": 0,
                "completed_pairs": 0,
                "pair_outcomes": [],
                "outcome_counts": {},
                "selected_titles": 0,
                "selected_platforms": 0,
            }
        )
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
        outcome_counts: Dict[str, int] = {
            "ok": 0,
            "not_found": 0,
            "error": 0,
            "skipped": 0,
            "preserved_success": 0,
            "updated": 0,
            "unchanged": 0,
            "identity_conflict": 0,
            "stale_target": 0,
            "unavailable": 0,
            "tombstoned": 0,
        }
        authenticated_counts: Dict[str, int] = {
            "ok": 0, "not_found": 0, "error": 0, "skipped": 0
        }
        authenticated_attempts = 0
        selected_platforms = sum(len(target.platforms) for target in targets)
        completion_cycle_id = (
            _create_metadata_completion_cycle(conn, targets, now=now())
            if completion_enabled and targets else None
        )
        if completion_cycle_id is not None and _test_failpoint is not None:
            _test_failpoint("completion_cycle_created")
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
            outcomes = record_platform_stats(
                conn,
                target.title.title_key,
                final_results,
                now=now(),
                error_retry_seconds=error_retry_seconds,
                expected_target=target,
                completion_cycle_id=completion_cycle_id,
            )
            for outcome in outcomes.values():
                outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
            if _test_failpoint is not None:
                _test_failpoint("primary_committed")
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
                    "outcome_counts": dict(outcome_counts),
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
            if lookup is lookup_platforms:
                stored_ids = {
                    platform: str(remote_id)
                    for platform, _status, remote_id, _attempt in target.row_hints
                    if remote_id is not None and platform in IDENTITY_AUDIT_PLATFORMS
                }
                direct_platforms = tuple(
                    platform for platform in target.platforms if platform in stored_ids
                )
                search_platforms = tuple(
                    platform for platform in target.platforms if platform not in stored_ids
                )
                gathered: Dict[str, PlatformStat] = {}
                if direct_platforms:
                    for result in lookup_existing_platform_metrics(
                        target.title.query_title,
                        direct_platforms,
                        remote_ids=stored_ids,
                        timeout=timeout,
                    ):
                        gathered[result.platform] = result
                if search_platforms:
                    for result in lookup_platforms(
                        target.title.query_title,
                        search_platforms,
                        author=target.title.author,
                        timeout=timeout,
                    ):
                        gathered[result.platform] = result
                results = [gathered[platform] for platform in target.platforms]
            else:
                results = lookup(
                    target.title.query_title, target.platforms, timeout=timeout
                )
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
        if completion_enabled and _test_failpoint is not None:
            _test_failpoint("before_metadata_completion")
        metadata_completion = (
            resume_metadata_completion_cycles(
                state_db_path,
                delay_seconds=delay_seconds,
                timeout=timeout,
                authenticated_novelpia_lookup=authenticated_novelpia_lookup,
                lookup=metadata_lookup or lookup_platforms,
                sleep=sleep,
                now=now,
                progress=progress,
            )
            if completion_enabled else {
                "cycles": 0,
                "pending_pairs": 0,
                "review_pairs": 0,
                "completed_pairs": 0,
                "pair_outcomes": [],
                "outcome_counts": {},
                "selected_titles": 0,
                "selected_platforms": 0,
            }
        )
        if completion_enabled and _test_failpoint is not None:
            _test_failpoint("metadata_completion_finished")
        return {
            "dry_run": False,
            **synced,
            "selected_titles": len(targets),
            "selected_platforms": selected_platforms,
            "status_counts": status_counts,
            "outcome_counts": outcome_counts,
            "authenticated_novelpia_attempts": authenticated_attempts,
            "authenticated_novelpia_status_counts": authenticated_counts,
            "authenticated_novelpia_relogins": int(getattr(
                getattr(authenticated_novelpia_lookup, "__self__", None),
                "relogin_count",
                0,
            )),
            "metadata_completion": metadata_completion,
            "resumed_metadata_completion": resumed_before,
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
        title_rows = _catalog_title_rows(conn) if "catalog_titles" in tables else {}
        tombstones = (
            _load_identity_tombstones(conn)
            if {"catalog_titles", "catalog_platform_stats"} <= tables else {}
        )
        pending = _refresh_targets(
            titles,
            stats,
            limit=None,
            now=utc_now(),
            title_rows=title_rows,
            tombstones=tombstones,
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
