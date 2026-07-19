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
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
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


# 한 파일은 인코딩 확정 중 최대 3회, deep 검사에서 short/long 역할로 각 1회 읽힐 수 있다.
MAX_ESTIMATED_READ_PASSES = 5
# v2: BOM UTF-16 LE/BE strict 판독을 fingerprint 의미에 포함한다.
# 판독 규칙이 바뀌면 기존 decode_lossy 결과를 재사용하지 않도록 반드시 올린다.
FINGERPRINT_VERSION = "2"
AUDITOR_VERSION = "1.2.2"
MANAGED_REPRESENTATIVE_MODE = "normalized_sha_join"
SUPPORTS_READ_ONLY_CACHE = True


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
    pass_recheck: bool = False


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
    parser.add_argument("--include-pass", action="store_true")
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--max-file-bytes", type=parse_binary_size, default=DEFAULT_MAX_FILE_BYTES)
    parser.add_argument("--max-read-bytes", type=parse_binary_size, default=DEFAULT_MAX_READ_BYTES)
    parser.add_argument("--max-candidates", type=int, default=50_000)
    parser.add_argument("--max-core-group-pairs", type=int, default=5_000)
    parser.add_argument("--max-neighbors-per-entry", type=int, default=1_024)
    parser.add_argument("--max-title-checks-per-entry", type=int, default=24)
    parser.add_argument("--max-deep-pairs", type=int, default=5_000)
    parser.add_argument("--max-deep-pairs-per-file", type=int, default=24)
    parser.add_argument("--anchor-chars", type=int, default=DEFAULT_ANCHOR_CHARS)
    parser.add_argument("--min-strong-chars", type=int, default=MIN_STRONG_TEXT_CHARS)
    return parser


def _validate_positive_args(args, parser):
    for name in (
        "max_candidates", "max_core_group_pairs", "max_neighbors_per_entry",
        "max_title_checks_per_entry", "max_deep_pairs", "max_deep_pairs_per_file",
        "anchor_chars", "min_strong_chars",
    ):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")


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


def _entry_from_stat(source, name, rel_path, path, recorded_size=None, pass_recheck=False):
    stat = os.stat(path, follow_symlinks=False)
    info = analyze_name(name)
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
    invalid = []
    entries = []
    root = Path(house_root).expanduser().resolve()
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
    return {value[index:index + 3] for index in range(max(0, len(value) - 2))}


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
            if shared < 2:
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
            if contained or similarity >= 0.72:
                add(entry, target, "metadata_leak" if contained and entry.core_title != target.core_title else "near_core")

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


def generate_managed_representative_candidates(entries, state_db_path):
    """Hash-join new temp TXT with active managed representatives."""
    if not state_db_path or not os.path.isfile(state_db_path):
        return [], []
    import decision_store

    conn = decision_store.connect_state_db_readonly(state_db_path)
    try:
        representative_rows = list(conn.execute(
                """
                SELECT f.canonical_path, fp.normalized_sha256,
                       fp.dev, fp.ino, fp.ctime_ns, fp.size, fp.mtime_ns
                FROM representatives AS r
                JOIN files AS f ON f.file_id = r.file_id
                LEFT JOIN fingerprints AS fp
                  ON fp.fingerprint_id = f.current_fingerprint_id
                WHERE f.active = 1 AND f.assignment_state = 'managed'
                """
            ))
        cached_rows = list(conn.execute(
                """
                SELECT f.canonical_path, fp.normalized_sha256,
                       fp.dev, fp.ino, fp.ctime_ns, fp.size, fp.mtime_ns
                FROM files AS f JOIN fingerprints AS fp
                  ON fp.fingerprint_id = f.current_fingerprint_id
                WHERE f.active = 1 AND fp.normalized_sha256 IS NOT NULL
                """
            ))
    finally:
        conn.close()

    def current_hashes(rows):
        values = {}
        for row in rows:
            try:
                current = os.stat(row[0], follow_symlinks=False)
            except OSError:
                continue
            if (
                row[2], row[3], row[4], row[5], row[6]
            ) == (
                current.st_dev, current.st_ino, current.st_ctime_ns,
                current.st_size, current.st_mtime_ns,
            ):
                values[decision_store.canonicalize_path(row[0])] = row[1]
        return values

    representative_paths = {
        decision_store.canonicalize_path(row[0]) for row in representative_rows
    }
    representative_hashes = current_hashes(representative_rows)
    cached_hashes = current_hashes(cached_rows)
    representatives = [
        entry for entry in entries
        if entry.ext == ".txt"
        and decision_store.canonicalize_path(entry.path) in representative_paths
    ]
    input_representative_paths = {
        decision_store.canonicalize_path(entry.path) for entry in representatives
    }
    missing_representatives = sorted(representative_paths - input_representative_paths)
    temp_entries = [entry for entry in entries if entry.source == "temp" and entry.ext == ".txt"]
    from mutation_io import inspect_normalized_text
    representatives_by_hash = defaultdict(list)
    for representative in representatives:
        canonical = decision_store.canonicalize_path(representative.path)
        normalized = representative_hashes.get(canonical)
        if not normalized:
            try:
                _, normalized = inspect_normalized_text(representative.path)
            except (OSError, RuntimeError):
                continue
        representatives_by_hash[normalized].append(representative)
    candidates = []
    for candidate_entry in sorted(temp_entries, key=_endpoint):
        canonical = decision_store.canonicalize_path(candidate_entry.path)
        normalized = cached_hashes.get(canonical)
        if not normalized:
            try:
                _, normalized = inspect_normalized_text(candidate_entry.path)
            except (OSError, RuntimeError):
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
        elif "managed_representative_full_scan" not in existing.reasons:
            existing.reasons.append("managed_representative_full_scan")
    return sorted(merged.values(), key=lambda candidate: candidate.pair_id)


