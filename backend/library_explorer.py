"""Read-only 1.3.1 catalog explorer projections."""

from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Mapping

import decision_store


SUPPORTED_EXTENSIONS = frozenset({".txt", ".epub", ".pdf"})
QUARANTINE_ACTIONS = frozenset({
    "exact_quarantine", "human_quarantine", "user_quarantine",
    "suspected_move", "warning_move", "house_review_move",
    "volume_coordinate_hold",
})
MANAGEABLE_QUARANTINE_ACTIONS = frozenset({
    "exact_quarantine", "human_quarantine", "user_quarantine",
})
MAX_FOLDER_INVENTORY = 5_000
MAX_QUARANTINE_INVENTORY = 10_000
MAX_QUARANTINE_RELATED_FILES = 100
_FOLDER_CACHE: dict[str, tuple[tuple[int, ...], float, list[dict[str, Any]]]] = {}
_FOLDER_CACHE_TTL = 15.0


def _bounded_limit(value: int, maximum: int = 100) -> int:
    return max(1, min(int(value), maximum))


def _quarantine_related_files(conn, operations) -> dict[str, list[dict[str, Any]]]:
    """Project active house files related to each quarantined/queued file.

    A direct keep is strongest.  Active decision/review pairs follow, then
    same-core files.  We expose the basis rather than claiming that title
    equality alone proves duplicate content.
    """
    source_ids = sorted({str(row["file_id"]) for row in operations if row["file_id"]})
    if not source_ids:
        return {}
    placeholders = ",".join("?" for _ in source_ids)
    related: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)

    def add(source_id, file_id, path, size, basis, priority, confidence):
        if not source_id or not file_id or source_id == file_id or not path:
            return
        bucket = related[str(source_id)]
        item = bucket.get(str(file_id))
        if item is None:
            path = str(path)
            item = {
                "file_id": str(file_id), "name": Path(path).name,
                "path": path, "size": int(size or 0), "bases": [],
                "priority": int(priority), "confidence": confidence,
            }
            bucket[str(file_id)] = item
        item["priority"] = min(item["priority"], int(priority))
        confidence_rank = {"candidate": 0, "confirmed": 1, "kept": 2}
        if confidence_rank[confidence] > confidence_rank[item["confidence"]]:
            item["confidence"] = confidence
        if basis not in item["bases"]:
            item["bases"].append(basis)

    for row in operations:
        if (
            row["keep_file_id"] and row["keep_active"]
            and row["keep_source"] == "house"
        ):
            add(
                row["file_id"], row["keep_file_id"], row["keep_path"],
                row["keep_size"], "keep", 0, "kept",
            )

    review_rows = conn.execute(
        f"""
        WITH relations(source_file_id, related_file_id, detail) AS (
            SELECT candidate_file_id, reference_file_id, classification
            FROM review_items
            WHERE state IN ('pending', 'deferred')
              AND candidate_file_id IN ({placeholders})
            UNION ALL
            SELECT reference_file_id, candidate_file_id, classification
            FROM review_items
            WHERE state IN ('pending', 'deferred')
              AND reference_file_id IN ({placeholders})
        )
        SELECT r.source_file_id, r.related_file_id, r.detail,
               f.canonical_path, f.size
        FROM relations AS r JOIN files AS f ON f.file_id = r.related_file_id
        WHERE f.active = 1 AND f.source = 'house'
        """,
        (*source_ids, *source_ids),
    ).fetchall()
    for row in review_rows:
        add(
            row["source_file_id"], row["related_file_id"], row["canonical_path"],
            row["size"], f"review:{row['detail']}", 2, "candidate",
        )

    decision_rows = conn.execute(
        f"""
        WITH relations(source_file_id, related_file_id, detail) AS (
            SELECT left_file_id, right_file_id, verdict
            FROM decisions WHERE active = 1 AND left_file_id IN ({placeholders})
            UNION ALL
            SELECT right_file_id, left_file_id, verdict
            FROM decisions WHERE active = 1 AND right_file_id IN ({placeholders})
        )
        SELECT r.source_file_id, r.related_file_id, r.detail,
               f.canonical_path, f.size
        FROM relations AS r JOIN files AS f ON f.file_id = r.related_file_id
        WHERE f.active = 1 AND f.source = 'house'
        """,
        (*source_ids, *source_ids),
    ).fetchall()
    for row in decision_rows:
        verdict = str(row["detail"])
        add(
            row["source_file_id"], row["related_file_id"], row["canonical_path"],
            row["size"], f"decision:{verdict}",
            1 if verdict == "same_content" else 3,
            "confirmed" if verdict == "same_content" else "candidate",
        )

    core_rows = conn.execute(
        f"""
        SELECT source.file_id AS source_file_id, related.file_id AS related_file_id,
               related.canonical_path, related.size
        FROM file_analysis AS source_analysis
        JOIN files AS source ON source.file_id = source_analysis.file_id
        JOIN file_analysis AS related_analysis
          ON related_analysis.core_title = source_analysis.core_title
        JOIN files AS related ON related.file_id = related_analysis.file_id
        WHERE source.file_id IN ({placeholders})
          AND source_analysis.core_title != ''
          AND related.file_id != source.file_id
          AND related.active = 1 AND related.source = 'house'
        """,
        source_ids,
    ).fetchall()
    for row in core_rows:
        add(
            row["source_file_id"], row["related_file_id"], row["canonical_path"],
            row["size"], "same_core_title", 4, "candidate",
        )

    output = {}
    for source_id, values in related.items():
        ordered = sorted(
            values.values(),
            key=lambda item: (item["priority"], -item["size"], item["name"].casefold()),
        )[:MAX_QUARANTINE_RELATED_FILES]
        output[source_id] = [
            {key: value for key, value in item.items() if key != "priority"}
            for item in ordered
        ]
    return output


