"""Read-only 1.2.9 volume-group inventory and review plans."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import unicodedata
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Mapping, Optional, Sequence

import decision_store
from mutation_io import mutation_lock_for_roots
from normalizer import extract_author, get_chosung
from volume_group_mutations import (
    cleanup_staging,
    merge_staged_volume_group,
    remove_empty_source_folders,
    stage_volume_sources,
)


VOLUME_KINDS = frozenset({"volume", "part", "symbol"})
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
        label = f"{value.numerator}권" if value.denominator == 1 else f"{float(value):g}권"
        return (0, value), label, (kind, value.numerator, value.denominator)
    if kind == "part":
        value = Fraction(int(row["part_num"]), int(row["part_den"] or 1))
        label = f"{value.numerator}부" if value.denominator == 1 else f"{float(value):g}부"
        return (1, value), label, (kind, value.numerator, value.denominator)
    symbol = str(row["coordinate_symbol"] or row["coordinate_raw"] or "미상")
    return (2, int(row["coordinate_sort_key"] or 0), symbol), symbol, (kind, symbol)


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
                   fa.analyzed_name, fa.core_title, fa.readable_title,
                   fa.author, fa.disambig, fa.effective_max, fa.unit,
                   fa.complete, fa.updated_at AS analysis_updated_at,
                   v.work_bucket_id,
                   CASE WHEN rep.file_id IS NULL THEN 0 ELSE 1 END AS representative
            FROM files AS f
            JOIN file_analysis AS fa ON fa.file_id = f.file_id
            LEFT JOIN variants AS v ON v.variant_id = f.variant_id
            LEFT JOIN representatives AS rep ON rep.file_id = f.file_id
            WHERE f.active = 1 AND f.source = 'house'
              AND f.coordinate_kind IN ('volume', 'part', 'symbol')
            ORDER BY fa.core_title COLLATE NOCASE, f.canonical_path COLLATE NOCASE
            """
        ).fetchall()
        return [
            {
                **dict(row),
                # Re-evaluate with the current parser so the review screen does
                # not wait for a full scanner pass after an author-rule fix.
                "author": extract_author(str(row["analyzed_name"])),
            }
            for row in rows
        ]
    finally:
        conn.close()


def _is_parallel_side_story_formats(rows: Sequence[Mapping[str, object]]) -> bool:
    """Return whether one completed side story is stored in multiple formats."""

    if len(rows) < 2 or any(
        row["coordinate_kind"] != "symbol"
        or row["coordinate_symbol"] != "side_story"
        or not bool(row["complete"])
        or int(row["effective_max"] or 0) <= 0
        for row in rows
    ):
        return False
    stems = {
        unicodedata.normalize(
            "NFC", Path(str(row["canonical_path"])).stem
        ).casefold()
        for row in rows
    }
    extensions = {
        Path(str(row["canonical_path"])).suffix.lower() for row in rows
    }
    coverage = {
        (int(row["effective_max"]), str(row["unit"] or "")) for row in rows
    }
    authors = {str(row["author"] or "") for row in rows}
    return (
        len(stems) == 1
        and len(extensions) == len(rows)
        and "" not in extensions
        and len(coverage) == 1
        and len(authors) == 1
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
        if _is_parallel_side_story_formats(rows_by_coordinate[key])
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
        values = {
            Fraction(int(row["volume_num"]), int(row["volume_den"] or 1)) for row in rows
        }
        if values and all(value.denominator == 1 for value in values):
            maximum = max(int(value) for value in values)
            if maximum <= 500:
                missing_coordinates = [
                    f"{number}권" for number in range(1, maximum + 1) if Fraction(number) not in values
                ]

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

    if "non_title_core" in blockers:
        classification = "excluded"
    elif blockers:
        classification = "review_required"
    elif already_grouped:
        classification = "already_grouped"
    else:
        classification = "auto_ready"

    target_name = _safe_folder_name(display_title)
    if already_grouped:
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


def analyze_volume_cases(state_db: Path, *, house_dir: Path) -> list[dict]:
    groups: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in _load_volume_rows(state_db):
        groups[str(row["core_title"])].append(row)
    return [
        _case_from_rows(core_title, rows, Path(house_dir))
        for core_title, rows in groups.items()
        if len(rows) >= 2
    ]


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
    if (
        selected_case is not None
        and target_folder_name is None
        and selected_case["classification"] == "already_grouped"
    ):
        destination_root = Path(selected_case["target_folder_path"])
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
    confirm_count: int,
    confirm_plan_sha256: str,
    progress=None,
) -> dict:
    """Apply one approved volume plan after staging, backup, and SHA confirmation."""

    state_db = Path(state_db).expanduser().resolve()
    house_dir = Path(house_dir).expanduser().resolve()
    temp_dir = Path(temp_dir).expanduser().resolve()
    with mutation_lock_for_roots(house_dir, temp_dir, "volume-group-merge-1.2.9"):
        plan = preview_volume_group(
            state_db,
            house_dir=house_dir,
            case_id=case_id,
            source_revision=source_revision,
            selected_file_ids=selected_file_ids,
            target_folder_name=target_folder_name,
            allow_duplicate_coordinates=allow_duplicate_coordinates,
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
        return response
