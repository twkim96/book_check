"""Contextual inference for volume files whose ``권`` marker is omitted.

The ordinary filename normalizer deliberately treats a lone trailing number as
ambiguous.  This module promotes that number to a volume coordinate only when
the surrounding inventory proves a series: another distinct bare coordinate,
an explicit volume with the same title, or an already managed work.

The module is pure.  It does not read SQLite or mutate files, which lets the
Scanner, duplicate auditor, and Folderling use exactly the same inference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Mapping, Sequence

from normalizer import (
    analyze_name,
    extract_author,
    extract_catalog_query_title,
    extract_readable_title,
    normalize_filename,
    normalize_nfc,
    strip_trash_suffix,
)


CONTEXT_POLICY_VERSION = "1.4.12"

_TRAILING_BRACKET_RE = re.compile(
    r"\s*(?:\[[^\[\]]+\]|\([^()]+\)|【[^【】]+】|\{[^{}]+\})\s*$"
)
_TRAILING_QUALIFIER_RE = re.compile(
    r"\s*(?:"
    r"소책자(?:\s*한정판)?|한정판|특장판|특별판|"
    r"완결|완|完|終|종"
    r")\s*$",
    re.IGNORECASE,
)
_BARE_DECIMAL_VOLUME_RE = re.compile(
    r"^(?P<title>.*?\S)[\s_-]+(?P<number>0*\d{1,2}\.\d{1,3})$"
)
_BARE_INTEGER_VOLUME_RE = re.compile(
    r"^(?P<title>.*?\S)[\s._-]+(?P<number>0*\d{1,2})$"
)
_CLOSED_AUTHOR_COPY_SUFFIX_RE = re.compile(
    r"^(?P<body>.*?\s0*\d{1,2}(?:\.\d{1,3})?\s*"
    r"(?P<bracket>\([^()]{2,50}\)|\[[^\[\]]{2,50}\]))"
    r"\s*-\s*[2-9]$"
)
_EXPLICIT_RANGE_RE = re.compile(r"\d+\s*[-~]\s*\d+")
_EXPLICIT_UNIT_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:화|회|권|부|장|편)")
_DATE_TAIL_RE = re.compile(r"(?:19|20)\d{2}\s*$")
_DECIMAL_VOLUME_QUALIFIER_RE = re.compile(
    r"(?:소책자(?:\s*한정판)?|한정판|특장판|특별판)", re.IGNORECASE
)


@dataclass(frozen=True)
class BareVolumeCandidate:
    core_title: str
    readable_title: str
    catalog_query_title: str
    volume_number: int | str
    author: str | None
    clean_name: str
    qualifier: str | None

    def apply_to_analysis(self, analysis: Mapping[str, object]) -> dict:
        decimal = Decimal(str(self.volume_number))
        effective_max = int(decimal)
        updated = dict(analysis)
        updated.update({
            "core_title": self.core_title,
            "readable_title": self.readable_title,
            "catalog_query_title": self.catalog_query_title,
            "author": self.author,
            "effective_max": effective_max,
            "unit": "권",
            "start_number": float(decimal),
            "end_number": float(decimal),
            "span_ambiguous": False,
            "volume_number": (None, self.volume_number),
        })
        return updated

    def coordinate_fields(self) -> dict:
        fraction = Fraction(Decimal(str(self.volume_number)))
        return {
            "coordinate_kind": "volume",
            "part_num": None,
            "part_den": None,
            "volume_num": fraction.numerator,
            "volume_den": fraction.denominator,
            "coordinate_symbol": None,
            "coordinate_sort_key": None,
            "episode_start": None,
            "episode_end": None,
            "coordinate_raw": str(self.volume_number),
            "span_ambiguous": 0,
        }


def context_name(name: str) -> str:
    """Return the same transport name that the temp auditor/Folderling sees."""

    return normalize_filename(strip_trash_suffix(normalize_nfc(name)))


def _strip_trailing_metadata(stem: str) -> tuple[str, str | None]:
    value = stem.strip()
    qualifier = None
    while True:
        match = _TRAILING_BRACKET_RE.search(value)
        if match is None:
            break
        token = match.group(0).strip(" \t[]()【】{}")
        if token in {"완결", "완", "完", "終", "종"}:
            qualifier = token
        value = value[:match.start()].rstrip()

    match = _TRAILING_QUALIFIER_RE.search(value)
    if match is not None:
        qualifier = match.group(0).strip()
        value = value[:match.start()].rstrip()
    return value, qualifier


def _strip_closed_author_copy_suffix(stem: str) -> str:
    """Ignore only ``N (명시 작가) -2`` style collision suffixes.

    Folderling can add a transport collision number after a complete author
    bracket.  Treating that final number as the book coordinate turned an
    existing volume 1 into volume 2.  The author extractor must independently
    accept the bracket, so arbitrary title ``-2`` endings remain untouched.
    """

    match = _CLOSED_AUTHOR_COPY_SUFFIX_RE.match(stem.strip())
    if match is None:
        return stem
    bracket = match.group("bracket")[1:-1].strip()
    parsed_author = str(extract_author(match.group("body")) or "").strip()
    if not parsed_author or parsed_author != bracket:
        return stem
    return match.group("body").rstrip()


def _canonical_bare_number(raw: str) -> int | str | None:
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value < 1 or value >= 100:
        return None
    if value == value.to_integral_value():
        return int(value)
    normalized = format(value.normalize(), "f")
    return normalized if len(normalized.partition(".")[2]) <= 3 else None


def _match_bare_volume(stem: str):
    # Decimal must win before the legacy dot-separator integer form, otherwise
    # ``11.5`` would be read as title ``... 11`` plus volume 5.
    return (
        _BARE_DECIMAL_VOLUME_RE.match(stem)
        or _BARE_INTEGER_VOLUME_RE.match(stem)
    )


def has_bare_volume_shape(name: str) -> bool:
    """Cheap syntax prefilter for an already transport-normalized name.

    This intentionally does not decide that the number is a volume.  It only
    avoids running the full normalizer over thousands of unrelated house
    names when a temp candidate needs house-side context.
    """

    stem = Path(normalize_nfc(name)).stem
    stem = _strip_closed_author_copy_suffix(stem)
    structural_stem, _qualifier = _strip_trailing_metadata(stem)
    if _EXPLICIT_RANGE_RE.search(structural_stem) or _EXPLICIT_UNIT_RE.search(
        structural_stem
    ):
        return False
    match = _match_bare_volume(structural_stem)
    if match is None:
        return False
    if "." in match.group("number") and not _DECIMAL_VOLUME_QUALIFIER_RE.search(stem):
        return False
    number = _canonical_bare_number(match.group("number"))
    if number is None:
        return False
    title = match.group("title").strip(" ._-")
    if not title or _DATE_TAIL_RE.search(title):
        return False
    compact_title = re.sub(
        r"[^0-9A-Za-z가-힣\u3400-\u9fff\uf900-\ufaff]+", "", title
    )
    return bool(
        compact_title
        and any(character.isalpha() for character in compact_title)
    )


def parse_bare_volume_candidate(
    name: str,
    *,
    analysis: Mapping[str, object] | None = None,
    title_override: bool = False,
) -> BareVolumeCandidate | None:
    """Parse one ambiguous bare-number filename without deciding it is a volume."""

    if title_override:
        return None
    clean_name = context_name(name)
    if not has_bare_volume_shape(clean_name):
        return None
    info = dict(analysis or analyze_name(clean_name))
    if (
        info.get("volume_number") is not None
        or info.get("is_side_story")
        or info.get("span_ambiguous")
        or int(info.get("disambig") or 1) > 1
    ):
        return None

    stem = Path(clean_name).stem
    stem = _strip_closed_author_copy_suffix(stem)
    structural_stem, qualifier = _strip_trailing_metadata(stem)
    match = _match_bare_volume(structural_stem)
    assert match is not None
    number = _canonical_bare_number(match.group("number"))
    if number is None:
        return None
    title = match.group("title").strip(" ._-")

    extension = Path(clean_name).suffix.lower()
    synthetic = f"{title} {number}권{extension}"
    synthetic_info = analyze_name(synthetic)
    core_title = str(synthetic_info.get("core_title") or "").strip()
    if not core_title:
        return None
    return BareVolumeCandidate(
        core_title=core_title,
        readable_title=extract_readable_title(synthetic).strip(),
        catalog_query_title=extract_catalog_query_title(synthetic).strip(),
        volume_number=number,
        author=(str(info.get("author")).strip() if info.get("author") else None),
        clean_name=clean_name,
        qualifier=qualifier,
    )


def infer_bare_volume_overrides(
    records: Sequence[Mapping[str, object]],
) -> dict[object, BareVolumeCandidate]:
    """Return context-proven candidate overrides keyed by ``record['key']``.

    Required record fields are ``key``, ``name``, ``analysis`` and
    ``coordinates``.  ``assignment_state``, ``current_core_title`` and
    ``title_override`` are optional evidence.
    """

    candidates_by_core: dict[str, list[tuple[Mapping[str, object], BareVolumeCandidate]]] = {}
    explicit_authors: dict[str, set[str]] = {}
    explicit_volume_cores: set[str] = set()
    managed_cores: set[str] = set()

    for record in records:
        analysis = record["analysis"]
        coordinates = record["coordinates"]
        core_title = str(analysis.get("core_title") or "").strip()
        author = str(analysis.get("author") or "").strip()
        if core_title and author:
            explicit_authors.setdefault(core_title, set()).add(author)
        if core_title and coordinates.get("coordinate_kind") in {"volume", "part"}:
            explicit_volume_cores.add(core_title)
        current_core = str(record.get("current_core_title") or "").strip()
        if record.get("assignment_state") == "managed" and current_core:
            managed_cores.add(current_core)

        candidate = None
        if record.get("candidate_enabled", True):
            candidate = parse_bare_volume_candidate(
                str(record["name"]),
                analysis=analysis,
                title_override=bool(record.get("title_override")),
            )
        if candidate is not None:
            candidates_by_core.setdefault(candidate.core_title, []).append(
                (record, candidate)
            )

    overrides: dict[object, BareVolumeCandidate] = {}
    for core_title, grouped in candidates_by_core.items():
        authors = set(explicit_authors.get(core_title, ()))
        authors.update(
            candidate.author for _record, candidate in grouped if candidate.author
        )
        if len(authors) > 1:
            continue
        distinct_numbers = {candidate.volume_number for _record, candidate in grouped}
        proven = (
            len(distinct_numbers) >= 2
            or core_title in explicit_volume_cores
            or core_title in managed_cores
        )
        if not proven:
            continue
        for record, candidate in grouped:
            overrides[record["key"]] = candidate
    return overrides