def _offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        offset = int(cursor)
    except (TypeError, ValueError) as exc:
        raise ValueError("cursor must be a non-negative integer") from exc
    if offset < 0:
        raise ValueError("cursor must be a non-negative integer")
    return offset


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _json(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return {"raw": str(value)}


def _page(items: list[dict], *, offset: int, limit: int, extra: dict | None = None) -> dict:
    visible = items[offset: offset + limit]
    next_offset = offset + len(visible)
    return {
        "items": visible,
        "total": len(items),
        "limit": limit,
        "cursor": str(offset) if offset else None,
        "next_cursor": str(next_offset) if next_offset < len(items) else None,
        "readonly": True,
        **(extra or {}),
    }


def file_listing(
    state_db: os.PathLike | str,
    *,
    search: str = "",
    source: str = "active",
    extension: str = "all",
    sort: str = "name",
    direction: str = "asc",
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    if source not in {"active", "review", "house", "temp", "queue", "quarantine", "inactive", "all"}:
        raise ValueError("unknown file source filter")
    if extension not in {"all", "txt", "epub", "pdf"}:
        raise ValueError("unknown extension filter")
    if sort not in {"name", "core", "size", "path", "seen"}:
        raise ValueError("unknown file sort")
    if direction not in {"asc", "desc"}:
        raise ValueError("unknown direction")
    limit = _bounded_limit(limit, 200)
    offset = _offset(cursor)
    clauses = []
    parameters: list[Any] = []
    if source == "active":
        clauses.append("f.active = 1")
    elif source == "review":
        clauses.append("f.active = 1")
        clauses.append(
            "EXISTS ("
            "SELECT 1 FROM review_items AS open_review "
            "JOIN files AS candidate ON candidate.file_id = open_review.candidate_file_id "
            "JOIN files AS reference ON reference.file_id = open_review.reference_file_id "
            "WHERE open_review.state IN ('pending', 'deferred') "
            "AND open_review.queue_path IS NULL "
            "AND candidate.source != 'queue' AND reference.source != 'queue' "
            "AND (open_review.candidate_file_id = f.file_id "
            "OR open_review.reference_file_id = f.file_id))"
        )
    elif source == "inactive":
        clauses.append("f.active = 0")
    elif source != "all":
        clauses.extend(["f.active = 1", "f.source = ?"])
        parameters.append(source)
    if extension != "all":
        clauses.append("LOWER(f.canonical_path) LIKE ?")
        parameters.append(f"%.{extension}")
    search = str(search or "").strip()
    if search:
        needle = _like(search)
        clauses.append(
            "(f.canonical_path LIKE ? ESCAPE '\\' OR f.file_id LIKE ? ESCAPE '\\' "
            "OR COALESCE(a.core_title, '') LIKE ? ESCAPE '\\' "
            "OR COALESCE(a.readable_title, '') LIKE ? ESCAPE '\\' "
            "OR COALESCE(a.catalog_query_title, '') LIKE ? ESCAPE '\\' "
            "OR COALESCE(a.author, '') LIKE ? ESCAPE '\\' "
            "OR CAST(f.variant_id AS TEXT) = ? OR CAST(v.work_bucket_id AS TEXT) = ?)"
        )
        parameters.extend([needle] * 6 + [search, search])
    where = " AND ".join(clauses) if clauses else "1 = 1"
    order = {
        "name": "f.canonical_path COLLATE NOCASE",
        "core": "COALESCE(a.core_title, '') COLLATE NOCASE",
        "size": "f.size",
        "path": "f.canonical_path COLLATE NOCASE",
        "seen": "f.last_seen_at",
    }[sort]
    direction_sql = "DESC" if direction == "desc" else "ASC"
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        base = f"""
            FROM files AS f
            LEFT JOIN file_analysis AS a ON a.file_id = f.file_id
            LEFT JOIN variants AS v ON v.variant_id = f.variant_id
            LEFT JOIN representatives AS r ON r.file_id = f.file_id
            LEFT JOIN fingerprints AS fp ON fp.fingerprint_id = f.current_fingerprint_id
            WHERE {where}
        """
        total = conn.execute(f"SELECT COUNT(*) {base}", parameters).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT f.file_id, f.canonical_path, f.source, f.active, f.size,
                   f.mtime_ns, f.last_seen_at, f.assignment_state,
                   f.assignment_origin, f.variant_id, f.protected,
                   f.coordinate_kind, f.part_num, f.part_den,
                   f.volume_num, f.volume_den, f.coordinate_symbol,
                   f.episode_start, f.episode_end, f.coordinate_raw,
                   a.core_title, a.readable_title, a.catalog_query_title,
                   a.author, a.effective_max, a.unit, a.complete,
                   v.work_bucket_id, v.variant_kind, v.label AS variant_label,
                   CASE WHEN r.file_id IS NULL THEN 0 ELSE 1 END AS representative,
                   fp.fingerprint_id, fp.status AS fingerprint_status,
                   fp.raw_sha256, fp.normalized_sha256, fp.normalized_length,
                   (SELECT COUNT(*) FROM review_items AS open_review
                    WHERE open_review.state IN ('pending', 'deferred')
                      AND (open_review.candidate_file_id = f.file_id
                           OR open_review.reference_file_id = f.file_id)) AS open_review_count
            {base}
            ORDER BY {order} {direction_sql}, f.file_id {direction_sql}
            LIMIT ? OFFSET ?
            """,
            [*parameters, limit, offset],
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            path = Path(str(row["canonical_path"]))
            item.update({
                "name": path.name,
                "extension": path.suffix.lower(),
                "parent": str(path.parent),
                "complete": bool(row["complete"] or 0),
                "active": bool(row["active"]),
                "protected": bool(row["protected"]),
                "representative": bool(row["representative"]),
                "retired_virtual_path": (
                    not bool(row["active"])
                    and ".dedup_state/retired_paths/" in str(row["canonical_path"])
                ),
            })
            items.append(item)
        next_offset = offset + len(items)
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "cursor": str(offset) if offset else None,
            "next_cursor": str(next_offset) if next_offset < total else None,
            "search": search,
            "source": source,
            "extension": extension,
            "sort": sort,
            "direction": direction,
            "readonly": True,
        }
    finally:
        conn.close()


def file_detail(state_db: os.PathLike | str, file_id: str) -> dict:
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        row = conn.execute(
            """
            SELECT f.*, a.*, v.work_bucket_id, v.variant_kind,
                   v.label AS variant_label, w.display_title AS work_title,
                   CASE WHEN r.file_id IS NULL THEN 0 ELSE 1 END AS representative,
                   fp.fingerprint_id, fp.fingerprint_version, fp.status AS fingerprint_status,
                   fp.raw_sha256, fp.normalized_sha256, fp.normalized_length,
                   fp.encoding, fp.front_anchor, fp.tail_anchor, fp.created_at AS fingerprint_created_at
            FROM files AS f
            LEFT JOIN file_analysis AS a ON a.file_id = f.file_id
            LEFT JOIN variants AS v ON v.variant_id = f.variant_id
            LEFT JOIN works AS w ON w.work_bucket_id = v.work_bucket_id
            LEFT JOIN representatives AS r ON r.file_id = f.file_id
            LEFT JOIN fingerprints AS fp ON fp.fingerprint_id = f.current_fingerprint_id
            WHERE f.file_id = ?
            """,
            (file_id,),
        ).fetchone()
        if row is None:
            raise KeyError(file_id)
        item = dict(row)
        item["active"] = bool(row["active"])
        item["protected"] = bool(row["protected"])
        item["representative"] = bool(row["representative"])
        item["complete"] = bool(row["complete"] or 0)
        path = Path(str(row["canonical_path"]))
        item["name"] = path.name
        item["parent"] = str(path.parent)
        item["extension"] = path.suffix.lower()
        item["retired_virtual_path"] = (
            not item["active"] and ".dedup_state/retired_paths/" in str(row["canonical_path"])
        )
        reviews = [
            {**dict(review), "evidence": _json(review["evidence_json"])}
            for review in conn.execute(
                """
                SELECT review_id, candidate_file_id, reference_file_id, classification,
                       state, decision_id, queue_path, evidence_json, created_at, updated_at
                FROM review_items
                WHERE candidate_file_id = ? OR reference_file_id = ?
                ORDER BY updated_at DESC, review_id DESC LIMIT 50
                """,
                (file_id, file_id),
            )
        ]
        decisions = [
            {**dict(decision), "evidence": _json(decision["evidence_json"])}
            for decision in conn.execute(
                """
                SELECT decision_id, left_file_id, right_file_id, verdict, decided_at,
                       note, supersedes_decision_id, active, evidence_json
                FROM decisions
                WHERE left_file_id = ? OR right_file_id = ?
                ORDER BY decided_at DESC, decision_id DESC LIMIT 50
                """,
                (file_id, file_id),
            )
        ]
        operations = [
            dict(operation)
            for operation in conn.execute(
                """
                SELECT operation_id, run_id, action, source_path, dest_path,
                       quarantine_path, file_id, keep_file_id, state, error,
                       created_at, updated_at, purged_at
                FROM operations
                WHERE file_id = ? OR keep_file_id = ?
                ORDER BY operation_id DESC LIMIT 50
                """,
                (file_id, file_id),
            )
        ]
        same_coordinate = []
        coordinate_kind = row["coordinate_kind"]
        if row["core_title"] and coordinate_kind in {"volume", "part", "symbol"}:
            conditions = {
                "volume": "other.volume_num = ? AND COALESCE(other.volume_den, 1) = COALESCE(?, 1)",
                "part": "other.part_num = ? AND COALESCE(other.part_den, 1) = COALESCE(?, 1)",
                "symbol": "other.coordinate_symbol = ? AND other.coordinate_symbol IS NOT NULL",
            }[coordinate_kind]
            values = {
                "volume": (row["volume_num"], row["volume_den"]),
                "part": (row["part_num"], row["part_den"]),
                "symbol": (row["coordinate_symbol"],),
            }[coordinate_kind]
            same_coordinate = [dict(other) for other in conn.execute(
                f"""
                SELECT other.file_id, other.canonical_path, other.size,
                       other.source, other.active, oa.author
                FROM files AS other
                JOIN file_analysis AS oa ON oa.file_id = other.file_id
                WHERE other.file_id != ? AND other.active = 1
                  AND oa.core_title = ? AND {conditions}
                ORDER BY other.canonical_path
                """,
                (file_id, row["core_title"], *values),
            )]
        common_blockers = []
        if not item["active"]:
            common_blockers.append("inactive_file")
        if item["source"] != "house":
            common_blockers.append("outside_active_house")
        quarantine_blockers = list(common_blockers)
        if item.get("fingerprint_id") is None:
            quarantine_blockers.append("missing_fingerprint")
        title_blockers = list(common_blockers)
        if item["protected"] or item["representative"]:
            title_blockers.append("protected_relationship")
        if item["assignment_state"] == "managed":
            title_blockers.append("managed_relationship")
        return {
            "file": item,
            "reviews": reviews,
            "decisions": decisions,
            "operations": operations,
            "same_coordinate": same_coordinate,
            "actions": {
                "compare": item["active"],
                "title_correction": not title_blockers,
                "quarantine": not quarantine_blockers,
                "move": False,
                "blocked_reasons": title_blockers,
                "quarantine_blocked_reasons": quarantine_blockers,
                "future_version": "1.3.3",
            },
            "readonly": True,
        }
    finally:
        conn.close()


def compare_files(state_db: os.PathLike | str, left_file_id: str, right_file_id: str) -> dict:
    if not left_file_id or not right_file_id or left_file_id == right_file_id:
        raise ValueError("two distinct file IDs are required")
    left = file_detail(state_db, left_file_id)["file"]
    right = file_detail(state_db, right_file_id)["file"]
    ordered = sorted((left_file_id, right_file_id))
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        review = conn.execute(
            """
            SELECT * FROM review_items
            WHERE (candidate_file_id = ? AND reference_file_id = ?)
               OR (candidate_file_id = ? AND reference_file_id = ?)
            ORDER BY updated_at DESC, review_id DESC LIMIT 1
            """,
            (left_file_id, right_file_id, right_file_id, left_file_id),
        ).fetchone()
        decision = conn.execute(
            """
            SELECT * FROM decisions
            WHERE left_file_id = ? AND right_file_id = ?
            ORDER BY active DESC, decided_at DESC, decision_id DESC LIMIT 1
            """,
            ordered,
        ).fetchone()
        pair = None
        left_fp, right_fp = left.get("fingerprint_id"), right.get("fingerprint_id")
        if left_fp is not None and right_fp is not None:
            fp_order = sorted((int(left_fp), int(right_fp)))
            pair = conn.execute(
                """
                SELECT * FROM pair_cache
                WHERE left_fingerprint_id = ? AND right_fingerprint_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                fp_order,
            ).fetchone()
        left_coordinate = _coordinate_key(left)
        right_coordinate = _coordinate_key(right)
        return {
            "left": left,
            "right": right,
            "comparison": {
                "same_core_title": bool(left.get("core_title")) and left.get("core_title") == right.get("core_title"),
                "same_author": bool(left.get("author")) and left.get("author") == right.get("author"),
                "same_coordinate": left_coordinate is not None and left_coordinate == right_coordinate,
                "same_raw_sha256": bool(left.get("raw_sha256")) and left.get("raw_sha256") == right.get("raw_sha256"),
                "same_normalized_sha256": bool(left.get("normalized_sha256")) and left.get("normalized_sha256") == right.get("normalized_sha256"),
                "size_delta": int(right.get("size") or 0) - int(left.get("size") or 0),
            },
            "latest_review": ({**dict(review), "evidence": _json(review["evidence_json"])} if review else None),
            "latest_decision": ({**dict(decision), "evidence": _json(decision["evidence_json"])} if decision else None),
            "latest_pair_cache": ({**dict(pair), "evidence": _json(pair["evidence_json"])} if pair else None),
            "relationship_preview": {
                "available_verdicts": ["same_content", "same_work_distinct_variant", "distinct_work"],
                "apply_available": False,
                "future_version": "1.3.2",
            },
            "readonly": True,
        }
    finally:
        conn.close()


def _coordinate_key(item: Mapping[str, Any]) -> tuple[Any, ...] | None:
    kind = item.get("coordinate_kind")
    if kind == "volume":
        return kind, item.get("volume_num"), item.get("volume_den") or 1
    if kind == "part":
        return kind, item.get("part_num"), item.get("part_den") or 1
    if kind == "symbol":
        return kind, item.get("coordinate_symbol")
    if kind == "episode":
        return kind, item.get("episode_start"), item.get("episode_end")
    return None


def _folder_snapshot(state_db: os.PathLike | str, house_dir: os.PathLike | str) -> list[dict]:
    db_path = Path(state_db).resolve()
    db_stat = db_path.stat()
    wal_path = Path(str(db_path) + "-wal")
    wal_stat = wal_path.stat() if wal_path.is_file() else None
    signature = (
        db_stat.st_size,
        db_stat.st_mtime_ns,
        wal_stat.st_size if wal_stat else 0,
        wal_stat.st_mtime_ns if wal_stat else 0,
    )
    cache_key = str(db_path)
    cached = _FOLDER_CACHE.get(cache_key)
    if cached and cached[0] == signature and time.monotonic() - cached[1] < _FOLDER_CACHE_TTL:
        return cached[2]
    house = Path(house_dir).resolve()
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        rows = conn.execute(
            """
            SELECT f.file_id, f.canonical_path, f.size, f.variant_id,
                   a.core_title, v.work_bucket_id
            FROM files AS f
            LEFT JOIN file_analysis AS a ON a.file_id = f.file_id
            LEFT JOIN variants AS v ON v.variant_id = f.variant_id
            WHERE f.active = 1 AND f.source = 'house'
            ORDER BY f.canonical_path
            """
        ).fetchall()
        managed_rows = conn.execute(
            """
            SELECT wf.folder_id, wf.work_bucket_id, wf.canonical_path, wf.role,
                   wf.state, w.display_title
            FROM work_folders AS wf
            JOIN works AS w ON w.work_bucket_id = wf.work_bucket_id
            WHERE wf.state = 'active'
            ORDER BY wf.canonical_path
            """
        ).fetchall()
    finally:
        conn.close()
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = Path(str(row["canonical_path"])).resolve()
        try:
            relative = path.parent.relative_to(house)
        except ValueError:
            continue
        parent = str(path.parent)
        item = grouped.setdefault(parent, {
            "path": parent,
            "name": path.parent.name,
            "relative_path": str(relative),
            "file_count": 0,
            "total_size": 0,
            "core_titles": set(),
            "work_bucket_ids": set(),
            "variant_ids": set(),
            "sample_files": [],
            "managed_folder_id": None,
            "managed_role": None,
            "managed_work_title": None,
        })
        item["file_count"] += 1
        item["total_size"] += int(row["size"])
        if row["core_title"]:
            item["core_titles"].add(str(row["core_title"]))
        if row["work_bucket_id"] is not None:
            item["work_bucket_ids"].add(int(row["work_bucket_id"]))
        if row["variant_id"] is not None:
            item["variant_ids"].add(int(row["variant_id"]))
        if len(item["sample_files"]) < 5:
            item["sample_files"].append(path.name)
    for row in managed_rows:
        path = Path(str(row["canonical_path"]))
        try:
            relative = path.relative_to(house)
        except ValueError:
            continue
        item = grouped.setdefault(str(path), {
            "path": str(path),
            "name": path.name,
            "relative_path": str(relative),
            "file_count": 0,
            "total_size": 0,
            "core_titles": set(),
            "work_bucket_ids": set(),
            "variant_ids": set(),
            "sample_files": [],
            "managed_folder_id": None,
            "managed_role": None,
            "managed_work_title": None,
        })
        item["managed_folder_id"] = int(row["folder_id"])
        item["managed_role"] = row["role"]
        item["managed_work_title"] = row["display_title"]
        item["work_bucket_ids"].add(int(row["work_bucket_id"]))
    output = []
    for item in grouped.values():
        output.append({
            **item,
            "core_titles": sorted(item["core_titles"]),
            "work_bucket_ids": sorted(item["work_bucket_ids"]),
            "variant_ids": sorted(item["variant_ids"]),
            "mixed_core": len(item["core_titles"]) > 1,
            "mixed_work": len(item["work_bucket_ids"]) > 1,
            "depth": len(Path(item["relative_path"]).parts),
        })
    _FOLDER_CACHE[cache_key] = (signature, time.monotonic(), output)
    return output


def folder_listing(
    state_db: os.PathLike | str,
    house_dir: os.PathLike | str,
    *,
    search: str = "",
    state: str = "all",
    sort: str = "name",
    direction: str = "asc",
    limit: int = 50,
    cursor: str | None = None,
    refresh: bool = False,
) -> dict:
    if state not in {"all", "managed", "mixed_core", "mixed_work", "single_file", "grouped"}:
        raise ValueError("unknown folder state filter")
    if sort not in {"name", "files", "size", "depth"}:
        raise ValueError("unknown folder sort")
    if direction not in {"asc", "desc"}:
        raise ValueError("unknown direction")
    if refresh:
        _FOLDER_CACHE.pop(str(Path(state_db).resolve()), None)
    items = list(_folder_snapshot(state_db, house_dir))
    needle = str(search or "").strip().casefold()
    if needle:
        items = [item for item in items if needle in " ".join([
            item["path"], item.get("managed_work_title") or "",
            *item["core_titles"], *item["sample_files"]
        ]).casefold()]
    filters = {
        "all": lambda item: True,
        "managed": lambda item: item.get("managed_folder_id") is not None,
        "mixed_core": lambda item: item["mixed_core"],
        "mixed_work": lambda item: item["mixed_work"],
        "single_file": lambda item: item["file_count"] == 1,
        "grouped": lambda item: item["file_count"] > 1,
    }
    items = [item for item in items if filters[state](item)]
    keys = {
        "name": lambda item: (item["name"].casefold(), item["path"].casefold()),
        "files": lambda item: (item["file_count"], item["name"].casefold()),
        "size": lambda item: (item["total_size"], item["name"].casefold()),
        "depth": lambda item: (item["depth"], item["name"].casefold()),
    }
    items.sort(key=keys[sort], reverse=direction == "desc")
    return _page(
        items,
        offset=_offset(cursor),
        limit=_bounded_limit(limit, 200),
        extra={"search": search, "state": state, "sort": sort, "direction": direction},
    )


def file_destination_candidates(
    state_db: os.PathLike | str,
    house_dir: os.PathLike | str,
    file_id: str,
    *,
    search: str = "",
    limit: int = 24,
) -> dict:
    """Rank existing house folders without changing the relocation contract."""
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        source = conn.execute(
            """
            SELECT f.file_id, f.canonical_path, a.core_title, a.readable_title,
                   v.work_bucket_id
            FROM files f
            LEFT JOIN file_analysis a ON a.file_id = f.file_id
            LEFT JOIN variants v ON v.variant_id = f.variant_id
            WHERE f.file_id = ? AND f.active = 1 AND f.source = 'house'
            """,
            (str(file_id),),
        ).fetchone()
        if source is None:
            raise KeyError(file_id)
    finally:
        conn.close()

    current_parent = str(Path(source["canonical_path"]).parent)
    source_core = str(source["core_title"] or "")
    source_readable = str(source["readable_title"] or Path(source["canonical_path"]).stem)
    source_work = int(source["work_bucket_id"]) if source["work_bucket_id"] is not None else None
    needle = str(search or "").strip().casefold()

    def key(value: str) -> str:
        return re.sub(r"[^0-9a-z가-힣]+", "", str(value).casefold())

    source_keys = [value for value in {key(source_core), key(source_readable)} if value]
    candidates = []
    for folder in _folder_snapshot(state_db, house_dir):
        haystack = " ".join([
            folder["name"], folder["relative_path"],
            folder.get("managed_work_title") or "",
            *folder["core_titles"], *folder["sample_files"],
        ]).casefold()
        if needle and needle not in haystack:
            continue
        folder_keys = [
            value for value in {
                key(folder["name"]),
                *(key(value) for value in folder["core_titles"]),
            } if value
        ]
        similarity = max(
            (SequenceMatcher(None, left, right).ratio()
             for left in source_keys for right in folder_keys),
            default=0.0,
        )
        exact_core = bool(source_core and source_core in folder["core_titles"])
        same_work = bool(source_work is not None and source_work in folder["work_bucket_ids"])
        current = folder["path"] == current_parent
        grouped_destination = bool(folder["depth"] > 1 or folder.get("managed_folder_id") is not None)
        if not needle and not current and not grouped_destination:
            continue
        if not needle and not (current or same_work or exact_core or similarity >= 0.48):
            continue
        score = similarity * 70.0
        reasons = []
        if same_work:
            score += 120
            reasons.append("같은 작품 관계")
        if exact_core:
            score += 100
            reasons.append("core title 일치")
        elif similarity >= 0.72:
            reasons.append("제목 매우 유사")
        elif similarity >= 0.5:
            reasons.append("제목 유사")
        if folder.get("managed_folder_id") is not None:
            score += 8
            reasons.append("관리 폴더")
        if needle:
            score += 35
            reasons.append("검색 일치")
        if current:
            score += 20 if grouped_destination else -100
            reasons.append("현재 위치")
        candidates.append({
            "path": folder["path"], "name": folder["name"],
            "relative_path": folder["relative_path"],
            "file_count": folder["file_count"], "total_size": folder["total_size"],
            "core_titles": folder["core_titles"][:4],
            "work_bucket_ids": folder["work_bucket_ids"],
            "managed_folder_id": folder.get("managed_folder_id"),
            "managed_role": folder.get("managed_role"),
            "similarity": round(similarity, 4), "score": round(score, 3),
            "reasons": reasons or (["검색 일치"] if needle else ["제목 일부 유사"]),
            "grouped_destination": grouped_destination, "current": current,
        })
    candidates.sort(
        key=lambda item: (item["score"], item["file_count"], item["name"].casefold()),
        reverse=True,
    )
    limit = _bounded_limit(limit, 50)
    return {
        "source": {
            "file_id": source["file_id"], "path": source["canonical_path"],
            "core_title": source_core, "readable_title": source_readable,
            "work_bucket_id": source_work, "current_parent": current_parent,
        },
        "items": candidates[:limit], "search": search, "limit": limit,
        "readonly": True,
    }


def folder_detail(
    state_db: os.PathLike | str,
    house_dir: os.PathLike | str,
    folder_path: str,
) -> dict:
    house = Path(house_dir).resolve()
    folder = Path(folder_path).expanduser().resolve()
    try:
        relative = folder.relative_to(house)
    except ValueError as exc:
        raise ValueError("folder is outside house") from exc
    if not folder.is_dir() or folder.is_symlink():
        raise FileNotFoundError(folder)
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        db_rows = conn.execute(
            """
            SELECT f.file_id, f.canonical_path, f.size, f.source, f.active,
                   f.assignment_state, f.variant_id, a.core_title, a.author,
                   v.work_bucket_id
            FROM files AS f
            LEFT JOIN file_analysis AS a ON a.file_id = f.file_id
            LEFT JOIN variants AS v ON v.variant_id = f.variant_id
            WHERE f.active = 1 AND f.source = 'house'
              AND (f.canonical_path = ? OR f.canonical_path LIKE ? ESCAPE '\\')
            ORDER BY f.canonical_path
            """,
            (str(folder), _like(str(folder) + os.sep)),
        ).fetchall()
        managed = conn.execute(
            """
            SELECT wf.folder_id, wf.work_bucket_id, wf.role, wf.state,
                   w.display_title AS work_title
            FROM work_folders AS wf
            JOIN works AS w ON w.work_bucket_id = wf.work_bucket_id
            WHERE wf.canonical_path = ? AND wf.state = 'active'
            """,
            (str(folder),),
        ).fetchone()
    finally:
        conn.close()
    by_path = {decision_store.canonicalize_path(row["canonical_path"]): dict(row) for row in db_rows}
    entries = []
    truncated = False
    for current, directories, filenames in os.walk(folder, followlinks=False):
        directories[:] = [name for name in directories if not (Path(current) / name).is_symlink()]
        for name in filenames:
            path = Path(current) / name
            if len(entries) >= MAX_FOLDER_INVENTORY:
                truncated = True
                break
            stat = path.lstat()
            canonical = decision_store.canonicalize_path(path)
            db = by_path.get(canonical)
            entries.append({
                "name": name,
                "path": str(path),
                "relative_path": str(path.relative_to(folder)),
                "size": stat.st_size,
                "extension": path.suffix.lower(),
                "registered": db is not None,
                "symlink": path.is_symlink(),
                "file": db,
            })
        if truncated:
            break
    entries.sort(key=lambda item: item["relative_path"].casefold())
    registered = sum(item["registered"] for item in entries)
    return {
        "path": str(folder),
        "relative_path": str(relative),
        "entries": entries,
        "registered_count": registered,
        "unregistered_count": len(entries) - registered,
        "total_size": sum(int(item["size"]) for item in entries),
        "truncated": truncated,
        "managed_folder": dict(managed) if managed else None,
        "actions": {
            "rename": managed is not None,
            "move": managed is not None,
            "quarantine": registered > 0 and not truncated,
            "future_version": None,
        },
        "readonly": True,
    }


def quarantine_listing(
    state_db: os.PathLike | str,
    temp_dir: os.PathLike | str,
    *,
    search: str = "",
    state: str = "all",
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    if state not in {"all", "present", "missing", "untracked", "purged"}:
        raise ValueError("unknown quarantine state filter")
    trash = (Path(temp_dir) / "trash_bin").resolve()
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        operations = conn.execute(
            """
            SELECT o.operation_id, o.run_id, o.action, o.source_path, o.dest_path,
                   o.quarantine_path, o.file_id, o.keep_file_id, o.state AS operation_state,
                   o.purged_at, o.created_at, o.updated_at,
                   o.expected_size AS source_size,
                   f.active AS file_active, f.source AS file_source,
                   keep.canonical_path AS keep_path, keep.size AS keep_size,
                   keep.active AS keep_active, keep.source AS keep_source
            FROM operations AS o
            LEFT JOIN files AS f ON f.file_id = o.file_id
            LEFT JOIN files AS keep ON keep.file_id = o.keep_file_id
            WHERE o.action IN ({}) AND o.state = 'committed'
            ORDER BY o.operation_id DESC
            """.format(",".join("?" for _ in QUARANTINE_ACTIONS)),
            sorted(QUARANTINE_ACTIONS),
        ).fetchall()
        related_by_source = _quarantine_related_files(conn, operations)
    finally:
        conn.close()
    items = []
    linked_paths = set()
    for row in operations:
        physical = row["quarantine_path"] or row["dest_path"]
        if not physical:
            continue
        path = Path(str(physical)).expanduser()
        try:
            path.resolve().relative_to(trash)
        except ValueError:
            continue
        resolved = str(path.resolve())
        linked_paths.add(resolved)
        exists = path.is_file() and not path.is_symlink()
        item_state = "purged" if row["purged_at"] else "present" if exists else "missing"
        stat = path.stat() if exists else None
        items.append({
            **dict(row),
            "name": path.name,
            "path": resolved,
            "category": str(path.parent.relative_to(trash)),
            "physical_state": item_state,
            "size": stat.st_size if stat else None,
            "modified_at": stat.st_mtime if stat else None,
            "age_days": max(0, int((time.time() - stat.st_mtime) // 86400)) if stat else None,
            "related_files": related_by_source.get(str(row["file_id"]), []),
            "tracked": True,
            "restore_available": item_state == "present" and row["action"] in MANAGEABLE_QUARANTINE_ACTIONS,
            "purge_available": item_state == "present" and row["action"] in MANAGEABLE_QUARANTINE_ACTIONS,
            "future_version": None,
        })
    if trash.is_dir():
        scanned = 0
        for path in sorted(trash.rglob("*"), key=lambda value: str(value).casefold()):
            if scanned >= MAX_QUARANTINE_INVENTORY:
                break
            if not path.is_file() or path.is_symlink():
                continue
            scanned += 1
            resolved = str(path.resolve())
            if resolved in linked_paths:
                continue
            stat = path.stat()
            items.append({
                "operation_id": None,
                "action": None,
                "source_path": None,
                "source_size": None,
                "keep_path": None,
                "keep_size": None,
                "name": path.name,
                "path": resolved,
                "category": str(path.parent.relative_to(trash)),
                "physical_state": "untracked",
                "size": stat.st_size,
                "modified_at": stat.st_mtime,
                "age_days": max(0, int((time.time() - stat.st_mtime) // 86400)),
                "related_files": [],
                "tracked": False,
                "restore_available": False,
                "purge_available": False,
                "future_version": "1.3.2",
            })
    needle = str(search or "").strip().casefold()
    if needle:
        items = [item for item in items if needle in " ".join(str(item.get(key) or "") for key in (
            "name", "path", "source_path", "keep_path", "category", "action"
        )).casefold()]
    summary = {key: sum(item["physical_state"] == key for item in items) for key in (
        "present", "missing", "untracked", "purged"
    )}
    if state != "all":
        items = [item for item in items if item["physical_state"] == state]
    # Put entries that can actually be managed by the 1.3.2 UI first.  The
    # trash tree also contains older warning/house review queue files; sorting
    # only by recency made an entire first screen look disabled even though
    # user/exact quarantine entries were available further down the list.
    items.sort(
        key=lambda item: (
            bool(item.get("purge_available") or item.get("restore_available")),
            str(item.get("updated_at") or ""),
            str(item["path"]),
        ),
        reverse=True,
    )
    return _page(
        items,
        offset=_offset(cursor),
        limit=_bounded_limit(limit, 200),
        extra={"search": search, "state": state, "summary": summary},
    )
