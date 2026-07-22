"""Read-only inventory and on-demand export for Folderling dedup reports."""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

from dedup_report_format import dedup_report_summary_line, render_dedup_report_text


REPORT_STEM_RE = re.compile(
    r"(?P<kind>dedup)_(?P<date>\d{8})_(?P<time>\d{6})"
    r"(?:_(?P<microseconds>\d{6}))?"
    r"|(?P<strong>strong_candidates)_(?P<strong_date>\d{8})_"
    r"(?P<strong_time>\d{6})_(?P<suffix>\d{6})"
)
REPORT_FILE_RE = re.compile(rf"(?P<stem>{REPORT_STEM_RE.pattern})\.(?P<extension>txt|json)")
SUMMARY_READ_BYTES = 128 * 1024
MAX_VIEW_BYTES = 2 * 1024 * 1024
MAX_STRUCTURED_BYTES = 16 * 1024 * 1024


def _report_root(temp_dir: os.PathLike | str) -> Path:
    return Path(temp_dir).expanduser().resolve() / "dedup_logs"


def _report_stem(identifier: str) -> str:
    name = str(identifier or "")
    file_match = REPORT_FILE_RE.fullmatch(name)
    stem = file_match.group("stem") if file_match else name
    if REPORT_STEM_RE.fullmatch(stem) is None:
        raise ValueError("지원하지 않는 dedup 보고서 이름입니다")
    return stem


def _safe_existing_file(root: Path, path: Path) -> Path:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise FileNotFoundError(path.name)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError("dedup 보고서는 일반 파일이어야 합니다")
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("dedup 보고서 경로가 관리 폴더 밖입니다") from exc
    return resolved


def _report_files(temp_dir: os.PathLike | str, identifier: str) -> tuple[str, dict[str, Path]]:
    stem = _report_stem(identifier)
    root = _report_root(temp_dir)
    files: dict[str, Path] = {}
    for extension in ("txt", "json"):
        candidate = root / f"{stem}.{extension}"
        try:
            files[extension] = _safe_existing_file(root, candidate)
        except FileNotFoundError:
            continue
    if not files:
        raise FileNotFoundError(identifier)
    return stem, files


def _created_at(match: re.Match[str], modified_at: float) -> str:
    date = match.group("date") or match.group("strong_date")
    clock = match.group("time") or match.group("strong_time")
    try:
        # Filenames were written in the machine's local time.
        return datetime.strptime(date + clock, "%Y%m%d%H%M%S").astimezone().isoformat()
    except (TypeError, ValueError):
        return datetime.fromtimestamp(modified_at, timezone.utc).isoformat()


