"""Read-only title review provider and approved 1.2.8 requeue workflow."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional, Sequence

import decision_store
from mutation_io import mutation_lock_for_roots
from normalizer import (
    NORMALIZER_VERSION,
    analyze_name,
    extract_catalog_query_title,
    extract_readable_title,
)
from title_review_mutations import EDITABLE_ASSIGNMENT_STATES, requeue_user_title_file


PLATFORMS = ("series", "kakao", "novelpia")
SUPPORTED_EXTENSIONS = frozenset({".txt", ".epub", ".pdf"})
SORT_COLUMNS = {
    "name": "fa.analyzed_name COLLATE NOCASE",
    "core": "fa.core_title COLLATE NOCASE",
    "path": "f.canonical_path COLLATE NOCASE",
}


def _readonly_connection(path: Path) -> sqlite3.Connection:
    return decision_store.connect_state_db_readonly(Path(path).expanduser().resolve())


def _encode_cursor(offset: int) -> str:
    raw = json.dumps({"offset": int(offset)}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: Optional[str]) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        value = int(payload["offset"])
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    if value < 0:
        raise ValueError("invalid cursor")
    return value


def _revision_payload(row: Mapping[str, object]) -> dict:
    return {
        "file_id": row["file_id"],
        "canonical_path": row["canonical_path"],
        "size": row["size"],
        "mtime_ns": row["mtime_ns"],
        "dev": row["dev"],
        "ino": row["ino"],
        "ctime_ns": row["ctime_ns"],
        "assignment_state": row["assignment_state"],
        "variant_id": row["variant_id"],
        "protected": row["protected"],
        "representative": row["representative"],
        "core_title": row["core_title"],
        "normalizer_version": row["normalizer_version"],
        "analysis_updated_at": row["analysis_updated_at"],
    }


def _source_revision(row: Mapping[str, object]) -> str:
    raw = json.dumps(
        _revision_payload(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _base_select() -> str:
    return """
        SELECT f.file_id, f.canonical_path, f.source, f.active,
               f.size, f.mtime_ns, f.dev, f.ino, f.ctime_ns,
               f.assignment_state, f.variant_id, f.protected,
               f.current_fingerprint_id,
               fa.normalizer_version, fa.analyzed_name, fa.core_title,
               fa.readable_title, fa.catalog_query_title, fa.author,
               fa.max_number, fa.effective_max, fa.unit, fa.complete,
               fa.updated_at AS analysis_updated_at,
               CASE WHEN r.file_id IS NULL THEN 0 ELSE 1 END AS representative,
               series.status AS series_status,
               kakao.status AS kakao_status,
               novelpia.status AS novelpia_status
        FROM files AS f
        JOIN file_analysis AS fa ON fa.file_id = f.file_id
        LEFT JOIN representatives AS r ON r.file_id = f.file_id
        LEFT JOIN catalog_platform_stats AS series
          ON series.title_key = fa.core_title AND series.platform = 'series'
        LEFT JOIN catalog_platform_stats AS kakao
          ON kakao.title_key = fa.core_title AND kakao.platform = 'kakao'
        LEFT JOIN catalog_platform_stats AS novelpia
          ON novelpia.title_key = fa.core_title AND novelpia.platform = 'novelpia'
    """


def _where_clauses(search: str, status_filter: str) -> tuple[list[str], list[object]]:
    clauses = [
        "f.active = 1",
        "f.source = 'house'",
        "NOT EXISTS (SELECT 1 FROM catalog_platform_stats AS ok "
        "WHERE ok.title_key = fa.core_title AND ok.status = 'ok')",
    ]
    params: list[object] = []
    value = (search or "").strip()
    if value:
        pattern = f"%{value}%"
        clauses.append(
            "(fa.analyzed_name LIKE ? ESCAPE '\\' OR fa.core_title LIKE ? ESCAPE '\\' "
            "OR fa.catalog_query_title LIKE ? ESCAPE '\\')"
        )
        params.extend((pattern, pattern, pattern))

    if status_filter == "all_not_found":
        clauses.append(
            "series.status = 'not_found' AND kakao.status = 'not_found' "
            "AND novelpia.status = 'not_found'"
        )
    elif status_filter == "error":
        clauses.append(
            "(series.status = 'error' OR kakao.status = 'error' "
            "OR novelpia.status = 'error')"
        )
    elif status_filter == "missing":
        clauses.append(
            "(series.status IS NULL OR kakao.status IS NULL OR novelpia.status IS NULL)"
        )
    elif status_filter != "all":
        raise ValueError(f"unknown status filter: {status_filter}")
    return clauses, params


def _edit_blockers(row: Mapping[str, object]) -> list[str]:
    blockers = []
    if row["assignment_state"] not in EDITABLE_ASSIGNMENT_STATES:
        blockers.append("managed_relationship")
    if row["variant_id"] is not None:
        blockers.append("variant_attached")
    if row["protected"]:
        blockers.append("protected_file")
    if row["representative"]:
        blockers.append("representative_file")
    return blockers


def _row_to_case(row: Mapping[str, object]) -> dict:
    path = Path(str(row["canonical_path"]))
    suffix = path.suffix if path.suffix.lower() in SUPPORTED_EXTENSIONS else ""
    name = path.name
    body = name[: -len(suffix)] if suffix else name
    blockers = _edit_blockers(row)
    return {
        "case_id": row["file_id"],
        "file_id": row["file_id"],
        "current_name": name,
        "current_body": body,
        "extension": suffix,
        "canonical_path": str(path),
        "core_title": row["core_title"],
        "readable_title": row["readable_title"],
        "query_title": row["catalog_query_title"],
        "author": row["author"],
        "effective_max": row["effective_max"],
        "unit": row["unit"],
        "complete": bool(row["complete"]),
        "assignment_state": row["assignment_state"],
        "protected": bool(row["protected"]),
        "representative": bool(row["representative"]),
        "platforms": {
            platform: row[f"{platform}_status"] or "missing" for platform in PLATFORMS
        },
        "source_revision": _source_revision(row),
        "editable": not blockers,
        "blocked_reasons": blockers,
    }


def list_title_cases(
    state_db: Path,
    *,
    search: str = "",
    status_filter: str = "all",
    cursor: Optional[str] = None,
    limit: int = 50,
    sort: str = "name",
    direction: str = "asc",
) -> dict:
    limit = max(1, min(int(limit), 200))
    offset = _decode_cursor(cursor)
    if sort not in SORT_COLUMNS:
        raise ValueError(f"unknown sort: {sort}")
    direction_sql = "DESC" if direction == "desc" else "ASC"
    if direction not in {"asc", "desc"}:
        raise ValueError(f"unknown direction: {direction}")
    clauses, params = _where_clauses(search, status_filter)
    where = " WHERE " + " AND ".join(clauses)
    conn = _readonly_connection(state_db)
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM (" + _base_select() + where + ")",
            tuple(params),
        ).fetchone()[0]
        rows = conn.execute(
            _base_select()
            + where
            + f" ORDER BY {SORT_COLUMNS[sort]} {direction_sql}, f.file_id {direction_sql}"
            + " LIMIT ? OFFSET ?",
            tuple(params) + (limit + 1, offset),
        ).fetchall()
    finally:
        conn.close()
    has_more = len(rows) > limit
    visible = rows[:limit]
    return {
        "provider": "title_correction",
        "items": [_row_to_case(row) for row in visible],
        "total": total,
        "limit": limit,
        "cursor": cursor,
        "next_cursor": _encode_cursor(offset + limit) if has_more else None,
        "sort": sort,
        "direction": direction,
        "status_filter": status_filter,
        "search": search,
    }


def get_title_case(state_db: Path, file_id: str) -> dict:
    conn = _readonly_connection(state_db)
    try:
        clauses, params = _where_clauses("", "all")
        row = conn.execute(
            _base_select()
            + " WHERE "
            + " AND ".join(clauses)
            + " AND f.file_id = ?",
            tuple(params) + (file_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise KeyError(file_id)
    return _row_to_case(row)


def _validated_new_body(value: object, extension: str) -> str:
    body = unicodedata.normalize("NFC", str(value or "")).strip()
    if not body or body in {".", ".."}:
        raise ValueError("새 파일명이 비어 있습니다")
    if "\x00" in body or "/" in body or "\\" in body or Path(body).name != body:
        raise ValueError("새 파일명에 경로 구분자를 사용할 수 없습니다")
    if any(ord(char) < 32 for char in body):
        raise ValueError("새 파일명에 제어 문자를 사용할 수 없습니다")
    if extension and body.lower().endswith(extension.lower()):
        raise ValueError("확장자는 입력하지 마세요. 기존 확장자가 자동 보존됩니다")
    materialized = body + extension
    if len(os.fsencode(materialized)) > 255:
        raise ValueError("새 파일명이 파일시스템 제한인 255바이트를 초과합니다")
    return body


def preview_title_change(
    state_db: Path,
    *,
    house_dir: Path,
    temp_dir: Path,
    file_id: str,
    new_body: object,
    source_revision: str,
) -> dict:
    state_db = Path(state_db).expanduser().resolve()
    house_dir = Path(house_dir).expanduser().resolve()
    temp_dir = Path(temp_dir).expanduser().resolve()
    conn = _readonly_connection(state_db)
    try:
        clauses, params = _where_clauses("", "all")
        row = conn.execute(
            _base_select() + " WHERE " + " AND ".join(clauses) + " AND f.file_id = ?",
            tuple(params) + (file_id,),
        ).fetchone()
        if row is None:
            raise KeyError(file_id)
        current = _row_to_case(row)
        blockers = list(current["blocked_reasons"])
        if source_revision != current["source_revision"]:
            blockers.append("source_revision_stale")
        source = Path(current["canonical_path"])
        try:
            source.resolve().relative_to(house_dir)
        except ValueError:
            blockers.append("source_outside_house")
        if not source.is_file() or source.is_symlink():
            blockers.append("source_missing_or_not_regular")
        else:
            stat = source.stat()
            expected = (
                row["dev"], row["ino"], row["ctime_ns"], row["size"], row["mtime_ns"]
            )
            actual = (
                stat.st_dev, stat.st_ino, stat.st_ctime_ns, stat.st_size, stat.st_mtime_ns
            )
            if any(value is not None for value in expected[:3]):
                if actual != expected:
                    blockers.append("source_identity_stale")
            elif actual[3:] != expected[3:]:
                blockers.append("source_snapshot_stale")

        try:
            body = _validated_new_body(new_body, current["extension"])
        except ValueError as exc:
            body = str(new_body or "")
            blockers.append(f"invalid_new_name:{exc}")
        candidate_name = body + current["extension"]
        if unicodedata.normalize("NFC", body) == unicodedata.normalize(
            "NFC", current["current_body"]
        ):
            blockers.append("unchanged_name")
        analysis = analyze_name(candidate_name)
        if not analysis["core_title"]:
            blockers.append("empty_core_title")
        destination = temp_dir / candidate_name
        if destination.exists() or destination.is_symlink():
            blockers.append("temp_destination_exists")

        target = conn.execute(
            """
            SELECT ct.title_key,
                   SUM(CASE WHEN cps.status = 'ok' THEN 1 ELSE 0 END) AS ok_count
            FROM catalog_titles AS ct
            LEFT JOIN catalog_platform_stats AS cps ON cps.title_key = ct.title_key
            WHERE ct.title_key = ?
            GROUP BY ct.title_key
            """,
            (analysis["core_title"],),
        ).fetchone()
    finally:
        conn.close()

    blockers = list(dict.fromkeys(blockers))
    return {
        "file_id": file_id,
        "source_revision": current["source_revision"],
        "source_path": current["canonical_path"],
        "current_name": current["current_name"],
        "current_body": current["current_body"],
        "before_core_title": current["core_title"],
        "new_body": body,
        "candidate_name": candidate_name,
        "destination_path": str(destination),
        "after_core_title": analysis["core_title"],
        "after_readable_title": extract_readable_title(candidate_name),
        "after_query_title": extract_catalog_query_title(candidate_name),
        "after_author": analysis["author"],
        "after_effective_max": analysis["effective_max"],
        "after_unit": analysis["unit"],
        "after_complete": bool(analysis["complete"]),
        "target_exists": target is not None,
        "target_has_ok": bool(target is not None and target["ok_count"]),
        "blocked_reasons": blockers,
        "runnable": not blockers,
    }


def _plan_sha256(items: Sequence[Mapping[str, object]]) -> str:
    payload = [
        {
            "file_id": item["file_id"],
            "source_revision": item["source_revision"],
            "source_path": item["source_path"],
            "new_body": item["new_body"],
            "candidate_name": item["candidate_name"],
            "destination_path": item["destination_path"],
            "before_core_title": item["before_core_title"],
            "after_core_title": item["after_core_title"],
            "blocked_reasons": item["blocked_reasons"],
        }
        for item in items
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def build_title_plan(
    state_db: Path,
    *,
    house_dir: Path,
    temp_dir: Path,
    changes: Sequence[Mapping[str, object]],
) -> dict:
    if not changes:
        return {
            "version": "1.2.8",
            "provider": "title_correction",
            "item_count": 0,
            "blocked_count": 0,
            "plan_sha256": _plan_sha256([]),
            "runnable": False,
            "items": [],
        }
    if len(changes) > 200:
        raise ValueError("한 번에 최대 200개 파일만 제목을 수정할 수 있습니다")
    seen = set()
    items = []
    for change in changes:
        file_id = str(change.get("file_id") or "")
        if not file_id:
            raise ValueError("file_id is required")
        if file_id in seen:
            raise ValueError(f"duplicate file_id: {file_id}")
        seen.add(file_id)
        items.append(
            preview_title_change(
                state_db,
                house_dir=house_dir,
                temp_dir=temp_dir,
                file_id=file_id,
                new_body=change.get("new_body"),
                source_revision=str(change.get("source_revision") or ""),
            )
        )

    destinations: dict[str, list[dict]] = {}
    for item in items:
        destinations.setdefault(item["destination_path"], []).append(item)
    for group in destinations.values():
        if len(group) <= 1:
            continue
        for item in group:
            if "batch_destination_collision" not in item["blocked_reasons"]:
                item["blocked_reasons"].append("batch_destination_collision")
                item["runnable"] = False

    blocked_count = sum(bool(item["blocked_reasons"]) for item in items)
    return {
        "version": "1.2.8",
        "provider": "title_correction",
        "normalizer_version": NORMALIZER_VERSION,
        "state_db": str(Path(state_db).resolve()),
        "house_dir": str(Path(house_dir).resolve()),
        "temp_dir": str(Path(temp_dir).resolve()),
        "item_count": len(items),
        "blocked_count": blocked_count,
        "plan_sha256": _plan_sha256(items),
        "runnable": bool(items) and blocked_count == 0,
        "items": items,
    }


def _backup_path(state_db: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return state_db.parent / "backups" / (
        f"before_user_title_requeue_{stamp}_{uuid.uuid4().hex[:8]}.sqlite3"
    )


def apply_title_plan(
    state_db: Path,
    *,
    house_dir: Path,
    temp_dir: Path,
    changes: Sequence[Mapping[str, object]],
    confirm_count: int,
    confirm_plan_sha256: str,
    progress=None,
) -> dict:
    state_db = Path(state_db).expanduser().resolve()
    house_dir = Path(house_dir).expanduser().resolve()
    temp_dir = Path(temp_dir).expanduser().resolve()
    with mutation_lock_for_roots(house_dir, temp_dir, "user-title-requeue-1.2.8"):
        plan = build_title_plan(
            state_db, house_dir=house_dir, temp_dir=temp_dir, changes=changes
        )
        if not plan["runnable"]:
            raise RuntimeError(
                f"title correction plan is not runnable: blocked={plan['blocked_count']}"
            )
        if int(confirm_count) != plan["item_count"]:
            raise RuntimeError(
                "title correction confirmation count mismatch: "
                f"expected={plan['item_count']} provided={confirm_count}"
            )
        if confirm_plan_sha256 != plan["plan_sha256"]:
            raise RuntimeError(
                "title correction plan SHA-256 mismatch: "
                f"expected={plan['plan_sha256']} provided={confirm_plan_sha256}"
            )

        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(
                    f"doctor failed before title correction: {len(issues)} issue(s), "
                    f"first={issues[0].get('kind')}"
                )
            backup = decision_store.backup_state_db(conn, _backup_path(state_db))
            decision_store.issue_actual_run_token(
                conn, str(backup), house_dir=house_dir, temp_dir=temp_dir
            )
        finally:
            conn.close()

        run_id, manifest_path = decision_store.prepare_actual_run(
            state_db, house_dir, temp_dir
        )
        completed = []
        try:
            conn = decision_store.connect_state_db(state_db)
            try:
                for index, item in enumerate(plan["items"], start=1):
                    result = requeue_user_title_file(
                        conn,
                        file_id=item["file_id"],
                        destination=item["destination_path"],
                        run_id=run_id,
                    )
                    completed.append(result)
                    if progress is not None:
                        progress(index, plan["item_count"], item["candidate_name"])
            finally:
                conn.close()
        except (Exception, KeyboardInterrupt) as exc:
            conn = decision_store.connect_state_db(state_db)
            try:
                decision_store.finish_actual_run(conn, run_id, success=False, error=str(exc))
            finally:
                conn.close()
            raise

        conn = decision_store.connect_state_db(state_db)
        try:
            decision_store.finish_actual_run(conn, run_id, success=True)
        finally:
            conn.close()
        return {
            "run_id": run_id,
            "manifest_path": manifest_path,
            "backup_path": str(backup),
            "planned": plan["item_count"],
            "completed": len(completed),
            "next_action": "Folderling 실행",
            "operations": completed,
        }
