"""Series-folder inventory, automatic plans, and approved mutations."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import unicodedata
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Mapping, Optional, Sequence

import decision_store
from mutation_io import mutation_lock_for_roots
from normalizer import NORMALIZER_VERSION, extract_author, get_chosung, normalize_nfc
from volume_group_mutations import (
    cleanup_staging,
    merge_staged_volume_group,
    remove_empty_source_folders,
    stage_volume_sources,
)


VOLUME_KINDS = frozenset({"volume", "part", "episode", "symbol"})
SUPPORTED_EXTENSIONS = frozenset({".txt", ".epub", ".pdf"})
CLASSIFICATIONS = frozenset(
    {"all", "auto_ready", "review_required", "already_grouped", "excluded"}
)
_CLASS_ORDER = {
    "review_required": 0,
    "auto_ready": 1,
    "already_grouped": 2,
    "excluded": 3,
}
_VOLUME_CASE_CACHE_CONDITION = threading.Condition()
_VOLUME_CASE_CACHE: dict[tuple[str, str], dict] = {}
_VOLUME_CASE_INFLIGHT: set[tuple[str, str]] = set()


def _volume_case_cache_key(state_db: Path, house_dir: Path) -> tuple[str, str]:
    return (
        str(Path(state_db).expanduser().resolve()),
        str(Path(house_dir).expanduser().resolve()),
    )


def _state_db_revision_signature(state_db: Path) -> tuple:
    """Return a cheap revision signal that also observes uncheckpointed WAL writes."""

    state_db = Path(state_db).expanduser().resolve()
    signature = []
    for index, path in enumerate((state_db, Path(str(state_db) + "-wal"))):
        try:
            stat = path.stat()
        except FileNotFoundError:
            signature.append(None)
        else:
            if index == 1 and stat.st_size == 0:
                # SQLite may create/remove an empty WAL while opening a reader.
                # It carries no committed revision and must not invalidate cache.
                signature.append(None)
                continue
            signature.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
    return tuple(signature)


def invalidate_volume_case_cache(
    state_db: Optional[Path] = None, *, house_dir: Optional[Path] = None
) -> None:
    """Drop cached listing data after a known mutation or in tests."""

    with _VOLUME_CASE_CACHE_CONDITION:
        if state_db is None or house_dir is None:
            _VOLUME_CASE_CACHE.clear()
        else:
            _VOLUME_CASE_CACHE.pop(
                _volume_case_cache_key(state_db, house_dir), None
            )
        _VOLUME_CASE_CACHE_CONDITION.notify_all()


def _encode_cursor(offset: int) -> str:
    raw = json.dumps({"offset": int(offset)}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: Optional[str]) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        offset = int(json.loads(base64.urlsafe_b64decode(padded))["offset"])
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    if offset < 0:
        raise ValueError("invalid cursor")
    return offset


def _coordinate(row: Mapping[str, object]) -> tuple[tuple, str, object]:
    kind = row["coordinate_kind"]
    if kind == "volume":
        value = Fraction(int(row["volume_num"]), int(row["volume_den"] or 1))
        volume_label = (
            f"{value.numerator}권"
            if value.denominator == 1 else f"{float(value):g}권"
        )
        part = None
        if row.get("part_num") is not None:
            part = Fraction(int(row["part_num"]), int(row["part_den"] or 1))
        part_label = ""
        if part is not None:
            part_label = (
                f"{part.numerator}부 "
                if part.denominator == 1 else f"{float(part):g}부 "
            )
        label = part_label + volume_label
        part_key = (
            (part.numerator, part.denominator) if part is not None else None
        )
        return (
            (0, 0 if part is None else 1, part or Fraction(0), value),
            label,
            (kind, part_key, value.numerator, value.denominator),
        )
    if kind == "part":
        value = Fraction(int(row["part_num"]), int(row["part_den"] or 1))
        label = f"{value.numerator}부" if value.denominator == 1 else f"{float(value):g}부"
        return (1, value), label, (kind, value.numerator, value.denominator)
    if kind == "episode":
        start = int(row["episode_start"])
        end = int(row["episode_end"])
        unit = str(row.get("unit") or "회차")
        if unit == "미상":
            unit = "회차"
        label = f"{start}{unit}" if start == end else f"{start}~{end}{unit}"
        return (2, start, end, unit), label, (kind, start, end, unit)
    symbol = str(row["coordinate_symbol"] or row["coordinate_raw"] or "미상")
    sort_key = int(row["coordinate_sort_key"] or 0)
    label = symbol
    if symbol == "side_story" and sort_key > 200:
        label = f"외전 {sort_key - 200}"
    return (3, sort_key, symbol), label, (kind, symbol, sort_key)


def _safe_folder_name(value: str) -> str:
    value = unicodedata.normalize("NFC", str(value or "")).strip()
    value = re.sub(r"[\\/:\x00]", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value or value in {".", ".."}:
        return "분권 작품"
    encoded = os.fsencode(value)
    while len(encoded) > 240 and value:
        value = value[:-1].rstrip()
        encoded = os.fsencode(value)
    return value or "분권 작품"


def _common_display(rows: Sequence[Mapping[str, object]], core_title: str) -> str:
    candidates = [str(row["readable_title"] or "").strip() for row in rows]
    candidates = [value for value in candidates if value]
    if not candidates:
        return core_title
    counts = Counter(candidates)
    return sorted(counts, key=lambda value: (-counts[value], len(value), value))[0]


def _relative_parent(path: Path, house_dir: Path) -> tuple[str, bool]:
    try:
        relative = path.resolve().relative_to(house_dir)
    except ValueError:
        return str(path.parent), False
    parent = relative.parent
    return (str(parent) if str(parent) != "." else "<house>"), True


def _source_revision(rows: Sequence[Mapping[str, object]]) -> str:
    payload = [
        {
            "file_id": row["file_id"],
            "canonical_path": row["canonical_path"],
            "size": row["size"],
            "mtime_ns": row["mtime_ns"],
            "dev": row["dev"],
            "ino": row["ino"],
            "ctime_ns": row["ctime_ns"],
            "fingerprint_id": row["current_fingerprint_id"],
            "assignment_state": row["assignment_state"],
            "assignment_origin": row["assignment_origin"],
            "variant_id": row["variant_id"],
            "work_bucket_id": row["work_bucket_id"],
            "core_title": row["core_title"],
            "analysis_updated_at": row["analysis_updated_at"],
            "coordinate_kind": row["coordinate_kind"],
            "part_num": row["part_num"],
            "part_den": row["part_den"],
            "volume_num": row["volume_num"],
            "volume_den": row["volume_den"],
            "coordinate_symbol": row["coordinate_symbol"],
            "episode_start": row["episode_start"],
            "episode_end": row["episode_end"],
            "author": row["author"],
            "effective_max": row["effective_max"],
            "unit": row["unit"],
            "complete": row["complete"],
        }
        for row in sorted(rows, key=lambda item: str(item["file_id"]))
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _load_volume_rows(state_db: Path) -> list[Mapping[str, object]]:
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        rows = conn.execute(
            """
            SELECT f.file_id, f.canonical_path, f.size, f.mtime_ns,
                   f.dev, f.ino, f.ctime_ns, f.current_fingerprint_id,
                   f.assignment_state, f.assignment_origin, f.variant_id, f.protected,
                   f.coordinate_kind, f.part_num, f.part_den,
                   f.volume_num, f.volume_den, f.coordinate_symbol,
                   f.coordinate_sort_key, f.coordinate_raw, f.span_ambiguous,
                   f.episode_start, f.episode_end,
                   fa.analyzed_name, fa.core_title, fa.readable_title,
                   fa.author, fa.disambig, fa.effective_max, fa.unit,
                   fa.complete, fa.updated_at AS analysis_updated_at,
                   fa.normalizer_version AS analysis_normalizer_version,
                   fa.analyzed_size, fa.analyzed_mtime_ns, fa.analyzed_ctime_ns,
                   v.work_bucket_id,
                   CASE WHEN rep.file_id IS NULL THEN 0 ELSE 1 END AS representative
            FROM files AS f
            JOIN file_analysis AS fa ON fa.file_id = f.file_id
            LEFT JOIN variants AS v ON v.variant_id = f.variant_id
            LEFT JOIN representatives AS rep ON rep.file_id = f.file_id
            WHERE f.active = 1 AND f.source = 'house'
              AND f.coordinate_kind IN ('volume', 'part', 'episode', 'symbol')
            ORDER BY fa.core_title COLLATE NOCASE, f.canonical_path COLLATE NOCASE
            """
        ).fetchall()
        current_rows = []
        for row in rows:
            item = dict(row)
            path_name = normalize_nfc(Path(str(row["canonical_path"])).name)
            analysis_is_current = (
                row["analysis_normalizer_version"] == NORMALIZER_VERSION
                and row["analyzed_name"] == path_name
                and row["analyzed_size"] == row["size"]
                and row["analyzed_mtime_ns"] == row["mtime_ns"]
                and (
                    row["analyzed_ctime_ns"] is None
                    or row["analyzed_ctime_ns"] == row["ctime_ns"]
                )
            )
            if not analysis_is_current:
                # Scanner를 기다릴 수 없는 stale 행만 현재 parser로 보정한다.
                # 정상 행은 DB에 저장된 같은-version 결과를 사용해 목록 조회가
                # 16k 파일 전체를 매번 재파싱하지 않게 한다.
                item.update(
                    author=extract_author(str(row["analyzed_name"])),
                    **decision_store.coordinate_fields_from_name(
                        str(row["analyzed_name"])
                    ),
                )
            current_rows.append(item)
        return current_rows
    finally:
        conn.close()


def _is_parallel_coordinate_formats(rows: Sequence[Mapping[str, object]]) -> bool:
    """Return whether one coordinate is intentionally stored in distinct formats."""

    if len(rows) < 2:
        return False
    extensions = {
        Path(str(row["canonical_path"])).suffix.lower() for row in rows
    }
    authors = {str(row["author"]) for row in rows if row["author"]}
    if len(extensions) != len(rows) or "" in extensions or len(authors) > 1:
        return False

    # Preserve the stricter legacy side-story rule: an unnumbered completed
    # side story needs matching coverage as well as matching stems.  Ordinary
    # numbered volumes already share an exact canonical coordinate, so one file
    # per extension is enough to identify EPUB/PDF parallel storage.
    if all(
        row["coordinate_kind"] == "symbol"
        and row["coordinate_symbol"] == "side_story"
        for row in rows
    ):
        stems = {
            unicodedata.normalize(
                "NFC", Path(str(row["canonical_path"])).stem
            ).casefold()
            for row in rows
        }
        coverage = {
            (int(row["effective_max"]), str(row["unit"] or ""))
            for row in rows
        }
        return (
            all(bool(row["complete"]) and int(row["effective_max"] or 0) > 0 for row in rows)
            and len(stems) == 1
            and len(coverage) == 1
        )
    return all(
        row["coordinate_kind"] in {"volume", "part", "episode"}
        for row in rows
    )


def _has_incompatible_coordinate_kinds(rows: Sequence[Mapping[str, object]]) -> bool:
    """Allow ordinary volumes/parts plus a side story in one work folder."""

    main_kinds = {
        str(row["coordinate_kind"])
        for row in rows
        if row["coordinate_kind"] != "symbol"
    }
    if len(main_kinds) > 1:
        return True
    volume_part_modes = {
        row.get("part_num") is not None
        for row in rows if row["coordinate_kind"] == "volume"
    }
    if len(volume_part_modes) > 1:
        return True
    return any(
        row["coordinate_kind"] == "symbol"
        and row["coordinate_symbol"] != "side_story"
        and bool(main_kinds)
        for row in rows
    )


def _case_from_rows(core_title: str, rows: Sequence[Mapping[str, object]], house_dir: Path) -> dict:
    display_title = _common_display(rows, core_title)
    house_dir = Path(house_dir).expanduser().resolve()
    coordinates = []
    coordinate_labels = {}
    rows_by_coordinate = defaultdict(list)
    items = []
    parents = set()
    parent_paths = set()
    outside_house = False
    for row in rows:
        path = Path(str(row["canonical_path"]))
        sort_key, coordinate_label, coordinate_key = _coordinate(row)
        coordinates.append(coordinate_key)
        coordinate_labels[coordinate_key] = coordinate_label
        rows_by_coordinate[coordinate_key].append(row)
        relative_parent, inside = _relative_parent(path, house_dir)
        outside_house = outside_house or not inside
        parents.add(relative_parent)
        parent_paths.add(path.parent.resolve())
        items.append(
            {
                "file_id": row["file_id"],
                "name": path.name,
                "canonical_path": str(path),
                "parent": relative_parent,
                "extension": path.suffix.lower(),
                "size": row["size"],
                "author": row["author"],
                "coordinate_kind": row["coordinate_kind"],
                "coordinate": coordinate_label,
                "coordinate_sort": [str(part) for part in sort_key],
                "coordinate_raw": row["coordinate_raw"],
                "effective_max": row["effective_max"],
                "unit": row["unit"],
                "complete": bool(row["complete"]),
                "span_ambiguous": bool(row["span_ambiguous"]),
                "_coordinate_key": coordinate_key,
                "_sort_key": sort_key,
                "assignment_state": row["assignment_state"],
                "assignment_origin": row["assignment_origin"],
                "variant_id": row["variant_id"],
                "work_bucket_id": row["work_bucket_id"],
                "protected": bool(row["protected"]),
                "representative": bool(row["representative"]),
            }
        )
    coordinate_counts = Counter(coordinates)
    repeated_coordinate_keys = {
        key for key, count in coordinate_counts.items() if count > 1
    }
    parallel_format_keys = {
        key
        for key in repeated_coordinate_keys
        if _is_parallel_coordinate_formats(rows_by_coordinate[key])
    }
    conflicting_coordinate_keys = repeated_coordinate_keys - parallel_format_keys
    duplicate_coordinates = sorted(
        coordinate_labels[key] for key in conflicting_coordinate_keys
    )
    parallel_format_coordinates = sorted(
        coordinate_labels[key] for key in parallel_format_keys
    )
    kinds = {str(row["coordinate_kind"]) for row in rows}
    authors = sorted({str(row["author"]) for row in rows if row["author"]})
    work_ids = sorted({int(row["work_bucket_id"]) for row in rows if row["work_bucket_id"] is not None})
    deep_parents = []
    for parent in parent_paths:
        try:
            relative = parent.relative_to(house_dir)
        except ValueError:
            continue
        if len(relative.parts) > 1:
            deep_parents.append(parent)
    already_grouped = len(parent_paths) == 1 and bool(deep_parents)
    main_coordinate_keys = {
        position
        for row in rows
        if (position := _series_position(row)) is not None
    }
    has_side_story = any(
        row["coordinate_kind"] == "symbol"
        and row["coordinate_symbol"] == "side_story"
        for row in rows
    )
    side_story_requires_review = (
        not already_grouped
        and has_side_story
        and len(main_coordinate_keys) < 2
    )

    # A user may deliberately keep two different EPUB variants at the same
    # volume coordinate.  Once every file is safely linked to one managed work
    # and the repeated coordinate itself carries a human-decision origin, the
    # review inventory should describe that fact instead of reopening it as a
    # blocking conflict.  Keep the coordinate evidence in the response.
    one_managed_work = (
        already_grouped
        and len(work_ids) == 1
        and all(
            row["work_bucket_id"] == work_ids[0]
            and row["assignment_state"] == "managed"
            and row["variant_id"] is not None
            and bool(row["protected"])
            and bool(row["representative"])
            for row in rows
        )
    )
    approved_duplicate_keys = {
        key
        for key in conflicting_coordinate_keys
        if one_managed_work
        and all(
            row["assignment_origin"] == "human_decision"
            for row in rows_by_coordinate[key]
        )
    }
    unapproved_duplicate_keys = conflicting_coordinate_keys - approved_duplicate_keys
    approved_duplicate_coordinates = sorted(
        coordinate_labels[key] for key in approved_duplicate_keys
    )
    unapproved_duplicate_coordinates = sorted(
        coordinate_labels[key] for key in unapproved_duplicate_keys
    )
    missing_coordinates = []
    if kinds == {"volume"}:
        values_by_part = defaultdict(set)
        for row in rows:
            part = (
                Fraction(int(row["part_num"]), int(row["part_den"] or 1))
                if row.get("part_num") is not None else None
            )
            values_by_part[part].add(
                Fraction(int(row["volume_num"]), int(row["volume_den"] or 1))
            )
        for part, values in sorted(
            values_by_part.items(), key=lambda item: item[0] or Fraction(0)
        ):
            if values and all(value.denominator == 1 for value in values):
                maximum = max(int(value) for value in values)
                if maximum <= 500:
                    prefix = "" if part is None else (
                        f"{part.numerator}부 "
                        if part.denominator == 1 else f"{float(part):g}부 "
                    )
                    missing_coordinates.extend(
                        f"{prefix}{number}권"
                        for number in range(1, maximum + 1)
                        if Fraction(number) not in values
                    )

    incompatible_coordinate_kinds = _has_incompatible_coordinate_kinds(rows)
    for item in items:
        item_issues = []
        coordinate_key = item["_coordinate_key"]
        if coordinate_key in unapproved_duplicate_keys:
            item_issues.append("duplicate_coordinate")
        elif coordinate_key in approved_duplicate_keys:
            item_issues.append("approved_duplicate_coordinate")
        if incompatible_coordinate_kinds:
            item_issues.append("mixed_coordinate_kind")
        if len(authors) > 1:
            item_issues.append("author_conflict")
        if len(work_ids) > 1:
            item_issues.append("work_conflict")
        if item["span_ambiguous"]:
            item_issues.append("ambiguous_coordinate")
        if side_story_requires_review:
            item_issues.append("side_story_requires_two_main_coordinates")
        item["same_coordinate_count"] = coordinate_counts[coordinate_key]
        item["issues"] = item_issues
    items.sort(key=lambda item: (item["_sort_key"], item["name"], item["file_id"]))
    for item in items:
        item.pop("_sort_key")
        item.pop("_coordinate_key")

    blockers = []
    if not any(character.isalpha() for character in core_title):
        blockers.append("non_title_core")
    if incompatible_coordinate_kinds:
        blockers.append("mixed_coordinate_kind")
    if unapproved_duplicate_coordinates:
        blockers.append("duplicate_coordinate")
    # A missing volume is useful review information, but it is not a conflict.
    # Keep the gap visible so a user can spot an incomplete set while allowing
    # the known, non-overlapping volumes to share one work folder.  A later
    # Folderling intake can then fill the gap or append a newer volume.
    if len(authors) > 1:
        blockers.append("author_conflict")
    if len(work_ids) > 1:
        blockers.append("work_conflict")
    if any(int(row["disambig"] or 1) > 1 for row in rows):
        blockers.append("disambig_conflict")
    if any(bool(row["span_ambiguous"]) for row in rows):
        blockers.append("ambiguous_coordinate")
    if outside_house:
        blockers.append("source_outside_house")
    if side_story_requires_review:
        blockers.append("side_story_requires_two_main_coordinates")

    if "non_title_core" in blockers:
        classification = "excluded"
    elif blockers:
        classification = "review_required"
    elif already_grouped:
        classification = "already_grouped"
    else:
        classification = "auto_ready"

    target_name = _safe_folder_name(display_title)
    if len(deep_parents) == 1:
        target_path = deep_parents[0]
        target_name = target_path.name
    else:
        first = target_name[0] if target_name else "#"
        target_path = house_dir / get_chosung(first) / target_name
    revision = _source_revision(rows)
    case_id = hashlib.sha256(
        (core_title + "\0" + "\0".join(sorted(str(row["file_id"]) for row in rows))).encode(
            "utf-8"
        )
    ).hexdigest()
    return {
        "provider": "volume_group",
        "case_id": case_id,
        "source_revision": revision,
        "core_title": core_title,
        "display_title": display_title,
        "classification": classification,
        "file_count": len(items),
        "parent_count": len(parent_paths),
        "parents": sorted(parents),
        "coordinate_kinds": sorted(kinds),
        "main_coordinate_count": len(main_coordinate_keys),
        "has_side_story": has_side_story,
        "coordinate_range": [items[0]["coordinate"], items[-1]["coordinate"]],
        "duplicate_coordinates": duplicate_coordinates,
        "approved_duplicate_coordinates": approved_duplicate_coordinates,
        "unapproved_duplicate_coordinates": unapproved_duplicate_coordinates,
        "parallel_format_coordinates": parallel_format_coordinates,
        "missing_coordinates": missing_coordinates,
        "authors": authors,
        "work_bucket_ids": work_ids,
        "target_folder_name": target_name,
        "target_folder_path": str(target_path),
        "blocked_reasons": blockers,
        "plan_ready": classification in {"auto_ready", "already_grouped"},
        "items": items,
    }


def _series_position(row: Mapping[str, object]) -> tuple | None:
    """Return the coordinate that proves this file is a separate series part.

    Episode compilations are separate parts only when their *start* differs.
    ``1-200.txt`` and ``1-200.epub`` (or ``1-180`` and ``1-200``) are parallel
    editions of one book, not a two-part series.  Volumes and parts use their
    explicit rational coordinate; upper/middle/lower symbols use their symbol.
    """

    kind = row["coordinate_kind"]
    if kind == "episode":
        start = row.get("episode_start")
        if start is None:
            return None
        # 배포본에 따라 서문/프롤로그를 0화 또는 1화로 세지만 둘 다
        # 작품 처음부터 시작하는 동일 좌표다.
        return (kind, max(1, int(start)))
    if kind == "volume":
        number = row.get("volume_num")
        if number is None:
            return None
        return (
            kind,
            int(row["part_num"]) if row.get("part_num") is not None else None,
            int(row.get("part_den") or 1),
            int(number),
            int(row.get("volume_den") or 1),
        )
    if kind == "part":
        number = row.get("part_num")
        return (
            kind,
            int(number),
            int(row.get("part_den") or 1),
        ) if number is not None else None
    if kind == "symbol" and row.get("coordinate_symbol") != "side_story":
        symbol = row.get("coordinate_symbol")
        return (
            kind,
            str(symbol),
            int(row.get("coordinate_sort_key") or 0),
        ) if symbol else None
    return None


def _select_distinct_series_rows(
    rows: Sequence[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    """Select only a cohort proven by two distinct main positions."""

    side_rows = [
        row for row in rows
        if row["coordinate_kind"] == "symbol"
        and row.get("coordinate_symbol") == "side_story"
    ]
    for kind in ("volume", "part", "episode", "symbol"):
        cohort = [
            row for row in rows
            if row["coordinate_kind"] == kind
            and not (
                kind == "symbol"
                and row.get("coordinate_symbol") == "side_story"
            )
        ]
        positions = {
            position
            for row in cohort
            if (position := _series_position(row)) is not None
        }
        if len(positions) >= 2:
            return [*cohort, *side_rows]
    return []


def _select_series_rows(rows: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    """Select a real multi-position cohort, or only a risky side-story case."""

    selected = _select_distinct_series_rows(rows)
    if selected:
        return selected

    # 단권+외전 또는 외전끼리는 기존 계약대로 사람 검토 대상으로 남긴다.
    # 외전이 없는 동일 시작점/동일 권의 병행 판본은 분권 화면에서 제외한다.
    side_rows = [
        row for row in rows
        if row["coordinate_kind"] == "symbol"
        and row.get("coordinate_symbol") == "side_story"
    ]
    if side_rows and len(rows) >= 2:
        return list(rows)
    return []


def _analyze_volume_cases_uncached(state_db: Path, *, house_dir: Path) -> list[dict]:
    groups: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in _load_volume_rows(state_db):
        groups[str(row["core_title"])].append(row)
    cases = []
    for core_title, rows in groups.items():
        # A compilation or parallel TXT/EPUB edition can share a core title
        # without being a separate series part.  At least two distinct main
        # positions are required; episode ranges specifically need different
        # starts.  Risky side-story relationships remain reviewable.
        selected = _select_series_rows(rows)
        if len(selected) >= 2:
            cases.append(_case_from_rows(core_title, selected, Path(house_dir)))
    return cases


def analyze_volume_cases(state_db: Path, *, house_dir: Path) -> list[dict]:
    """Return revision-bound cases while coalescing identical concurrent reads."""

    key = _volume_case_cache_key(state_db, house_dir)
    signature = _state_db_revision_signature(Path(state_db))
    while True:
        with _VOLUME_CASE_CACHE_CONDITION:
            cached = _VOLUME_CASE_CACHE.get(key)
            if (
                cached is not None
                and cached["signature"] == signature
            ):
                return list(cached["cases"])
            if key not in _VOLUME_CASE_INFLIGHT:
                _VOLUME_CASE_INFLIGHT.add(key)
                break
            _VOLUME_CASE_CACHE_CONDITION.wait(timeout=10.0)
        signature = _state_db_revision_signature(Path(state_db))

    try:
        cases = _analyze_volume_cases_uncached(state_db, house_dir=house_dir)
        final_signature = _state_db_revision_signature(Path(state_db))
    except BaseException:
        with _VOLUME_CASE_CACHE_CONDITION:
            _VOLUME_CASE_INFLIGHT.discard(key)
            _VOLUME_CASE_CACHE_CONDITION.notify_all()
        raise

    with _VOLUME_CASE_CACHE_CONDITION:
        _VOLUME_CASE_INFLIGHT.discard(key)
        if final_signature == signature:
            _VOLUME_CASE_CACHE[key] = {
                "signature": final_signature,
                "cases": tuple(cases),
            }
        _VOLUME_CASE_CACHE_CONDITION.notify_all()
    return list(cases)


def list_volume_cases(
    state_db: Path,
    *,
    house_dir: Path,
    search: str = "",
    classification: str = "all",
    cursor: Optional[str] = None,
    limit: int = 50,
    sort: str = "classification",
    direction: str = "asc",
) -> dict:
    if classification not in CLASSIFICATIONS:
        raise ValueError(f"unknown classification: {classification}")
    if sort not in {"classification", "title", "files", "parents"}:
        raise ValueError(f"unknown sort: {sort}")
    if direction not in {"asc", "desc"}:
        raise ValueError(f"unknown direction: {direction}")
    offset = _decode_cursor(cursor)
    limit = max(1, min(int(limit), 200))
    cases = analyze_volume_cases(state_db, house_dir=house_dir)
    needle = unicodedata.normalize("NFC", search or "").strip().casefold()
    if needle:
        cases = [
            case
            for case in cases
            if needle
            in " ".join(
                [case["display_title"], case["core_title"], *case["parents"]]
            ).casefold()
        ]
    summary = Counter(case["classification"] for case in cases)
    if classification != "all":
        cases = [case for case in cases if case["classification"] == classification]
    key_functions = {
        "classification": lambda case: (
            _CLASS_ORDER[case["classification"]], case["display_title"].casefold()
        ),
        "title": lambda case: (case["display_title"].casefold(), case["case_id"]),
        "files": lambda case: (case["file_count"], case["display_title"].casefold()),
        "parents": lambda case: (case["parent_count"], case["display_title"].casefold()),
    }
    cases.sort(key=key_functions[sort], reverse=direction == "desc")
    total = len(cases)
    visible = cases[offset : offset + limit]
    next_offset = offset + limit
    return {
        "provider": "volume_group",
        "items": visible,
        "total": total,
        "summary": {key: summary.get(key, 0) for key in sorted(CLASSIFICATIONS - {"all"})},
        "limit": limit,
        "cursor": cursor,
        "next_cursor": _encode_cursor(next_offset) if next_offset < total else None,
        "search": search,
        "classification": classification,
        "sort": sort,
        "direction": direction,
        "readonly": False,
    }


def get_volume_case(state_db: Path, *, house_dir: Path, case_id: str) -> dict:
    for case in analyze_volume_cases(state_db, house_dir=house_dir):
        if case["case_id"] == case_id:
            return case
    raise KeyError(case_id)


def _preserved_source_items(
    items: Sequence[Mapping[str, object]], house_dir: Path
) -> list[str]:
    """List unselected source-folder content that the manual move will preserve."""

    house_dir = Path(house_dir).resolve()
    selected_paths = {Path(str(item["canonical_path"])).resolve() for item in items}
    selected = {
        decision_store.canonicalize_path(path) for path in selected_paths
    }
    parents = {path.parent for path in selected_paths}
    preserved = set()
    for parent in parents:
        try:
            relative = parent.relative_to(house_dir)
        except ValueError:
            continue
        if len(relative.parts) <= 1:
            continue
        for current, directories, filenames in os.walk(parent, followlinks=False):
            current_path = Path(current)
            for name in directories:
                candidate = current_path / name
                if candidate.is_symlink():
                    preserved.add(str(candidate.relative_to(house_dir)))
            for name in filenames:
                candidate = current_path / name
                if decision_store.canonicalize_path(candidate.resolve()) not in selected:
                    preserved.add(str(candidate.relative_to(house_dir)))
    return sorted(preserved, key=str.casefold)


def _backup_path(state_db: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return Path(state_db).resolve().parent / "backups" / (
        f"before_volume_group_merge_{stamp}_{uuid.uuid4().hex[:8]}.sqlite3"
    )


def preview_volume_group(
    state_db: Path,
    *,
    house_dir: Path,
    case_id: str,
    source_revision: str,
    selected_file_ids: Optional[Sequence[str]] = None,
    target_folder_name: Optional[str] = None,
    allow_duplicate_coordinates: bool = False,
    allow_side_story_without_two_main_coordinates: bool = False,
) -> dict:
    case = get_volume_case(state_db, house_dir=house_dir, case_id=case_id)
    selected = set(
        [item["file_id"] for item in case["items"]]
        if selected_file_ids is None
        else selected_file_ids
    )
    known_ids = {item["file_id"] for item in case["items"]}
    unknown = selected - known_ids
    rows_by_id = {str(row["file_id"]): row for row in _load_volume_rows(state_db)}
    selected_rows = [rows_by_id[file_id] for file_id in selected if file_id in rows_by_id]
    selected_case = (
        _case_from_rows(case["core_title"], selected_rows, Path(house_dir))
        if len(selected_rows) >= 2
        else None
    )
    items = selected_case["items"] if selected_case is not None else [
        item for item in case["items"] if item["file_id"] in selected
    ]
    blockers = list(selected_case["blocked_reasons"] if selected_case else [])
    if allow_duplicate_coordinates:
        blockers = [reason for reason in blockers if reason != "duplicate_coordinate"]
    if allow_side_story_without_two_main_coordinates:
        blockers = [
            reason
            for reason in blockers
            if reason != "side_story_requires_two_main_coordinates"
        ]
    if source_revision != case["source_revision"]:
        blockers.append("source_revision_stale")
    if len(items) < 2:
        blockers.append("at_least_two_files_required")
    if unknown:
        blockers.append("unknown_selected_file")
    default_folder = (
        selected_case["target_folder_name"] if selected_case else case["target_folder_name"]
    )
    folder_name = _safe_folder_name(target_folder_name or default_folder)
    filenames = [unicodedata.normalize("NFC", item["name"]).casefold() for item in items]
    if len(filenames) != len(set(filenames)):
        blockers.append("target_filename_collision")
    if selected_case is not None and target_folder_name is None:
        destination_root = Path(selected_case["target_folder_path"])
        folder_name = destination_root.name
    else:
        destination_root = Path(house_dir).resolve() / get_chosung(folder_name[0]) / folder_name
    if destination_root.is_symlink() or (
        destination_root.exists() and not destination_root.is_dir()
    ):
        blockers.append("target_folder_invalid")
    moved_count = 0
    for item in items:
        source = Path(item["canonical_path"])
        destination = destination_root / item["name"]
        if not source.is_file() or source.is_symlink():
            blockers.append("source_missing_or_not_regular")
            continue
        row = rows_by_id.get(item["file_id"])
        stat = source.stat()
        if row is None or (
            row["size"], row["mtime_ns"], row["dev"], row["ino"], row["ctime_ns"]
        ) != (
            stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino, stat.st_ctime_ns
        ):
            blockers.append("source_identity_stale")
        if source.resolve() == destination.resolve():
            continue
        moved_count += 1
        if destination.exists() or destination.is_symlink():
            blockers.append("target_filename_collision")
    preserved_source_items = (
        _preserved_source_items(items, Path(house_dir)) if items else []
    )
    if moved_count == 0:
        blockers.append("no_files_to_move")
    blockers = list(dict.fromkeys(blockers))
    tree = [f"{folder_name}/{item['name']}" for item in items]
    payload = {
        "case_id": case_id,
        "source_revision": case["source_revision"],
        "selected_file_ids": [item["file_id"] for item in items],
        "target_folder_name": folder_name,
        "allow_duplicate_coordinates": bool(allow_duplicate_coordinates),
        "allow_side_story_without_two_main_coordinates": bool(
            allow_side_story_without_two_main_coordinates
        ),
        "destination_root": str(destination_root),
        "tree": tree,
        "moved_count": moved_count,
        "preserved_source_items": preserved_source_items,
        "blocked_reasons": blockers,
    }
    plan_sha256 = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return {
        **payload,
        "provider": "volume_group",
        "item_count": len(items),
        "plan_sha256": plan_sha256,
        "plan_ready": not blockers and moved_count > 0,
        "apply_available": not blockers and moved_count > 0,
        "readonly_reason": None,
        "items": items,
    }


def apply_volume_plan(
    state_db: Path,
    *,
    house_dir: Path,
    temp_dir: Path,
    case_id: str,
    source_revision: str,
    selected_file_ids: Optional[Sequence[str]],
    target_folder_name: Optional[str],
    allow_duplicate_coordinates: bool = False,
    allow_side_story_without_two_main_coordinates: bool = False,
    confirm_count: int,
    confirm_plan_sha256: str,
    progress=None,
) -> dict:
    """Apply one approved volume plan after staging, backup, and SHA confirmation."""

    state_db = Path(state_db).expanduser().resolve()
    house_dir = Path(house_dir).expanduser().resolve()
    temp_dir = Path(temp_dir).expanduser().resolve()
    with mutation_lock_for_roots(house_dir, temp_dir, "volume-group-merge-1.4.3"):
        plan = preview_volume_group(
            state_db,
            house_dir=house_dir,
            case_id=case_id,
            source_revision=source_revision,
            selected_file_ids=selected_file_ids,
            target_folder_name=target_folder_name,
            allow_duplicate_coordinates=allow_duplicate_coordinates,
            allow_side_story_without_two_main_coordinates=(
                allow_side_story_without_two_main_coordinates
            ),
        )
        if not plan["apply_available"]:
            raise RuntimeError(
                "volume group plan is not runnable: "
                + ",".join(plan["blocked_reasons"])
            )
        if int(confirm_count) != plan["item_count"]:
            raise RuntimeError("volume group confirmation count mismatch")
        if confirm_plan_sha256 != plan["plan_sha256"]:
            raise RuntimeError("volume group plan SHA-256 mismatch")

        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(
                    f"doctor failed before volume merge: {len(issues)} issue(s), "
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
        staging_root = temp_dir / ".volume_group_staging" / run_id / case_id[:16]
        staged = []
        try:
            conn = decision_store.connect_state_db(state_db)
            try:
                staged = stage_volume_sources(
                    conn,
                    file_ids=plan["selected_file_ids"],
                    staging_root=staging_root,
                    run_id=run_id,
                )
                result = merge_staged_volume_group(
                    conn,
                    staged=staged,
                    destination_root=Path(plan["destination_root"]),
                    display_title=plan["target_folder_name"],
                    run_id=run_id,
                    relationship_origin="human_decision",
                    progress=progress,
                )
                decision_store.finish_actual_run(conn, run_id, success=True)
            finally:
                conn.close()
        except BaseException as exc:
            conn = decision_store.connect_state_db(state_db)
            try:
                decision_store.finish_actual_run(
                    conn, run_id, success=False, error=str(exc)
                )
            finally:
                conn.close()
            if staged:
                try:
                    cleanup_staging(staged, staging_root)
                except Exception as cleanup_exc:
                    if hasattr(exc, "add_note"):
                        exc.add_note(
                            f"volume staging cleanup also failed: {cleanup_exc}"
                        )
            raise

        maintenance_warnings = []
        try:
            cleanup_staging(staged, staging_root)
        except Exception as exc:
            maintenance_warnings.append(f"staging cleanup failed: {exc}")
        try:
            removed_folders = remove_empty_source_folders(
                [item["source_path"] for item in staged],
                house_root=house_dir,
                destination_root=Path(plan["destination_root"]),
            )
        except Exception as exc:
            removed_folders = []
            maintenance_warnings.append(f"empty-folder cleanup failed: {exc}")
        response = {
            **result,
            "run_id": run_id,
            "manifest_path": manifest_path,
            "backup_path": str(backup),
            "staged": len(staged),
            "removed_empty_folders": removed_folders,
            "destination_root": plan["destination_root"],
        }
        if maintenance_warnings:
            response["maintenance_warnings"] = maintenance_warnings
        invalidate_volume_case_cache(state_db, house_dir=house_dir)
        return response


def apply_auto_ready_volume_groups(
    state_db: Path,
    *,
    house_dir: Path,
    temp_dir: Path,
    run_id: str,
    progress=None,
) -> dict:
    """Apply every currently safe loose series group inside one Folderling run.

    This intentionally has no affected-title filter.  A run repairs the complete
    historical ``auto_ready`` backlog as well as groups made eligible by files
    ingested earlier in that same run.  Risky side-story-only and single-main
    plus side-story relationships stay in ``review_required`` and are never
    overridden here.
    """

    state_db = Path(state_db).expanduser().resolve()
    house_dir = Path(house_dir).expanduser().resolve()
    temp_dir = Path(temp_dir).expanduser().resolve()
    initial_cases = analyze_volume_cases(state_db, house_dir=house_dir)
    candidates = [
        case for case in initial_cases if case["classification"] == "auto_ready"
    ]
    applied = []
    moved = []
    removed_empty_folders = []
    conn = decision_store.connect_state_db(state_db)
    try:
        decision_store.assert_active_actual_run(conn, run_id)
        for case_index, case in enumerate(candidates, start=1):
            case_id = case["case_id"]
            selected_file_ids = [item["file_id"] for item in case["items"]]
            destination_root = Path(case["target_folder_path"])
            staging_root = (
                temp_dir / ".volume_group_staging" / run_id / case_id[:16]
            )
            staged = []
            try:
                staged = stage_volume_sources(
                    conn,
                    file_ids=selected_file_ids,
                    staging_root=staging_root,
                    run_id=run_id,
                )
                result = merge_staged_volume_group(
                    conn,
                    staged=staged,
                    destination_root=destination_root,
                    display_title=case["display_title"],
                    run_id=run_id,
                    relationship_origin="strong_match",
                    progress=(
                        None
                        if progress is None
                        else lambda item_index, item_total, name: progress(
                            case_index,
                            len(candidates),
                            item_index,
                            item_total,
                            name,
                        )
                    ),
                )
            except BaseException:
                if staged:
                    cleanup_staging(staged, staging_root)
                raise

            cleanup_staging(staged, staging_root)
            removed = remove_empty_source_folders(
                [item["source_path"] for item in staged],
                house_root=house_dir,
                destination_root=destination_root,
            )
            removed_empty_folders.extend(removed)
            moved.extend(result["moved"])
            applied.append(
                {
                    "case_id": case_id,
                    "core_title": case["core_title"],
                    "display_title": case["display_title"],
                    "destination_root": str(destination_root),
                    "file_count": len(selected_file_ids),
                    "moved_count": len(result["moved"]),
                    "work_bucket_id": result["work_bucket_id"],
                }
            )
    finally:
        conn.close()

    invalidate_volume_case_cache(state_db, house_dir=house_dir)
    remaining = analyze_volume_cases(state_db, house_dir=house_dir)
    summary = Counter(case["classification"] for case in remaining)
    return {
        "candidate_count": len(candidates),
        "applied_count": len(applied),
        "moved_count": len(moved),
        "applied": applied,
        "moved": moved,
        "removed_empty_folders": removed_empty_folders,
        "remaining_summary": {
            key: summary.get(key, 0)
            for key in sorted(CLASSIFICATIONS - {"all"})
        },
    }
