"""Suppress weak review edges between structurally distinct EPUB books."""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

import decision_store
from mutation_io import mutation_lock_for_roots
from normalizer import extract_volume_number, is_side_story
from project_paths import HOUSE_DIR, STATE_DB, TEMP_DIR
from epub_review_context import metadata_only_epub_context_reason


SUPPRESSION_REASON = "distinct_terminal_epub_volume"
CORE_SUPPRESSION_REASON = "cross_core_metadata_only"
SIDE_STORY_SUPPRESSION_REASON = "side_story_vs_numbered_volume"
STRUCTURAL_SUPPRESSION_REASON = "structurally_distinct_epub_books"
CROSS_CORE_DECODE_SUPPRESSION_REASON = "cross_core_decode_lossy"
STALE_EVIDENCE_SUPPRESSION_REASON = "stale_open_review_evidence"
INACTIVE_ENDPOINT_SUPPRESSION_REASON = "inactive_review_endpoint"
DUPLICATE_SUPPRESSION_REASON = "stale_duplicate_open_review"
WEAKER_RELATION_SUPPRESSION_REASON = "weaker_open_review_relation"
SUPPRESSION_VERSION = "1.5.4-context-v2"


# Current evidence always wins before this ordering is considered.  The rank
# is used only to collapse multiple open rows for the same unordered file pair;
# superseded rows retain their evidence for audit/recovery.
_REVIEW_CLASSIFICATION_PRIORITY = {
    "text_equivalent": 100,
    "epub_equivalent": 100,
    "ordered_body_match": 95,
    "contained_exact": 90,
    "near_identical": 85,
    "contained_version": 80,
    "ordered_body_review": 75,
    "epub_package_variant": 70,
    "longer_unresolved": 60,
    "marker_recheck": 55,
    "decode_lossy": 30,
    "insufficient_text": 20,
    "metadata_only": 10,
}


def terminal_epub_volume(name: str) -> tuple[str, int] | None:
    """Return a conservative base/volume pair for ``Title 05.epub`` names."""
    path = Path(str(name))
    if path.suffix.casefold() != ".epub":
        return None
    stem = unicodedata.normalize("NFKC", path.stem).strip()
    match = re.fullmatch(r"(.+?)\s+(0*[1-9]\d{0,2})", stem)
    if match is None:
        return None
    base = re.sub(r"[^0-9A-Za-z가-힣]+", "", match.group(1)).casefold()
    if not base:
        return None
    return base, int(match.group(2))


def distinct_terminal_epub_volumes(left_name: str, right_name: str) -> bool:
    left = terminal_epub_volume(left_name)
    right = terminal_epub_volume(right_name)
    return bool(left and right and left[0] == right[0] and left[1] != right[1])


def side_story_vs_numbered_epub_volume(left_name: str, right_name: str) -> bool:
    """Return true for a standalone side story paired with a numbered volume.

    This is deliberately limited to EPUB and weak ``metadata_only`` callers.
    Strong body-equivalence classifications remain reviewable even when one
    filename contains ``외전``.
    """
    left_path = Path(str(left_name))
    right_path = Path(str(right_name))
    if left_path.suffix.casefold() != ".epub" or right_path.suffix.casefold() != ".epub":
        return False
    left_side = is_side_story(left_path.name)
    right_side = is_side_story(right_path.name)
    if left_side == right_side:
        return False
    numbered_name = right_path.name if left_side else left_path.name
    return extract_volume_number(numbered_name) is not None


def different_core_titles(left_core: str, right_core: str) -> bool:
    left = unicodedata.normalize("NFC", str(left_core or "")).strip()
    right = unicodedata.normalize("NFC", str(right_core or "")).strip()
    return bool(left and right and left != right)


