#!/usr/bin/env python3
"""Revalidate Folderling automatic quarantines against current physical bytes.

This is an evidence audit, not a mutation command.  It reads committed journal
rows, hashes the recoverable quarantine copy, resolves the recorded keep file's
current physical path, and replays the proof appropriate to each automatic
disposition:

* ``exact_quarantine``: full raw SHA-256 equality;
* ``strong_equivalent_duplicates``: normalized TXT equality;
* ``superseded_versions``: strict prefix or distributed containment proof;
* ``ordered_body_duplicates``: the current directional 95% ordered-body rule.

Warning/coordinate/EPUB-analysis holds are reported separately because they are
safe review holds, not claims that the file is a duplicate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import decision_store  # noqa: E402
from deduplicator import (  # noqa: E402
    count_actionable_pending_strong_reviews,
    count_pending_active_distinct_decision_reviews,
)
from mutation_io import (  # noqa: E402
    contained_anchor_proof_sufficient,
    inspect_contained_text,
    inspect_normalized_text,
    inspect_ordered_text,
    inspect_regular_file,
)
from text_preview import ordered_body_coverage_sufficient  # noqa: E402


AUTO_QUARANTINE_DIRS = {
    "exact_quarantine": "raw_exact",
    "strong_equivalent_duplicates": "normalized_equivalent",
    "superseded_versions": "contained_version",
    "ordered_body_duplicates": "ordered_body_match",
}


def _parser():
    parser = argparse.ArgumentParser(
        description="Revalidate committed Folderling automatic quarantines"
    )
    parser.add_argument(
        "--state-db",
        default=str(ROOT / ".dedup_state" / "dedup_decisions.sqlite3"),
    )
    parser.add_argument(
        "--since",
        required=True,
        help="SQLite UTC timestamp lower bound, for example 2026-08-13 00:00:00",
    )
    parser.add_argument(
        "--report-dir",
        default="/Users/twkim/Documents/txt_temp/dedup_logs",
    )
    parser.add_argument("--progress-every", type=int, default=100)
    return parser


def _kind_for(row):
    if row["action"] == "exact_quarantine":
        return "raw_exact"
    path = Path(row["quarantine_path"] or "")
    for part in reversed(path.parts):
        if part in AUTO_QUARANTINE_DIRS:
            return AUTO_QUARANTINE_DIRS[part]
    return None


def _row_value(row, key):
    value = row[key]
    return value if value not in {"", None} else None


def _require_equal(label, actual, expected_values):
    expected = {value for value in expected_values if value not in {None, ""}}
    if not expected:
        raise RuntimeError(f"{label} has no immutable expected value")
    if expected != {actual}:
        raise RuntimeError(
            f"{label} mismatch: actual={actual}, expected={sorted(expected)}"
        )


def _operation_rows(conn, since):
    return conn.execute(
        """
        SELECT o.*,
               discarded.canonical_path AS discarded_current_path,
               discarded.active AS discarded_active,
               kept.canonical_path AS keep_current_path,
               kept.active AS keep_active,
               source_fp.raw_sha256 AS expected_source_raw_sha256,
               source_fp.normalized_sha256 AS expected_source_normalized_sha256,
               source_fp.normalized_length AS expected_source_normalized_length,
               keep_fp.raw_sha256 AS expected_keep_raw_sha256,
               keep_fp.normalized_sha256 AS expected_keep_normalized_sha256,
               keep_fp.normalized_length AS expected_keep_normalized_length
        FROM operations AS o
        JOIN files AS discarded ON discarded.file_id = o.file_id
        LEFT JOIN files AS kept ON kept.file_id = o.keep_file_id
        LEFT JOIN fingerprints AS source_fp
          ON source_fp.fingerprint_id = o.expected_fingerprint_id
        LEFT JOIN fingerprints AS keep_fp
          ON keep_fp.fingerprint_id = o.expected_keep_fingerprint_id
        WHERE o.state = 'committed'
          AND o.created_at >= ?
          AND o.action IN ('exact_quarantine', 'user_quarantine')
        ORDER BY o.operation_id
        """,
        (since,),
    ).fetchall()


def _classification_rows(conn, operation):
    file_id = operation["file_id"]
    keep_file_id = operation["keep_file_id"]
    source_fingerprint_id = operation["expected_fingerprint_id"]
    keep_fingerprint_id = operation["expected_keep_fingerprint_id"]
    if not keep_file_id or not source_fingerprint_id or not keep_fingerprint_id:
        return []
    return [
        row[0]
        for row in conn.execute(
            """
            SELECT DISTINCT classification FROM review_items
            WHERE (
                    candidate_file_id = ? AND reference_file_id = ?
                AND left_fingerprint_id = ? AND right_fingerprint_id = ?
                  )
               OR (
                    candidate_file_id = ? AND reference_file_id = ?
                AND left_fingerprint_id = ? AND right_fingerprint_id = ?
                  )
            ORDER BY classification
            """,
            (
                file_id, keep_file_id,
                source_fingerprint_id, keep_fingerprint_id,
                keep_file_id, file_id,
                keep_fingerprint_id, source_fingerprint_id,
            ),
        )
    ]


def _active_duplicate_group_counts(conn):
    raw = conn.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT fp.raw_sha256, fp.size
          FROM files AS f
          JOIN fingerprints AS fp
            ON fp.fingerprint_id = f.current_fingerprint_id
          WHERE f.active = 1 AND fp.raw_sha256 IS NOT NULL
          GROUP BY fp.raw_sha256, fp.size HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    normalized = conn.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT fp.normalized_sha256, fp.normalized_length
          FROM files AS f
          JOIN fingerprints AS fp
            ON fp.fingerprint_id = f.current_fingerprint_id
          WHERE f.active = 1 AND fp.normalized_sha256 IS NOT NULL
          GROUP BY fp.normalized_sha256, fp.normalized_length
          HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    return int(raw), int(normalized)


def run(args):
    state_db = Path(args.state_db).expanduser().resolve()
    report_dir = Path(args.report_dir).expanduser().resolve()
    conn = decision_store.connect_state_db_readonly(state_db)
    physical_cache = {}
    details = []
    failures = []
    counters = Counter()
    ordered_coverages = []
    ordered_gaps = []
    contained_modes = Counter()

    def regular(path):
        key = os.path.normcase(os.path.abspath(os.fspath(path)))
        if key not in physical_cache:
            physical_cache[key] = inspect_regular_file(key)
        return physical_cache[key]

    try:
        operations = [
            row for row in _operation_rows(conn, args.since)
            if _kind_for(row) is not None
        ]
        safe_holds = [
            dict(row) for row in conn.execute(
                """
                SELECT action, COUNT(*) AS count,
                       COALESCE(SUM(destination_size), 0) AS bytes
                FROM operations
                WHERE state = 'committed' AND created_at >= ?
                  AND action IN (
                    'warning_move', 'volume_coordinate_hold',
                    'epub_analysis_hold', 'house_review_move'
                  )
                GROUP BY action ORDER BY action
                """,
                (args.since,),
            )
        ]

        for index, row in enumerate(operations, start=1):
            kind = _kind_for(row)
            counters[f"{kind}_total"] += 1
            detail = {
                "operation_id": int(row["operation_id"]),
                "run_id": row["run_id"],
                "kind": kind,
                "file_id": row["file_id"],
                "keep_file_id": row["keep_file_id"],
                "quarantine_path": row["quarantine_path"],
                "keep_path": row["keep_current_path"],
                "classifications": _classification_rows(conn, row),
                "status": "failed",
            }
            try:
                if not row["keep_file_id"] or not row["keep_current_path"]:
                    raise RuntimeError("automatic quarantine has no recorded keep")
                quarantine_path = Path(row["quarantine_path"] or "")
                keep_path = Path(row["keep_current_path"])
                if row["discarded_current_path"] != str(quarantine_path):
                    raise RuntimeError(
                        "discarded file row no longer points to quarantine path"
                    )

                if kind == "raw_exact":
                    quarantine = regular(quarantine_path)
                    keep = regular(keep_path)
                    _require_equal(
                        "quarantine raw SHA-256",
                        quarantine.sha256,
                        (
                            row["source_sha256"], row["destination_sha256"],
                            row["expected_source_raw_sha256"],
                        ),
                    )
                    _require_equal(
                        "keep raw SHA-256",
                        keep.sha256,
                        (row["expected_keep_raw_sha256"], quarantine.sha256),
                    )
                    detail["proof"] = {
                        "raw_sha256": quarantine.sha256,
                        "quarantine_size": quarantine.size,
                        "keep_size": keep.size,
                    }
                elif kind == "normalized_equivalent":
                    quarantine, quarantine_normalized = inspect_normalized_text(
                        quarantine_path
                    )
                    keep, keep_normalized = inspect_normalized_text(keep_path)
                    _require_equal(
                        "quarantine raw SHA-256",
                        quarantine.sha256,
                        (row["source_sha256"], row["destination_sha256"]),
                    )
                    _require_equal(
                        "normalized equivalent SHA-256",
                        quarantine_normalized,
                        (
                            keep_normalized,
                            row["expected_source_normalized_sha256"],
                            row["expected_keep_normalized_sha256"],
                        ),
                    )
                    detail["proof"] = {
                        "normalized_sha256": quarantine_normalized,
                        "quarantine_raw_sha256": quarantine.sha256,
                        "keep_raw_sha256": keep.sha256,
                    }
                elif kind == "contained_version":
                    proof = inspect_contained_text(quarantine_path, keep_path)
                    _require_equal(
                        "quarantine raw SHA-256",
                        proof.short_file_evidence.sha256,
                        (row["source_sha256"], row["destination_sha256"]),
                    )
                    if proof.short_normalized_length >= proof.long_normalized_length:
                        raise RuntimeError("quarantine is not the shorter body")
                    prefix = (
                        proof.long_prefix_sha256
                        == proof.short_normalized_sha256
                    )
                    anchors = contained_anchor_proof_sufficient(proof)
                    if not prefix and not anchors:
                        raise RuntimeError("current containment proof is insufficient")
                    mode = "exact_prefix" if prefix else "distributed_anchors"
                    contained_modes[mode] += 1
                    detail["proof"] = {
                        "mode": mode,
                        "short_normalized_length": proof.short_normalized_length,
                        "long_normalized_length": proof.long_normalized_length,
                        "ordered_anchor_count": proof.ordered_anchor_count,
                        "anchor_offset_span": proof.anchor_offset_span,
                    }
                elif kind == "ordered_body_match":
                    proof = inspect_ordered_text(quarantine_path, keep_path)
                    _require_equal(
                        "quarantine raw SHA-256",
                        proof.source_file_evidence.sha256,
                        (row["source_sha256"], row["destination_sha256"]),
                    )
                    if not ordered_body_coverage_sufficient(proof.coverage):
                        raise RuntimeError(
                            "current ordered-body proof is below the 95% contract"
                        )
                    ordered_coverages.append(proof.coverage.coverage_ppm)
                    ordered_gaps.append(proof.coverage.max_unmatched_chars)
                    detail["proof"] = {
                        **asdict(proof.coverage),
                        "source_normalized_sha256": (
                            proof.source_normalized_sha256
                        ),
                        "target_normalized_sha256": (
                            proof.target_normalized_sha256
                        ),
                    }
                    if "near_identical" in detail["classifications"]:
                        reverse = inspect_ordered_text(
                            keep_path, quarantine_path
                        )
                        if not ordered_body_coverage_sufficient(
                            reverse.coverage
                        ):
                            raise RuntimeError(
                                "current reverse ordered-body proof is below "
                                "the 95% contract"
                            )
                        detail["proof"]["reverse_ordered_body_coverage"] = (
                            asdict(reverse.coverage)
                        )
                        detail["proof"]["reverse_source_normalized_sha256"] = (
                            reverse.source_normalized_sha256
                        )
                        detail["proof"]["reverse_target_normalized_sha256"] = (
                            reverse.target_normalized_sha256
                        )
                        counters["near_identical_bidirectional_passed"] += 1
                else:  # pragma: no cover - guarded by _kind_for
                    raise RuntimeError(f"unsupported audit kind: {kind}")

                detail["status"] = "passed"
                counters[f"{kind}_passed"] += 1
            except Exception as exc:
                detail["error"] = f"{type(exc).__name__}: {exc}"
                counters[f"{kind}_failed"] += 1
                failures.append({
                    key: detail.get(key)
                    for key in (
                        "operation_id", "kind", "quarantine_path",
                        "keep_path", "error",
                    )
                })
            details.append(detail)
            if args.progress_every > 0 and (
                index % args.progress_every == 0 or index == len(operations)
            ):
                print(
                    f"revalidated {index}/{len(operations)} automatic quarantines",
                    file=sys.stderr,
                    flush=True,
                )

        doctor = decision_store.doctor_issues(conn)
        raw_groups, normalized_groups = _active_duplicate_group_counts(conn)
        recovery_operations = conn.execute(
            """
            SELECT COUNT(*) FROM operations
            WHERE state IN ('planned', 'fs_done', 'db_done', 'stale')
            """
        ).fetchone()[0]
        active_runs = conn.execute(
            "SELECT COUNT(*) FROM actual_runs WHERE state = 'active'"
        ).fetchone()[0]
        invariants = {
            "doctor_issue_count": len(doctor),
            "doctor_first_issue": doctor[0] if doctor else None,
            "active_run_count": int(active_runs),
            "recovery_operation_count": int(recovery_operations),
            "active_raw_duplicate_group_count": raw_groups,
            "active_normalized_duplicate_group_count": normalized_groups,
            "actionable_pending_strong_review_count": (
                count_actionable_pending_strong_reviews(state_db)
            ),
            "pending_active_decision_review_count": (
                count_pending_active_distinct_decision_reviews(state_db)
            ),
        }
        invariant_failures = {
            key: value for key, value in invariants.items()
            if key.endswith("_count") and value != 0
        }
        completed = not failures and not invariant_failures
        summary = {
            "completed": completed,
            "since_utc": args.since,
            "automatic_quarantine_count": len(operations),
            "automatic_quarantine_passed": sum(
                detail["status"] == "passed" for detail in details
            ),
            "automatic_quarantine_failed": len(failures),
            "counts": dict(sorted(counters.items())),
            "contained_proof_modes": dict(sorted(contained_modes.items())),
            "ordered_body_min_coverage_ppm": (
                min(ordered_coverages) if ordered_coverages else None
            ),
            "ordered_body_max_unmatched_chars": (
                max(ordered_gaps) if ordered_gaps else None
            ),
            "physically_hashed_unique_file_count": len(physical_cache),
            "safe_hold_operations": safe_holds,
            "invariants": invariants,
            "invariant_failures": invariant_failures,
        }
        payload = {
            "kind": "folderling_outcome_audit_1_4_16",
            "generated_at": datetime.now().astimezone().isoformat(),
            "state_db": str(state_db),
            "summary": summary,
            "failures": failures,
            "operations": details,
        }
    finally:
        conn.close()

    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    json_path = report_dir / f"folderling_outcome_audit_{stamp}.json"
    text_path = report_dir / f"folderling_outcome_audit_{stamp}.txt"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "Folderling 1.4.17 outcome audit",
        f"completed: {str(summary['completed']).lower()}",
        f"automatic quarantines: {summary['automatic_quarantine_count']}",
        f"passed: {summary['automatic_quarantine_passed']}",
        f"failed: {summary['automatic_quarantine_failed']}",
        f"counts: {summary['counts']}",
        f"contained proof modes: {summary['contained_proof_modes']}",
        f"ordered minimum coverage ppm: {summary['ordered_body_min_coverage_ppm']}",
        f"ordered maximum unmatched chars: {summary['ordered_body_max_unmatched_chars']}",
        f"invariants: {summary['invariants']}",
        f"safe holds: {summary['safe_hold_operations']}",
        f"failures: {failures}",
        f"JSON: {json_path}",
        "",
    ]
    text_path.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"text_report={text_path}")
    print(f"json_report={json_path}")
    return 0 if summary["completed"] else 2


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if args.progress_every < 0:
        parser.error("--progress-every must be zero or positive")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
