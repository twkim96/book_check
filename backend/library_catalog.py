"""Read-only catalog and review-queue projections for the 1.3.0 server."""

from __future__ import annotations

import os
from pathlib import Path

import decision_store


PLATFORMS = ("series", "kakao", "novelpia")
REVIEW_QUEUE_NAMES = (
    "exact_quarantine",
    "exact_duplicates",
    "suspected_duplicates",
    "author_conflicts",
    "warning",
)
SUPPORTED_EXTENSIONS = frozenset({".txt", ".epub", ".pdf"})


def _bounded_limit(value: int) -> int:
    return max(1, min(int(value), 100))


def _offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        value = int(cursor)
    except (TypeError, ValueError) as exc:
        raise ValueError("cursor must be a non-negative integer") from exc
    if value < 0:
        raise ValueError("cursor must be a non-negative integer")
    return value


def _catalog_filter(status: str) -> str:
    values = {
        "all": "",
        "found": (
            "AND EXISTS (SELECT 1 FROM catalog_platform_stats AS cps "
            "WHERE cps.title_key = a.core_title AND cps.status = 'ok')"
        ),
        "missing": (
            "AND NOT EXISTS (SELECT 1 FROM catalog_platform_stats AS cps "
            "WHERE cps.title_key = a.core_title AND cps.status = 'ok')"
        ),
        "error": (
            "AND EXISTS (SELECT 1 FROM catalog_platform_stats AS cps "
            "WHERE cps.title_key = a.core_title AND cps.status = 'error')"
        ),
        "not_found": (
            "AND 3 = (SELECT COUNT(*) FROM catalog_platform_stats AS cps "
            "WHERE cps.title_key = a.core_title "
            "AND cps.platform IN ('series', 'kakao', 'novelpia') "
            "AND cps.status = 'not_found')"
        ),
    }
    if status not in values:
        raise ValueError("unknown catalog status filter")
    return values[status]


