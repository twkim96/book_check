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


SUPPRESSION_REASON = "distinct_terminal_epub_volume"
CORE_SUPPRESSION_REASON = "cross_core_metadata_only"
SIDE_STORY_SUPPRESSION_REASON = "side_story_vs_numbered_volume"
DUPLICATE_SUPPRESSION_REASON = "stale_duplicate_open_review"
SUPPRESSION_VERSION = "1.3.0"


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


def find_open_review_noise(conn) -> list[dict]:
    """Find only unqueued active house/house metadata-only volume noise."""
    rows = conn.execute(
        """
        SELECT r.review_id, r.classification, r.state, r.queue_path, r.evidence_json,
               candidate.file_id AS candidate_file_id,
               candidate.canonical_path AS candidate_path,
               reference.file_id AS reference_file_id,
               reference.canonical_path AS reference_path,
               candidate_analysis.core_title AS candidate_core_title,
               reference_analysis.core_title AS reference_core_title
        FROM review_items AS r
        JOIN files AS candidate ON candidate.file_id = r.candidate_file_id
        JOIN files AS reference ON reference.file_id = r.reference_file_id
        LEFT JOIN file_analysis AS candidate_analysis
          ON candidate_analysis.file_id = candidate.file_id
        LEFT JOIN file_analysis AS reference_analysis
          ON reference_analysis.file_id = reference.file_id
        WHERE r.state IN ('pending', 'deferred')
          AND r.classification = 'metadata_only'
          AND (r.queue_path IS NULL OR r.queue_path = '')
          AND candidate.active = 1 AND reference.active = 1
          AND candidate.source = 'house' AND reference.source = 'house'
        ORDER BY r.review_id
        """
    ).fetchall()
    result = []
    for row in rows:
        terminal_volume_noise = distinct_terminal_epub_volumes(
            Path(row["candidate_path"]).name,
            Path(row["reference_path"]).name,
        )
        side_story_volume_noise = side_story_vs_numbered_epub_volume(
            Path(row["candidate_path"]).name,
            Path(row["reference_path"]).name,
        )
        cross_core_noise = different_core_titles(
            row["candidate_core_title"], row["reference_core_title"]
        )
        if not terminal_volume_noise and not side_story_volume_noise and not cross_core_noise:
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
            "suppression_reason": (
                SUPPRESSION_REASON
                if terminal_volume_noise
                else SIDE_STORY_SUPPRESSION_REASON
                if side_story_volume_noise
                else CORE_SUPPRESSION_REASON
            ),
            "evidence_json": row["evidence_json"],
        })
    return result