def _entry_public(entry):
    data = asdict(entry)
    data.pop("path", None)
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


def _snapshot(entries):
    return {entry.path: (entry.size, entry.mtime_ns) for entry in entries}


def _snapshot_changes(snapshot):
    changed = []
    for path, before in snapshot.items():
        try:
            stat = os.stat(path, follow_symlinks=False)
            after = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            after = None
        if before != after:
            changed.append({"path": path, "before": before, "after": after})
    return changed


class PersistentAuditCache:
    def __init__(self, state_db_path, entries, configuration_hash):
        import decision_store

        self.store = decision_store
        self.conn = decision_store.initialize_state_db(state_db_path)
        self.configuration_hash = configuration_hash
        self.analysis_policy_hash = configuration_hash
        self.fingerprint_version = f"{FINGERPRINT_VERSION}:{configuration_hash}"
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

    def analysis(self, entry):
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
        if row is None:
            self.pending_identities[entry.path] = self._identity(current)
            self.stats["fingerprint_cache_misses"] += 1
            return None
        self.pending_identities.pop(entry.path, None)
        metadata = json.loads(row["anchors_json"] or "{}")
        analysis = TextAnalysis(
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
        self.fingerprint_ids[entry.path] = row["fingerprint_id"]
        self.stats["fingerprint_cache_hits"] += 1
        return analysis

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
            "text_equivalent", "marker_recheck", "near_identical", "contained_exact",
            "contained_version", "longer_unresolved", "boilerplate_only", "different",
            "decode_lossy", "empty_text", "insufficient_text", "metadata_only",
        }
        by_pair = {result.pair_id: result for result in results}
        with self.store.transaction(self.conn):
            for candidate in candidates:
                result = by_pair.get(candidate.pair_id)
                if result is None or result.classification not in stable:
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
            "text_equivalent", "marker_recheck", "near_identical", "contained_exact",
            "contained_version", "longer_unresolved", "decode_lossy",
            "metadata_only", "insufficient_text",
        }
        if result.classification not in reviewable:
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