def structurally_distinct_epub_books(left_name: str, right_name: str) -> bool:
    """Return true only when two EPUB names declare incompatible coordinates.

    This is intentionally limited to weak ``metadata_only`` evidence. Strong
    body/content proofs are evaluated before this helper and remain actionable.
    Both sides must expose a current, unambiguous coordinate; a missing or
    one-sided coordinate stays manual instead of being guessed away.
    """
    left_path = Path(str(left_name))
    right_path = Path(str(right_name))
    if left_path.suffix.casefold() != ".epub" or right_path.suffix.casefold() != ".epub":
        return False
    left = decision_store.coordinate_fields_from_name(left_path.name)
    right = decision_store.coordinate_fields_from_name(right_path.name)
    if (
        left.get("span_ambiguous")
        or right.get("span_ambiguous")
        or left.get("coordinate_kind") is None
        or right.get("coordinate_kind") is None
    ):
        return False
    return not decision_store.coordinates_compatible(left, right)


def diagnostic_only_review_reason(
    classification: str,
    *,
    left_name: str,
    right_name: str,
    left_core: str,
    right_core: str,
    left_path: str | None = None,
    right_path: str | None = None,
    metadata_titles=None,
) -> str | None:
    """Classify only evidence that cannot justify an actionable review row."""
    if classification == "decode_lossy" and different_core_titles(
        left_core, right_core
    ):
        return CROSS_CORE_DECODE_SUPPRESSION_REASON
    if classification != "metadata_only":
        return None
    if distinct_terminal_epub_volumes(left_name, right_name):
        return SUPPRESSION_REASON
    if side_story_vs_numbered_epub_volume(left_name, right_name):
        return SIDE_STORY_SUPPRESSION_REASON
    if different_core_titles(left_core, right_core):
        return CORE_SUPPRESSION_REASON
    if structurally_distinct_epub_books(left_name, right_name):
        return STRUCTURAL_SUPPRESSION_REASON
    return metadata_only_epub_context_reason(
        left_path or left_name, right_path or right_name, metadata_titles,
    )


def find_open_review_noise(conn) -> list[dict]:
    """Find only unqueued active diagnostic rows with current file metadata."""
    rows = conn.execute(
        """
        SELECT r.review_id, r.classification, r.state, r.queue_path, r.evidence_json,
               candidate.file_id AS candidate_file_id,
               candidate.canonical_path AS candidate_path,
               reference.file_id AS reference_file_id,
               reference.canonical_path AS reference_path,
               candidate_analysis.core_title AS candidate_core_title,
               reference_analysis.core_title AS reference_core_title,
               CASE
                 WHEN r.left_fingerprint_id = candidate.current_fingerprint_id
                  AND r.right_fingerprint_id = reference.current_fingerprint_id
                 THEN 1 ELSE 0
               END AS current_evidence
        FROM review_items AS r
        JOIN files AS candidate ON candidate.file_id = r.candidate_file_id
        JOIN files AS reference ON reference.file_id = r.reference_file_id
        LEFT JOIN file_analysis AS candidate_analysis
          ON candidate_analysis.file_id = candidate.file_id
        LEFT JOIN file_analysis AS reference_analysis
          ON reference_analysis.file_id = reference.file_id
        WHERE r.state IN ('pending', 'deferred')
          AND r.classification IN ('metadata_only', 'decode_lossy')
          AND (r.queue_path IS NULL OR r.queue_path = '')
          AND candidate.active = 1 AND reference.active = 1
        ORDER BY r.review_id
        """
    ).fetchall()
    result = []
    for row in rows:
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            evidence = {}
        reason = diagnostic_only_review_reason(
            row["classification"],
            left_name=Path(row["candidate_path"]).name,
            right_name=Path(row["reference_path"]).name,
            left_core=row["candidate_core_title"],
            right_core=row["reference_core_title"],
            left_path=row["candidate_path"],
            right_path=row["reference_path"],
            metadata_titles=(
                evidence.get("epub_metadata_titles")
                if row["current_evidence"] and isinstance(evidence, dict) else None
            ),
        )
        if reason is None:
            continue
        candidate = terminal_epub_volume(Path(row["candidate_path"]).name)
        reference = terminal_epub_volume(Path(row["reference_path"]).name)
        result.append({
            "review_id": row["review_id"],
            "classification": row["classification"],
            "state": row["state"],
            "candidate_file_id": row["candidate_file_id"],
            "candidate_path": row["candidate_path"],
            "candidate_volume": candidate[1] if candidate else None,
            "candidate_side_story": is_side_story(Path(row["candidate_path"]).name),
            "candidate_core_title": row["candidate_core_title"],
            "reference_file_id": row["reference_file_id"],
            "reference_path": row["reference_path"],
            "reference_volume": reference[1] if reference else None,
            "reference_side_story": is_side_story(Path(row["reference_path"]).name),
            "reference_core_title": row["reference_core_title"],
            "suppression_reason": reason,
            "evidence_json": row["evidence_json"],
        })
    return result