def find_redundant_open_reviews(conn, *, excluded_review_ids=()) -> list[dict]:
    """Keep the current-fingerprint/newest row for each open unordered pair."""
    excluded = {int(review_id) for review_id in excluded_review_ids}
    rows = conn.execute(
        """
        SELECT r.review_id, r.classification, r.evidence_json,
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
        groups.setdefault((pair, row["classification"]), []).append(row)
    redundant = []
    for (_pair, _classification), group in groups.items():
        if len(group) < 2:
            continue
        keep = max(
            group,
            key=lambda row: (int(row["current_evidence"]), int(row["review_id"])),
        )
        for row in group:
            if row["review_id"] == keep["review_id"]:
                continue
            redundant.append({
                "review_id": row["review_id"],
                "classification": row["classification"],
                "keep_review_id": keep["review_id"],
                "evidence_json": row["evidence_json"],
            })
    return sorted(redundant, key=lambda row: row["review_id"])


def supersede_open_pair_reviews(
    conn,
    *,
    candidate_file_id: str,
    reference_file_id: str,
    classification: str,
) -> int:
    """Close stale open rows immediately before persisting fresher evidence."""
    rows = conn.execute(
        """
        SELECT review_id, evidence_json FROM review_items
        WHERE state IN ('pending', 'deferred') AND classification = ?
          AND ((candidate_file_id = ? AND reference_file_id = ?)
            OR (candidate_file_id = ? AND reference_file_id = ?))
        """,
        (
            classification,
            candidate_file_id, reference_file_id,
            reference_file_id, candidate_file_id,
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
    """Preview or supersede weak cross-core and redundant open reviews."""
    state_db = Path(state_db).resolve()
    if not apply:
        conn = decision_store.connect_state_db_readonly(state_db)
        try:
            rows = find_open_review_noise(conn)
            redundant = find_redundant_open_reviews(
                conn,
                excluded_review_ids=[row["review_id"] for row in rows],
            )
        finally:
            conn.close()
        return {
            "dry_run": True,
            "planned_superseded": len(rows) + len(redundant),
            "planned_noise_superseded": len(rows),
            "planned_duplicate_superseded": len(redundant),
            "review_ids": [
                row["review_id"] for row in [*rows, *redundant]
            ],
            "items": rows,
            "duplicate_items": redundant,
        }

    with mutation_lock_for_roots(house_dir, temp_dir, "review-noise-cleanup-1.3.0"):
        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(
                    f"doctor failed before review cleanup: {len(issues)} issue(s), "
                    f"first={issues[0]}"
                )
            rows = find_open_review_noise(conn)
            noise_ids = [row["review_id"] for row in rows]
            redundant = find_redundant_open_reviews(
                conn, excluded_review_ids=noise_ids
            )
            if not rows and not redundant:
                return {
                    "dry_run": False,
                    "planned_superseded": 0,
                    "superseded": 0,
                    "noise_superseded": 0,
                    "duplicate_superseded": 0,
                    "backup_path": None,
                    "review_ids": [],
                }
            backup = decision_store.backup_state_db(conn, _backup_path(state_db))
            noise_changed = 0
            duplicate_changed = 0
            with decision_store.transaction(conn):
                for row in rows:
                    try:
                        evidence = json.loads(row["evidence_json"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        evidence = {"previous_evidence": row["evidence_json"]}
                    evidence["automatic_suppression"] = {
                        "reason": row["suppression_reason"],
                        "version": SUPPRESSION_VERSION,
                        "candidate_volume": row["candidate_volume"],
                        "candidate_side_story": row["candidate_side_story"],
                        "reference_volume": row["reference_volume"],
                        "reference_side_story": row["reference_side_story"],
                        "candidate_core_title": row["candidate_core_title"],
                        "reference_core_title": row["reference_core_title"],
                    }
                    cursor = conn.execute(
                        """
                        UPDATE review_items
                        SET state = 'superseded', decision_id = NULL,
                            evidence_json = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE review_id = ?
                          AND state IN ('pending', 'deferred')
                          AND classification = 'metadata_only'
                          AND (queue_path IS NULL OR queue_path = '')
                        """,
                        (
                            json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                            row["review_id"],
                        ),
                    )
                    noise_changed += cursor.rowcount
                for row in redundant:
                    try:
                        evidence = json.loads(row["evidence_json"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        evidence = {"previous_evidence": row["evidence_json"]}
                    evidence["automatic_suppression"] = {
                        "reason": DUPLICATE_SUPPRESSION_REASON,
                        "version": SUPPRESSION_VERSION,
                        "keep_review_id": row["keep_review_id"],
                    }
                    cursor = conn.execute(
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
                    duplicate_changed += cursor.rowcount
                remaining_issues = decision_store.doctor_issues(conn)
                if remaining_issues:
                    raise RuntimeError(
                        f"doctor failed after review cleanup: {len(remaining_issues)} issue(s), "
                        f"first={remaining_issues[0]}"
                    )
            return {
                "dry_run": False,
                "planned_superseded": len(rows) + len(redundant),
                "superseded": noise_changed + duplicate_changed,
                "noise_superseded": noise_changed,
                "duplicate_superseded": duplicate_changed,
                "backup_path": str(backup),
                "review_ids": [
                    row["review_id"] for row in [*rows, *redundant]
                ],
            }
        finally:
            conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="cross-core metadata_only와 중복 open review 노이즈 정리"
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
