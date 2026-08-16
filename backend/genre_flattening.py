"""Deterministic title-level genre projection over raw platform metadata.

Raw platform genres and tags remain provenance.  This module derives one
reader-facing canonical genre using fixed source precedence when platforms
disagree: Kakao > Series > NovelPia.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple


CANONICAL_GENRES: Tuple[str, ...] = (
    "판타지",
    "현대판타지",
    "무협",
    "로맨스판타지",
    "로맨스",
    "현대",
    "라이트노벨",
    "SF",
    "대체역사",
    "스포츠",
    "미스터리",
    "공포",
    "BL",
    "패러디",
    "기타",
)

_GENRE_ORDER = {genre: index for index, genre in enumerate(CANONICAL_GENRES)}
_PLATFORM_PRIORITY = ("kakao", "series", "novelpia")
_GENRE_ALIASES = {
    "현판": "현대판타지",
    "로판": "로맨스판타지",
}

# NovelPia detail metadata is a writer-tag list.  These values can appear first
# even though they describe audience/trope/style rather than a top-level genre.
_NOVELPIA_MODIFIER_FIRST_TAGS = frozenset({
    "고수위",
    "TS",
    "환생",
    "중세",
    "하렘",
    "퓨전",
})

@dataclass(frozen=True)
class GenreResolution:
    canonical_genre: Optional[str]
    candidates: Tuple[str, ...]
    review_required: bool

    @property
    def state(self) -> str:
        if self.review_required:
            return "review"
        if self.canonical_genre is None:
            return "missing"
        return "resolved"


def _canonical_label(value: object) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    text = _GENRE_ALIASES.get(text, text)
    return text if text in _GENRE_ORDER else None


def _novelpia_candidate(
    genre: object,
    tags: Optional[Sequence[object]],
) -> Optional[str]:
    raw = str(genre or "").strip()
    mapped = _canonical_label(raw)
    if mapped is not None:
        return mapped
    if raw not in _NOVELPIA_MODIFIER_FIRST_TAGS:
        return None

    # Preserve NovelPia's source order: when a modifier occupies the first slot,
    # use the first subsequent tag that is itself a recognized content genre.
    for tag in tags or ():
        candidate = _canonical_label(tag)
        if candidate is not None:
            return candidate
    return None


def platform_genre_candidate(
    platform: str,
    genre: object,
    tags: Optional[Sequence[object]] = None,
) -> Optional[str]:
    if platform == "novelpia":
        return _novelpia_candidate(genre, tags)
    return _canonical_label(genre)


def resolve_canonical_genre(
    sources: Iterable[Tuple[str, object, Optional[Sequence[object]]]],
) -> GenreResolution:
    """Resolve one title genre using fixed platform precedence.

    Each source is ``(platform, raw_genre, raw_tags)``. Unknown labels are not
    coerced to ``기타``; absence remains absence. Per-platform normalization is
    applied first, then the first available candidate wins in this order:
    Kakao > Series > NovelPia.
    """
    by_platform = {}
    for platform, genre, tags in sources:
        candidate = platform_genre_candidate(platform, genre, tags)
        if candidate is not None:
            by_platform[platform] = candidate

    if not by_platform:
        return GenreResolution(None, (), False)

    candidates = tuple(
        dict.fromkeys(
            by_platform[platform]
            for platform in _PLATFORM_PRIORITY
            if platform in by_platform
        )
    )
    winner = next(
        by_platform[platform]
        for platform in _PLATFORM_PRIORITY
        if platform in by_platform
    )
    return GenreResolution(winner, candidates, False)
