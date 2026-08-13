"""Episode-coordinate relations used only by strong body deduplication.

The normalizer deliberately keeps main episodes, side stories, and printed
volumes as different semantic coordinates.  Deduplication may still compare
those editions when their exact core title agrees, but it must never invent a
global conversion such as ``150 episodes == 9 volumes``.  This module only
decides whether a pair is eligible for a current-body proof and whether one
side has an unambiguously wider declared coverage.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from normalizer import analyze_name, extract_episode_spans, select_main_episode_span


DEDUP_SPECIAL_COORDINATE_MODES = frozenset({
    "compound_part_contained",
    "aggregate_suffix_contained",
    "season_total_contained",
})

_COMPOUND_PART_RE = re.compile(
    r"(?<!\d)1\s*[-~～]\s*(?P<first>\d{2,5})\s*(?:화\s*)?"
    r"1\s*부(?:\s*(?:완|完))?.*?2\s*부\s*"
    r"1\s*[-~～]\s*(?P<second>\d{1,5})(?:\s*화)?",
    re.IGNORECASE,
)
_AGGREGATE_SUFFIX_RE = re.compile(
    r"^(?P<title>.+?)\s+(?P<base>\d{2,5})\s*[+＋]"
    r"\s*(?P<extra>\d{1,4})(?=\s|完|완|본|외|후|@|\.|$)",
    re.IGNORECASE,
)
_SEASON_TOTAL_RE = re.compile(
    r"^(?P<title>.+?)\s+(?P<end>\d{3,5})\s*시즌\s*\d+"
    r"(?=\s*(?:완결|완|完|終|\.|$))",
    re.IGNORECASE,
)
_KNOWN_DISTRIBUTION_PREFIX_RE = re.compile(
    r"^\s*마늘소금\s*[-_:：]+\s*", re.IGNORECASE
)
_ALTERNATE_EDITION_RE = re.compile(
    r"개정|수정판|누락\s*수정|19\s*(?:n|금|禁)|성인판|무삭제|번역판|특전",
    re.IGNORECASE,
)
_KNOWN_LOOSE_CORE_ALIASES = frozenset({
    frozenset(("테라리움어드벤쳐", "테라리움어드벤처")),
    frozenset(("내가키운s급", "내가키운s급들")),
})


@dataclass(frozen=True)
class EpisodeProfile:
    unit: str
    primary_start: int
    primary_end: int
    primary_role: str
    side_ranges: tuple[tuple[int, int], ...]
    side_count: int
    total_count: int

    def as_evidence(self) -> dict:
        evidence = asdict(self)
        evidence["side_ranges"] = [list(item) for item in self.side_ranges]
        return evidence


@dataclass(frozen=True)
class DedupCoordinateRelation:
    mode: str
    preferred_side: str | None
    left: EpisodeProfile
    right: EpisodeProfile

    def as_evidence(self) -> dict:
        return {
            "mode": self.mode,
            "preferred_side": self.preferred_side,
            "left_profile": self.left.as_evidence(),
            "right_profile": self.right.as_evidence(),
        }


def _merged_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1] + 1:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return tuple((start, end) for start, end in merged)


def _dedup_primary_span(spans):
    primary = select_main_episode_span(spans)
    if primary is not None:
        return primary

    # The shared normalizer deliberately marks an otherwise explicit range as
    # metadata when an author/status bracket immediately follows it.  For
    # dedup coordinates, one unambiguous 1-N range is still the book's declared
    # coverage (for example ``1-150화 [작가]``).  Keep this narrow: dates,
    # edition numbers, multiple metadata ranges, and short 1-N labels remain
    # ineligible.
    fallbacks = [
        span for span in spans
        if span.role == "metadata"
        and span.explicit_range
        and span.start == 1
        and span.end >= 10
        and span.unit in {"화", "권"}
    ]
    return fallbacks[0] if len(fallbacks) == 1 else None


def episode_profile(name: str, *, span_ambiguous: bool = False) -> EpisodeProfile | None:
    """Return one explicit primary span plus separately counted side stories."""
    if span_ambiguous:
        return None
    spans = extract_episode_spans(name)
    primary = _dedup_primary_span(spans)
    if primary is None or primary.start is None or primary.end is None:
        return None
    if primary.end < primary.start:
        return None
    side_ranges = _merged_ranges(
        (span.start, span.end)
        for span in spans
        if span.role == "side" and span.end >= span.start
    )
    side_count = sum(end - start + 1 for start, end in side_ranges)
    primary_count = primary.end - primary.start + 1
    return EpisodeProfile(
        unit=primary.unit,
        primary_start=primary.start,
        primary_end=primary.end,
        primary_role="main" if primary.role == "metadata" else primary.role,
        side_ranges=side_ranges,
        side_count=side_count,
        total_count=primary_count + side_count,
    )


def _aggregate_profile(end: int) -> EpisodeProfile:
    return EpisodeProfile(
        unit="화",
        primary_start=1,
        primary_end=end,
        primary_role="main",
        side_ranges=(),
        side_count=0,
        total_count=end,
    )


def _compound_part_profile(name: str) -> tuple[int, EpisodeProfile] | None:
    match = _COMPOUND_PART_RE.search(name)
    if match is None:
        return None
    first = int(match.group("first"))
    second = int(match.group("second"))
    if first < 10 or second < 1:
        return None
    return first, _aggregate_profile(first + second)


def _aggregate_suffix_profile(name: str):
    match = _AGGREGATE_SUFFIX_RE.search(name)
    if match is None:
        return None
    base = int(match.group("base"))
    extra = int(match.group("extra"))
    if base < 10 or extra < 1 or extra > base:
        return None
    title_core = analyze_name(match.group("title"))["core_title"]
    if not title_core:
        return None
    return title_core, base, _aggregate_profile(base + extra)


def _season_total_profile(name: str):
    match = _SEASON_TOTAL_RE.search(name)
    if match is None:
        return None
    end = int(match.group("end"))
    if end < 100:
        return None
    title_core = analyze_name(match.group("title"))["core_title"]
    if not title_core:
        return None
    return title_core, _aggregate_profile(end)


def _loose_upgrade_core(name: str) -> str:
    cleaned = _KNOWN_DISTRIBUTION_PREFIX_RE.sub("", name, count=1)
    season = _season_total_profile(cleaned)
    if season is not None:
        return season[0].casefold()
    return str(analyze_name(cleaned)["core_title"] or "").casefold()


def _loose_core_compatible(left: str, right: str) -> bool:
    if left == right:
        return True
    return frozenset((left, right)) in _KNOWN_LOOSE_CORE_ALIASES


def _special_coordinate_profiles(left_name: str, right_name: str):
    left_compound = _compound_part_profile(left_name)
    right_compound = _compound_part_profile(right_name)
    left_core = analyze_name(left_name)["core_title"]
    right_core = analyze_name(right_name)["core_title"]
    if (
        left_compound is not None
        and right_compound is not None
        and left_compound[0] == right_compound[0]
        and left_core
        and left_core == right_core
    ):
        return (
            "compound_part_contained",
            left_compound[1],
            right_compound[1],
        )

    left_plus = _aggregate_suffix_profile(left_name)
    right_plus = _aggregate_suffix_profile(right_name)
    if left_plus is not None and right_plus is not None:
        if left_plus[:2] == right_plus[:2]:
            return "aggregate_suffix_contained", left_plus[2], right_plus[2]
        return None

    if left_plus is not None:
        right = episode_profile(right_name, span_ambiguous=False)
        right_core = analyze_name(right_name)["core_title"]
        if (
            right is not None
            and right.unit == "화"
            and right.primary_start == 1
            and right.primary_end == left_plus[1]
            and right_core == left_plus[0]
        ):
            return "aggregate_suffix_contained", left_plus[2], right
    if right_plus is not None:
        left = episode_profile(left_name, span_ambiguous=False)
        left_core = analyze_name(left_name)["core_title"]
        if (
            left is not None
            and left.unit == "화"
            and left.primary_start == 1
            and left.primary_end == right_plus[1]
            and left_core == right_plus[0]
        ):
            return "aggregate_suffix_contained", left, right_plus[2]

    left_season = _season_total_profile(left_name)
    right_season = _season_total_profile(right_name)
    if left_season is not None and right_season is not None:
        if _loose_core_compatible(left_season[0], right_season[0]):
            return "season_total_contained", left_season[1], right_season[1]
        return None
    if left_season is not None:
        right = episode_profile(right_name, span_ambiguous=False)
        if right is not None and _loose_core_compatible(
            left_season[0], _loose_upgrade_core(right_name)
        ):
            return "season_total_contained", left_season[1], right
    if right_season is not None:
        left = episode_profile(left_name, span_ambiguous=False)
        if left is not None and _loose_core_compatible(
            _loose_upgrade_core(left_name), right_season[0]
        ):
            return "season_total_contained", left, right_season[1]
    return None


def classify_loose_title_upgrade_relation(
    left_name: str,
    right_name: str,
    *,
    left_span_ambiguous: bool = False,
    right_span_ambiguous: bool = False,
) -> DedupCoordinateRelation | None:
    """Return a strict wider-coverage relation for one-edit/source-prefix titles.

    This is deliberately narrower than general near-title matching.  Both
    editions must start at episode 1, the keep side must be complete and have
    strictly wider declared coverage, the short side cannot be a side story,
    and explicit adult/revision/translation markers veto the relation.  A
    caller still needs a current ordered-body proof before any mutation.
    """
    if _ALTERNATE_EDITION_RE.search(left_name) or _ALTERNATE_EDITION_RE.search(
        right_name
    ):
        return None
    relation = classify_dedup_coordinate_relation(
        left_name,
        right_name,
        left_span_ambiguous=left_span_ambiguous,
        right_span_ambiguous=right_span_ambiguous,
    )
    if relation is None or relation.preferred_side not in {"left", "right"}:
        return None
    short_name, long_name = (
        (right_name, left_name)
        if relation.preferred_side == "left"
        else (left_name, right_name)
    )
    short_info = analyze_name(short_name)
    long_info = analyze_name(long_name)
    if (
        short_info["is_side_story"]
        or not long_info["complete"]
        or relation.left.primary_start != 1
        or relation.right.primary_start != 1
        or not _loose_core_compatible(
            _loose_upgrade_core(short_name), _loose_upgrade_core(long_name)
        )
    ):
        return None
    return relation


def classify_dedup_coordinate_relation(
    left_name: str,
    right_name: str,
    *,
    left_span_ambiguous: bool = False,
    right_span_ambiguous: bool = False,
) -> DedupCoordinateRelation | None:
    """Classify coordinates that may be finalized by a 95% ordered-body proof.

    ``preferred_side`` is only populated when declared coverage is directed.
    Equal totals, side-story redistribution, and episode/volume editions leave
    keep selection to the existing ``choose_keep`` policy.
    """
    special = _special_coordinate_profiles(left_name, right_name)
    if special is not None:
        mode, left, right = special
        if left.total_count == right.total_count:
            return DedupCoordinateRelation(mode, None, left, right)
        preferred = "left" if left.total_count > right.total_count else "right"
        return DedupCoordinateRelation(mode, preferred, left, right)

    left = episode_profile(left_name, span_ambiguous=left_span_ambiguous)
    right = episode_profile(right_name, span_ambiguous=right_span_ambiguous)
    if left is None or right is None:
        return None

    if left.unit != right.unit:
        if {left.unit, right.unit} != {"화", "권"}:
            return None
        if left.primary_start != 1 or right.primary_start != 1:
            return None
        return DedupCoordinateRelation("cross_unit_edition", None, left, right)

    if left.primary_start != right.primary_start:
        return None

    same_primary = (
        left.primary_end == right.primary_end
        and left.primary_role == right.primary_role
    )
    same_side = left.side_ranges == right.side_ranges
    if same_primary and same_side:
        return DedupCoordinateRelation("same_coordinates", None, left, right)

    if left.total_count == right.total_count:
        return DedupCoordinateRelation(
            "side_aggregate_equivalent", None, left, right
        )

    if same_primary:
        preferred = "left" if left.total_count > right.total_count else "right"
        return DedupCoordinateRelation(
            "contained_coordinates", preferred, left, right
        )

    primary_preferred = (
        "left" if left.primary_end > right.primary_end else "right"
    )
    total_preferred = "left" if left.total_count > right.total_count else "right"
    if primary_preferred != total_preferred:
        return None
    return DedupCoordinateRelation(
        "contained_coordinates", primary_preferred, left, right
    )
