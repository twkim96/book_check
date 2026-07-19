"""Pure filename cleanup proposals for the 1.2.7 title audit.

This module never touches the filesystem or SQLite.  It only converts one
display filename into a virtual candidate filename and records the exact closed
rules that fired.  The normalizer does not consume these rules until the
read-only FOUND-ZERO-DIFF audit has passed.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Tuple


SUPPORTED_EXTENSIONS = frozenset({".txt", ".epub", ".pdf"})


@dataclass(frozen=True)
class CleanupProposal:
    original_name: str
    candidate_name: str
    rule_ids: Tuple[str, ...]

    @property
    def matched(self) -> bool:
        return bool(self.rule_ids)


Rule = Tuple[str, Callable[[str], str]]


def _split_supported_extension(value: str) -> tuple[str, str]:
    base, ext = os.path.splitext(value)
    return (base, ext) if ext.lower() in SUPPORTED_EXTENSIONS else (value, "")


def _with_base(value: str, transform: Callable[[str], str]) -> str:
    base, ext = _split_supported_extension(value)
    return transform(base) + ext


_DOUBLE_ENCODED_SEPARATOR_RE = re.compile(r"%25(?:28|29|40|5b|5d)", re.IGNORECASE)
_INTERNAL_DUP_RE = re.compile(r"_dup_\d+$", re.IGNORECASE)
_PLAIN_ADULT_PREFIX_RE = re.compile(
    r"^\s*(?:19\s*[\)）]|[\(（]\s*19\s*[\)）]|[\[［]\s*19\s*[\]］])\s*"
)
_LEADING_GENRE_RE = re.compile(r"^\s*(?:무협)\s*[\)）]\s*")
_COPYRIGHT_AUTHOR_BEFORE_RANGE_RE = re.compile(
    r"\s*(?<![\[［])[ⓒ©]\s*(?P<author>.{1,50}?)(?=\s+0*\d+\s*[-~]\s*\d+)",
    re.DOTALL,
)
_CIRCLED_SOURCE_BEFORE_RANGE_RE = re.compile(
    r"\s*[ⓩⓖ].{0,50}?(?=\s+0*\d+\s*[-~]\s*\d+)", re.DOTALL
)
_EP_RANGE_RE = re.compile(
    r"(?<![A-Za-z0-9])ep\s*(?=0*\d+\s*[-~]\s*\d+)", re.IGNORECASE
)
_TOTAL_EPISODE_RE = re.compile(r"\s+총\s*(\d+)\s*화")
_SPACED_RANGE_AUTHOR_RE = re.compile(
    r"\s+(\d+)\s+(\d+)\s*\[\s*(?:완결|완|完)\s*\]\s*-\s*(.+?)\s*$"
)
_SPACED_RANGE_AT_TAG_RE = re.compile(r"\s+(\d+)\s+(\d+)\s+@.+$")
_RS_SUFFIX_RE = re.compile(r"\s+RS\s*[\(（][^()（）]{1,50}[\)）]\s*$")
_FORMAT_SUFFIX_RE = re.compile(
    r"\s+(?:UTF\s*8\s+BOM|noPic\s+ver)\s*$"
)
_DOLLAR_SOURCE_SUFFIX_RE = re.compile(r"\s*\$공금\$직\s*$")
_ATTACHED_COMPLETE_ICON_RE = re.compile(
    r"([^0-9외\s])(?:완|完)[⓳⑲](?=\s*(?:\[[^\]]+\])?\s*$)"
)
_COMPLETE_BEFORE_RANGE_RE = re.compile(
    r"(?P<head>[A-Za-z가-힣\u3400-\u9fff\uf900-\ufaff])"
    r"(?P<marker>완|完)(?P<span>\d+\s*[-~]\s*\d+)"
)
_BARE_VOLUME_AUTHOR_RE = re.compile(
    r"^(.*?\S)\s+(\d{1,3})\s+(\([^()]{2,40}\))$"
)


def _decode_one_extra_separator_layer(value: str) -> str:
    if not _DOUBLE_ENCODED_SEPARATOR_RE.search(value):
        return value

    def replace(match: re.Match) -> str:
        return "%" + match.group(0)[3:]

    return _DOUBLE_ENCODED_SEPARATOR_RE.sub(replace, value)


def _normalize_fullwidth_at(value: str) -> str:
    return value.replace("＠", "@")


def _strip_internal_dup(value: str) -> str:
    return _with_base(value, lambda base: _INTERNAL_DUP_RE.sub("", base))


def _strip_nwn_prefix(value: str) -> str:
    return re.sub(r"^NWN\s+", "", value)


def _strip_plain_adult_prefix(value: str) -> str:
    return _PLAIN_ADULT_PREFIX_RE.sub("", value, count=1)


def _strip_leading_genre(value: str) -> str:
    return _LEADING_GENRE_RE.sub("", value, count=1)


def _normalize_copyright_author_before_range(value: str) -> str:
    def transform(base: str) -> str:
        return _COPYRIGHT_AUTHOR_BEFORE_RANGE_RE.sub(
            lambda match: f" [ⓒ{match.group('author').strip()}]",
            base,
            count=1,
        )

    return _with_base(value, transform)


def _strip_circled_source_before_range(value: str) -> str:
    return _with_base(
        value,
        lambda base: _CIRCLED_SOURCE_BEFORE_RANGE_RE.sub(" ", base, count=1),
    )


def _strip_dollar_source_suffix(value: str) -> str:
    return _with_base(
        value, lambda base: _DOLLAR_SOURCE_SUFFIX_RE.sub("", base, count=1)
    )


def _strip_rs_suffix(value: str) -> str:
    _, ext = _split_supported_extension(value)
    if ext.lower() != ".epub":
        return value
    return _with_base(value, lambda base: _RS_SUFFIX_RE.sub("", base, count=1))


def _strip_format_suffix(value: str) -> str:
    return _with_base(value, lambda base: _FORMAT_SUFFIX_RE.sub("", base, count=1))


def _normalize_spaced_range_author(value: str) -> str:
    def transform(base: str) -> str:
        return _SPACED_RANGE_AUTHOR_RE.sub(
            lambda match: (
                f" {match.group(1)}-{match.group(2)} 완 "
                f"[{match.group(3).strip()}]"
            ),
            base,
            count=1,
        )

    return _with_base(value, transform)


def _normalize_spaced_range_at_tag(value: str) -> str:
    def transform(base: str) -> str:
        return _SPACED_RANGE_AT_TAG_RE.sub(
            lambda match: f" {match.group(1)}-{match.group(2)}", base, count=1
        )

    return _with_base(value, transform)


def _normalize_bare_volume_author(value: str) -> str:
    base, ext = _split_supported_extension(value)
    if ext.lower() not in {".epub", ".pdf"}:
        return value
    match = _BARE_VOLUME_AUTHOR_RE.match(base)
    if match is None:
        return value
    return f"{match.group(1)} {match.group(2)}권 {match.group(3)}{ext}"


def _strip_ep_range_prefix(value: str) -> str:
    return _with_base(value, lambda base: _EP_RANGE_RE.sub("", base, count=1))


def _strip_total_episode_prefix(value: str) -> str:
    return _with_base(
        value,
        lambda base: _TOTAL_EPISODE_RE.sub(
            lambda match: f" 1-{match.group(1)}화", base, count=1
        ),
    )


def _normalize_attached_complete_icon(value: str) -> str:
    def transform(base: str) -> str:
        return _ATTACHED_COMPLETE_ICON_RE.sub(
            lambda match: f"{match.group(1)} 完", base, count=1
        )

    return _with_base(value, transform)


def _normalize_complete_before_range(value: str) -> str:
    def transform(base: str) -> str:
        return _COMPLETE_BEFORE_RANGE_RE.sub(
            lambda match: (
                f"{match.group('head')} {match.group('span')} {match.group('marker')}"
            ),
            base,
            count=1,
        )

    return _with_base(value, transform)


RULES: Tuple[Rule, ...] = (
    ("double_percent_separator", _decode_one_extra_separator_layer),
    ("fullwidth_at_separator", _normalize_fullwidth_at),
    ("internal_dup_suffix", _strip_internal_dup),
    ("nwn_source_prefix", _strip_nwn_prefix),
    ("plain_adult_prefix", _strip_plain_adult_prefix),
    ("leading_genre_label", _strip_leading_genre),
    ("copyright_author_tag", _normalize_copyright_author_before_range),
    ("circled_source_before_range", _strip_circled_source_before_range),
    ("dollar_source_suffix", _strip_dollar_source_suffix),
    ("rs_uploader_suffix", _strip_rs_suffix),
    ("format_noise_suffix", _strip_format_suffix),
    ("spaced_range_completed_author", _normalize_spaced_range_author),
    ("spaced_range_at_tag", _normalize_spaced_range_at_tag),
    ("bare_volume_before_author", _normalize_bare_volume_author),
    ("ep_range_prefix", _strip_ep_range_prefix),
    ("total_episode_prefix", _strip_total_episode_prefix),
    ("attached_complete_adult_icon", _normalize_attached_complete_icon),
    ("complete_before_explicit_range", _normalize_complete_before_range),
)


def apply_title_cleanup_rules(
    filename: str,
    *,
    enabled_rule_ids: Optional[Iterable[str]] = None,
) -> CleanupProposal:
    original = unicodedata.normalize("NFC", filename or "")
    candidate = original
    applied = []
    enabled = None if enabled_rule_ids is None else frozenset(enabled_rule_ids)
    for rule_id, transform in RULES:
        if enabled is not None and rule_id not in enabled:
            continue
        updated = transform(candidate)
        if updated != candidate:
            candidate = updated
            applied.append(rule_id)
    candidate = re.sub(r"\s+", " ", candidate).strip()
    return CleanupProposal(original, candidate, tuple(applied))


def rule_ids() -> Tuple[str, ...]:
    return tuple(rule_id for rule_id, _ in RULES)