def _read_structured(path: Path) -> dict:
    info = path.stat()
    if info.st_size > MAX_STRUCTURED_BYTES:
        raise ValueError(
            f"구조화 보고서 열람 한도({MAX_STRUCTURED_BYTES} bytes)를 넘었습니다."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("구조화 dedup 보고서 루트는 객체여야 합니다")
    return payload


def _summary_from_text(text: str, kind: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if kind == "dedup":
        return next((line for line in lines if line.startswith("모드:")), lines[0] if lines else "")
    return next(
        (line for line in lines if line.startswith("강력 후보 감사:")),
        lines[0] if lines else "",
    )


def _text_from_files(files: dict[str, Path], *, for_view: bool = False) -> str:
    text_path = files.get("txt")
    if text_path is not None:
        if for_view and text_path.stat().st_size > MAX_VIEW_BYTES:
            raise ValueError(
                f"화면 열람 한도({MAX_VIEW_BYTES} bytes)를 넘었습니다. 다운로드를 사용하세요."
            )
        return text_path.read_text(encoding="utf-8", errors="replace")

    payload = _read_structured(files["json"])
    if payload.get("kind") == "folderling_dedup":
        text = render_dedup_report_text(payload)
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if for_view and len(text.encode("utf-8")) > MAX_VIEW_BYTES:
        raise ValueError(
            f"화면 열람 한도({MAX_VIEW_BYTES} bytes)를 넘었습니다. TXT 내보내기를 사용하세요."
        )
    return text


def _report_item(stem: str, files: dict[str, Path]) -> dict:
    match = REPORT_STEM_RE.fullmatch(stem)
    if match is None:
        raise ValueError(stem)
    kind = "dedup" if match.group("kind") else "strong_candidates"
    primary = files.get("txt") or files["json"]
    latest_mtime = max(path.stat().st_mtime for path in files.values())
    try:
        if "txt" in files:
            with files["txt"].open("r", encoding="utf-8", errors="replace") as stream:
                summary = _summary_from_text(stream.read(SUMMARY_READ_BYTES), kind)
        else:
            payload = _read_structured(files["json"])
            summary = (
                dedup_report_summary_line(payload)
                if payload.get("kind") == "folderling_dedup"
                else "구조화 보고서"
            )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        summary = "구조화 보고서" if "json" in files else ""
    return {
        "report_id": stem,
        "name": primary.name,
        "kind": kind,
        "size": primary.stat().st_size,
        "created_at": _created_at(match, latest_mtime),
        "modified_at": datetime.fromtimestamp(latest_mtime, timezone.utc).isoformat(),
        "summary": summary,
        "text_available": "txt" in files,
        "structured_available": "json" in files,
    }


def dedup_report_listing(
    temp_dir: os.PathLike | str,
    *,
    search: str = "",
    kind: str = "all",
    limit: int = 200,
    cursor: str | int | None = None,
) -> dict:
    if kind not in {"all", "dedup", "strong_candidates"}:
        raise ValueError("지원하지 않는 dedup 보고서 종류입니다")
    limit = max(1, min(int(limit), 500))
    try:
        offset = max(0, int(cursor or 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("지원하지 않는 보고서 페이지 위치입니다") from exc
    needle = str(search or "").strip().casefold()
    root = _report_root(temp_dir)
    grouped: dict[str, dict[str, Path]] = {}
    if root.is_dir():
        for path in root.iterdir():
            match = REPORT_FILE_RE.fullmatch(path.name)
            if path.is_symlink() or match is None:
                continue
            try:
                safe = _safe_existing_file(root, path)
            except (OSError, ValueError):
                continue
            grouped.setdefault(match.group("stem"), {})[match.group("extension")] = safe

    items = []
    for stem, files in grouped.items():
        try:
            item = _report_item(stem, files)
        except (OSError, ValueError):
            continue
        if kind != "all" and item["kind"] != kind:
            continue
        if needle and needle not in f"{item['name']} {item['summary']}".casefold():
            continue
        items.append(item)
    items.sort(key=lambda item: (item["created_at"], item["report_id"]), reverse=True)
    page = items[offset:offset + limit]
    next_offset = offset + len(page)
    return {
        "items": page,
        "total": len(items),
        "limit": limit,
        "cursor": str(offset) if offset else None,
        "next_cursor": str(next_offset) if next_offset < len(items) else None,
        "search": search,
        "kind": kind,
        "readonly": True,
        "root": str(root),
    }


def read_dedup_report(temp_dir: os.PathLike | str, identifier: str) -> dict:
    stem, files = _report_files(temp_dir, identifier)
    item = _report_item(stem, files)
    text = _text_from_files(files, for_view=True)
    structured_summary = None
    structured_metadata = None
    if "json" in files:
        try:
            payload = _read_structured(files["json"])
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
        "text": text,
        "structured_summary": structured_summary,
        "structured_metadata": structured_metadata,
        "readonly": True,
    }


def export_dedup_report_text(
    temp_dir: os.PathLike | str,
    identifier: str,
) -> tuple[str, str]:
    """Return an in-memory TXT export; no file is created in dedup_logs."""
    stem, files = _report_files(temp_dir, identifier)
    return f"{stem}.txt", _text_from_files(files)


def dedup_report_path(
    temp_dir: os.PathLike | str,
    identifier: str,
    *,
    structured: bool = False,
) -> Path:
    """Return a validated existing report path for a Flask response."""
    _stem, files = _report_files(temp_dir, identifier)
    extension = "json" if structured else "txt"
    try:
        return files[extension]
    except KeyError:
        raise FileNotFoundError(f"{_report_stem(identifier)}.{extension}")