def analyze_candidates(candidates, config, coverage, stop_reasons, persistent=None):
    results = {}
    budget = ReadBudget(max_bytes=config.max_read_bytes)
    cache = TextAnalysisCache()
    unique_entries = {}
    for candidate in candidates:
        unique_entries[candidate.left.path] = candidate.left
        unique_entries[candidate.right.path] = candidate.right

    if config.metadata_only:
        for candidate in candidates:
            results[candidate.pair_id] = _basic_result(candidate, "metadata_only", {"body_read": False})
        return list(results.values()), budget, cache, stop_reasons

    analyses = {}
    budget_exhausted = False
    txt_items = [(path, entry) for path, entry in sorted(unique_entries.items()) if entry.ext == ".txt"]
    for item_index, (path, entry) in enumerate(txt_items, start=1):
        try:
            analysis = persistent.analysis(entry) if persistent is not None else None
            if analysis is None:
                analysis = cache.analyze(
                    path,
                    budget=budget,
                    max_file_bytes=config.max_file_bytes,
                    anchor_chars=config.anchor_chars,
                    min_strong_chars=config.min_strong_chars,
                )
                if persistent is not None:
                    persistent.store_analysis(entry, analysis)
            else:
                cache.put(analysis)
            analyses[path] = analysis
        except BodyBudgetExceeded:
            budget_exhausted = True
            stop_reasons.append("body_budget_exhausted")
            break
        except (StaleInputDuringAnalysis, OSError):
            stop_reasons.append("stale_input")
            break
        if getattr(config, "progress", False) and (item_index % 100 == 0 or item_index == len(txt_items)):
            print(
                f"  ... 본문 기본 분석 {item_index}/{len(txt_items)} "
                f"({budget.read_bytes / (1024 ** 3):.2f} GiB read)",
                flush=True,
            )

    deep_pairs = []
    for candidate in candidates:
        if candidate.left.ext != ".txt" or candidate.right.ext != ".txt":
            results[candidate.pair_id] = _basic_result(candidate, "metadata_only")
            continue
        left = analyses.get(candidate.left.path)
        right = analyses.get(candidate.right.path)
        if left is None or right is None:
            results[candidate.pair_id] = _basic_result(candidate, "body_budget_exhausted")
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
        if left.front_anchor != right.front_anchor:
            results[candidate.pair_id] = _basic_result(candidate, "different", evidence)
            continue
        evidence["front_anchor_equal"] = True
        evidence["tail_anchor_equal"] = left.tail_anchor == right.tail_anchor
        deep_pairs.append((candidate, left, right, evidence))

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

    for long_path, items in sorted(grouped.items()):
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
            else:
                classification = "boilerplate_only"
            results[candidate.pair_id] = _basic_result(candidate, classification, evidence)

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
    mandatory_candidates, missing_representatives = generate_managed_representative_candidates(
        entries, getattr(args, "state_db", None)
    )
    if missing_representatives:
        stop_reasons.append("managed_representative_missing")
    candidates = merge_mandatory_candidates(candidates, mandatory_candidates)
    persistent = None
    try:
        if getattr(args, "state_db", None) and getattr(args, "cache_write", True):
            persistent = PersistentAuditCache(
                args.state_db, entries, _pair_configuration_hash(args)
            )
        results, budget, cache, stop_reasons = analyze_candidates(
            candidates, args, coverage, stop_reasons, persistent=persistent
        )
        if persistent is not None:
            persistent.store_pair_results(candidates, results)
        persistent_stats = dict(persistent.stats) if persistent is not None else {}
    finally:
        if persistent is not None:
            persistent.close()
    changed = _snapshot_changes(snapshot)
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
        "managed_representative_pairs": len(mandatory_candidates),
        "managed_representatives_missing": len(missing_representatives),
        "result_pairs": len(results),
        "classification_counts": dict(sorted(counts.items())),
        "coverage_counts": dict(sorted(coverage.items())),
        "unique_candidate_files": len(unique_candidate_paths),
        "unique_candidate_bytes": unique_candidate_bytes,
        "unique_txt_files": len(unique_txt_paths),
        "unique_txt_bytes": unique_txt_bytes,
        "estimated_min_read_bytes": unique_txt_bytes,
        "estimated_max_read_bytes": unique_txt_bytes * MAX_ESTIMATED_READ_PASSES,
        "actual_read_bytes": budget.read_bytes,
        "analysis_cache_entries": len(cache._items),
        "fingerprint_cache_hits": persistent_stats.get("fingerprint_cache_hits", 0),
        "fingerprint_cache_misses": persistent_stats.get("fingerprint_cache_misses", 0),
        "fingerprint_stale_inputs": persistent_stats.get("fingerprint_stale_inputs", 0),
        "pair_cache_hits": persistent_stats.get("pair_cache_hits", 0),
        "pair_cache_misses": persistent_stats.get("pair_cache_misses", 0),
        "review_items_created": persistent_stats.get("review_items_created", 0),
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
        "text_equivalent", "near_identical", "contained_exact", "contained_version",
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
