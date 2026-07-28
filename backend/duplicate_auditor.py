#!/usr/bin/env python3
"""Read-only duplicate candidate auditor for txt_house/txt_temp.

This module deliberately has no bridge to library mutation records. It reads input files, may persist
fingerprint/pair cache rows in --state-db, and writes reports only below <temp>/dedup_logs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import unicodedata
import zipfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from normalizer import (
    NORMALIZER_VERSION,
    analyze_name,
    has_pass_marker,
    is_supported_file,
    normalize_filename,
    normalize_nfc,
    should_exclude_dir,
    should_exclude_file,
    strip_trash_suffix,
)
from text_preview import (
    BodyBudgetExceeded,
    DEFAULT_ANCHOR_CHARS,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_READ_BYTES,
    MIN_STRONG_TEXT_CHARS,
    ReadBudget,
    TextAnalysis,
    TextAnalysisCache,
    batch_scan_normalized,
    extract_position_anchors,
)
from project_paths import FILE_INDEX, HOUSE_DIR, TEMP_DIR
from mutation_io import inspect_epub_content, inspect_regular_file
from review_noise import (
    different_core_titles,
    distinct_terminal_epub_volumes,
    side_story_vs_numbered_epub_volume,
    supersede_open_pair_reviews,
)


# 한 파일은 인코딩 확정 중 최대 3회, deep 검사에서 short/long 역할로 각 1회 읽힐 수 있다.
MAX_ESTIMATED_READ_PASSES = 5
# v2: BOM UTF-16 LE/BE strict 판독을 fingerprint 의미에 포함한다.
# 판독 규칙이 바뀌면 기존 decode_lossy 결과를 재사용하지 않도록 반드시 올린다.
FINGERPRINT_VERSION = "5"
AUDITOR_VERSION = "1.4.0"
MANAGED_REPRESENTATIVE_MODE = "normalized_sha_join"
SUPPORTS_READ_ONLY_CACHE = True
DEFAULT_FULL_SWEEP_MAX_READ_BYTES = 256 * 1024 * 1024 * 1024
DEFAULT_FULL_SWEEP_MAX_FILE_BYTES = 1024 * 1024 * 1024
DEFAULT_FULL_SWEEP_MAX_EPUB_UNCOMPRESSED_BYTES = 6 * 1024 * 1024 * 1024


class StaleInputDuringAnalysis(RuntimeError):
    """The file identity changed between cache lookup and analysis persistence."""


DEFAULT_INDEX = str(FILE_INDEX)
DEFAULT_HOUSE = str(HOUSE_DIR)
DEFAULT_TEMP = str(TEMP_DIR)
ORIGIN = "auditor_aux"


@dataclass(frozen=True)
class AuditEntry:
    source: str
    name: str
    rel_path: str
    path: str
    size: int
    mtime_ns: int
    recorded_size: int | None
    ext: str
    core_title: str
    author: str | None
    max_number: int
    effective_max: int
    unit: str
    volume_number: tuple | None
    start_number: int | None
    end_number: int | None
    span_ambiguous: bool
    is_side_story: bool
    disambig: int
    complete: bool
    dev: int | None = None
    ino: int | None = None
    ctime_ns: int | None = None
    pass_recheck: bool = False

    def __post_init__(self):
        """Backfill identity for older direct constructors.

        Real inventory entries always pass these fields explicitly. Keeping the
        constructor compatible avoids weakening older tests and integrations,
        while still freezing the full no-follow identity at construction time.
        """
        if (
            self.dev is not None
            and self.ino is not None
            and self.ctime_ns is not None
        ):
            return
        current = os.stat(self.path, follow_symlinks=False)
        object.__setattr__(self, "dev", current.st_dev)
        object.__setattr__(self, "ino", current.st_ino)
        object.__setattr__(self, "ctime_ns", current.st_ctime_ns)


@dataclass
class AuditCandidate:
    pair_id: str
    left: AuditEntry
    right: AuditEntry
    reasons: list[str] = field(default_factory=list)
    origin: str = ORIGIN


@dataclass
class AuditResult:
    pair_id: str
    classification: str
    candidate_reasons: list[str]
    origin: str
    left: dict
    right: dict
    evidence: dict = field(default_factory=dict)


@dataclass
class AuditReport:
    started_at: str
    duration_seconds: float
    completed: bool
    coverage_limited: bool
    coverage_reasons: list[str]
    stop_reasons: list[str]
    stats: dict
    results: list[dict]
    invalid_records: list[dict]
    configuration: dict


def parse_binary_size(value):
    if isinstance(value, int):
        if value <= 0:
            raise argparse.ArgumentTypeError("size must be positive")
        return value
    text = str(value).strip()
    match = __import__("re").fullmatch(r"([1-9]\d*)(KiB|MiB|GiB)", text, flags=__import__("re").IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError("size must use a positive KiB, MiB, or GiB suffix")
    factors = {"kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3}
    return int(match.group(1)) * factors[match.group(2).lower()]


def build_parser():
    parser = argparse.ArgumentParser(description="Read-only cross-bucket duplicate auditor")
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--house", default=DEFAULT_HOUSE)
    parser.add_argument("--temp", default=DEFAULT_TEMP)
    parser.add_argument("--report-dir")
    parser.add_argument("--state-db")
    parser.add_argument("--house-only", action="store_true")
    parser.add_argument(
        "--same-coordinate-only",
        action="store_true",
        help="inspect only equal extension/core-title/canonical-coordinate groups",
    )
    parser.add_argument("--include-pass", action="store_true")
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument(
        "--full-fingerprint-sweep",
        action="store_true",
        help=(
            "explicit maintenance run: fingerprint every house TXT/EPUB, "
            "backfill the versioned cache, then perform a global exact-content join"
        ),
    )
    parser.add_argument("--max-file-bytes", type=parse_binary_size, default=DEFAULT_MAX_FILE_BYTES)
    parser.add_argument("--max-read-bytes", type=parse_binary_size, default=DEFAULT_MAX_READ_BYTES)
    parser.add_argument(
        "--full-sweep-max-read-bytes",
        type=parse_binary_size,
        default=DEFAULT_FULL_SWEEP_MAX_READ_BYTES,
    )
    parser.add_argument(
        "--full-sweep-max-file-bytes",
        type=parse_binary_size,
        default=DEFAULT_FULL_SWEEP_MAX_FILE_BYTES,
    )
    parser.add_argument(
        "--full-sweep-max-epub-uncompressed-bytes",
        type=parse_binary_size,
        default=DEFAULT_FULL_SWEEP_MAX_EPUB_UNCOMPRESSED_BYTES,
    )
    parser.add_argument("--max-candidates", type=int, default=50_000)
    parser.add_argument(
        "--max-candidate-files",
        type=int,
        help="cap unique files whose content may be read; excess candidates are deferred",
    )
    parser.add_argument("--max-core-group-pairs", type=int, default=5_000)
    parser.add_argument("--max-global-hash-group-pairs", type=int, default=5_000)
    parser.add_argument("--max-global-fingerprint-pairs", type=int, default=50_000)
    parser.add_argument("--max-neighbors-per-entry", type=int, default=1_024)
    parser.add_argument("--max-title-checks-per-entry", type=int, default=24)
    parser.add_argument("--max-deep-pairs", type=int, default=5_000)
    parser.add_argument("--max-deep-pairs-per-file", type=int, default=24)
    parser.add_argument("--anchor-chars", type=int, default=DEFAULT_ANCHOR_CHARS)
    parser.add_argument("--min-strong-chars", type=int, default=MIN_STRONG_TEXT_CHARS)
    return parser


def _validate_positive_args(args, parser):
    for name in (
        "max_candidates", "max_core_group_pairs", "max_global_hash_group_pairs",
        "max_global_fingerprint_pairs", "max_neighbors_per_entry",
        "max_title_checks_per_entry", "max_deep_pairs", "max_deep_pairs_per_file",
        "anchor_chars", "min_strong_chars",
    ):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.max_candidate_files is not None and args.max_candidate_files <= 0:
        parser.error("--max-candidate-files must be positive")


def _is_relative_safe(rel_path):
    path = Path(rel_path)
    return bool(rel_path) and not path.is_absolute() and ".." not in path.parts


def _within(root, candidate):
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _contains_symlink(root, candidate):
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _entry_from_stat(
    source, name, rel_path, path, recorded_size=None, pass_recheck=False,
    core_title_override=None,
):
    stat = os.stat(path, follow_symlinks=False)
    info = analyze_name(name)
    if core_title_override:
        info["core_title"] = str(core_title_override)
    volume = info.get("volume_number")
    if volume is not None:
        volume = tuple(volume)
    return AuditEntry(
        source=source,
        name=name,
        rel_path=normalize_nfc(rel_path),
        path=str(path),
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        dev=stat.st_dev,
        ino=stat.st_ino,
        ctime_ns=stat.st_ctime_ns,
        recorded_size=recorded_size,
        ext=info["ext"],
        core_title=info["core_title"],
        author=info["author"],
        max_number=info["max_number"],
        effective_max=info["effective_max"],
        unit=info["unit"],
        volume_number=volume,
        start_number=info.get("start_number"),
        end_number=info.get("end_number"),
        span_ambiguous=info.get("span_ambiguous", False),
        is_side_story=info.get("is_side_story", False),
        disambig=info.get("disambig", 1),
        complete=info["complete"],
        pass_recheck=pass_recheck,
    )


def load_house_entries(index_path, house_root, include_pass=False):
    from scanner import validate_index_generation

    invalid = []
    entries = []
    root = Path(house_root).expanduser().resolve()
    validate_index_generation(index_path)
    with open(index_path, "r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if payload.get("version") != 2 or not isinstance(payload.get("entries"), list):
        raise ValueError("file_index.json must be a v2 index")

    for raw in payload["entries"]:
        if raw.get("type") != "file":
            continue
        name = normalize_nfc(raw.get("name", ""))
        rel_path = normalize_nfc(raw.get("rel_path", ""))
        if not name or not is_supported_file(name):
            continue
        pass_entry = has_pass_marker(name)
        if not _is_relative_safe(rel_path):
            invalid.append({"source": "house", "name": name, "rel_path": rel_path, "reason": "invalid_path"})
            continue
        lexical = root.joinpath(*Path(rel_path).parts)
        try:
            resolved = lexical.resolve(strict=True)
        except OSError as exc:
            invalid.append({"source": "house", "name": name, "rel_path": rel_path, "reason": "missing_path", "error": str(exc)})
            continue
        if not _within(root, resolved):
            invalid.append({"source": "house", "name": name, "rel_path": rel_path, "reason": "invalid_path"})
            continue
        if _contains_symlink(root, lexical):
            invalid.append({"source": "house", "name": name, "rel_path": rel_path, "reason": "symlink_excluded"})
            continue
        if not resolved.is_file():
            invalid.append({"source": "house", "name": name, "rel_path": rel_path, "reason": "not_file"})
            continue
        entries.append(_entry_from_stat(
            "house", name, rel_path, resolved, raw.get("size"), pass_recheck=pass_entry,
            core_title_override=(
                raw.get("core_title") if raw.get("title_override") else None
            ),
        ))
    return entries, invalid


def scan_temp_entries(temp_root, include_pass=False):
    invalid = []
    entries = []
    root = Path(temp_root).expanduser().resolve()
    if not root.exists():
        return entries, invalid
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        kept_dirs = []
        for directory in sorted(dirs):
            child = current_path / directory
            if child.is_symlink():
                invalid.append({"source": "temp", "rel_path": str(child.relative_to(root)), "reason": "symlink_excluded"})
                continue
            if directory.lower() == "pass" and include_pass:
                kept_dirs.append(directory)
            elif not should_exclude_dir(directory):
                kept_dirs.append(directory)
        dirs[:] = kept_dirs

        for filename in sorted(files):
            if should_exclude_file(filename):
                continue
            path = current_path / filename
            rel_path = normalize_nfc(str(path.relative_to(root)))
            if path.is_symlink():
                invalid.append({"source": "temp", "rel_path": rel_path, "reason": "symlink_excluded"})
                continue
            clean_name = normalize_filename(strip_trash_suffix(filename))
            if not clean_name or not is_supported_file(clean_name):
                continue
            in_pass_dir = bool(Path(rel_path).parts and Path(rel_path).parts[0].lower() == "pass")
            pass_entry = has_pass_marker(clean_name) or in_pass_dir
            if in_pass_dir and not include_pass:
                continue
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                invalid.append({"source": "temp", "rel_path": rel_path, "reason": "missing_path", "error": str(exc)})
                continue
            if not _within(root, resolved):
                invalid.append({"source": "temp", "rel_path": rel_path, "reason": "invalid_path"})
                continue
            entries.append(_entry_from_stat(
                "temp", clean_name, rel_path, resolved, None, pass_recheck=pass_entry,
            ))
    return entries, invalid


def _endpoint(entry):
    return (entry.source, unicodedata.normalize("NFC", entry.rel_path))


def pair_id(left, right):
    endpoints = sorted((_endpoint(left), _endpoint(right)))
    raw = json.dumps(endpoints, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ordered_pair(left, right):
    return (left, right) if _endpoint(left) <= _endpoint(right) else (right, left)


def _explicit_single_volume(entry):
    if not entry.volume_number:
        return None
    part, volume = entry.volume_number
    return (part, volume) if volume is not None else None


def _different_explicit_volumes(left, right):
    a = _explicit_single_volume(left)
    b = _explicit_single_volume(right)
    return bool(a and b and a != b)


def _bucket(entry):
    return (
        entry.volume_number, entry.start_number, entry.end_number, entry.span_ambiguous,
        entry.is_side_story, entry.disambig,
    )


def _grams(value):
    # Three/four-character cores have only one/two trigrams and previously could
    # never satisfy the shared>=2 posting guard.  Bigrams make those short cores
    # retrievable; one/two-character cores remain explicitly coverage-limited.
    width = 2 if len(value) <= 4 else 3
    return {
        value[index:index + width]
        for index in range(max(0, len(value) - width + 1))
    }


_KOREAN_PARTICLE_RE = re.compile(r"(?<=[가-힣])(?:의|은|는|이|가|을|를|과|와)(?=[가-힣])")


def _particle_fold(value):
    return _KOREAN_PARTICLE_RE.sub("", value)


def generate_candidates(entries, config):
    candidates = {}
    coverage = Counter()
    stop_reasons = []
    groups = defaultdict(list)
    for entry in entries:
        if entry.core_title:
            groups[(entry.ext, entry.core_title)].append(entry)

    def add(left, right, reason):
        if left.path == right.path or left.ext != right.ext or _different_explicit_volumes(left, right):
            return
        left, right = _ordered_pair(left, right)
        identifier = pair_id(left, right)
        candidate = candidates.get(identifier)
        if candidate is None:
            candidate = AuditCandidate(identifier, left, right)
            candidates[identifier] = candidate
        if reason not in candidate.reasons:
            candidate.reasons.append(reason)
        if left.pass_recheck or right.pass_recheck:
            if "pass_recheck" not in candidate.reasons:
                candidate.reasons.append("pass_recheck")

    for (_ext, _core), group in sorted(groups.items(), key=lambda item: item[0]):
        same_bucket_groups = defaultdict(list)
        for entry in group:
            same_bucket_groups[_bucket(entry)].append(entry)
        for bucket_group in same_bucket_groups.values():
            same_bucket_pair_count = len(bucket_group) * (len(bucket_group) - 1) // 2
            if same_bucket_pair_count > config.max_core_group_pairs:
                stop_reasons.append("core_group_overflow")
                coverage["same_bucket_unprocessed_pairs"] += same_bucket_pair_count
                continue
            bucket_group = sorted(bucket_group, key=_endpoint)
            for index, left in enumerate(bucket_group):
                for right in bucket_group[index + 1:]:
                    add(left, right, "same_core_same_bucket")

        if getattr(config, "same_coordinate_only", False):
            continue
        pair_count = len(group) * (len(group) - 1) // 2
        if pair_count > config.max_core_group_pairs:
            stop_reasons.append("core_group_overflow")
            coverage["core_group_unprocessed_pairs"] += pair_count
            continue
        group = sorted(group, key=_endpoint)
        for index, left in enumerate(group):
            for right in group[index + 1:]:
                if _bucket(left) != _bucket(right):
                    add(left, right, "same_core_cross_bucket")
                if left.disambig != right.disambig:
                    add(left, right, "marker_recheck")

    if getattr(config, "same_coordinate_only", False):
        ordered = sorted(candidates.values(), key=lambda candidate: candidate.pair_id)
        if len(ordered) > config.max_candidates:
            stop_reasons.append("candidate_overflow")
        return ordered[:config.max_candidates], coverage, sorted(set(stop_reasons)), {
            "posting_cap": 0,
            "posting_max": 0,
            "posting_mean": 0,
            "posting_p95": 0,
        }

    # 조사 한 글자 삽입/삭제는 academy처럼 고빈도 제목군의 top-K에서 밀릴 수 있으므로,
    # O(N²) 전수 비교 대신 조사 제거 key의 bounded 그룹 안에서만 직접 회수한다.
    particle_groups = defaultdict(list)
    for entry in entries:
        folded = _particle_fold(entry.core_title)
        if len(folded) >= 3:
            particle_groups[(entry.ext, folded)].append(entry)
    for (_ext, _folded), group in sorted(particle_groups.items(), key=lambda item: item[0]):
        pair_count = len(group) * (len(group) - 1) // 2
        if pair_count > config.max_core_group_pairs:
            stop_reasons.append("core_group_overflow")
            coverage["particle_group_unprocessed_pairs"] += pair_count
            continue
        group = sorted(group, key=_endpoint)
        for index, left in enumerate(group):
            for right in group[index + 1:]:
                if left.core_title != right.core_title:
                    add(left, right, "particle_variant")

    eligible = [entry for entry in entries if len(entry.core_title) >= 3]
    short_count = sum(1 for entry in entries if 0 < len(entry.core_title) < 3)
    if short_count:
        coverage["short_core_no_fuzzy"] = short_count
    adaptive_count = sum(1 for entry in eligible if len(entry.core_title) <= 4)
    if adaptive_count:
        coverage["adaptive_short_gram_entries"] = adaptive_count
    posting_cap = min(128, max(32, math.ceil(len(eligible) * 0.005)))
    postings = defaultdict(list)
    gram_sets = {}
    for index, entry in enumerate(eligible):
        grams = _grams(entry.core_title)
        gram_sets[index] = grams
        for gram in grams:
            postings[gram].append(index)
    high_frequency = {gram for gram, ids in postings.items() if len(ids) > posting_cap}
    coverage["high_frequency_grams"] = len(high_frequency)
    eligible_grams = {index: grams - high_frequency for index, grams in gram_sets.items()}

    for index, entry in enumerate(eligible):
        grams = sorted(eligible_grams[index], key=lambda gram: (len(postings[gram]), gram))[:8]
        neighbor_ids = set()
        for gram in grams:
            neighbor_ids.update(postings[gram])
        neighbor_ids.discard(index)
        ranked_neighbors = sorted(
            neighbor_ids,
            key=lambda other: (
                -len(eligible_grams[index] & eligible_grams[other]),
                _endpoint(eligible[other]),
            ),
        )
        if len(ranked_neighbors) > config.max_neighbors_per_entry:
            coverage["neighbor_truncated"] += len(ranked_neighbors) - config.max_neighbors_per_entry
            ranked_neighbors = ranked_neighbors[:config.max_neighbors_per_entry]

        scored = []
        for other in ranked_neighbors:
            if other <= index:
                continue
            target = eligible[other]
            if entry.ext != target.ext or _different_explicit_volumes(entry, target):
                continue
            shared = len(eligible_grams[index] & eligible_grams[other])
            adaptive_short = min(len(entry.core_title), len(target.core_title)) <= 4
            if shared < (1 if adaptive_short else 2):
                continue
            shorter, longer = sorted((entry.core_title, target.core_title), key=len)
            contained = shorter in longer
            length_ratio = len(shorter) / len(longer)
            if not contained and length_ratio < 0.65:
                continue
            union = len(eligible_grams[index] | eligible_grams[other]) or 1
            scored.append((shared, shared / union, contained, target, length_ratio))
        scored.sort(key=lambda row: (-row[0], -row[1], _endpoint(row[3])))
        if len(scored) > config.max_title_checks_per_entry:
            coverage["topk_truncated"] += len(scored) - config.max_title_checks_per_entry
            scored = scored[:config.max_title_checks_per_entry]
        for shared, jaccard, contained, target, length_ratio in scored:
            similarity = SequenceMatcher(None, entry.core_title, target.core_title, autojunk=False).ratio()
            adaptive_short = min(len(entry.core_title), len(target.core_title)) <= 4
            threshold = (2 / 3) if adaptive_short else 0.72
            if contained or similarity >= threshold:
                if contained and entry.core_title != target.core_title:
                    reason = "metadata_leak"
                else:
                    reason = "near_core_adaptive" if adaptive_short else "near_core"
                add(entry, target, reason)

    if len(candidates) > config.max_candidates:
        stop_reasons.append("candidate_overflow")
    ordered = sorted(candidates.values(), key=lambda candidate: candidate.pair_id)
    posting_sizes = sorted(len(ids) for ids in postings.values())
    p95_index = max(0, math.ceil(len(posting_sizes) * 0.95) - 1) if posting_sizes else 0
    return ordered[:config.max_candidates], coverage, sorted(set(stop_reasons)), {
        "posting_cap": posting_cap,
        "posting_max": max((len(ids) for ids in postings.values()), default=0),
        "posting_mean": (sum(len(ids) for ids in postings.values()) / len(postings)) if postings else 0,
        "posting_p95": posting_sizes[p95_index] if posting_sizes else 0,
    }


def _load_managed_representatives(entries, state_db_path):
    """Return indexed TXT representatives without reading any file bodies."""
    if not state_db_path or not os.path.isfile(state_db_path):
        return [], []
    import decision_store

    conn = decision_store.connect_state_db_readonly(state_db_path)
    try:
        representative_rows = list(conn.execute(
                """
                SELECT f.canonical_path
                FROM representatives AS r
                JOIN files AS f ON f.file_id = r.file_id
                WHERE f.active = 1 AND f.assignment_state = 'managed'
                """
            ))
    finally:
        conn.close()

    # 이 보강 경로는 TXT normalized SHA join 전용이다. EPUB 대표는
    # 누락 판정 모수에서도 제외해야 한다.
    # 그렇지 않으면 정상 인덱스에 존재하는 EPUB 대표를 모두 missing으로 오판한다.
    representative_paths = {
        decision_store.canonicalize_path(row[0])
        for row in representative_rows
        if Path(row[0]).suffix.lower() == ".txt"
    }
    representatives = [
        entry for entry in entries
        if entry.ext == ".txt"
        and decision_store.canonicalize_path(entry.path) in representative_paths
    ]
    input_representative_paths = {
        decision_store.canonicalize_path(entry.path) for entry in representatives
    }
    missing_representatives = sorted(representative_paths - input_representative_paths)
    return representatives, missing_representatives


def generate_managed_representative_candidates(
    entries,
    state_db_path,
    analyses=None,
    *,
    representatives=None,
    missing_representatives=None,
):
    """Join temp TXT with managed representatives using bounded analyses only.

    File bodies are deliberately not read here. ``run_audit`` prepares any
    uncached representative and temp analyses through ``_analyze_entry_set`` so
    the shared ``ReadBudget`` and max-file policy remain authoritative.
    """
    if representatives is None or missing_representatives is None:
        loaded_representatives, loaded_missing = _load_managed_representatives(
            entries, state_db_path
        )
        if representatives is None:
            representatives = loaded_representatives
        if missing_representatives is None:
            missing_representatives = loaded_missing
    analyses = analyses or {}
    temp_entries = [entry for entry in entries if entry.source == "temp" and entry.ext == ".txt"]
    representatives_by_hash = defaultdict(list)
    for representative in representatives:
        analysis = analyses.get(representative.path)
        if analysis is None or not _analysis_matches_current(representative, analysis):
            continue
        normalized = analysis.normalized_sha256
        if not normalized:
            continue
        representatives_by_hash[normalized].append(representative)
    candidates = []
    for candidate_entry in sorted(temp_entries, key=_endpoint):
        analysis = analyses.get(candidate_entry.path)
        if analysis is None or not _analysis_matches_current(candidate_entry, analysis):
            continue
        normalized = analysis.normalized_sha256
        if not normalized:
            continue
        for representative in sorted(representatives_by_hash.get(normalized, []), key=_endpoint):
            if candidate_entry.path == representative.path:
                continue
            left, right = _ordered_pair(candidate_entry, representative)
            candidates.append(AuditCandidate(
                pair_id(left, right), left, right,
                reasons=["managed_representative_full_scan"],
            ))
    return candidates, missing_representatives


def merge_mandatory_candidates(candidates, mandatory):
    merged = {candidate.pair_id: candidate for candidate in candidates}
    for candidate in mandatory:
        existing = merged.get(candidate.pair_id)
        if existing is None:
            merged[candidate.pair_id] = candidate
        else:
            for reason in candidate.reasons:
                if reason not in existing.reasons:
                    existing.reasons.append(reason)
    return sorted(merged.values(), key=lambda candidate: candidate.pair_id)


def generate_fingerprint_candidates(entries, analyses, config):
    """Bounded title-independent join over current versioned fingerprints.

    Only byte/full-normalized/EPUB-content equality enters this global union.
    Near/contained relations still require an existing title candidate and the
    bounded deep checker, so this function cannot promote weak similarity into
    a strong relation.
    """
    candidates = {}
    coverage = Counter()
    stop_reasons = []
    groups = defaultdict(list)
    eligible = [entry for entry in entries if entry.ext in {".txt", ".epub"}]
    coverage["global_fingerprint_eligible_files"] = len(eligible)

    def add(left, right, reason):
        if left.path == right.path or left.ext != right.ext:
            return
        left, right = _ordered_pair(left, right)
        identifier = pair_id(left, right)
        candidate = candidates.get(identifier)
        if candidate is None:
            candidate = AuditCandidate(identifier, left, right)
            candidates[identifier] = candidate
        if reason not in candidate.reasons:
            candidate.reasons.append(reason)

    for entry in eligible:
        analysis = analyses.get(entry.path)
        if analysis is None:
            coverage["global_fingerprint_missing_files"] += 1
            continue
        if not _analysis_matches_current(entry, analysis):
            coverage["global_fingerprint_stale_files"] += 1
            stop_reasons.append("stale_input")
            continue
        coverage["global_fingerprint_available_files"] += 1
        if analysis.raw_sha256:
            groups[("raw", entry.ext, analysis.raw_sha256)].append(entry)
        if not analysis.normalized_sha256:
            continue
        if entry.ext == ".txt" and analysis.status == "ok":
            groups[("normalized", entry.ext, analysis.normalized_sha256)].append(entry)
        elif entry.ext == ".epub" and analysis.status == "epub_content":
            groups[("epub_content", entry.ext, analysis.normalized_sha256)].append(entry)

    for (kind, _ext, _digest), group in sorted(
        groups.items(), key=lambda item: (item[0][0], item[0][1], item[0][2])
    ):
        if len(group) < 2:
            continue
        coverage[f"global_{kind}_groups"] += 1
        pair_count = len(group) * (len(group) - 1) // 2
        if pair_count > config.max_global_hash_group_pairs:
            coverage["global_hash_group_unprocessed_pairs"] += pair_count
            stop_reasons.append("global_hash_group_overflow")
            continue
        reason = {
            "raw": "global_raw_sha256",
            "normalized": "global_normalized_sha256",
            "epub_content": "global_epub_content_sha256",
        }[kind]
        ordered = sorted(group, key=_endpoint)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1:]:
                add(left, right, reason)

    ordered = sorted(candidates.values(), key=lambda candidate: candidate.pair_id)
    coverage["global_fingerprint_pairs_generated"] = len(ordered)
    if len(ordered) > config.max_global_fingerprint_pairs:
        coverage["global_fingerprint_pairs_truncated"] = (
            len(ordered) - config.max_global_fingerprint_pairs
        )
        stop_reasons.append("global_fingerprint_pair_overflow")
        ordered = ordered[:config.max_global_fingerprint_pairs]
    return ordered, coverage, sorted(set(stop_reasons))


def _entry_public(entry):
    data = asdict(entry)
    for private_key in ("path", "dev", "ino", "ctime_ns"):
        data.pop(private_key, None)
    return data


def _basic_result(candidate, classification, evidence=None):
    return AuditResult(
        pair_id=candidate.pair_id,
        classification=classification,
        candidate_reasons=sorted(candidate.reasons),
        origin=ORIGIN,
        left=_entry_public(candidate.left),
        right=_entry_public(candidate.right),
        evidence=evidence or {},
    )


def _status_for_pair(left_analysis, right_analysis):
    priority = (
        "stale", "normalization_deferred", "oversize_deferred", "decode_lossy",
        "empty_text", "insufficient_text",
    )
    for status in priority:
        if left_analysis.status == status or right_analysis.status == status:
            return status
    return None


def _stat_identity(current):
    return (
        current.st_dev,
        current.st_ino,
        current.st_ctime_ns,
        current.st_size,
        current.st_mtime_ns,
    )


def _entry_identity(entry):
    return (entry.dev, entry.ino, entry.ctime_ns, entry.size, entry.mtime_ns)


def _entry_is_current(entry):
    try:
        current = os.stat(entry.path, follow_symlinks=False)
    except OSError:
        return False
    return _stat_identity(current) == _entry_identity(entry)


def _analysis_matches_current(entry, analysis):
    if (analysis.size, analysis.mtime_ns) != (entry.size, entry.mtime_ns):
        return False
    return _entry_is_current(entry)


def _stale_analysis(entry, analysis=None):
    """Discard all content evidence once the entry identity is no longer current."""
    if analysis is not None:
        return replace(
            analysis,
            path=entry.path,
            size=entry.size,
            mtime_ns=entry.mtime_ns,
            raw_sha256=None,
            normalized_sha256=None,
            normalized_length=0,
            front_anchor="",
            tail_anchor="",
            status="stale",
        )
    return TextAnalysis(
        path=entry.path,
        size=entry.size,
        mtime_ns=entry.mtime_ns,
        encoding=None,
        lossy=False,
        error="file identity changed during audit",
        raw_sha256=None,
        normalized_sha256=None,
        normalized_length=0,
        front_anchor="",
        tail_anchor="",
        status="stale",
        read_bytes=0,
    )


def _snapshot(entries):
    return {entry.path: _entry_identity(entry) for entry in entries}


def _snapshot_changes(snapshot):
    changed = []
    for path, before in snapshot.items():
        try:
            stat = os.stat(path, follow_symlinks=False)
            after = _stat_identity(stat)
        except OSError:
            after = None
        if before != after:
            changed.append({"path": path, "before": before, "after": after})
    return changed


def _without_changed_inputs(candidates, results, changed):
    changed_paths = {item["path"] for item in changed}
    if not changed_paths:
        return list(candidates), list(results)
    stale_pair_ids = {
        candidate.pair_id
        for candidate in candidates
        if candidate.left.path in changed_paths or candidate.right.path in changed_paths
    }
    safe_candidates = [
        candidate for candidate in candidates
        if candidate.pair_id not in stale_pair_ids
    ]
    safe_results = [
        result for result in results
        if result.pair_id not in stale_pair_ids
    ]
    return safe_candidates, safe_results


class PersistentAuditCache:
    def __init__(
        self, state_db_path, entries, configuration_hash,
        analysis_policy_hash=None,
    ):
        import decision_store

        self.store = decision_store
        self.conn = decision_store.initialize_state_db(state_db_path)
        self.configuration_hash = configuration_hash
        self.analysis_policy_hash = analysis_policy_hash or configuration_hash
        self.fingerprint_version = (
            f"{FINGERPRINT_VERSION}:{self.analysis_policy_hash}"
        )
        self.file_ids = {}
        self.canonical_paths = {}
        self.fingerprint_ids = {}
        self.pending_identities = {}
        self.raw_sha_cache = {}
        self.stats = Counter()
        with decision_store.transaction(self.conn):
            for entry in entries:
                row = decision_store.reconcile_file_metadata(
                    self.conn,
                    entry.path,
                    source=entry.source,
                    legacy_marker=entry.pass_recheck or entry.disambig > 1,
                )
                self.file_ids[entry.path] = row["file_id"]
                self.canonical_paths[entry.path] = row["canonical_path"]

    def close(self):
        self.conn.close()

    def _identity_fingerprint_version(self, current):
        return (
            f"{self.fingerprint_version}:{current.st_dev}:"
            f"{current.st_ino}:{current.st_ctime_ns}"
        )

    @staticmethod
    def _identity(current):
        return (
            current.st_dev,
            current.st_ino,
            current.st_ctime_ns,
            current.st_size,
            current.st_mtime_ns,
        )

    def _raw_sha_for_fingerprint(self, fingerprint_id, path):
        """Return raw SHA, filling decode-lossy omissions once per fingerprint."""
        if fingerprint_id in self.raw_sha_cache:
            return self.raw_sha_cache[fingerprint_id]
        row = self.conn.execute(
            """
            SELECT raw_sha256, dev, ino, ctime_ns, size, mtime_ns
            FROM fingerprints WHERE fingerprint_id = ?
            """,
            (fingerprint_id,),
        ).fetchone()
        if row is None:
            return None
        raw_sha256 = row["raw_sha256"]
        if raw_sha256 is None:
            from mutation_io import inspect_regular_file

            evidence = inspect_regular_file(path)
            expected = (
                row["dev"], row["ino"], row["ctime_ns"],
                row["size"], row["mtime_ns"],
            )
            actual = (
                evidence.dev, evidence.ino, evidence.ctime_ns,
                evidence.size, evidence.mtime_ns,
            )
            if expected != actual:
                raise StaleInputDuringAnalysis(path)
            raw_sha256 = evidence.sha256
        self.raw_sha_cache[fingerprint_id] = raw_sha256
        return raw_sha256

    @staticmethod
    def _text_analysis_from_row(entry, row):
        metadata = json.loads(row["anchors_json"] or "{}")
        return TextAnalysis(
            path=entry.path,
            size=row["size"],
            mtime_ns=row["mtime_ns"],
            encoding=row["encoding"],
            lossy=bool(metadata.get("lossy", False)),
            error=metadata.get("error"),
            raw_sha256=row["raw_sha256"],
            normalized_sha256=row["normalized_sha256"],
            normalized_length=row["normalized_length"] or 0,
            front_anchor=row["front_anchor"] or "",
            tail_anchor=row["tail_anchor"] or "",
            status=row["status"],
            read_bytes=0,
        )

    def analysis(self, entry, *, track_miss=True, retry_deferred=False):
        file_id = self.file_ids[entry.path]
        current = os.stat(entry.path, follow_symlinks=False)
        row = self.conn.execute(
            """
            SELECT * FROM fingerprints
            WHERE file_id = ? AND canonical_path = ? AND size = ? AND mtime_ns = ?
              AND normalizer_version = ? AND fingerprint_version = ?
              AND analysis_policy_hash = ?
              AND dev = ? AND ino = ? AND ctime_ns = ?
            """,
            (
                file_id,
                self.canonical_paths[entry.path],
                entry.size,
                entry.mtime_ns,
                NORMALIZER_VERSION,
                self._identity_fingerprint_version(current),
                self.analysis_policy_hash,
                current.st_dev,
                current.st_ino,
                current.st_ctime_ns,
            ),
        ).fetchone()
        if (
            row is not None
            and retry_deferred
            and row["status"] in {
                "oversize_deferred", "normalization_deferred", "epub_error",
            }
        ):
            row = None
        if row is None:
            if track_miss:
                self.pending_identities[entry.path] = self._identity(current)
                self.stats["fingerprint_cache_misses"] += 1
            else:
                self.stats["fingerprint_cache_peek_misses"] += 1
            return None
        self.pending_identities.pop(entry.path, None)
        analysis = self._text_analysis_from_row(entry, row)
        self.fingerprint_ids[entry.path] = row["fingerprint_id"]
        self.stats["fingerprint_cache_hits"] += 1
        return analysis

    def peek_analysis(self, entry, *, retry_deferred=False):
        """Return a current versioned fingerprint without scheduling a body read."""
        return self.analysis(
            entry, track_miss=False, retry_deferred=retry_deferred
        )

    def peek_many(self, entries, *, retry_deferred=False):
        """Bulk-load current cache rows with one query, then verify identities."""
        rows = {
            row["file_id"]: row
            for row in self.conn.execute(
                """
                SELECT fp.* FROM files AS f
                JOIN fingerprints AS fp
                  ON fp.fingerprint_id = f.current_fingerprint_id
                WHERE f.active = 1 AND fp.normalizer_version = ?
                  AND fp.analysis_policy_hash = ?
                """,
                (NORMALIZER_VERSION, self.analysis_policy_hash),
            ).fetchall()
        }
        analyses = {}
        for entry in entries:
            file_id = self.file_ids[entry.path]
            row = rows.get(file_id)
            current = os.stat(entry.path, follow_symlinks=False)
            valid = bool(
                row is not None
                and row["canonical_path"] == self.canonical_paths[entry.path]
                and row["size"] == entry.size == current.st_size
                and row["mtime_ns"] == entry.mtime_ns == current.st_mtime_ns
                and row["fingerprint_version"]
                == self._identity_fingerprint_version(current)
                and (row["dev"], row["ino"], row["ctime_ns"])
                == (current.st_dev, current.st_ino, current.st_ctime_ns)
                and not (
                    retry_deferred
                    and row["status"] in {
                        "oversize_deferred", "normalization_deferred", "epub_error",
                    }
                )
            )
            if not valid:
                self.stats["fingerprint_cache_peek_misses"] += 1
                continue
            analysis = self._text_analysis_from_row(entry, row)
            analyses[entry.path] = analysis
            self.fingerprint_ids[entry.path] = row["fingerprint_id"]
            self.stats["fingerprint_cache_hits"] += 1
        return analyses

    def store_analysis(self, entry, analysis):
        file_id = self.file_ids[entry.path]
        current = os.stat(entry.path, follow_symlinks=False)
        expected = self.pending_identities.pop(entry.path, None)
        actual = self._identity(current)
        if expected is None or actual != expected:
            self.stats["fingerprint_stale_inputs"] += 1
            raise StaleInputDuringAnalysis(entry.path)
        if (analysis.size, analysis.mtime_ns) != (current.st_size, current.st_mtime_ns):
            self.stats["fingerprint_stale_inputs"] += 1
            raise StaleInputDuringAnalysis(entry.path)
        # Resource-limit/deferred results are not immutable content evidence.
        # Leaving them uncached lets a later explicit sweep retry with its larger
        # maintenance budget under the same semantic fingerprint version.
        if analysis.status in {"oversize_deferred", "normalization_deferred"}:
            return
        with self.store.transaction(self.conn):
            cursor = self.conn.execute(
                """
                INSERT INTO fingerprints(
                    file_id, canonical_path, size, mtime_ns, normalizer_version,
                    fingerprint_version, analysis_policy_hash,
                    dev, ino, ctime_ns,
                    raw_sha256, normalized_sha256,
                    normalized_length, encoding, status, front_anchor, tail_anchor,
                    anchors_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    self.canonical_paths[entry.path],
                    analysis.size,
                    analysis.mtime_ns,
                    NORMALIZER_VERSION,
                    self._identity_fingerprint_version(current),
                    self.analysis_policy_hash,
                    current.st_dev,
                    current.st_ino,
                    current.st_ctime_ns,
                    analysis.raw_sha256,
                    analysis.normalized_sha256,
                    analysis.normalized_length,
                    analysis.encoding,
                    analysis.status,
                    analysis.front_anchor,
                    analysis.tail_anchor,
                    json.dumps(
                        {"lossy": analysis.lossy, "error": analysis.error},
                        ensure_ascii=False,
                    ),
                ),
            )
            fingerprint_id = cursor.lastrowid
            self.conn.execute(
                "UPDATE files SET current_fingerprint_id = ? WHERE file_id = ?",
                (fingerprint_id, file_id),
            )
        self.fingerprint_ids[entry.path] = fingerprint_id

    def pair_result(self, candidate):
        left_id = self.fingerprint_ids.get(candidate.left.path)
        right_id = self.fingerprint_ids.get(candidate.right.path)
        if left_id is None or right_id is None or left_id == right_id:
            self.stats["pair_cache_misses"] += 1
            return None
        left_id, right_id = sorted((left_id, right_id))
        row = self.conn.execute(
            """
            SELECT classification, evidence_json FROM pair_cache
            WHERE left_fingerprint_id = ? AND right_fingerprint_id = ?
              AND auditor_version = ? AND configuration_hash = ? AND completed = 1
            """,
            (left_id, right_id, AUDITOR_VERSION, self.configuration_hash),
        ).fetchone()
        if row is None:
            self.stats["pair_cache_misses"] += 1
            return None
        self.stats["pair_cache_hits"] += 1
        return _basic_result(
            candidate,
            row["classification"],
            json.loads(row["evidence_json"] or "{}"),
        )

    def store_pair_results(self, candidates, results):
        stable = {
            "text_equivalent", "epub_equivalent", "marker_recheck", "near_identical", "contained_exact",
            "contained_version", "longer_unresolved", "boilerplate_only", "different",
            "decode_lossy", "empty_text", "insufficient_text", "metadata_only",
        }
        by_pair = {result.pair_id: result for result in results}
        with self.store.transaction(self.conn):
            for candidate in candidates:
                result = by_pair.get(candidate.pair_id)
                if result is None or result.classification not in stable:
                    continue
                if not (
                    _entry_is_current(candidate.left)
                    and _entry_is_current(candidate.right)
                ):
                    self.stats["fingerprint_stale_inputs"] += 1
                    continue
                left_id = self.fingerprint_ids.get(candidate.left.path)
                right_id = self.fingerprint_ids.get(candidate.right.path)
                if left_id is None or right_id is None or left_id == right_id:
                    continue
                left_id, right_id = sorted((left_id, right_id))
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO pair_cache(
                        left_fingerprint_id, right_fingerprint_id, auditor_version,
                        configuration_hash, classification, evidence_json, completed
                    ) VALUES (?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        left_id,
                        right_id,
                        AUDITOR_VERSION,
                        self.configuration_hash,
                        result.classification,
                        json.dumps(result.evidence, ensure_ascii=False, sort_keys=True),
                    ),
                )
                self._store_review_item(candidate, result, left_id, right_id)

    def _store_review_item(self, candidate, result, ordered_left_fp, ordered_right_fp):
        reviewable = {
            "text_equivalent", "epub_equivalent", "marker_recheck", "near_identical", "contained_exact",
            "contained_version", "longer_unresolved", "decode_lossy",
            "metadata_only", "insufficient_text",
        }
        if result.classification not in reviewable:
            return
        if (
            result.classification == "metadata_only"
            and distinct_terminal_epub_volumes(
                candidate.left.name, candidate.right.name
            )
        ):
            self.stats["distinct_volume_reviews_suppressed"] += 1
            return
        if (
            result.classification == "metadata_only"
            and side_story_vs_numbered_epub_volume(
                candidate.left.name, candidate.right.name
            )
        ):
            self.stats["side_story_volume_reviews_suppressed"] += 1
            return
        if (
            result.classification == "metadata_only"
            and different_core_titles(
                candidate.left.core_title, candidate.right.core_title
            )
        ):
            self.stats["cross_core_reviews_suppressed"] += 1
            return
        left_file = self.file_ids[candidate.left.path]
        right_file = self.file_ids[candidate.right.path]
        rows = {
            row["file_id"]: row
            for row in self.conn.execute(
                """
                SELECT f.file_id, f.assignment_state, f.protected,
                       CASE WHEN r.file_id IS NULL THEN 0 ELSE 1 END AS representative
                FROM files AS f
                LEFT JOIN representatives AS r ON r.file_id = f.file_id
                WHERE f.file_id IN (?, ?)
                """,
                (left_file, right_file),
            ).fetchall()
        }

        def reference_rank(entry, file_id):
            row = rows[file_id]
            return (
                0 if row["representative"] else 1,
                0 if row["protected"] else 1,
                0 if row["assignment_state"] == "managed" else 1,
                0 if entry.source == "house" else 1,
                entry.rel_path,
            )

        pair = [(candidate.left, left_file), (candidate.right, right_file)]
        reference_entry, reference_file = min(
            pair, key=lambda item: reference_rank(item[0], item[1])
        )
        candidate_entry, candidate_file = next(item for item in pair if item[1] != reference_file)
        candidate_fp = self.fingerprint_ids[candidate_entry.path]
        reference_fp = self.fingerprint_ids[reference_entry.path]
        existing = self.conn.execute(
            """
            SELECT 1 FROM review_items
            WHERE candidate_file_id = ? AND reference_file_id = ?
              AND left_fingerprint_id = ? AND right_fingerprint_id = ?
            LIMIT 1
            """,
            (candidate_file, reference_file, candidate_fp, reference_fp),
        ).fetchone()
        if existing:
            return
        if self.store.human_disposition_suppresses_review(
            self.conn,
            candidate_file_id=candidate_file,
            reference_file_id=reference_file,
            candidate_raw_sha256=self._raw_sha_for_fingerprint(
                candidate_fp, candidate_entry.path
            ),
            reference_raw_sha256=self._raw_sha_for_fingerprint(
                reference_fp, reference_entry.path
            ),
        ):
            self.stats["human_disposition_cache_hits"] += 1
            return
        self.stats["stale_open_reviews_superseded"] += supersede_open_pair_reviews(
            self.conn,
            candidate_file_id=candidate_file,
            reference_file_id=reference_file,
            classification=result.classification,
        )
        self.conn.execute(
            """
            INSERT INTO review_items(
                candidate_file_id, reference_file_id,
                left_fingerprint_id, right_fingerprint_id,
                classification, state, evidence_json
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                candidate_file,
                reference_file,
                candidate_fp,
                reference_fp,
                result.classification,
                json.dumps(result.evidence, ensure_ascii=False, sort_keys=True),
            ),
        )
        self.stats["review_items_created"] += 1


def load_persisted_analyses_readonly(
    entries, state_db_path, analysis_policy_hash, *, retry_deferred=False
):
    """Load current fingerprints without opening the state DB for writes.

    Folderling pure-plan runs deliberately set ``cache_write=False``.  They must
    still be able to join a newly fingerprinted temp file against an earlier
    explicit house backfill, while leaving the database byte-for-byte unchanged.
    """
    stats = Counter()
    if not state_db_path or not os.path.isfile(state_db_path):
        return {}, stats

    import decision_store

    conn = decision_store.connect_state_db_readonly(state_db_path)
    try:
        rows = conn.execute(
            """
            SELECT fp.* FROM files AS f
            JOIN fingerprints AS fp
              ON fp.fingerprint_id = f.current_fingerprint_id
            WHERE f.active = 1 AND fp.normalizer_version = ?
              AND fp.analysis_policy_hash = ?
            """,
            (NORMALIZER_VERSION, analysis_policy_hash),
        ).fetchall()
    finally:
        conn.close()

    rows_by_path = {row["canonical_path"]: row for row in rows}
    analyses = {}
    for entry in entries:
        canonical_path = decision_store.canonicalize_path(entry.path)
        row = rows_by_path.get(canonical_path)
        try:
            current = os.stat(entry.path, follow_symlinks=False)
        except OSError:
            stats["fingerprint_cache_peek_misses"] += 1
            continue
        expected_version = (
            f"{FINGERPRINT_VERSION}:{analysis_policy_hash}:"
            f"{current.st_dev}:{current.st_ino}:{current.st_ctime_ns}"
        )
        valid = bool(
            row is not None
            and row["size"] == entry.size == current.st_size
            and row["mtime_ns"] == entry.mtime_ns == current.st_mtime_ns
            and row["fingerprint_version"] == expected_version
            and (row["dev"], row["ino"], row["ctime_ns"])
            == (current.st_dev, current.st_ino, current.st_ctime_ns)
            and not (
                retry_deferred
                and row["status"] in {
                    "oversize_deferred", "normalization_deferred", "epub_error",
                }
            )
        )
        if not valid:
            stats["fingerprint_cache_peek_misses"] += 1
            continue
        analyses[entry.path] = PersistentAuditCache._text_analysis_from_row(
            entry, row
        )
        stats["fingerprint_cache_hits"] += 1
    return analyses, stats


def _pair_configuration_hash(config):
    relevant = {
        "auditor_version": AUDITOR_VERSION,
        "normalizer_version": NORMALIZER_VERSION,
        "fingerprint_version": FINGERPRINT_VERSION,
        "anchor_chars": config.anchor_chars,
        "min_strong_chars": config.min_strong_chars,
        "max_file_bytes": config.max_file_bytes,
    }
    payload = json.dumps(relevant, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _analysis_policy_hash(config):
    """Hash fingerprint semantics, excluding one-run resource/candidate caps."""
    relevant = {
        "auditor_version": AUDITOR_VERSION,
        "normalizer_version": NORMALIZER_VERSION,
        "fingerprint_version": FINGERPRINT_VERSION,
        "anchor_chars": config.anchor_chars,
        "min_strong_chars": config.min_strong_chars,
        "text_contract": "strict-decode+nfc+whitespace-v2",
        "epub_contract": "normalized-member-content-v1",
    }
    payload = json.dumps(relevant, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_AUDIT_PROGRESS_LABELS = {
    "full_sweep_text": "전체 TXT fingerprint 갱신",
    "full_sweep_epub": "전체 EPUB fingerprint 갱신",
    "managed_representative_text": "관리 대표 TXT fingerprint 갱신",
    "temp_fingerprint_text": "신규 TXT fingerprint 갱신",
    "temp_fingerprint_epub": "신규 EPUB fingerprint 갱신",
    "text_analysis": "본문 기본 분석",
    "epub_analysis": "EPUB 내용 분석",
    "pair_classification": "후보 쌍 판정",
    "deep_scan": "정밀 본문 비교",
}


def _emit_audit_progress(config, audit_phase, completed, total, budget):
    event = {
        "audit_phase": audit_phase,
        "completed": int(completed),
        "total": int(total),
        "read_bytes": int(budget.read_bytes),
    }
    label = _AUDIT_PROGRESS_LABELS.get(audit_phase, audit_phase)
    if getattr(config, "progress", False):
        print(
            f"  ... {label} {completed}/{total} "
            f"({budget.read_bytes / (1024 ** 3):.2f} GiB read)",
            flush=True,
        )
    callback = getattr(config, "progress_callback", None)
    if callable(callback):
        callback(event)


def _ensure_analysis_raw_sha(analysis, entry, budget):
    if analysis.raw_sha256 is not None:
        return analysis
    path = entry.path
    if not _entry_is_current(entry):
        raise StaleInputDuringAnalysis(path)
    size = os.path.getsize(path)
    budget.reserve_pass(size)
    evidence = inspect_regular_file(path)
    budget.consume(size)
    if (
        evidence.dev,
        evidence.ino,
        evidence.ctime_ns,
        evidence.size,
        evidence.mtime_ns,
    ) != _entry_identity(entry):
        raise StaleInputDuringAnalysis(path)
    if (analysis.size, analysis.mtime_ns) != (evidence.size, evidence.mtime_ns):
        raise StaleInputDuringAnalysis(path)
    return replace(
        analysis,
        raw_sha256=evidence.sha256,
        read_bytes=analysis.read_bytes + evidence.size,
    )


def _analyze_entry_set(
    entries,
    config,
    stop_reasons,
    *,
    persistent=None,
    analyses=None,
    budget=None,
    cache=None,
    max_file_bytes=None,
    max_epub_uncompressed_bytes=None,
    retry_deferred=False,
    text_phase="text_analysis",
    epub_phase="epub_analysis",
):
    analyses = analyses if analyses is not None else {}
    budget = budget or ReadBudget(max_bytes=config.max_read_bytes)
    cache = cache or TextAnalysisCache()
    max_file_bytes = max_file_bytes or config.max_file_bytes
    stats = Counter()
    unique_entries = {entry.path: entry for entry in entries}
    budget_exhausted = False
    for path, entry in unique_entries.items():
        analysis = analyses.get(path)
        if analysis is not None and not _analysis_matches_current(entry, analysis):
            analyses[path] = _stale_analysis(entry, analysis)
            stats["stale_analyses"] += 1
            stop_reasons.append("stale_input")
    txt_items = [
        (path, entry) for path, entry in sorted(unique_entries.items())
        if entry.ext == ".txt" and path not in analyses
    ]
    stats["eligible_txt"] = sum(entry.ext == ".txt" for entry in unique_entries.values())
    stats["eligible_epub"] = sum(entry.ext == ".epub" for entry in unique_entries.values())
    if txt_items:
        _emit_audit_progress(config, text_phase, 0, len(txt_items), budget)
    for item_index, (path, entry) in enumerate(txt_items, start=1):
        try:
            if not _entry_is_current(entry):
                analyses[path] = _stale_analysis(entry)
                stats["stale_analyses"] += 1
                stop_reasons.append("stale_input")
                continue
            analysis = (
                persistent.analysis(entry, retry_deferred=retry_deferred)
                if persistent is not None else None
            )
            if analysis is None:
                analysis = cache.analyze(
                    path,
                    budget=budget,
                    max_file_bytes=max_file_bytes,
                    anchor_chars=config.anchor_chars,
                    min_strong_chars=config.min_strong_chars,
                )
                analysis = _ensure_analysis_raw_sha(analysis, entry, budget)
                if persistent is not None:
                    persistent.store_analysis(entry, analysis)
                stats["analyzed_txt"] += 1
            else:
                cache.put(analysis)
                stats["cache_hit_txt"] += 1
            if not _analysis_matches_current(entry, analysis):
                raise StaleInputDuringAnalysis(path)
            analyses[path] = analysis
            if analysis.status not in {"ok", "insufficient_text", "empty_text"}:
                stats["failed_txt"] += 1
        except BodyBudgetExceeded:
            budget_exhausted = True
            stop_reasons.append("body_budget_exhausted")
            break
        except (StaleInputDuringAnalysis, OSError):
            stop_reasons.append("stale_input")
            break
        if item_index % 100 == 0 or item_index == len(txt_items):
            _emit_audit_progress(
                config, text_phase, item_index, len(txt_items), budget
            )

    epub_items = [
        (path, entry) for path, entry in sorted(unique_entries.items())
        if entry.ext == ".epub" and path not in analyses
    ]
    if epub_items:
        _emit_audit_progress(config, epub_phase, 0, len(epub_items), budget)
    for item_index, (path, entry) in enumerate(epub_items, start=1):
        try:
            if not _entry_is_current(entry):
                analyses[path] = _stale_analysis(entry)
                stats["stale_analyses"] += 1
                stop_reasons.append("stale_input")
                continue
            analysis = (
                persistent.analysis(entry, retry_deferred=retry_deferred)
                if persistent is not None else None
            )
            if analysis is None:
                epub_kwargs = {
                    "max_file_bytes": max_file_bytes,
                    "budget": budget,
                }
                if max_epub_uncompressed_bytes is not None:
                    epub_kwargs["max_uncompressed_bytes"] = max_epub_uncompressed_bytes
                evidence = inspect_epub_content(path, **epub_kwargs)
                if (
                    evidence.file_evidence.dev,
                    evidence.file_evidence.ino,
                    evidence.file_evidence.ctime_ns,
                    evidence.file_evidence.size,
                    evidence.file_evidence.mtime_ns,
                ) != _entry_identity(entry):
                    raise StaleInputDuringAnalysis(path)
                analysis = TextAnalysis(
                    path=path,
                    size=evidence.file_evidence.size,
                    mtime_ns=evidence.file_evidence.mtime_ns,
                    encoding="epub-zip",
                    lossy=False,
                    error=None,
                    raw_sha256=evidence.file_evidence.sha256,
                    normalized_sha256=evidence.content_sha256,
                    normalized_length=evidence.uncompressed_size,
                    front_anchor="",
                    tail_anchor="",
                    status="epub_content",
                    read_bytes=(
                        evidence.file_evidence.size + evidence.uncompressed_size
                    ),
                )
                if persistent is not None:
                    persistent.store_analysis(entry, analysis)
                stats["analyzed_epub"] += 1
            else:
                stats["cache_hit_epub"] += 1
            if not _analysis_matches_current(entry, analysis):
                raise StaleInputDuringAnalysis(path)
            analyses[path] = analysis
        except BodyBudgetExceeded:
            budget_exhausted = True
            stop_reasons.append("body_budget_exhausted")
            break
        except (StaleInputDuringAnalysis, OSError):
            stop_reasons.append("stale_input")
            break
        except (RuntimeError, zipfile.BadZipFile) as exc:
            stop_reasons.append("epub_analysis_error")
            stats["failed_epub"] += 1
            analyses[path] = TextAnalysis(
                path=path,
                size=entry.size,
                mtime_ns=entry.mtime_ns,
                encoding="epub-zip",
                lossy=False,
                error=str(exc),
                raw_sha256=None,
                normalized_sha256=None,
                normalized_length=0,
                front_anchor="",
                tail_anchor="",
                status="epub_error",
                read_bytes=0,
            )
        if item_index % 100 == 0 or item_index == len(epub_items):
            _emit_audit_progress(
                config, epub_phase, item_index, len(epub_items), budget
            )

    return analyses, budget, cache, stats, budget_exhausted


def _analysis_for_use(analyses, entry, stop_reasons):
    analysis = analyses.get(entry.path)
    if analysis is None:
        return None
    if not _analysis_matches_current(entry, analysis):
        analysis = _stale_analysis(entry, analysis)
        analyses[entry.path] = analysis
        stop_reasons.append("stale_input")
    return analysis


def analyze_candidates(
    candidates, config, coverage, stop_reasons, persistent=None,
    preloaded_analyses=None, budget=None, cache=None,
):
    results = {}
    budget = budget or ReadBudget(max_bytes=config.max_read_bytes)
    cache = cache or TextAnalysisCache()
    unique_entries = {}
    for candidate in candidates:
        unique_entries[candidate.left.path] = candidate.left
        unique_entries[candidate.right.path] = candidate.right

    if config.metadata_only:
        for candidate in candidates:
            results[candidate.pair_id] = _basic_result(candidate, "metadata_only", {"body_read": False})
        return list(results.values()), budget, cache, stop_reasons

    analyses = preloaded_analyses if preloaded_analyses is not None else {}
    analyses, budget, cache, _analysis_stats, budget_exhausted = _analyze_entry_set(
        unique_entries.values(),
        config,
        stop_reasons,
        persistent=persistent,
        analyses=analyses,
        budget=budget,
        cache=cache,
    )

    deep_pairs = []
    if candidates:
        _emit_audit_progress(config, "pair_classification", 0, len(candidates), budget)
    for candidate_index, candidate in enumerate(candidates, start=1):
        if candidate_index > 1 and (candidate_index - 1) % 500 == 0:
            _emit_audit_progress(
                config, "pair_classification", candidate_index - 1, len(candidates), budget
            )
        if candidate.left.ext == ".epub" and candidate.right.ext == ".epub":
            left = _analysis_for_use(analyses, candidate.left, stop_reasons)
            right = _analysis_for_use(analyses, candidate.right, stop_reasons)
            if left is None or right is None:
                results[candidate.pair_id] = _basic_result(
                    candidate, "body_budget_exhausted"
                )
                continue
            if left.status == "stale" or right.status == "stale":
                stop_reasons.append("stale_input")
                results[candidate.pair_id] = _basic_result(
                    candidate,
                    "stale",
                    {"left_status": left.status, "right_status": right.status},
                )
                continue
            if persistent is not None:
                cached_result = persistent.pair_result(candidate)
                if cached_result is not None:
                    results[candidate.pair_id] = cached_result
                    continue
            evidence = {
                "left_status": left.status,
                "right_status": right.status,
                "left_raw_sha256": left.raw_sha256,
                "right_raw_sha256": right.raw_sha256,
                "left_normalized_sha256": left.normalized_sha256,
                "right_normalized_sha256": right.normalized_sha256,
                "left_normalized_length": left.normalized_length,
                "right_normalized_length": right.normalized_length,
                "left_error": left.error,
                "right_error": right.error,
            }
            if (
                left.status == "epub_content"
                and right.status == "epub_content"
                and left.normalized_sha256
                and left.normalized_sha256 == right.normalized_sha256
            ):
                results[candidate.pair_id] = _basic_result(
                    candidate, "epub_equivalent", evidence
                )
            else:
                results[candidate.pair_id] = _basic_result(
                    candidate, "metadata_only", evidence
                )
            continue
        if candidate.left.ext != ".txt" or candidate.right.ext != ".txt":
            results[candidate.pair_id] = _basic_result(candidate, "metadata_only")
            continue
        left = _analysis_for_use(analyses, candidate.left, stop_reasons)
        right = _analysis_for_use(analyses, candidate.right, stop_reasons)
        if left is None or right is None:
            results[candidate.pair_id] = _basic_result(candidate, "body_budget_exhausted")
            continue
        if left.status == "stale" or right.status == "stale":
            stop_reasons.append("stale_input")
            results[candidate.pair_id] = _basic_result(
                candidate,
                "stale",
                {"left_status": left.status, "right_status": right.status},
            )
            continue
        if persistent is not None:
            cached_result = persistent.pair_result(candidate)
            if cached_result is not None:
                results[candidate.pair_id] = cached_result
                continue
        blocked = _status_for_pair(left, right)
        evidence = {
            "left_status": left.status,
            "right_status": right.status,
            "left_encoding": left.encoding,
            "right_encoding": right.encoding,
            "left_raw_sha256": left.raw_sha256,
            "right_raw_sha256": right.raw_sha256,
            "left_normalized_sha256": left.normalized_sha256,
            "right_normalized_sha256": right.normalized_sha256,
            "left_normalized_length": left.normalized_length,
            "right_normalized_length": right.normalized_length,
        }
        if blocked:
            results[candidate.pair_id] = _basic_result(candidate, blocked, evidence)
            if blocked in {"stale", "normalization_deferred", "oversize_deferred"}:
                stop_reasons.append(blocked)
            continue
        if left.normalized_sha256 == right.normalized_sha256:
            classification = "marker_recheck" if candidate.left.disambig != candidate.right.disambig else "text_equivalent"
            evidence["text_classification"] = "text_equivalent"
            results[candidate.pair_id] = _basic_result(candidate, classification, evidence)
            continue
        # A different upload header used to terminate here as ``different``.
        # Keep the pair inside the existing bounded deep-pair/read budgets so
        # internal and tail anchors can recover header-shifted editions.
        evidence["front_anchor_equal"] = left.front_anchor == right.front_anchor
        evidence["tail_anchor_equal"] = left.tail_anchor == right.tail_anchor
        deep_pairs.append((candidate, left, right, evidence))
    if candidates:
        _emit_audit_progress(
            config, "pair_classification", len(candidates), len(candidates), budget
        )

    if budget_exhausted:
        return list(results.values()), budget, cache, stop_reasons

    per_long = Counter()
    accepted = []
    for item in sorted(deep_pairs, key=lambda row: row[0].pair_id):
        candidate, left, right, evidence = item
        long_analysis = left if left.normalized_length >= right.normalized_length else right
        if len(accepted) >= config.max_deep_pairs or per_long[long_analysis.path] >= config.max_deep_pairs_per_file:
            results[candidate.pair_id] = _basic_result(candidate, "deep_check_deferred", evidence)
            stop_reasons.append("deep_check_deferred")
            continue
        per_long[long_analysis.path] += 1
        accepted.append(item)

    anchor_cache = {}
    grouped = defaultdict(list)
    for candidate, left, right, evidence in accepted:
        short = left if left.normalized_length <= right.normalized_length else right
        long = right if short is left else left
        anchor_length = min(config.anchor_chars, max(config.min_strong_chars, short.normalized_length // 6))
        if short.normalized_length < anchor_length * 3:
            results[candidate.pair_id] = _basic_result(candidate, "longer_unresolved", evidence)
            continue
        key = (short.path, anchor_length)
        if key not in anchor_cache:
            positions = {
                "third": max(0, short.normalized_length // 3 - anchor_length // 2),
                "two_thirds": max(0, (short.normalized_length * 2) // 3 - anchor_length // 2),
            }
            try:
                anchor_cache[key] = extract_position_anchors(
                    short.path, short, positions, anchor_chars=anchor_length, budget=budget,
                )
            except BodyBudgetExceeded:
                stop_reasons.append("body_budget_exhausted")
                results[candidate.pair_id] = _basic_result(candidate, "body_budget_exhausted", evidence)
                continue
        grouped[long.path].append((candidate, short, long, evidence, anchor_cache[key]))

    grouped_items = sorted(grouped.items())
    if grouped_items:
        _emit_audit_progress(config, "deep_scan", 0, len(grouped_items), budget)
    for group_index, (long_path, items) in enumerate(grouped_items, start=1):
        if group_index > 1 and (group_index - 1) % 25 == 0:
            _emit_audit_progress(
                config, "deep_scan", group_index - 1, len(grouped_items), budget
            )
        queries = {}
        prefix_lengths = []
        for candidate, short, _long, _evidence, anchors in items:
            queries[f"{candidate.pair_id}:tail"] = short.tail_anchor
            queries[f"{candidate.pair_id}:third"] = anchors.get("third", "")
            queries[f"{candidate.pair_id}:two_thirds"] = anchors.get("two_thirds", "")
            prefix_lengths.append(short.normalized_length)
        try:
            scan = batch_scan_normalized(
                long_path, items[0][2], queries, prefix_lengths=prefix_lengths, budget=budget,
            )
        except BodyBudgetExceeded:
            stop_reasons.append("body_budget_exhausted")
            for candidate, _short, _long, evidence, _anchors in items:
                results[candidate.pair_id] = _basic_result(candidate, "body_budget_exhausted", evidence)
            continue

        for candidate, short, long, evidence, _anchors in items:
            prefix_digest = scan.prefix_digests.get(short.normalized_length)
            if prefix_digest == short.normalized_sha256:
                evidence["prefix_digest"] = prefix_digest
                results[candidate.pair_id] = _basic_result(candidate, "contained_exact", evidence)
                continue
            positions = {
                label: scan.occurrences.get(f"{candidate.pair_id}:{label}", [])
                for label in ("third", "two_thirds", "tail")
            }
            evidence["anchor_occurrences"] = positions
            unique = all(len(positions[label]) == 1 for label in positions)
            ordered = unique and positions["third"][0] < positions["two_thirds"][0] < positions["tail"][0]
            length_delta = abs(short.normalized_length - long.normalized_length) / max(short.normalized_length, long.normalized_length)
            if evidence.get("tail_anchor_equal") and length_delta <= 0.01 and len(positions["third"]) == 1:
                classification = "near_identical"
            elif ordered:
                classification = "contained_version"
            elif any(positions.values()):
                classification = "longer_unresolved"
            elif not evidence.get("front_anchor_equal"):
                classification = "different"
            else:
                classification = "boilerplate_only"
            results[candidate.pair_id] = _basic_result(candidate, classification, evidence)
    if grouped_items:
        _emit_audit_progress(
            config, "deep_scan", len(grouped_items), len(grouped_items), budget
        )

    return [results[candidate.pair_id] for candidate in candidates if candidate.pair_id in results], budget, cache, stop_reasons


def _configuration(args):
    return {
        key: value for key, value in vars(args).items()
        if key not in {"write_report"}
    }


def run_audit(args):
    started = time.monotonic()
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    house_entries, house_invalid = load_house_entries(args.index, args.house, args.include_pass)
    temp_entries, temp_invalid = ([], []) if args.house_only else scan_temp_entries(args.temp, args.include_pass)
    entries = house_entries + temp_entries
    snapshot = _snapshot(entries)
    candidates, coverage, stop_reasons, posting_stats = generate_candidates(entries, args)
    if getattr(args, "same_coordinate_only", False):
        managed_representatives, missing_representatives = [], []
    else:
        managed_representatives, missing_representatives = _load_managed_representatives(
            entries, getattr(args, "state_db", None)
        )
    mandatory_candidates = []
    if missing_representatives:
        stop_reasons.append("managed_representative_missing")
    managed_representative_pair_count = 0
    global_fingerprint_pair_count = 0
    if getattr(args, "full_fingerprint_sweep", False):
        if getattr(args, "metadata_only", False):
            raise ValueError("--full-fingerprint-sweep cannot be metadata-only")
        if not getattr(args, "state_db", None):
            raise ValueError("--full-fingerprint-sweep requires --state-db")
        if not getattr(args, "cache_write", True):
            raise ValueError("--full-fingerprint-sweep requires cache writes")

    persistent = None
    preloaded_analyses = {}
    readonly_fingerprint_stats = Counter()
    sweep_stats = Counter()
    managed_fingerprint_stats = Counter()
    temp_fingerprint_stats = Counter()
    sweep_read_bytes = 0
    managed_preparation_read_bytes = 0
    temp_preparation_read_bytes = 0
    main_budget = ReadBudget(max_bytes=args.max_read_bytes)
    main_cache = TextAnalysisCache()
    try:
        analysis_policy_hash = _analysis_policy_hash(args)
        house_fingerprint_entries = [
            entry for entry in house_entries
            if entry.ext in {".txt", ".epub"}
        ]
        if getattr(args, "state_db", None) and getattr(args, "cache_write", True):
            persistent = PersistentAuditCache(
                args.state_db,
                entries,
                _pair_configuration_hash(args),
                analysis_policy_hash,
            )
            retry_house = bool(getattr(args, "full_fingerprint_sweep", False))
            preloaded_analyses.update(persistent.peek_many(
                house_fingerprint_entries, retry_deferred=retry_house
            ))
        elif getattr(args, "state_db", None):
            readonly_analyses, readonly_fingerprint_stats = (
                load_persisted_analyses_readonly(
                    house_fingerprint_entries,
                    args.state_db,
                    analysis_policy_hash,
                )
            )
            preloaded_analyses.update(readonly_analyses)

        if getattr(args, "full_fingerprint_sweep", False):
            sweep_budget = ReadBudget(
                max_bytes=args.full_sweep_max_read_bytes
            )
            sweep_cache = TextAnalysisCache()
            sweep_cache_hits = sum(
                entry.path in preloaded_analyses
                for entry in house_fingerprint_entries
            )
            (
                preloaded_analyses,
                sweep_budget,
                sweep_cache,
                analyzed_stats,
                _sweep_budget_exhausted,
            ) = _analyze_entry_set(
                house_fingerprint_entries,
                args,
                stop_reasons,
                persistent=persistent,
                analyses=preloaded_analyses,
                budget=sweep_budget,
                cache=sweep_cache,
                max_file_bytes=args.full_sweep_max_file_bytes,
                max_epub_uncompressed_bytes=(
                    args.full_sweep_max_epub_uncompressed_bytes
                ),
                retry_deferred=True,
                text_phase="full_sweep_text",
                epub_phase="full_sweep_epub",
            )
            sweep_stats.update(analyzed_stats)
            sweep_stats["eligible_files"] = len(house_fingerprint_entries)
            sweep_stats["cache_hits"] = sweep_cache_hits
            sweep_stats["read_bytes"] = sweep_budget.read_bytes
            sweep_stats["available_files"] = sum(
                entry.path in preloaded_analyses
                for entry in house_fingerprint_entries
            )
            sweep_stats["failed_files"] = sum(
                entry.path not in preloaded_analyses
                or (
                    entry.ext == ".txt"
                    and preloaded_analyses[entry.path].status
                    not in {"ok", "insufficient_text", "empty_text"}
                )
                or (
                    entry.ext == ".epub"
                    and preloaded_analyses[entry.path].status != "epub_content"
                )
                for entry in house_fingerprint_entries
            )
            if sweep_stats["failed_files"]:
                stop_reasons.append("full_fingerprint_sweep_incomplete")
            sweep_read_bytes = sweep_budget.read_bytes

        if managed_representatives and not args.metadata_only:
            managed_start_read_bytes = main_budget.read_bytes
            (
                preloaded_analyses,
                main_budget,
                main_cache,
                analyzed_stats,
                _managed_budget_exhausted,
            ) = _analyze_entry_set(
                managed_representatives,
                args,
                stop_reasons,
                persistent=persistent,
                analyses=preloaded_analyses,
                budget=main_budget,
                cache=main_cache,
                retry_deferred=True,
                text_phase="managed_representative_text",
            )
            managed_fingerprint_stats.update(analyzed_stats)
            managed_fingerprint_stats["eligible_files"] = len(
                managed_representatives
            )
            managed_preparation_read_bytes = (
                main_budget.read_bytes - managed_start_read_bytes
            )
            managed_fingerprint_stats["read_bytes"] = (
                managed_preparation_read_bytes
            )
            managed_fingerprint_stats["available_files"] = sum(
                entry.path in preloaded_analyses
                and preloaded_analyses[entry.path].normalized_sha256 is not None
                for entry in managed_representatives
            )

        temp_fingerprint_entries = [
            entry for entry in temp_entries
            if entry.ext in {".txt", ".epub"}
        ]
        if temp_fingerprint_entries and not args.metadata_only:
            temp_start_read_bytes = main_budget.read_bytes
            (
                preloaded_analyses,
                main_budget,
                main_cache,
                analyzed_stats,
                _temp_budget_exhausted,
            ) = _analyze_entry_set(
                temp_fingerprint_entries,
                args,
                stop_reasons,
                persistent=persistent,
                analyses=preloaded_analyses,
                budget=main_budget,
                cache=main_cache,
                retry_deferred=True,
                text_phase="temp_fingerprint_text",
                epub_phase="temp_fingerprint_epub",
            )
            temp_fingerprint_stats.update(analyzed_stats)
            temp_fingerprint_stats["eligible_files"] = len(
                temp_fingerprint_entries
            )
            temp_preparation_read_bytes = (
                main_budget.read_bytes - temp_start_read_bytes
            )
            temp_fingerprint_stats["read_bytes"] = temp_preparation_read_bytes
            temp_fingerprint_stats["available_files"] = sum(
                entry.path in preloaded_analyses
                for entry in temp_fingerprint_entries
            )
            temp_fingerprint_stats["failed_files"] = sum(
                entry.path not in preloaded_analyses
                or (
                    entry.ext == ".txt"
                    and preloaded_analyses[entry.path].status
                    not in {"ok", "insufficient_text", "empty_text"}
                )
                or (
                    entry.ext == ".epub"
                    and preloaded_analyses[entry.path].status != "epub_content"
                )
                for entry in temp_fingerprint_entries
            )

        if not getattr(args, "same_coordinate_only", False):
            mandatory_candidates, _ = generate_managed_representative_candidates(
                entries,
                getattr(args, "state_db", None),
                preloaded_analyses,
                representatives=managed_representatives,
                missing_representatives=missing_representatives,
            )
            managed_representative_pair_count = len(mandatory_candidates)

        if not getattr(args, "same_coordinate_only", False):
            fingerprint_candidates, fingerprint_coverage, fingerprint_stops = (
                generate_fingerprint_candidates(
                    entries, preloaded_analyses, args
                )
            )
            for key, value in fingerprint_coverage.items():
                coverage[key] = value
            stop_reasons.extend(fingerprint_stops)
            global_fingerprint_pair_count = len(fingerprint_candidates)
            mandatory_candidates = merge_mandatory_candidates(
                mandatory_candidates, fingerprint_candidates
            )

        candidates = merge_mandatory_candidates(candidates, mandatory_candidates)
        max_candidate_files = getattr(args, "max_candidate_files", None)
        if max_candidate_files is not None:
            selected = []
            selected_paths = set()
            deferred_pairs = 0
            deferred_paths = set()
            for candidate in candidates:
                pair_paths = {candidate.left.path, candidate.right.path}
                if len(selected_paths | pair_paths) <= max_candidate_files:
                    selected.append(candidate)
                    selected_paths.update(pair_paths)
                else:
                    deferred_pairs += 1
                    deferred_paths.update(pair_paths - selected_paths)
            if deferred_pairs:
                coverage["candidate_file_limit_deferred_pairs"] += deferred_pairs
                coverage["candidate_file_limit_deferred_files"] += len(deferred_paths)
                stop_reasons.append("candidate_file_limit")
            candidates = selected

        results, budget, cache, stop_reasons = analyze_candidates(
            candidates,
            args,
            coverage,
            stop_reasons,
            persistent=persistent,
            preloaded_analyses=preloaded_analyses,
            budget=main_budget,
            cache=main_cache,
        )
        # Candidate analysis may have created the first current-version
        # fingerprints for house files selected by title rules. Re-run the
        # bounded exact join once so cold/warm and read-only reports have
        # identical candidate reasons and any newly visible exact pair is
        # handled in the same run.
        if not getattr(args, "same_coordinate_only", False):
            post_candidates, post_coverage, post_stops = (
                generate_fingerprint_candidates(
                    entries, preloaded_analyses, args
                )
            )
            for key, value in post_coverage.items():
                coverage[key] = value
            stop_reasons.extend(post_stops)
            existing_by_id = {
                candidate.pair_id: candidate for candidate in candidates
            }
            new_candidates = []
            for candidate in post_candidates:
                existing = existing_by_id.get(candidate.pair_id)
                if existing is None:
                    new_candidates.append(candidate)
                else:
                    for reason in candidate.reasons:
                        if reason not in existing.reasons:
                            existing.reasons.append(reason)
            if new_candidates:
                extra_results, _extra_budget, _extra_cache, stop_reasons = (
                    analyze_candidates(
                        new_candidates,
                        args,
                        coverage,
                        stop_reasons,
                        persistent=persistent,
                        preloaded_analyses=preloaded_analyses,
                        budget=budget,
                        cache=cache,
                    )
                )
                results.extend(extra_results)
                candidates = merge_mandatory_candidates(
                    candidates, new_candidates
                )
            reasons_by_id = {
                candidate.pair_id: sorted(candidate.reasons)
                for candidate in candidates
            }
            for result in results:
                result.candidate_reasons = reasons_by_id[result.pair_id]
            global_fingerprint_pair_count = len(post_candidates)
            by_result_id = {result.pair_id: result for result in results}
            results = [
                by_result_id[candidate.pair_id]
                for candidate in candidates
                if candidate.pair_id in by_result_id
            ]
        changed = _snapshot_changes(snapshot)
        if changed:
            stop_reasons.append("stale")
        safe_candidates, results = _without_changed_inputs(
            candidates, results, changed
        )
        if persistent is not None:
            persistent.store_pair_results(safe_candidates, results)
        persistent_stats = Counter(readonly_fingerprint_stats)
        if persistent is not None:
            persistent_stats.update(persistent.stats)
    finally:
        if persistent is not None:
            persistent.close()
    final_changed = _snapshot_changes(snapshot)
    changed_by_path = {item["path"]: item for item in changed}
    changed_by_path.update({item["path"]: item for item in final_changed})
    changed = [changed_by_path[path] for path in sorted(changed_by_path)]
    _, results = _without_changed_inputs(candidates, results, changed)
    invalid_records = house_invalid + temp_invalid
    if changed:
        stop_reasons.append("stale")
    if any(item.get("reason") in {"invalid_path", "missing_path", "not_file"} for item in invalid_records):
        stop_reasons.append("invalid_path")
    stop_reasons = sorted(set(stop_reasons))
    coverage_reasons = sorted(
        key for key in ("high_frequency_grams", "short_core_no_fuzzy", "neighbor_truncated", "topk_truncated")
        if coverage.get(key)
    )
    counts = Counter(result.classification for result in results)
    unique_candidate_paths = {candidate.left.path for candidate in candidates} | {candidate.right.path for candidate in candidates}
    unique_candidate_bytes = sum(os.path.getsize(path) for path in unique_candidate_paths if os.path.exists(path))
    unique_txt_paths = {
        entry.path for candidate in candidates for entry in (candidate.left, candidate.right)
        if entry.ext == ".txt"
    }
    unique_txt_bytes = sum(os.path.getsize(path) for path in unique_txt_paths if os.path.exists(path))
    stats = {
        "house_entries": len(house_entries),
        "temp_entries": len(temp_entries),
        "candidate_pairs": len(candidates),
        "managed_representative_pairs": managed_representative_pair_count,
        "managed_representatives_missing": len(missing_representatives),
        "global_fingerprint_pairs": global_fingerprint_pair_count,
        "result_pairs": len(results),
        "classification_counts": dict(sorted(counts.items())),
        "coverage_counts": dict(sorted(coverage.items())),
        "unique_candidate_files": len(unique_candidate_paths),
        "unique_candidate_bytes": unique_candidate_bytes,
        "unique_txt_files": len(unique_txt_paths),
        "unique_txt_bytes": unique_txt_bytes,
        "estimated_min_read_bytes": unique_txt_bytes,
        "estimated_max_read_bytes": unique_txt_bytes * MAX_ESTIMATED_READ_PASSES,
        "actual_read_bytes": sweep_read_bytes + budget.read_bytes,
        "candidate_analysis_read_bytes": (
            budget.read_bytes
            - managed_preparation_read_bytes
            - temp_preparation_read_bytes
        ),
        "fingerprint_preparation_read_bytes": (
            sweep_read_bytes
            + managed_preparation_read_bytes
            + temp_preparation_read_bytes
        ),
        "analysis_cache_entries": len(preloaded_analyses),
        "full_fingerprint_sweep_requested": bool(
            getattr(args, "full_fingerprint_sweep", False)
        ),
        "full_fingerprint_sweep_eligible_files": sweep_stats.get(
            "eligible_files", 0
        ),
        "full_fingerprint_sweep_available_files": sweep_stats.get(
            "available_files", 0
        ),
        "full_fingerprint_sweep_cache_hits": sweep_stats.get("cache_hits", 0),
        "full_fingerprint_sweep_analyzed_files": (
            sweep_stats.get("analyzed_txt", 0)
            + sweep_stats.get("analyzed_epub", 0)
        ),
        "full_fingerprint_sweep_failed_files": sweep_stats.get(
            "failed_files", 0
        ),
        "full_fingerprint_sweep_read_bytes": sweep_stats.get("read_bytes", 0),
        "managed_representative_fingerprint_eligible_files": (
            managed_fingerprint_stats.get("eligible_files", 0)
        ),
        "managed_representative_fingerprint_available_files": (
            managed_fingerprint_stats.get("available_files", 0)
        ),
        "managed_representative_fingerprint_analyzed_files": (
            managed_fingerprint_stats.get("analyzed_txt", 0)
        ),
        "managed_representative_fingerprint_read_bytes": (
            managed_fingerprint_stats.get("read_bytes", 0)
        ),
        "temp_fingerprint_eligible_files": temp_fingerprint_stats.get(
            "eligible_files", 0
        ),
        "temp_fingerprint_available_files": temp_fingerprint_stats.get(
            "available_files", 0
        ),
        "temp_fingerprint_analyzed_files": (
            temp_fingerprint_stats.get("analyzed_txt", 0)
            + temp_fingerprint_stats.get("analyzed_epub", 0)
        ),
        "temp_fingerprint_failed_files": temp_fingerprint_stats.get(
            "failed_files", 0
        ),
        "temp_fingerprint_read_bytes": temp_fingerprint_stats.get(
            "read_bytes", 0
        ),
        "fingerprint_cache_hits": persistent_stats.get("fingerprint_cache_hits", 0),
        "fingerprint_cache_misses": persistent_stats.get("fingerprint_cache_misses", 0),
        "fingerprint_cache_peek_misses": persistent_stats.get(
            "fingerprint_cache_peek_misses", 0
        ),
        "fingerprint_stale_inputs": persistent_stats.get("fingerprint_stale_inputs", 0),
        "pair_cache_hits": persistent_stats.get("pair_cache_hits", 0),
        "pair_cache_misses": persistent_stats.get("pair_cache_misses", 0),
        "review_items_created": persistent_stats.get("review_items_created", 0),
        "distinct_volume_reviews_suppressed": persistent_stats.get(
            "distinct_volume_reviews_suppressed", 0
        ),
        "side_story_volume_reviews_suppressed": persistent_stats.get(
            "side_story_volume_reviews_suppressed", 0
        ),
        "cross_core_reviews_suppressed": persistent_stats.get(
            "cross_core_reviews_suppressed", 0
        ),
        "stale_open_reviews_superseded": persistent_stats.get(
            "stale_open_reviews_superseded", 0
        ),
        "human_disposition_cache_hits": persistent_stats.get(
            "human_disposition_cache_hits", 0
        ),
        "input_changes": changed,
        **posting_stats,
    }
    completed = not stop_reasons
    report = AuditReport(
        started_at=started_at,
        duration_seconds=round(time.monotonic() - started, 3),
        completed=completed,
        coverage_limited=bool(coverage_reasons),
        coverage_reasons=coverage_reasons,
        stop_reasons=stop_reasons,
        stats=stats,
        results=[asdict(result) for result in results],
        invalid_records=invalid_records,
        configuration=_configuration(args),
    )
    return report


def _report_directory(args):
    temp = Path(args.temp).expanduser().resolve()
    allowed = (temp / "dedup_logs").resolve()
    requested = Path(args.report_dir).expanduser().resolve() if args.report_dir else allowed
    if not _within(allowed, requested):
        raise ValueError("--report-dir must resolve inside <temp>/dedup_logs")
    return requested


def _text_report(report, include_details=True):
    counts = report.stats["classification_counts"]
    lines = [
        f"강력 후보 감사: house {report.stats['house_entries']}개 / temp {report.stats['temp_entries']}개 / "
        f"메타 후보 {report.stats['candidate_pairs']}쌍 / 결과 {report.stats['result_pairs']}쌍",
    ]
    for key in (
        "text_equivalent", "epub_equivalent", "near_identical", "contained_exact", "contained_version",
        "marker_recheck", "boilerplate_only", "longer_unresolved", "metadata_only",
        "decode_lossy", "empty_text", "insufficient_text", "oversize_deferred",
        "normalization_deferred", "deep_check_deferred", "body_budget_exhausted", "different",
    ):
        lines.append(f"  {key}: {counts.get(key, 0)}쌍")
    lines.extend([
        f"  분석 고유 파일: {report.stats['unique_candidate_files']}개 / 실제 read: {report.stats['actual_read_bytes']} bytes",
        f"  예상 read: {report.stats['estimated_min_read_bytes']}..{report.stats['estimated_max_read_bytes']} bytes",
        f"  completed: {str(report.completed).lower()}",
        f"  coverage_limited: {str(report.coverage_limited).lower()}",
        f"  coverage_reasons: {report.coverage_reasons}",
        f"  stop_reasons: {report.stop_reasons}",
        "  completed=true는 설정된 bounded heuristic 완료이며 모든 파일쌍 전수조사를 뜻하지 않습니다.",
        "  모든 auditor_aux 결과는 report-only이며 이동·삭제·리네임 명령이 아닙니다.",
        "",
    ])
    if include_details:
        for result in report.results:
            lines.append(f"[{result['classification']}] {result['pair_id']}")
            lines.append(f"  A({result['left']['source']}): {result['left']['rel_path']}")
            lines.append(f"  B({result['right']['source']}): {result['right']['rel_path']}")
            lines.append(f"  reasons: {result['candidate_reasons']}")
    return "\n".join(lines) + "\n"


def write_reports(report, args):
    directory = _report_directory(args)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = directory / f"strong_candidates_{stamp}"
    text_path = base.with_suffix(".txt")
    json_path = base.with_suffix(".json")
    text_path.write_text(_text_report(report, include_details=True), encoding="utf-8")
    json_path.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return text_path, json_path


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_positive_args(args, parser)
    args.progress = True
    try:
        report = run_audit(args)
        print(_text_report(report, include_details=False), end="")
        if args.write_report:
            text_path, json_path = write_reports(report, args)
            print(f"리포트: {text_path}")
            print(f"JSON: {json_path}")
        return 0 if report.completed else 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"감사 실패: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
