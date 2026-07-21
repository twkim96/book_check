"""Read-only inventory for historical Folderling dedup reports."""

from __future__ import annotations

import os
import json
import re
import stat
from datetime import datetime, timezone
from pathlib import Path


REPORT_NAME_RE = re.compile(
    r"(?P<kind>dedup)_(?P<date>\d{8})_(?P<time>\d{6})"
    r"(?:_(?P<microseconds>\d{6}))?\.txt"
    r"|(?P<strong>strong_candidates)_(?P<strong_date>\d{8})_"
    r"(?P<strong_time>\d{6})_(?P<suffix>\d{6})\.txt"
)
SUMMARY_READ_BYTES = 128 * 1024
MAX_VIEW_BYTES = 2 * 1024 * 1024
MAX_STRUCTURED_BYTES = 16 * 1024 * 1024


def _report_root(temp_dir: os.PathLike | str) -> Path:
    return Path(temp_dir).expanduser().resolve() / "dedup_logs"


def _match_report_name(name: str):
    return REPORT_NAME_RE.fullmatch(str(name or ""))


def _safe_report_path(temp_dir: os.PathLike | str, name: str) -> Path:
    if _match_report_name(name) is None:
        raise ValueError("지원하지 않는 dedup 보고서 이름입니다")
    root = _report_root(temp_dir)
    path = root / name
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise FileNotFoundError(name)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError("dedup 보고서는 일반 파일이어야 합니다")
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("dedup 보고서 경로가 관리 폴더 밖입니다") from exc
    return resolved


def _created_at(match, modified_at: float) -> str:
    date = match.group("date") or match.group("strong_date")
    clock = match.group("time") or match.group("strong_time")
    try:
        # Filenames were written in the machine's local time.  ``astimezone``
        # attaches that same local zone without pretending the stamp is UTC.
        return datetime.strptime(date + clock, "%Y%m%d%H%M%S").astimezone().isoformat()
    except (TypeError, ValueError):
        return datetime.fromtimestamp(modified_at, timezone.utc).isoformat()


def _summary(path: Path, kind: str) -> str:
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        sample = stream.read(SUMMARY_READ_BYTES)
    lines = [line.strip() for line in sample.splitlines() if line.strip()]
    if kind == "dedup":
        return next((line for line in lines if line.startswith("모드:")), lines[0] if lines else "")
    return next(
        (line for line in lines if line.startswith("강력 후보 감사:")),
        lines[0] if lines else "",
    )


def _report_item(path: Path) -> dict:
    match = _match_report_name(path.name)
    if match is None:
        raise ValueError(path.name)
    info = path.stat()
    kind = "dedup" if match.group("kind") else "strong_candidates"
    structured = path.with_suffix(".json")
    structured_available = False
    try:
        structured_info = structured.lstat()
        structured_available = stat.S_ISREG(structured_info.st_mode) and not stat.S_ISLNK(
            structured_info.st_mode
        )
    except FileNotFoundError:
        pass
    return {
        "name": path.name,
        "kind": kind,
        "size": info.st_size,
        "created_at": _created_at(match, info.st_mtime),
        "modified_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
        "summary": _summary(path, kind),
        "structured_available": structured_available,
    }


def dedup_report_listing(
    temp_dir: os.PathLike | str,
    *,
    search: str = "",
    kind: str = "all",
    limit: int = 200,
) -> dict:
    if kind not in {"all", "dedup", "strong_candidates"}:
        raise ValueError("지원하지 않는 dedup 보고서 종류입니다")
    limit = max(1, min(int(limit), 500))
    needle = str(search or "").strip().casefold()
    root = _report_root(temp_dir)
    items = []
    if root.is_dir():
        for path in root.iterdir():
            if path.is_symlink() or _match_report_name(path.name) is None:
                continue
            try:
                item = _report_item(path)
            except (OSError, ValueError):
                continue
            if kind != "all" and item["kind"] != kind:
                continue
            if needle and needle not in f"{item['name']} {item['summary']}".casefold():
                continue
            items.append(item)
    items.sort(key=lambda item: (item["created_at"], item["name"]), reverse=True)
    return {
        "items": items[:limit],
        "total": len(items),
        "limit": limit,
        "search": search,
        "kind": kind,
        "readonly": True,
        "root": str(root),
    }


def read_dedup_report(temp_dir: os.PathLike | str, name: str) -> dict:
    path = _safe_report_path(temp_dir, name)
    item = _report_item(path)
    if item["size"] > MAX_VIEW_BYTES:
        raise ValueError(
            f"화면 열람 한도({MAX_VIEW_BYTES} bytes)를 넘었습니다. 다운로드를 사용하세요."
        )
    structured_summary = None
    structured_metadata = None
    structured = path.with_suffix(".json")
    if item["structured_available"]:
        try:
            info = structured.lstat()
            if info.st_size <= MAX_STRUCTURED_BYTES:
                payload = json.loads(structured.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    summary = payload.get("summary")
                    structured_summary = summary if isinstance(summary, dict) else None
                    structured_metadata = {
                        "schema_version": payload.get("schema_version"),
                        "kind": payload.get("kind"),
                        "generated_at": payload.get("generated_at"),
                    }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            structured_summary = None
            structured_metadata = None
    return {
        **item,
        "text": path.read_text(encoding="utf-8", errors="replace"),
        "structured_summary": structured_summary,
        "structured_metadata": structured_metadata,
        "readonly": True,
    }


def dedup_report_path(
    temp_dir: os.PathLike | str,
    name: str,
    *,
    structured: bool = False,
) -> Path:
    """Return a validated path for a Flask download response."""
    path = _safe_report_path(temp_dir, name)
    if not structured:
        return path
    companion = path.with_suffix(".json")
    try:
        info = companion.lstat()
    except FileNotFoundError:
        raise FileNotFoundError(companion.name)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError("구조화 dedup 보고서는 일반 파일이어야 합니다")
    return companion