def catalog_listing(
    state_db: os.PathLike | str,
    *,
    search: str = "",
    status: str = "all",
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    """Return one bounded page of current house works and platform metadata."""
    limit = _bounded_limit(limit)
    offset = _offset(cursor)
    search = str(search or "").strip()
    status_sql = _catalog_filter(status)
    search_sql = ""
    parameters: list[object] = []
    if search:
        search_sql = (
            "AND (a.core_title LIKE ? ESCAPE '\\' "
            "OR a.readable_title LIKE ? ESCAPE '\\' "
            "OR a.catalog_query_title LIKE ? ESCAPE '\\' "
            "OR a.author LIKE ? ESCAPE '\\')"
        )
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        parameters.extend([f"%{escaped}%"] * 4)
    base = f"""
        FROM files AS f
        JOIN file_analysis AS a ON a.file_id = f.file_id
        LEFT JOIN catalog_titles AS ct ON ct.title_key = a.core_title
        WHERE f.active = 1 AND f.source = 'house'
        {search_sql}
        {status_sql}
    """
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        decision_store.validate_schema(conn, check_integrity=False)
        total = conn.execute(
            f"SELECT COUNT(DISTINCT a.core_title) {base}", parameters
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT
                a.core_title AS title_key,
                COALESCE(MAX(NULLIF(ct.display_title, '')), MAX(a.readable_title), a.core_title)
                    AS display_title,
                COALESCE(MAX(NULLIF(ct.query_title, '')), MAX(a.catalog_query_title), a.core_title)
                    AS query_title,
                MIN(NULLIF(a.author, '')) AS author,
                COUNT(*) AS file_count,
                MAX(a.effective_max) AS effective_max,
                MAX(a.complete) AS complete,
                MIN(a.unit) AS unit
            {base}
            GROUP BY a.core_title
            ORDER BY display_title COLLATE NOCASE, a.core_title
            LIMIT ? OFFSET ?
            """,
            [*parameters, limit, offset],
        ).fetchall()
        keys = [row["title_key"] for row in rows]
        files_by_key = {key: [] for key in keys}
        stats_by_key = {key: {} for key in keys}
        relations_by_key = {
            key: {"work_bucket_ids": set(), "variant_ids": set(), "folders": set(), "representative_file_ids": []}
            for key in keys
        }
        if keys:
            placeholders = ",".join("?" for _ in keys)
            file_rows = conn.execute(
                f"""
                SELECT a.core_title AS title_key, f.file_id, f.canonical_path,
                       a.readable_title, a.author, a.effective_max, a.unit, a.complete,
                       f.variant_id, v.work_bucket_id,
                       CASE WHEN r.file_id IS NULL THEN 0 ELSE 1 END AS representative
                FROM files AS f
                JOIN file_analysis AS a ON a.file_id = f.file_id
                LEFT JOIN variants AS v ON v.variant_id = f.variant_id
                LEFT JOIN representatives AS r ON r.file_id = f.file_id
                WHERE f.active = 1 AND f.source = 'house'
                  AND a.core_title IN ({placeholders})
                ORDER BY a.core_title, f.canonical_path
                """,
                keys,
            ).fetchall()
            for row in file_rows:
                path = str(row["canonical_path"])
                relation = relations_by_key[row["title_key"]]
                relation["folders"].add(str(Path(path).parent))
                if row["work_bucket_id"] is not None:
                    relation["work_bucket_ids"].add(int(row["work_bucket_id"]))
                if row["variant_id"] is not None:
                    relation["variant_ids"].add(int(row["variant_id"]))
                if row["representative"]:
                    relation["representative_file_ids"].append(row["file_id"])
                files_by_key[row["title_key"]].append({
                    "file_id": row["file_id"],
                    "name": Path(path).name,
                    "path": path,
                    "readable_title": row["readable_title"],
                    "author": row["author"],
                    "effective_max": row["effective_max"],
                    "unit": row["unit"],
                    "complete": bool(row["complete"]),
                })
            stat_rows = conn.execute(
                f"""
                SELECT * FROM catalog_platform_stats
                WHERE title_key IN ({placeholders})
                ORDER BY title_key, platform
                """,
                keys,
            ).fetchall()
            for row in stat_rows:
                stats_by_key[row["title_key"]][row["platform"]] = {
                    key: row[key]
                    for key in (
                        "platform", "status", "remote_title", "remote_url",
                        "download_count", "view_count", "recommend_count", "rating",
                        "rating_count", "last_attempt_at", "last_success_at", "retry_after",
                        "error_message",
                    )
                }
        items = []
        for row in rows:
            key = row["title_key"]
            relation = relations_by_key[key]
            items.append({
                "title_key": key,
                "display_title": row["display_title"],
                "query_title": row["query_title"],
                "author": row["author"],
                "file_count": row["file_count"],
                "effective_max": row["effective_max"],
                "complete": bool(row["complete"]),
                "unit": row["unit"],
                "files": files_by_key[key],
                "work_bucket_ids": sorted(relation["work_bucket_ids"]),
                "variant_ids": sorted(relation["variant_ids"]),
                "folders": sorted(relation["folders"], key=str.casefold),
                "representative_file_ids": relation["representative_file_ids"],
                "platforms": {
                    platform: stats_by_key[key].get(platform, {
                        "platform": platform,
                        "status": "missing",
                    })
                    for platform in PLATFORMS
                },
            })
        next_offset = offset + len(items)
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "cursor": str(offset) if offset else None,
            "next_cursor": str(next_offset) if next_offset < total else None,
            "search": search,
            "status": status,
            "readonly": True,
        }
    finally:
        conn.close()


def review_queue_listing(
    state_db: os.PathLike | str,
    temp_dir: os.PathLike | str,
    *,
    search: str = "",
    category: str = "all",
    physical: str = "all",
    limit: int = 100,
) -> dict:
    """Combine DB review rows and managed queue files without mutating either."""
    limit = _bounded_limit(limit)
    search = str(search or "").strip()
    if category not in {"all", "database", *REVIEW_QUEUE_NAMES}:
        raise ValueError("unknown review queue category")
    if physical not in {"all", "relation_only", "quarantined", "queue_missing"}:
        raise ValueError("unknown review queue physical filter")
    needle = search.casefold()
    all_items = []
    linked_queue_paths = set()
    if category in {"all", "database"}:
        conn = decision_store.connect_state_db_readonly(state_db)
        try:
            rows = conn.execute(
                """
                SELECT r.review_id, r.classification, r.state, r.queue_path,
                       r.created_at, r.candidate_file_id, r.reference_file_id,
                       candidate.canonical_path AS candidate_path,
                       candidate.source AS candidate_source,
                       reference.canonical_path AS reference_path,
                       reference.source AS reference_source
                FROM review_items AS r
                LEFT JOIN files AS candidate ON candidate.file_id = r.candidate_file_id
                LEFT JOIN files AS reference ON reference.file_id = r.reference_file_id
                WHERE r.state IN ('pending', 'deferred')
                ORDER BY r.created_at DESC, r.review_id DESC
                """
            ).fetchall()
            for row in rows:
                haystack = " ".join(str(row[key] or "") for key in row.keys()).casefold()
                if needle and needle not in haystack:
                    continue
                physical_path = row["queue_path"]
                if not physical_path and row["candidate_source"] == "queue":
                    physical_path = row["candidate_path"]
                if not physical_path and row["reference_source"] == "queue":
                    physical_path = row["reference_path"]
                physical_path = str(physical_path or "")
                path = Path(physical_path).expanduser() if physical_path else None
                if path is None:
                    physical_state = "relation_only"
                elif path.is_file():
                    physical_state = "quarantined"
                    physical_path = str(path.resolve())
                    linked_queue_paths.add(physical_path)
                else:
                    physical_state = "queue_missing"
                item = {
                    "kind": "database",
                    "category": row["classification"],
                    "state": row["state"],
                    "physical_state": physical_state,
                    "review_id": row["review_id"],
                    "candidate_path": row["candidate_path"],
                    "reference_path": row["reference_path"],
                    "queue_path": row["queue_path"],
                    "created_at": row["created_at"],
                }
                if physical_state == "quarantined":
                    stat = path.stat()
                    item.update({
                        "name": path.name,
                        "path": physical_path,
                        "size": stat.st_size,
                        "modified_at": stat.st_mtime,
                    })
                all_items.append(item)
        finally:
            conn.close()
    trash_root = Path(temp_dir) / "trash_bin"
    categories = REVIEW_QUEUE_NAMES if category == "all" else (category,)
    for name in categories:
        if name == "database":
            continue
        root = trash_root / name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
            if len(all_items) >= limit * 10:
                break
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if needle and needle not in str(path).casefold():
                continue
            resolved = str(path.resolve())
            if resolved in linked_queue_paths:
                continue
            stat = path.stat()
            all_items.append({
                "kind": "filesystem",
                "category": name,
                "state": "queued",
                "physical_state": "quarantined",
                "name": path.name,
                "path": resolved,
                "size": stat.st_size,
                "modified_at": stat.st_mtime,
            })
    summary = {
        state: sum(item["physical_state"] == state for item in all_items)
        for state in ("relation_only", "quarantined", "queue_missing")
    }
    items = [
        item for item in all_items
        if physical == "all" or item["physical_state"] == physical
    ]
    return {
        "items": items[:limit],
        "total_visible": len(items),
        "summary": summary,
        "limit": limit,
        "search": search,
        "category": category,
        "physical": physical,
        "readonly": True,
    }