def find_stale_open_reviews(conn, *, excluded_review_ids=()) -> list[dict]:
    """Find unqueued rows that cannot describe the current active endpoints."""
    excluded = {int(review_id) for review_id in excluded_review_ids}
    rows = conn.execute(
        """
        SELECT r.review_id, r.classification, r.evidence_json,
               candidate.active AS candidate_active,
               reference.active AS reference_active,
               CASE
                 WHEN r.left_fingerprint_id = candidate.current_fingerprint_id
                  AND r.right_fingerprint_id = reference.current_fingerprint_id
                 THEN 1 ELSE 0
               END AS current_evidence
        FROM review_items AS r
        JOIN files AS candidate ON candidate.file_id = r.candidate_file_id
        JOIN files AS reference ON reference.file_id = r.reference_file_id
        WHERE r.state IN ('pending', 'deferred')
          AND (r.queue_path IS NULL OR r.queue_path = '')
        ORDER BY r.review_id
        """
    ).fetchall()
    result = []
    for row in rows:
        if row["review_id"] in excluded:
            continue
        if row["candidate_active"] and row["reference_active"] and row["current_evidence"]:
            continue
        result.append({
            "review_id": row["review_id"],
            "classification": row["classification"],
            "evidence_json": row["evidence_json"],
            "suppression_reason": (
                STALE_EVIDENCE_SUPPRESSION_REASON
                if row["candidate_active"] and row["reference_active"]
                else INACTIVE_ENDPOINT_SUPPRESSION_REASON
            ),
        })
    return result


def find_redundant_open_reviews(conn, *, excluded_review_ids=()) -> list[dict]:
    """Keep one current, queued, strongest row for each open unordered pair."""
    excluded = {int(review_id) for review_id in excluded_review_ids}
    rows = conn.execute(
        """
        SELECT r.review_id, r.classification, r.evidence_json, r.queue_path,
               r.candidate_file_id, r.reference_file_id,
               CASE
                 WHEN r.left_fingerprint_id = candidate.current_fingerprint_id
                  AND r.right_fingerprint_id = reference.current_fingerprint_id
                 THEN 1 ELSE 0
               END AS current_evidence
        FROM review_items AS r
        JOIN files AS candidate ON candidate.file_id = r.candidate_file_id
        JOIN files AS reference ON reference.file_id = r.reference_file_id
        WHERE r.state IN ('pending', 'deferred')
        ORDER BY r.review_id
        """
    ).fetchall()
    groups = {}
    for row in rows:
        if row["review_id"] in excluded:
            continue
        pair = tuple(sorted((row["candidate_file_id"], row["reference_file_id"])))
        groups.setdefault(pair, []).append(row)
    redundant = []
    for _pair, group in groups.items():
        if len(group) < 2:
            continue
        keep = max(
            group,
            key=lambda row: (
                int(row["current_evidence"]),
                int(bool(row["queue_path"])),
                _REVIEW_CLASSIFICATION_PRIORITY.get(row["classification"], 0),
                int(row["review_id"]),
            ),
        )
        for row in group:
            if row["review_id"] == keep["review_id"]:
                continue
            # A physical queue entry needs an explicit file disposition. Never
            # orphan it merely because another database row is stronger.
            if row["queue_path"]:
                continue
            redundant.append({
                "review_id": row["review_id"],
                "classification": row["classification"],
                "keep_review_id": keep["review_id"],
                "keep_classification": keep["classification"],
                "suppression_reason": (
                    DUPLICATE_SUPPRESSION_REASON
                    if row["classification"] == keep["classification"]
                    else WEAKER_RELATION_SUPPRESSION_REASON
                ),
                "evidence_json": row["evidence_json"],
            })
    return sorted(redundant, key=lambda row: row["review_id"])


