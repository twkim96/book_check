"""Episode-coordinate relations used only by strong body deduplication.

The normalizer deliberately keeps main episodes, side stories, and printed
volumes as different semantic coordinates.  Deduplication may still compare
those editions when their exact core title agrees, but it must never invent a
global conversion such as ``150 episodes == 9 volumes``.  This module only
decides whether a pair is eligible for a current-body proof and whether one
side has an unambiguously wider declared coverage.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from normalizer import extract_episode_spans, select_main_episode_span


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
