"""Canonical volume/episode coordinate policy shared by every caller."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Optional, Tuple


_SYMBOL_COORDINATES = {
    "상": ("upper", 10),
    "상권": ("upper", 10),
    "upper": ("upper", 10),
    "중": ("middle", 20),
    "중권": ("middle", 20),
    "middle": ("middle", 20),
    "하": ("lower", 30),
    "하권": ("lower", 30),
    "lower": ("lower", 30),
    "본편": ("main", 100),
    "main": ("main", 100),
    "외전": ("side_story", 200),
    "외": ("side_story", 200),
    "side_story": ("side_story", 200),
    "특별편": ("special", 300),
    "special": ("special", 300),
}


def canonical_rational(value) -> Optional[Tuple[int, int]]:
    if value is None or value == "":
        return None
    try:
        decimal_value = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid numeric coordinate: {value!r}") from exc
    if not decimal_value.is_finite() or decimal_value < 0:
        raise ValueError(f"invalid numeric coordinate: {value!r}")
    fraction = Fraction(decimal_value)
    return fraction.numerator, fraction.denominator



def canonical_symbol(value: str) -> Tuple[str, int]:
    key = str(value).strip().lower()
    try:
        return _SYMBOL_COORDINATES[key]
    except KeyError as exc:
        raise ValueError(f"unknown symbolic coordinate: {value!r}") from exc



def coordinate_sort_token(kind: str, value) -> tuple:
    if kind == "numeric":
        rational = canonical_rational(value)
        if rational is None:
            raise ValueError("numeric coordinate requires a value")
        return 0, Fraction(*rational)
    symbol, sort_key = canonical_symbol(value)
    return 1, sort_key, symbol



def coordinate_fields_from_name(name: str) -> dict:
    """Return the canonical coordinate columns derived from the current parser."""
    from normalizer import analyze_name

    filename = Path(name).name
    stem = Path(filename).stem
    info = analyze_name(filename)
    part = None
    bare_volume = None
    if info["volume_number"] is not None:
        part, bare_volume = info["volume_number"]
    part_rational = canonical_rational(part)

    masked = re.sub(
        r"\d+(?:\.\d+)?\s*(?:권\s*)?[-~]\s*\d+(?:\.\d+)?\s*권",
        " ",
        stem,
    )
    numeric_matches = re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*권", masked)
    volume_rational = canonical_rational(numeric_matches[0]) if len(numeric_matches) == 1 else None
    inferred_bare_volume = False
    if (
        volume_rational is None
        and part_rational is None
        and bare_volume is not None
        and Path(filename).suffix.lower() in {".epub", ".pdf"}
        and info.get("author")
        and not info.get("complete")
        and not info.get("span_ambiguous")
        and 1 <= int(bare_volume) <= 99
    ):
        # 전자책 유통 파일의 ``작품명 38 (작가).epub`` 형식만 권수로
        # 추론한다. TXT, 작가 없는 단독 숫자, 100 이상의 합본 회차는
        # 기존 episode 판정을 유지해 웹소설 완결본 오탐을 피한다.
        bare_match = re.search(
            r"(?:^|\s)(\d{1,2})\s*(?:\([^()]+\)|\[[^\[\]]+\])\s*$",
            stem,
        )
        if bare_match is not None and int(bare_match.group(1)) == int(bare_volume):
            volume_rational = canonical_rational(bare_volume)
            inferred_bare_volume = True
    symbol_match = re.search(r"(상권|중권|하권)\s*$", stem)
    if symbol_match is None:
        symbol_match = re.search(r"(?:^|\s)(상|중|하)\s*$", stem)
    if symbol_match is None:
        symbol_match = re.search(r"(본편|특별편)\s*$", stem)
    symbol = sort_key = None
    coordinate_raw = None
    if symbol_match:
        symbol, sort_key = canonical_symbol(symbol_match.group(1))
        coordinate_raw = symbol_match.group(1)
    elif volume_rational is not None:
        coordinate_raw = (
            str(int(bare_volume)) if inferred_bare_volume else numeric_matches[0]
        )
    elif info["is_side_story"]:
        symbol, sort_key = canonical_symbol("외전")
        numbered_side_story = re.search(
            r"(?:외전|후일담)\s*(\d+)\s*$", stem
        )
        if numbered_side_story is not None:
            side_number = int(numbered_side_story.group(1))
            sort_key += side_number
            coordinate_raw = f"외전 {side_number}"
        else:
            coordinate_raw = "외전"

    if symbol is not None:
        coordinate_kind = "symbol"
    elif volume_rational is not None:
        coordinate_kind = "volume"
    elif part_rational is not None:
        coordinate_kind = "part"
    elif info["start_number"] is not None and info["end_number"] is not None:
        coordinate_kind = "episode"
    else:
        coordinate_kind = None
    has_book_coordinate = symbol is not None or volume_rational is not None
    return {
        "coordinate_kind": coordinate_kind,
        "part_num": part_rational[0] if part_rational else None,
        "part_den": part_rational[1] if part_rational else None,
        "volume_num": volume_rational[0] if volume_rational else None,
        "volume_den": volume_rational[1] if volume_rational else None,
        "coordinate_symbol": symbol,
        "coordinate_sort_key": sort_key,
        "episode_start": None if has_book_coordinate else info["start_number"],
        "episode_end": None if has_book_coordinate else info["end_number"],
        "coordinate_raw": coordinate_raw or stem,
        "span_ambiguous": 1 if info["span_ambiguous"] else 0,
    }



def coordinates_compatible(left, right) -> bool:
    """Use one fail-closed coordinate contract for every mutation path."""
    if left["span_ambiguous"] or right["span_ambiguous"]:
        return False
    if left["coordinate_kind"] is not None and right["coordinate_kind"] is not None:
        if left["coordinate_kind"] != right["coordinate_kind"]:
            return False
    for numerator, denominator in (("part_num", "part_den"), ("volume_num", "volume_den")):
        if left[numerator] is not None and right[numerator] is not None:
            left_value = Fraction(left[numerator], left[denominator] or 1)
            right_value = Fraction(right[numerator], right[denominator] or 1)
            if left_value != right_value:
                return False
    if left["coordinate_symbol"] != right["coordinate_symbol"]:
        if left["coordinate_symbol"] is not None or right["coordinate_symbol"] is not None:
            return False
    if (
        left["coordinate_symbol"] is not None
        and right["coordinate_symbol"] is not None
        and left["coordinate_sort_key"] is not None
        and right["coordinate_sort_key"] is not None
        and left["coordinate_sort_key"] != right["coordinate_sort_key"]
    ):
        return False
    if None not in (
        left["episode_start"], left["episode_end"],
        right["episode_start"], right["episode_end"],
    ):
        if left["episode_end"] < right["episode_start"] or right["episode_end"] < left["episode_start"]:
            return False
    return True
