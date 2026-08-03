"""Versioned filename analysis and SQLite projection for library files.

All current/stale analysis decisions live here so Scanner, Folderling, volume
review, and catalog synchronization cannot silently reimplement the contract.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Iterable, Mapping, Optional

from state_repository import canonicalize_path
from volume_policy import coordinate_fields_from_name


def build_file_analysis(name: str) -> dict:
    """Return the versioned filename-derived metadata stored in SQLite."""
    from normalizer import (
        NORMALIZER_VERSION,
        analyze_name,
        extract_catalog_query_title,
        extract_readable_title,
        extract_structure_hint_tokens,
        normalize_nfc,
    )

    analyzed_name = normalize_nfc(name)
    info = analyze_name(analyzed_name)
    literal_tokens = tuple(info.get("title_literal_tokens") or ())
    structure_tokens = tuple(
        info.get("structure_hint_tokens")
        or extract_structure_hint_tokens(analyzed_name)
    )
    override_payload = None
    if structure_tokens:
        override_payload = {
            "title_literals": list(literal_tokens),
            "structure_hints": list(structure_tokens),
        }
    elif literal_tokens:
        # 1.2.10 저장 형식과 호환한다.
        override_payload = list(literal_tokens)
    return {
        "normalizer_version": NORMALIZER_VERSION,
        "analyzed_name": analyzed_name,
        "core_title": str(info.get("core_title") or "").strip(),
        "readable_title": extract_readable_title(analyzed_name).strip(),
        "catalog_query_title": extract_catalog_query_title(analyzed_name).strip(),
        "title_override_json": (
            json.dumps(override_payload, ensure_ascii=False, separators=(",", ":"))
            if override_payload is not None else None
        ),
        "author": info.get("author"),
        "max_number": int(info.get("max_number") or 0),
        "effective_max": int(info.get("effective_max") or 0),
        "unit": str(info.get("unit") or "미상"),
        "complete": 1 if info.get("complete") else 0,
        "disambig": int(info.get("disambig") or 1),
        "ext": info.get("ext") or "",
        "volume_number": info.get("volume_number"),
        "start_number": info.get("start_number"),
        "end_number": info.get("end_number"),
        "span_ambiguous": bool(info.get("span_ambiguous")),
        "is_side_story": bool(info.get("is_side_story")),
    }



def file_analysis_snapshot_is_current(row: Mapping[str, object]) -> bool:
    """Return whether a joined file/file_analysis row still describes its file.

    Callers that make routing decisions must preserve a current stored analysis,
    including a human-corrected author.  Reparse only when the normalizer or
    file identity columns prove that the stored snapshot is stale.
    """
    from normalizer import NORMALIZER_VERSION, normalize_nfc

    item = dict(row)
    canonical_path = item.get("canonical_path")
    if not canonical_path:
        return False
    analysis_version = item.get(
        "analysis_normalizer_version", item.get("normalizer_version")
    )
    return bool(
        analysis_version == NORMALIZER_VERSION
        and item.get("analyzed_name")
        == normalize_nfc(Path(str(canonical_path)).name)
        and item.get("analyzed_size") == item.get("size")
        and item.get("analyzed_mtime_ns") == item.get("mtime_ns")
        and (
            item.get("analyzed_ctime_ns") is None
            or item.get("analyzed_ctime_ns") == item.get("ctime_ns")
        )
    )



def resolve_current_file_analysis(
    row: Mapping[str, object], *, analysis_name: Optional[str] = None
) -> dict:
    """Resolve current metadata while retaining explicit title overrides.

    A stale filename identity invalidates parser-derived author and coordinate
    fields, but not a human-confirmed title override.  Keeping those policies
    separate prevents ``제 N권``-style works from losing their work identity
    after an mtime or normalizer change.
    """
    item = dict(row)
    if file_analysis_snapshot_is_current(item):
        return item
    name = analysis_name or Path(str(item.get("canonical_path") or "")).name
    effective = _effective_file_analysis(item, build_file_analysis(name))
    item.update({
        key: effective[key]
        for key in (
            "core_title", "readable_title", "catalog_query_title",
            "title_override_json", "author", "max_number", "effective_max",
            "unit", "complete", "disambig",
        )
    })
    item.update(coordinate_fields_from_name(name))
    return item



def _effective_file_analysis(current, analysis: dict) -> dict:
    """Preserve a human literal-title override after its transport markers are gone."""
    effective = dict(analysis)
    if effective.get("title_override_json"):
        return effective
    current_item = dict(current) if current is not None else {}
    if current_item.get("title_override_json"):
        effective.update({
            "core_title": current_item["core_title"],
            "readable_title": current_item["readable_title"],
            "catalog_query_title": current_item["catalog_query_title"],
            "title_override_json": current_item["title_override_json"],
        })
    return effective



def build_effective_file_analysis(
    conn: sqlite3.Connection, file_id: str, name: str
) -> dict:
    """Build filename metadata while honoring an existing user title override."""
    current = conn.execute(
        "SELECT * FROM file_analysis WHERE file_id = ?", (file_id,)
    ).fetchone()
    return _effective_file_analysis(current, build_file_analysis(name))



def _file_analysis_current(row, analysis: dict, stat_result) -> bool:
    return bool(
        row
        and row["normalizer_version"] == analysis["normalizer_version"]
        and row["analyzed_name"] == analysis["analyzed_name"]
        and row["core_title"] == analysis["core_title"]
        and row["readable_title"] == analysis["readable_title"]
        and row["catalog_query_title"] == analysis["catalog_query_title"]
        and row["title_override_json"] == analysis.get("title_override_json")
        and row["analyzed_size"] == stat_result.st_size
        and row["analyzed_mtime_ns"] == stat_result.st_mtime_ns
        and (
            row["analyzed_ctime_ns"] is None
            or row["analyzed_ctime_ns"] == stat_result.st_ctime_ns
        )
    )



def upsert_file_analysis(
    conn: sqlite3.Connection,
    file_id: str,
    path: os.PathLike | str,
    *,
    analysis: Optional[dict] = None,
    stat_result=None,
) -> bool:
    """Store one file's derived title metadata; return whether the row changed."""
    canonical_path = canonicalize_path(path)
    stat_result = stat_result or os.stat(canonical_path, follow_symlinks=False)
    current = conn.execute(
        "SELECT * FROM file_analysis WHERE file_id = ?", (file_id,)
    ).fetchone()
    analysis = _effective_file_analysis(
        current, analysis or build_file_analysis(Path(canonical_path).name)
    )
    if _file_analysis_current(current, analysis, stat_result):
        return False
    conn.execute(
        """
        INSERT INTO file_analysis(
            file_id, normalizer_version, analyzed_name,
            core_title, readable_title, catalog_query_title, title_override_json, author,
            max_number, effective_max, unit, complete, disambig,
            analyzed_size, analyzed_mtime_ns, analyzed_ctime_ns
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(file_id) DO UPDATE SET
            normalizer_version = excluded.normalizer_version,
            analyzed_name = excluded.analyzed_name,
            core_title = excluded.core_title,
            readable_title = excluded.readable_title,
            catalog_query_title = excluded.catalog_query_title,
            title_override_json = excluded.title_override_json,
            author = excluded.author,
            max_number = excluded.max_number,
            effective_max = excluded.effective_max,
            unit = excluded.unit,
            complete = excluded.complete,
            disambig = excluded.disambig,
            analyzed_size = excluded.analyzed_size,
            analyzed_mtime_ns = excluded.analyzed_mtime_ns,
            analyzed_ctime_ns = excluded.analyzed_ctime_ns,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            file_id,
            analysis["normalizer_version"],
            analysis["analyzed_name"],
            analysis["core_title"],
            analysis["readable_title"],
            analysis["catalog_query_title"],
            analysis.get("title_override_json"),
            analysis.get("author"),
            analysis["max_number"],
            analysis["effective_max"],
            analysis["unit"],
            analysis["complete"],
            analysis["disambig"],
            stat_result.st_size,
            stat_result.st_mtime_ns,
            stat_result.st_ctime_ns,
        ),
    )
    return True



def sync_contextual_bare_volume_metadata(
    conn: sqlite3.Connection,
    *,
    target_sources: Iterable[str] = ("house", "temp"),
    evidence_sources: Iterable[str] = ("house", "temp"),
    managed_core_hints: Optional[Mapping[str, str]] = None,
    eligible_file_ids: Optional[set[str]] = None,
    _build_file_analysis=None,
    _upsert_file_analysis=None,
) -> dict:
    """Recompute context-proven bare volume cores and coordinates.

    All active house/temp rows participate as evidence, while only
    ``target_sources`` are updated.  This lets the auditor project incoming
    temp metadata before a mutation without rewriting an already verified
    house snapshot.
    """

    from bare_volume_context import (
        context_name,
        has_bare_volume_shape,
        infer_bare_volume_overrides,
        parse_bare_volume_candidate,
    )
    from normalizer import NORMALIZER_VERSION, normalize_nfc

    analysis_builder = _build_file_analysis or build_file_analysis
    analysis_upserter = _upsert_file_analysis or upsert_file_analysis
    targets = {str(source) for source in target_sources}
    evidence = {str(source) for source in evidence_sources}
    if (
        not targets
        or not evidence
        or not targets <= evidence
        or not evidence <= {"house", "temp"}
    ):
        raise ValueError("contextual volume target_sources must be house/temp")

    placeholders = ", ".join("?" for _source in sorted(evidence))
    rows = conn.execute(
        f"""
        SELECT f.*, a.normalizer_version AS analysis_normalizer_version,
               a.analyzed_name AS analysis_analyzed_name,
               a.core_title AS analysis_core_title,
               a.readable_title AS analysis_readable_title,
               a.catalog_query_title AS analysis_catalog_query_title,
               a.title_override_json AS analysis_title_override_json,
               a.author AS analysis_author,
               a.max_number AS analysis_max_number,
               a.effective_max AS analysis_effective_max,
               a.unit AS analysis_unit,
               a.complete AS analysis_complete,
               a.disambig AS analysis_disambig,
               a.analyzed_size AS analysis_analyzed_size,
               a.analyzed_mtime_ns AS analysis_analyzed_mtime_ns,
               a.analyzed_ctime_ns AS analysis_analyzed_ctime_ns
        FROM files AS f
        LEFT JOIN file_analysis AS a ON a.file_id = f.file_id
        WHERE f.active = 1 AND f.source IN ({placeholders})
        ORDER BY f.canonical_path
        """,
        tuple(sorted(evidence)),
    ).fetchall()
    if eligible_file_ids is not None:
        eligible_file_ids = {str(file_id) for file_id in eligible_file_ids}
        rows = [
            row for row in rows if str(row["file_id"]) in eligible_file_ids
        ]

    managed_core_hints = {
        str(file_id): str(core_title or "").strip()
        for file_id, core_title in (managed_core_hints or {}).items()
        if str(core_title or "").strip()
    }
    records = []
    for row in rows:
        raw_name = normalize_nfc(Path(str(row["canonical_path"])).name)
        analysis_name = context_name(raw_name) if row["source"] == "temp" else raw_name
        candidate_shape = has_bare_volume_shape(analysis_name)
        current_analysis = None
        if row["analysis_normalizer_version"] is not None:
            current_analysis = {
                "normalizer_version": row["analysis_normalizer_version"],
                "analyzed_name": row["analysis_analyzed_name"],
                "core_title": row["analysis_core_title"],
                "readable_title": row["analysis_readable_title"],
                "catalog_query_title": row["analysis_catalog_query_title"],
                "title_override_json": row["analysis_title_override_json"],
                "author": row["analysis_author"],
                "max_number": row["analysis_max_number"],
                "effective_max": row["analysis_effective_max"],
                "unit": row["analysis_unit"],
                "complete": row["analysis_complete"],
                "disambig": row["analysis_disambig"],
                "analyzed_size": row["analysis_analyzed_size"],
                "analyzed_mtime_ns": row["analysis_analyzed_mtime_ns"],
                "analyzed_ctime_ns": row["analysis_analyzed_ctime_ns"],
                "volume_number": (
                    (row["part_num"], row["volume_num"])
                    if row["coordinate_kind"] == "volume"
                    and row["volume_num"] is not None
                    else None
                ),
                "start_number": (
                    row["episode_start"]
                    if row["coordinate_kind"] == "episode"
                    else None
                ),
                "end_number": (
                    row["episode_end"]
                    if row["coordinate_kind"] == "episode"
                    else None
                ),
                "span_ambiguous": bool(row["span_ambiguous"]),
                "is_side_story": (
                    row["coordinate_kind"] == "symbol"
                    and row["coordinate_symbol"] == "side_story"
                ),
            }
        current_projection_is_reusable = bool(
            current_analysis is not None
            and row["analysis_normalizer_version"] == NORMALIZER_VERSION
            and row["analysis_analyzed_name"] == raw_name
            and row["analysis_analyzed_size"] == row["size"]
            and row["analysis_analyzed_mtime_ns"] == row["mtime_ns"]
            and (
                row["analysis_analyzed_ctime_ns"] is None
                or row["analysis_analyzed_ctime_ns"] == row["ctime_ns"]
            )
        )
        if current_analysis is not None and (
            row["source"] not in targets
            or (current_projection_is_reusable and not candidate_shape)
        ):
            # Evidence-only rows and warm non-candidate target rows reuse the
            # verified projection. Only likely bare-number names need full
            # title reanalysis for promotion/demotion.
            effective_analysis = current_analysis
            baseline_coordinates = {
                "coordinate_kind": row["coordinate_kind"],
                "part_num": row["part_num"],
                "part_den": row["part_den"],
                "volume_num": row["volume_num"],
                "volume_den": row["volume_den"],
                "coordinate_symbol": row["coordinate_symbol"],
                "coordinate_sort_key": row["coordinate_sort_key"],
                "episode_start": row["episode_start"],
                "episode_end": row["episode_end"],
                "coordinate_raw": row["coordinate_raw"],
                "span_ambiguous": row["span_ambiguous"],
            }
        else:
            baseline_analysis = analysis_builder(analysis_name)
            # The analysis row is bound to the physical path identity even
            # when a temp transport name drops an uploader/source suffix for
            # semantics.
            baseline_analysis["analyzed_name"] = raw_name
            effective_analysis = _effective_file_analysis(
                current_analysis, baseline_analysis
            )
            baseline_coordinates = coordinate_fields_from_name(analysis_name)
        records.append({
            "key": row["file_id"],
            "name": analysis_name,
            "raw_name": raw_name,
            "row": row,
            "analysis": effective_analysis,
            "coordinates": baseline_coordinates,
            "assignment_state": row["assignment_state"],
            "current_core_title": (
                managed_core_hints.get(str(row["file_id"]))
                if row["assignment_state"] == "managed"
                else row["analysis_core_title"]
            ),
            "title_override": bool(row["analysis_title_override_json"]),
            # A house singleton and a newly arrived temp singleton can prove
            # one another.  Parse likely house names as evidence, but use the
            # cheap shape prefilter so warm audits do not run the full title
            # normalizer over the whole library.
            "candidate_enabled": candidate_shape,
        })

    overrides = infer_bare_volume_overrides(records)
    changed_analysis = 0
    changed_coordinates = 0
    promoted = 0
    rekeys = []

    def analysis_matches_current(row, analysis):
        """Compare semantic projection and its already-reconciled DB identity."""

        return bool(
            row["analysis_normalizer_version"] == analysis["normalizer_version"]
            and row["analysis_analyzed_name"] == analysis["analyzed_name"]
            and row["analysis_core_title"] == analysis["core_title"]
            and row["analysis_readable_title"] == analysis["readable_title"]
            and row["analysis_catalog_query_title"]
            == analysis["catalog_query_title"]
            and row["analysis_title_override_json"]
            == analysis.get("title_override_json")
            and row["analysis_author"] == analysis.get("author")
            and row["analysis_max_number"] == analysis["max_number"]
            and row["analysis_effective_max"] == analysis["effective_max"]
            and row["analysis_unit"] == analysis["unit"]
            and row["analysis_complete"] == analysis["complete"]
            and row["analysis_disambig"] == analysis["disambig"]
            and row["analysis_analyzed_size"] == row["size"]
            and row["analysis_analyzed_mtime_ns"] == row["mtime_ns"]
            and (
                row["analysis_analyzed_ctime_ns"] is None
                or row["analysis_analyzed_ctime_ns"] == row["ctime_ns"]
            )
        )

    for record in records:
        row = record["row"]
        if row["source"] not in targets:
            continue
        analysis = dict(record["analysis"])
        coordinates = dict(record["coordinates"])
        override = overrides.get(record["key"])
        if override is not None:
            analysis = override.apply_to_analysis(analysis)
            analysis["analyzed_name"] = record["raw_name"]
            coordinates = override.coordinate_fields()
            promoted += 1

        old_core = str(row["analysis_core_title"] or "").strip()
        new_core = str(analysis.get("core_title") or "").strip()
        if row["source"] == "house" and old_core and new_core and old_core != new_core:
            rekeys.append((old_core, new_core))

        if not analysis_matches_current(row, analysis):
            stat_result = os.stat(row["canonical_path"], follow_symlinks=False)
            if analysis_upserter(
                conn,
                row["file_id"],
                row["canonical_path"],
                analysis=analysis,
                stat_result=stat_result,
            ):
                changed_analysis += 1

        coordinate_values = (
            coordinates["coordinate_kind"],
            coordinates["part_num"],
            coordinates["part_den"],
            coordinates["volume_num"],
            coordinates["volume_den"],
            coordinates["coordinate_symbol"],
            coordinates["coordinate_sort_key"],
            coordinates["episode_start"],
            coordinates["episode_end"],
            coordinates["coordinate_raw"],
            int(bool(coordinates["span_ambiguous"])),
        )
        current_values = (
            row["coordinate_kind"], row["part_num"], row["part_den"],
            row["volume_num"], row["volume_den"], row["coordinate_symbol"],
            row["coordinate_sort_key"], row["episode_start"],
            row["episode_end"], row["coordinate_raw"],
            int(bool(row["span_ambiguous"])),
        )
        if current_values != coordinate_values:
            conn.execute(
                """
                UPDATE files SET
                    coordinate_kind = ?, part_num = ?, part_den = ?,
                    volume_num = ?, volume_den = ?, coordinate_symbol = ?,
                    coordinate_sort_key = ?, episode_start = ?, episode_end = ?,
                    coordinate_raw = ?, span_ambiguous = ?
                WHERE file_id = ?
                """,
                (*coordinate_values, row["file_id"]),
            )
            changed_coordinates += 1

    candidate_count = sum(
        parse_bare_volume_candidate(
            str(record["name"]),
            analysis=record["analysis"],
            title_override=bool(record["title_override"]),
        )
        is not None
        for record in records
        if record["row"]["source"] in targets
    )
    return {
        "evidence_file_count": len(records),
        "target_file_count": sum(
            record["row"]["source"] in targets for record in records
        ),
        "candidate_count": candidate_count,
        "promoted_count": promoted,
        "analysis_changed": changed_analysis,
        "coordinate_changed": changed_coordinates,
        "rekeys": rekeys,
    }



def migrate_catalog_title_keys(
    conn: sqlite3.Connection,
    rekeys: Iterable[tuple[str, str]],
) -> dict:
    """Move catalog state after a versioned filename-analysis key correction.

    The caller must update every file-analysis row in its scan scope first.  A
    source key is migrated only when no active house file still uses it.  This
    prevents a partial scan from stealing metadata from a still-valid title.

    Successful platform rows are preserved.  Negative/error rows are discarded
    because the corrected query title deserves a fresh lookup.  If the target
    already has a successful row, that newer canonical row wins.
    """
    destinations: dict[str, set[str]] = {}
    for old_key, new_key in rekeys:
        old_key = str(old_key or "").strip()
        new_key = str(new_key or "").strip()
        if old_key and new_key and old_key != new_key:
            destinations.setdefault(old_key, set()).add(new_key)

    ambiguous = {
        old_key: sorted(new_keys)
        for old_key, new_keys in destinations.items()
        if len(new_keys) != 1
    }
    if ambiguous:
        raise RuntimeError(
            "normalizer rekey is ambiguous; refusing catalog migration: "
            + json.dumps(ambiguous, ensure_ascii=False, sort_keys=True)
        )

    mapping = {old_key: next(iter(new_keys)) for old_key, new_keys in destinations.items()}
    chained = sorted(set(mapping) & set(mapping.values()))
    if chained:
        raise RuntimeError(
            "normalizer rekey contains a chain/cycle; refusing catalog migration: "
            + ", ".join(chained)
        )

    # Context-proven bare volumes commonly converge multiple former
    # ``title+number`` keys onto one work key. If two old keys both carry a
    # successful row for the same platform, selecting one by lexical filename
    # would silently bless arbitrary volume-level metadata as work-level data.
    # Drop only that ambiguous platform evidence so the canonical title gets a
    # fresh lookup; an already-successful canonical target still wins.
    sources_by_target: dict[str, set[str]] = {}
    for old_key, new_key in mapping.items():
        sources_by_target.setdefault(new_key, set()).add(old_key)
    ambiguous_success_rows: set[tuple[str, str]] = set()
    for new_key, old_keys in sources_by_target.items():
        if len(old_keys) < 2:
            continue
        placeholders = ", ".join("?" for _old_key in old_keys)
        target_ok_platforms = {
            str(row["platform"])
            for row in conn.execute(
                "SELECT platform FROM catalog_platform_stats "
                "WHERE title_key = ? AND status = 'ok'",
                (new_key,),
            )
        }
        ok_sources_by_platform: dict[str, list[str]] = {}
        for row in conn.execute(
            f"""
            SELECT title_key, platform
            FROM catalog_platform_stats
            WHERE title_key IN ({placeholders}) AND status = 'ok'
            """,
            tuple(sorted(old_keys)),
        ):
            ok_sources_by_platform.setdefault(str(row["platform"]), []).append(
                str(row["title_key"])
            )
        for platform, source_keys in ok_sources_by_platform.items():
            if platform in target_ok_platforms or len(source_keys) < 2:
                continue
            ambiguous_success_rows.update(
                (source_key, platform) for source_key in source_keys
            )

    active_keys = {
        str(row[0])
        for row in conn.execute(
            """
            SELECT DISTINCT a.core_title
            FROM file_analysis AS a
            JOIN files AS f ON f.file_id = a.file_id
            WHERE f.active = 1 AND f.source = 'house' AND a.core_title != ''
            """
        )
    }
    blocked = sorted(old_key for old_key in mapping if old_key in active_keys)
    migrated = 0
    successful_rows_preserved = 0
    failed_rows_discarded = 0
    ambiguous_success_rows_discarded = 0

    stat_columns = (
        "platform, status, remote_id, remote_title, remote_url, "
        "download_count, interest_count, view_count, recommend_count, "
        "rating, rating_count, last_attempt_at, last_success_at, retry_after, "
        "error_message, created_at, updated_at"
    )
    for old_key, new_key in sorted(mapping.items()):
        if old_key in active_keys:
            continue
        old_title = conn.execute(
            "SELECT 1 FROM catalog_titles WHERE title_key = ?", (old_key,)
        ).fetchone()
        if old_title is None:
            continue
        title = conn.execute(
            """
            SELECT a.readable_title, a.catalog_query_title
            FROM file_analysis AS a
            JOIN files AS f ON f.file_id = a.file_id
            WHERE f.active = 1 AND f.source = 'house' AND a.core_title = ?
            ORDER BY LENGTH(a.catalog_query_title), a.catalog_query_title
            LIMIT 1
            """,
            (new_key,),
        ).fetchone()
        if title is None:
            raise RuntimeError(
                f"normalizer rekey target has no active file analysis: {old_key} -> {new_key}"
            )

        from normalizer import NORMALIZER_VERSION

        display_title = str(title["catalog_query_title"] or title["readable_title"] or new_key)
        query_title = str(title["catalog_query_title"] or title["readable_title"] or new_key)
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
            (new_key, display_title, query_title, NORMALIZER_VERSION),
        )

        old_stats = conn.execute(
            "SELECT platform, status FROM catalog_platform_stats WHERE title_key = ?",
            (old_key,),
        ).fetchall()
        failed_rows_discarded += sum(row["status"] != "ok" for row in old_stats)
        for row in old_stats:
            if row["status"] != "ok":
                continue
            platform = row["platform"]
            if (old_key, platform) in ambiguous_success_rows:
                ambiguous_success_rows_discarded += 1
                continue
            existing = conn.execute(
                "SELECT status FROM catalog_platform_stats "
                "WHERE title_key = ? AND platform = ?",
                (new_key, platform),
            ).fetchone()
            if existing is not None and existing["status"] == "ok":
                continue
            if existing is not None:
                conn.execute(
                    "DELETE FROM catalog_platform_stats "
                    "WHERE title_key = ? AND platform = ?",
                    (new_key, platform),
                )
            conn.execute(
                f"""
                INSERT INTO catalog_platform_stats(title_key, {stat_columns})
                SELECT ?, {stat_columns}
                FROM catalog_platform_stats
                WHERE title_key = ? AND platform = ? AND status = 'ok'
                """,
                (new_key, old_key, platform),
            )
            successful_rows_preserved += 1

        # Cascades any negative rows intentionally left on the obsolete key.
        conn.execute("DELETE FROM catalog_titles WHERE title_key = ?", (old_key,))
        migrated += 1

    result = {
        "requested": len(mapping),
        "migrated": migrated,
        "blocked_active_source": len(blocked),
        "blocked_keys": blocked,
        "successful_rows_preserved": successful_rows_preserved,
        "failed_rows_discarded": failed_rows_discarded,
    }
    if ambiguous_success_rows_discarded:
        result["ambiguous_success_rows_discarded"] = (
            ambiguous_success_rows_discarded
        )
    return result



def file_analysis_sync_status(
    conn: sqlite3.Connection,
    *,
    eligible_paths: Optional[set[str]] = None,
) -> dict:
    """Inspect active house rows without changing the DB or library files."""
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    has_analysis = "file_analysis" in tables
    query = """
        SELECT f.file_id, f.canonical_path
        {analysis_columns}
        FROM files AS f
        {analysis_join}
        WHERE f.active = 1 AND f.source = 'house'
        ORDER BY f.canonical_path
    """.format(
        analysis_columns=(
            ", a.normalizer_version, a.analyzed_name, a.analyzed_size, "
            "a.analyzed_mtime_ns, a.analyzed_ctime_ns"
            if has_analysis else ""
        ),
        analysis_join=(
            "LEFT JOIN file_analysis AS a ON a.file_id = f.file_id"
            if has_analysis else ""
        ),
    )
    current = stale = missing = unindexed_active = 0
    seen_eligible_paths = set()
    from normalizer import NORMALIZER_VERSION, normalize_nfc

    for row in conn.execute(query):
        path = row["canonical_path"]
        canonical_path = canonicalize_path(path)
        if eligible_paths is not None and canonical_path not in eligible_paths:
            unindexed_active += 1
            continue
        seen_eligible_paths.add(canonical_path)
        try:
            stat_result = os.stat(path, follow_symlinks=False)
        except OSError:
            missing += 1
            continue
        if not has_analysis or row["normalizer_version"] is None:
            stale += 1
            continue
        is_current = (
            row["normalizer_version"] == NORMALIZER_VERSION
            and row["analyzed_name"] == normalize_nfc(Path(path).name)
            and row["analyzed_size"] == stat_result.st_size
            and row["analyzed_mtime_ns"] == stat_result.st_mtime_ns
            and (
                row["analyzed_ctime_ns"] is None
                or row["analyzed_ctime_ns"] == stat_result.st_ctime_ns
            )
        )
        if is_current:
            current += 1
        else:
            stale += 1
    index_missing_db = (
        len(eligible_paths - seen_eligible_paths) if eligible_paths is not None else 0
    )
    return {
        "total": current + stale + missing + index_missing_db,
        "current": current,
        "stale": stale,
        "missing_files": missing,
        "index_missing_db": index_missing_db,
        "schema_ready": has_analysis,
        "unindexed_active": unindexed_active,
    }



def sync_active_file_analysis(
    conn: sqlite3.Connection,
    *,
    eligible_paths: Optional[set[str]] = None,
    progress=None,
) -> dict:
    """Backfill indexed active house files inside the caller's transaction."""
    rows = conn.execute(
        """
        SELECT f.file_id, f.canonical_path, f.assignment_state,
               a.core_title AS previous_core_title
        FROM files AS f
        LEFT JOIN file_analysis AS a ON a.file_id = f.file_id
        WHERE f.active = 1 AND f.source = 'house'
        ORDER BY f.canonical_path
        """
    ).fetchall()
    if eligible_paths is not None:
        by_path = {canonicalize_path(row["canonical_path"]): row for row in rows}
        missing_db = sorted(eligible_paths - set(by_path))
        if missing_db:
            raise RuntimeError(
                "file index contains paths missing from active house DB; run Scanner first: "
                f"count={len(missing_db)}"
            )
        rows = [by_path[path] for path in sorted(eligible_paths)]
    # First calculate the complete projection without writing.  A normalizer
    # change may cause multiple old keys to meet an existing clean key.  That is
    # a dedup candidate, not permission to merge catalog/work state.
    planned = []
    for row in rows:
        path = row["canonical_path"]
        try:
            stat_result = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError(f"active house file is missing or unreadable: {path}: {exc}") from exc
        analysis = build_effective_file_analysis(
            conn, row["file_id"], Path(path).name
        )
        planned.append((row, path, stat_result, analysis))

    # Decide the final contextual keys before writing anything.  This keeps
    # the existing collision workflow fail-closed even when the caller forgot
    # to wrap this helper in a transaction, and avoids recording a transient
    # contextual-core -> raw-core -> contextual-core cycle.
    from bare_volume_context import (
        has_bare_volume_shape,
        infer_bare_volume_overrides,
    )

    prospective_records = []
    previous_core_by_file_id = {
        str(row["file_id"]): str(row["previous_core_title"] or "").strip()
        for row, _path, _stat_result, _analysis in planned
    }
    for row, path, _stat_result, analysis in planned:
        name = Path(path).name
        prospective_records.append({
            "key": row["file_id"],
            "name": name,
            "analysis": analysis,
            "coordinates": coordinate_fields_from_name(name),
            "assignment_state": row["assignment_state"],
            "current_core_title": row["previous_core_title"],
            "title_override": bool(analysis.get("title_override_json")),
            "candidate_enabled": has_bare_volume_shape(name),
        })
    prospective_overrides = infer_bare_volume_overrides(prospective_records)
    rekeys = []
    contextual_rekeys = set()
    for record in prospective_records:
        previous_core = previous_core_by_file_id.get(str(record["key"]), "")
        override = prospective_overrides.get(record["key"])
        final_core = str(
            override.core_title
            if override is not None
            else record["analysis"].get("core_title") or ""
        ).strip()
        if previous_core and final_core and previous_core != final_core:
            rekeys.append((previous_core, final_core))
            if override is not None:
                contextual_rekeys.add((previous_core, final_core))

    destinations: dict[str, set[str]] = {}
    sources: dict[str, set[str]] = {}
    for old_key, new_key in rekeys:
        old_key = str(old_key or "").strip()
        new_key = str(new_key or "").strip()
        if not old_key or not new_key:
            raise RuntimeError(
                f"normalizer produced an empty rekey: {old_key!r} -> {new_key!r}"
            )
        if old_key == new_key:
            continue
        destinations.setdefault(old_key, set()).add(new_key)
        sources.setdefault(new_key, set()).add(old_key)

    ambiguous_sources = {
        old_key: sorted(new_keys)
        for old_key, new_keys in destinations.items()
        if len(new_keys) != 1
    }
    catalog_keys = {
        str(row[0]) for row in conn.execute("SELECT title_key FROM catalog_titles")
    }
    collision_targets = {
        new_key: {
            "source_keys": sorted(old_keys),
            "existing_catalog_target": new_key in catalog_keys,
            "multiple_source_keys": len(old_keys) > 1,
            "context_proven_volume_convergence": all(
                (old_key, new_key) in contextual_rekeys for old_key in old_keys
            ),
        }
        for new_key, old_keys in sources.items()
        if new_key in catalog_keys
        or (
            len(old_keys) > 1
            and not all(
                (old_key, new_key) in contextual_rekeys for old_key in old_keys
            )
        )
    }
    if ambiguous_sources or collision_targets:
        detail = {
            "ambiguous_sources": ambiguous_sources,
            "collision_targets": collision_targets,
        }
        raise RuntimeError(
            "normalizer rekey requires dedup-before-catalog migration; "
            "run the title cleanup collision workflow first: "
            + json.dumps(detail, ensure_ascii=False, sort_keys=True)
        )

    changed = 0
    for index, (row, path, stat_result, analysis) in enumerate(planned, start=1):
        if upsert_file_analysis(
            conn, row["file_id"], path, analysis=analysis, stat_result=stat_result
        ):
            changed += 1
        if progress is not None and (index == 1 or index == len(rows) or index % 1000 == 0):
            progress({
                "phase": "file_analysis",
                "completed": index,
                "total": len(rows),
                "changed": changed,
            })
    contextual = sync_contextual_bare_volume_metadata(
        conn,
        target_sources=("house",),
        evidence_sources=("house",),
        managed_core_hints={
            file_id: core_title
            for file_id, core_title in previous_core_by_file_id.items()
            if core_title
        },
        eligible_file_ids={str(row["file_id"]) for row in rows},
    )
    title_rekeys = migrate_catalog_title_keys(conn, rekeys)
    changed = min(len(rows), changed + contextual["analysis_changed"])
    result = {
        "total": len(rows),
        "changed": changed,
        "unchanged": max(0, len(rows) - changed),
    }
    if any(
        contextual[key]
        for key in (
            "candidate_count", "promoted_count", "analysis_changed",
            "coordinate_changed",
        )
    ):
        result["contextual_bare_volumes"] = {
            key: value for key, value in contextual.items() if key != "rekeys"
        }
    if title_rekeys["requested"] or title_rekeys["blocked_active_source"]:
        result["title_rekeys"] = title_rekeys
    return result



def prune_file_analysis_projection(
    conn: sqlite3.Connection,
    *,
    seen_file_ids: set[str],
    scanned_roots: Iterable[os.PathLike | str],
) -> int:
    """Remove derived metadata for house rows excluded from a complete root scan."""
    roots = [
        canonicalize_path(root) for root in scanned_roots if os.path.isdir(root)
    ]
    if not roots:
        return 0

    def under_scanned_root(path: str) -> bool:
        canonical = canonicalize_path(path)
        for root in roots:
            try:
                if os.path.commonpath((canonical, root)) == root:
                    return True
            except ValueError:
                continue
        return False

    stale_ids = [
        row["file_id"]
        for row in conn.execute(
            "SELECT file_id, canonical_path FROM files WHERE active = 1 AND source = 'house'"
        )
        if row["file_id"] not in seen_file_ids and under_scanned_root(row["canonical_path"])
    ]
    if stale_ids:
        conn.executemany(
            "DELETE FROM file_analysis WHERE file_id = ?",
            ((file_id,) for file_id in stale_ids),
        )
    return len(stale_ids)



def reconcile_file_metadata(
    conn: sqlite3.Connection,
    path: os.PathLike | str,
    *,
    source: str,
    legacy_marker: bool = False,
    analysis: Optional[dict] = None,
):
    """Create/update one stable file identity without inferring any decision.

    Existing managed content changes are downgraded to decision_required and
    their old immutable fingerprint remains as provenance.  A marker on an
    already managed file is treated as display-only; markers only initialize
    previously unseen/unassigned files as legacy_unresolved.
    """
    canonical_path = canonicalize_path(path)
    stat = os.stat(canonical_path, follow_symlinks=False)
    coordinates = coordinate_fields_from_name(Path(canonical_path).name)
    row = conn.execute(
        "SELECT * FROM files WHERE canonical_path = ?", (canonical_path,)
    ).fetchone()
    if row is None:
        file_id = str(uuid.uuid4())
        assignment_state = "legacy_unresolved" if legacy_marker else "unassigned"
        conn.execute(
            """
            INSERT INTO files(
                file_id, canonical_path, source, size, mtime_ns, dev, ino, ctime_ns,
                assignment_state, active, coordinate_kind,
                part_num, part_den, volume_num, volume_den,
                coordinate_symbol, coordinate_sort_key, episode_start, episode_end,
                coordinate_raw, span_ambiguous
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                canonical_path,
                source,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_dev,
                stat.st_ino,
                stat.st_ctime_ns,
                assignment_state,
                coordinates["coordinate_kind"],
                coordinates["part_num"],
                coordinates["part_den"],
                coordinates["volume_num"],
                coordinates["volume_den"],
                coordinates["coordinate_symbol"],
                coordinates["coordinate_sort_key"],
                coordinates["episode_start"],
                coordinates["episode_end"],
                coordinates["coordinate_raw"],
                coordinates["span_ambiguous"],
            ),
        )
    else:
        file_id = row["file_id"]
        changed = (
            row["size"] != stat.st_size
            or row["mtime_ns"] != stat.st_mtime_ns
            or (row["dev"] is not None and row["dev"] != stat.st_dev)
            or (row["ino"] is not None and row["ino"] != stat.st_ino)
            or (row["ctime_ns"] is not None and row["ctime_ns"] != stat.st_ctime_ns)
        )
        assignment_state = row["assignment_state"]
        assignment_origin = row["assignment_origin"]
        current_fingerprint_id = row["current_fingerprint_id"]
        if changed:
            current_fingerprint_id = None
            if assignment_state == "managed":
                assignment_state = "decision_required"
                assignment_origin = None
        elif assignment_state == "unassigned" and legacy_marker:
            assignment_state = "legacy_unresolved"
        conn.execute(
            """
            UPDATE files
            SET source = ?, size = ?, mtime_ns = ?, dev = ?, ino = ?, ctime_ns = ?,
                last_seen_at = CURRENT_TIMESTAMP,
                assignment_state = ?, assignment_origin = ?, current_fingerprint_id = ?, active = 1
                , coordinate_kind = ?, part_num = ?, part_den = ?, volume_num = ?, volume_den = ?
                , coordinate_symbol = ?, coordinate_sort_key = ?, episode_start = ?, episode_end = ?
                , coordinate_raw = ?, span_ambiguous = ?
            WHERE file_id = ?
            """,
            (
                source,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_dev,
                stat.st_ino,
                stat.st_ctime_ns,
                assignment_state,
                assignment_origin,
                current_fingerprint_id,
                coordinates["coordinate_kind"],
                coordinates["part_num"],
                coordinates["part_den"],
                coordinates["volume_num"],
                coordinates["volume_den"],
                coordinates["coordinate_symbol"],
                coordinates["coordinate_sort_key"],
                coordinates["episode_start"],
                coordinates["episode_end"],
                coordinates["coordinate_raw"],
                coordinates["span_ambiguous"],
                file_id,
            ),
        )

    if source == "house":
        upsert_file_analysis(
            conn, file_id, canonical_path, analysis=analysis, stat_result=stat
        )
    elif source == "temp":
        # ``[[...]]`` 제목 literal과 ``{{...}}`` 구조 힌트는 temp 운반
        # 파일명에서 처음 발견된다.
        # house 입고 때 같은 file_id가 유지되므로 여기서 override를 붙여 둔다.
        temp_analysis = analysis or build_file_analysis(Path(canonical_path).name)
        if temp_analysis.get("title_override_json"):
            upsert_file_analysis(
                conn, file_id, canonical_path,
                analysis=temp_analysis, stat_result=stat,
            )

    return conn.execute(
        """
        SELECT
            f.canonical_path, f.file_id, f.variant_id, f.assignment_state,
            f.protected, v.work_bucket_id,
            CASE WHEN r.file_id IS NULL THEN 0 ELSE 1 END AS representative
        FROM files AS f
        LEFT JOIN variants AS v ON v.variant_id = f.variant_id
        LEFT JOIN representatives AS r ON r.file_id = f.file_id
        WHERE f.file_id = ?
        """,
        (file_id,),
    ).fetchone()