def plan_open_review_reconciliation(conn) -> dict:
    """Plan coverage-independent review cleanup without closing unseen pairs."""
    noise = find_open_review_noise(conn)
    noise_ids = [row["review_id"] for row in noise]
    stale = find_stale_open_reviews(conn, excluded_review_ids=noise_ids)
    excluded = [*noise_ids, *(row["review_id"] for row in stale)]
    redundant = find_redundant_open_reviews(
        conn, excluded_review_ids=excluded
    )
    return {
        "noise": noise,
        "stale": stale,
        "redundant": redundant,
    }


def _suppressed_evidence(row: dict) -> str:
    try:
        evidence = json.loads(row.get("evidence_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        evidence = {"previous_evidence": row.get("evidence_json")}
    marker = {
        "reason": row["suppression_reason"],
        "version": SUPPRESSION_VERSION,
    }
    for key in (
        "candidate_volume", "candidate_side_story", "candidate_core_title",
        "reference_volume", "reference_side_story", "reference_core_title",
        "keep_review_id", "keep_classification",
    ):
        if key in row:
            marker[key] = row[key]
    evidence["automatic_suppression"] = marker
    return json.dumps(evidence, ensure_ascii=False, sort_keys=True)


def reconcile_open_reviews(conn) -> dict:
    """Supersede only provably diagnostic, stale, or weaker unqueued rows.

    Candidate coverage is deliberately irrelevant: a current actionable pair
    that was not visited in this audit is left open. This makes the pass safe
    even when the auditor reports ``coverage_limited=true``.
    """
    plan = plan_open_review_reconciliation(conn)
    changed = {"noise": 0, "stale": 0, "redundant": 0}
    for category, rows in plan.items():
        for row in rows:
            cursor = conn.execute(
                """
                UPDATE review_items
                SET state = 'superseded', decision_id = NULL,
                    evidence_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE review_id = ? AND state IN ('pending', 'deferred')
                  AND (queue_path IS NULL OR queue_path = '')
                """,
                (_suppressed_evidence(row), row["review_id"]),
            )
            changed[category] += cursor.rowcount
    return {
        "planned_noise_superseded": len(plan["noise"]),
        "planned_stale_superseded": len(plan["stale"]),
        "planned_duplicate_superseded": len(plan["redundant"]),
        "noise_superseded": changed["noise"],
        "stale_superseded": changed["stale"],
        "duplicate_superseded": changed["redundant"],
        "superseded": sum(changed.values()),
        "review_ids": [
            row["review_id"]
            for category in ("noise", "stale", "redundant")
            for row in plan[category]
        ],
        "items": plan["noise"],
        "stale_items": plan["stale"],
        "duplicate_items": plan["redundant"],
    }


def supersede_open_pair_reviews(
    conn,
    *,
    candidate_file_id: str,
    reference_file_id: str,
    classification: str,
    unqueued_only: bool = False,
) -> int:
    """Close stale open rows immediately before persisting fresher evidence."""
    rows = conn.execute(
        """
        SELECT review_id, evidence_json FROM review_items
        WHERE state IN ('pending', 'deferred') AND classification = ?
          AND ((candidate_file_id = ? AND reference_file_id = ?)
            OR (candidate_file_id = ? AND reference_file_id = ?))
          AND (? = 0 OR queue_path IS NULL OR queue_path = '')
        """,
        (
            classification,
            candidate_file_id, reference_file_id,
            reference_file_id, candidate_file_id,
            int(unqueued_only),
        ),
    ).fetchall()
    for row in rows:
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            evidence = {"previous_evidence": row["evidence_json"]}
        evidence["automatic_suppression"] = {
            "reason": DUPLICATE_SUPPRESSION_REASON,
            "version": SUPPRESSION_VERSION,
        }
        conn.execute(
            """
            UPDATE review_items
            SET state = 'superseded', decision_id = NULL,
                evidence_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE review_id = ? AND state IN ('pending', 'deferred')
            """,
            (
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                row["review_id"],
            ),
        )
    return len(rows)


def _backup_path(state_db: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return (
        state_db.parent / "backups" /
        f"before_review_noise_cleanup_{stamp}_{uuid.uuid4().hex[:8]}.sqlite3"
    )


def cleanup_review_noise(
    state_db: Path,
    *,
    house_dir: Path,
    temp_dir: Path,
    apply: bool = False,
) -> dict:
    """Preview or supersede diagnostic, stale, and redundant open reviews."""
    state_db = Path(state_db).resolve()
    if not apply:
        conn = decision_store.connect_state_db_readonly(state_db)
        try:
            plan = plan_open_review_reconciliation(conn)
        finally:
            conn.close()
        rows = plan["noise"]
        stale = plan["stale"]
        redundant = plan["redundant"]
        return {
            "dry_run": True,
            "planned_superseded": len(rows) + len(stale) + len(redundant),
            "planned_noise_superseded": len(rows),
            "planned_stale_superseded": len(stale),
            "planned_duplicate_superseded": len(redundant),
            "review_ids": [
                row["review_id"] for row in [*rows, *stale, *redundant]
            ],
            "items": rows,
            "stale_items": stale,
            "duplicate_items": redundant,
        }

    with mutation_lock_for_roots(house_dir, temp_dir, "review-noise-cleanup-1.5.4"):
        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(
                    f"doctor failed before review cleanup: {len(issues)} issue(s), "
                    f"first={issues[0]}"
                )
            plan = plan_open_review_reconciliation(conn)
            planned = sum(len(rows) for rows in plan.values())
            if not planned:
                return {
                    "dry_run": False,
                    "planned_superseded": 0,
                    "superseded": 0,
                    "noise_superseded": 0,
                    "stale_superseded": 0,
                    "duplicate_superseded": 0,
                    "backup_path": None,
                    "review_ids": [],
                }
            backup = decision_store.backup_state_db(conn, _backup_path(state_db))
            with decision_store.transaction(conn):
                result = reconcile_open_reviews(conn)
                remaining_issues = decision_store.doctor_issues(conn)
                if remaining_issues:
                    raise RuntimeError(
                        f"doctor failed after review cleanup: {len(remaining_issues)} issue(s), "
                        f"first={remaining_issues[0]}"
                    )
            return {
                "dry_run": False,
                "planned_superseded": planned,
                **{
                    key: value for key, value in result.items()
                    if key not in {"items", "stale_items", "duplicate_items"}
                },
                "backup_path": str(backup),
            }
        finally:
            conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="진단·stale·중복 open review 노이즈 정리"
    )
    parser.add_argument("--state-db", default=str(STATE_DB))
    parser.add_argument("--house", default=str(HOUSE_DIR))
    parser.add_argument("--temp", default=str(TEMP_DIR))
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    result = cleanup_review_noise(
        Path(args.state_db),
        house_dir=Path(args.house),
        temp_dir=Path(args.temp),
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
