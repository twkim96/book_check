"""Conservative filename context for weak EPUB review edges only.

These hints never change stored identities or establish content equivalence.
Callers must handle strong content proof before suppressing metadata-only noise.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from normalizer import analyze_name, extract_volume_number


_ROLE = re.compile(
    r"(?<![\w가-힣])(?P<role>외전|外|본편|after|extra)"
    r"(?P<numbers>\s*\d+(?:\s*[,~-]\s*\d+)*)?"
    r"(?=$|[\[\](){}〔〕│|★@.,]|\s*(?:$|[\[\](){}〔〕│|★@.,]))",
    re.IGNORECASE,
)
_GENERIC_PARENTS = {
    "house", "temp", "txt_house", "txt_temp", "epub", "epubs", "books",
    "download", "downloads", "warning", "suspected_duplicates", "trash_bin",
}


def _core(name: str) -> str:
    return str(analyze_name(name).get("core_title") or "").casefold().strip()


def _numeric_parent(path: Path) -> str | None:
    if not re.fullmatch(r"0*\d{1,3}", path.stem):
        return None
    parent = unicodedata.normalize("NFC", path.parent.name).strip()
    parent = re.sub(r"\s+epubs?$", "", parent, flags=re.IGNORECASE).strip()
    if len(parent) < 2 or parent.casefold() in _GENERIC_PARENTS:
        return None
    value = _core(parent + ".epub")
    if not value or value.isdecimal():
        return None
    return value


def _context(path: Path) -> dict:
    stem = unicodedata.normalize("NFKC", path.stem)
    # A punctuation separator before an explicit volume is not a decimal point.
    # Never rewrite 1.5권, ranges, or the general filename parser.
    stem = re.sub(r"(?<=[^\d\W])\.(?=\d+(?:\.\d+)?\s*권)", " ", stem)
    roles = list(_ROLE.finditer(stem))
    kinds = {m.group("role").casefold() for m in roles}
    side_roles = kinds - {"본편"}
    ambiguous = len(roles) > 1 or bool(re.search(r"외전\s*포함|외포", stem))
    numbers = None
    if len(roles) == 1 and roles[0].group("numbers"):
        raw = roles[0].group("numbers").strip()
        # Lists are explicit sets. Ranges require a separate proof rather than
        # treating endpoints as the complete set.
        if re.fullmatch(r"\d+(?:\s*,\s*\d+)*", raw):
            numbers = frozenset(int(n.strip()) for n in raw.split(","))
    clean = _ROLE.sub(" ", stem)
    return {
        "core": _core(clean + ".epub"),
        "volume": extract_volume_number(clean + ".epub"),
        "side": bool(side_roles),
        "role": next(iter(side_roles), None),
        "numbers": numbers,
        "ambiguous": ambiguous,
    }


def metadata_only_epub_context_reason(left_path: str, right_path: str) -> str | None:
    """Explain an explicit distinction; missing/ambiguous context stays manual."""
    left, right = Path(left_path), Path(right_path)
    if left.suffix.casefold() != ".epub" or right.suffix.casefold() != ".epub":
        return None
    left_parent, right_parent = _numeric_parent(left), _numeric_parent(right)
    if left_parent and right_parent and left_parent != right_parent:
        return "numeric_epub_different_parent_work"
    a, b = _context(left), _context(right)
    if not a["core"] or a["core"] != b["core"] or a["ambiguous"] or b["ambiguous"]:
        return None
    if a["volume"] and b["volume"] and a["volume"] != b["volume"]:
        return "explicit_epub_volume_context"
    if a["side"] != b["side"]:
        # after/extra are standalone supplements only beside an explicit volume.
        side = a if a["side"] else b
        if side["role"] in {"after", "extra"} and not side["volume"]:
            return None
        return "epub_main_vs_supplement"
    if a["side"] and b["side"] and a["numbers"] and b["numbers"]:
        if a["numbers"].isdisjoint(b["numbers"]):
            return "epub_disjoint_supplement_numbers"
    return None
