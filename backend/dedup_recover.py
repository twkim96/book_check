#!/usr/bin/env python3
"""Doctor, backup, recovery, and explicit quarantine purge for dedup state."""

import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import decision_store
from project_paths import HOUSE_DIR, STATE_DB, TEMP_DIR


DEFAULT_STATE_DB = str(STATE_DB)
DEFAULT_HOUSE_DIR = str(HOUSE_DIR)
DEFAULT_TEMP_DIR = str(TEMP_DIR)


def build_parser():
    parser = argparse.ArgumentParser(description="dedup DB/파일 상태를 진단하고 복구합니다.")
    parser.add_argument("--state-db", default=DEFAULT_STATE_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    backup = sub.add_parser("backup")
    backup.add_argument("--output", required=True)
    backup.add_argument("--run", action="store_true")
    recover = sub.add_parser("recover")
    recover.add_argument("--operation-id", type=int)
    recover.add_argument("--group-id", type=int)
    recover.add_argument("--run", action="store_true")
    enable = sub.add_parser("enable")
    enable.add_argument("--backup", required=True)
    enable.add_argument("--house", default=DEFAULT_HOUSE_DIR)
    enable.add_argument("--temp", default=DEFAULT_TEMP_DIR)
    enable.add_argument("--ack-user-approved", action="store_true")
    enable.add_argument("--run", action="store_true")
    disable = sub.add_parser("disable")
    disable.add_argument("--run", action="store_true")
    purge = sub.add_parser("purge")
    purge.add_argument("--older-than-days", type=int, default=30)
    purge.add_argument("--ack-user-approved", action="store_true")
    purge.add_argument("--run", action="store_true")
    missing = sub.add_parser("ack-missing-review")
    missing.add_argument("--ack-user-approved", action="store_true")
    missing.add_argument("--run", action="store_true")
    return parser


def _json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _missing_review_candidates(conn):
    """Return only externally deleted files with complete committed queue provenance."""
    rows = conn.execute(
        """
        SELECT f.file_id, f.canonical_path, f.size, f.mtime_ns,
               f.current_fingerprint_id, f.protected,
               CASE WHEN rep.file_id IS NULL THEN 0 ELSE 1 END AS representative,
               o.operation_id AS origin_operation_id, o.run_id, o.action,
               o.dest_path, o.expected_fingerprint_id,
               o.destination_dev, o.destination_ino, o.destination_ctime_ns,
               o.destination_size, o.destination_mtime_ns, o.destination_sha256,
               ar.temp_root
        FROM files AS f
        LEFT JOIN representatives AS rep ON rep.file_id = f.file_id
        JOIN operations AS o ON o.operation_id = (
            SELECT MAX(latest.operation_id) FROM operations AS latest
            WHERE latest.file_id = f.file_id
              AND latest.action IN ('suspected_move', 'warning_move', 'house_review_move')
              AND latest.state = 'committed'
        )
        JOIN actual_runs AS ar ON ar.run_id = o.run_id
        WHERE f.active = 1 AND f.source = 'queue'
        ORDER BY f.canonical_path
        """
    ).fetchall()
    candidates = []
    for row in rows:
        path = Path(row["canonical_path"])
        if path.exists() or path.is_symlink():
            continue
        if row["protected"] or row["representative"]:
            continue
        if row["dest_path"] != row["canonical_path"]:
            continue
        if row["current_fingerprint_id"] != row["expected_fingerprint_id"]:
            continue
        expected_identity = (
            row["destination_dev"], row["destination_ino"],
            row["destination_ctime_ns"], row["destination_size"],
            row["destination_mtime_ns"], row["destination_sha256"],
        )
        if any(value is None for value in expected_identity):
            continue
        trash_root = Path(row["temp_root"]) / "trash_bin"
        try:
            if os.path.commonpath((str(path), str(trash_root))) != str(trash_root):
                continue
        except ValueError:
            continue
        unfinished = conn.execute(
            """
            SELECT 1 FROM operations
            WHERE file_id = ? AND state IN ('planned', 'fs_done', 'db_done')
            LIMIT 1
            """,
            (row["file_id"],),
        ).fetchone()
        if unfinished is not None:
            continue
        candidates.append(row)
    return candidates


def _ack_missing_reviews(conn):
    candidates = _missing_review_candidates(conn)
    all_issues = decision_store.doctor_issues(conn)
    candidate_ids = {row["file_id"] for row in candidates}
    blocking = [
        issue for issue in all_issues
        if issue["kind"] != "missing_file" or issue.get("file_id") not in candidate_ids
    ]
    if blocking:
        raise RuntimeError(
            f"non-review doctor issue blocks acknowledgement: {blocking[0]['kind']}"
        )
    missing_ids = {
        issue.get("file_id") for issue in all_issues if issue["kind"] == "missing_file"
    }
    if missing_ids != candidate_ids:
        raise RuntimeError("some missing files lack safe committed review provenance")
    with decision_store.transaction(conn):
        for row in candidates:
            conn.execute(
                """
                UPDATE files SET active = 0, last_seen_at = CURRENT_TIMESTAMP
                WHERE file_id = ? AND active = 1 AND source = 'queue'
                """,
                (row["file_id"],),
            )
            decision_store.supersede_open_reviews_for_file(
                conn, row["file_id"], reason="user_deleted_review_file"
            )
    return candidates


def _purge_candidates(conn, older_than_days):
    if older_than_days < 1:
        raise ValueError("--older-than-days must be at least 1")
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    rows = conn.execute(
        """
        SELECT o.operation_id, o.action, o.quarantine_path, o.created_at,
               o.keep_file_id, o.expected_fingerprint_id,
               o.expected_keep_fingerprint_id, o.expected_size, o.expected_mtime_ns,
               o.destination_dev AS parent_dev, o.destination_ino AS parent_ino,
               o.destination_ctime_ns AS parent_ctime_ns,
               o.destination_size AS parent_size,
               o.destination_mtime_ns AS parent_mtime_ns,
               o.destination_sha256 AS parent_sha256,
               f.file_id, f.protected
        FROM operations AS o JOIN files AS f ON f.file_id = o.file_id
        WHERE o.action IN ('exact_quarantine', 'human_quarantine', 'user_quarantine')
          AND o.state = 'committed'
          AND o.purged_at IS NULL AND f.active = 0 AND f.source = 'quarantine'
        ORDER BY o.operation_id
        """
    ).fetchall()
    candidates = []
    for row in rows:
        created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created <= cutoff and not row["protected"]:
            candidates.append(row)
    return candidates


def _validate_purge_candidate(conn, row):
    from mutation_io import FileEvidence, evidence_matches, inspect_regular_file
    quarantine = Path(row["quarantine_path"])
    if quarantine.is_symlink() or not quarantine.is_file():
        raise RuntimeError(f"quarantine path is not a regular file: {quarantine}")
    quarantine_evidence = inspect_regular_file(quarantine)
    parent_values = (
        row["parent_dev"], row["parent_ino"], row["parent_ctime_ns"],
        row["parent_size"], row["parent_mtime_ns"], row["parent_sha256"],
    )
    if any(value is None for value in parent_values):
        raise RuntimeError("legacy quarantine has no owned destination evidence")
    parent_evidence = FileEvidence(*parent_values)
    if not evidence_matches(quarantine_evidence, parent_evidence):
        raise RuntimeError(f"quarantine journal ownership is stale: {quarantine}")

    quarantined_fp = conn.execute(
        "SELECT raw_sha256 FROM fingerprints WHERE fingerprint_id = ? AND file_id = ?",
        (row["expected_fingerprint_id"], row["file_id"]),
    ).fetchone()
    if (
        quarantined_fp is None or not quarantined_fp["raw_sha256"]
        or quarantined_fp["raw_sha256"] != quarantine_evidence.sha256
    ):
        raise RuntimeError("purge quarantined fingerprint evidence is stale")

    # user_quarantine records an explicit human discard, not byte equality.
    # A paired keep must remain the exact approved snapshot; a standalone
    # action-inbox discard has no keep and is authorized by the two explicit
    # approvals (placing it in delete, then running purge) plus owned evidence.
    if row["action"] == "user_quarantine":
        if row["keep_file_id"] is not None:
            keep = conn.execute(
                """
                SELECT f.canonical_path, f.active, f.current_fingerprint_id,
                       f.dev, f.ino, f.ctime_ns, f.size, f.mtime_ns
                FROM files AS f WHERE f.file_id = ?
                """,
                (row["keep_file_id"],),
            ).fetchone()
            if (
                keep is None or not keep["active"]
                or keep["current_fingerprint_id"] != row["expected_keep_fingerprint_id"]
            ):
                raise RuntimeError("user purge keep file is missing or fingerprint-stale")
            keep_evidence = inspect_regular_file(keep["canonical_path"])
            expected_keep = (
                keep["dev"], keep["ino"], keep["ctime_ns"],
                keep["size"], keep["mtime_ns"],
            )
            actual_keep = (
                keep_evidence.dev, keep_evidence.ino, keep_evidence.ctime_ns,
                keep_evidence.size, keep_evidence.mtime_ns,
            )
            if expected_keep != actual_keep:
                raise RuntimeError("user purge keep snapshot is stale")
        return quarantine, parent_evidence

    keep = conn.execute(
        """
        SELECT f.file_id, f.canonical_path, f.active, f.assignment_state,
               f.current_fingerprint_id, f.size, f.mtime_ns, fp.raw_sha256
        FROM files AS f
        JOIN representatives AS r
          ON r.file_id = f.file_id AND r.variant_id = f.variant_id
        LEFT JOIN fingerprints AS fp ON fp.fingerprint_id = f.current_fingerprint_id
        WHERE f.file_id = ?
        """,
        (row["keep_file_id"],),
    ).fetchone()
    if (
        keep is None
        or not keep["active"]
        or keep["assignment_state"] != "managed"
        or keep["current_fingerprint_id"] != row["expected_keep_fingerprint_id"]
    ):
        raise RuntimeError("purge keep file is missing, unmanaged, or fingerprint-stale")
    keep_path = Path(keep["canonical_path"])
    if keep_path.is_symlink() or not keep_path.is_file():
        raise RuntimeError(f"purge keep path is not a regular file: {keep_path}")
    keep_stat = keep_path.stat()
    if keep_stat.st_size != keep["size"] or keep_stat.st_mtime_ns != keep["mtime_ns"]:
        raise RuntimeError(f"purge keep snapshot is stale: {keep_path}")

    if row["action"] == "human_quarantine":
        decision = conn.execute(
            """
            SELECT 1 FROM decisions
            WHERE active = 1 AND verdict = 'same_content'
              AND ((left_file_id = ? AND right_file_id = ?
                    AND left_fingerprint_id = ? AND right_fingerprint_id = ?)
                OR (left_file_id = ? AND right_file_id = ?
                    AND left_fingerprint_id = ? AND right_fingerprint_id = ?))
            LIMIT 1
            """,
            (
                row["file_id"], row["keep_file_id"],
                row["expected_fingerprint_id"], row["expected_keep_fingerprint_id"],
                row["keep_file_id"], row["file_id"],
                row["expected_keep_fingerprint_id"], row["expected_fingerprint_id"],
            ),
        ).fetchone()
        if decision is None:
            raise RuntimeError("human quarantine decision is no longer active")
        return quarantine, parent_evidence

    if quarantined_fp is None or not quarantined_fp["raw_sha256"] or not keep["raw_sha256"]:
        raise RuntimeError("purge raw fingerprint evidence is missing")
    keep_evidence = inspect_regular_file(keep_path)
    quarantine_hash = quarantine_evidence.sha256
    keep_hash = keep_evidence.sha256
    if (
        quarantine_hash != keep_hash
        or quarantine_hash != quarantined_fp["raw_sha256"]
        or keep_hash != keep["raw_sha256"]
    ):
        raise RuntimeError("purge byte identity revalidation failed")
    return quarantine, parent_evidence


def _execute_purge(conn, older_than_days):
    candidates = _purge_candidates(conn, older_than_days)
    issues = decision_store.doctor_issues(conn)
    if issues:
        raise RuntimeError(
            f"doctor issues must be zero before purge: {len(issues)} ({issues[0]['kind']})"
        )
    validated = [_validate_purge_candidate(conn, row) for row in candidates]
    purge_run_id = f"purge-{uuid.uuid4()}"
    purge_operations = []
    with decision_store.transaction(conn):
        for row, (path, evidence) in zip(candidates, validated):
            purge_operations.append(decision_store.create_operation(
                conn,
                run_id=purge_run_id,
                action="quarantine_purge",
                source_path=str(path),
                file_id=row["file_id"],
                keep_file_id=row["keep_file_id"],
                expected_size=row["expected_size"],
                expected_mtime_ns=row["expected_mtime_ns"],
                expected_fingerprint_id=row["expected_fingerprint_id"],
                expected_keep_fingerprint_id=row["expected_keep_fingerprint_id"],
                parent_operation_id=row["operation_id"],
                source_dev=evidence.dev,
                source_ino=evidence.ino,
                source_ctime_ns=evidence.ctime_ns,
                source_sha256=evidence.sha256,
            ))
    purged = []
    from mutation_io import unlink_owned
    for row, (path, evidence), purge_operation_id in zip(
        candidates, validated, purge_operations
    ):
        _, current_evidence = _validate_purge_candidate(conn, row)
        unlink_owned(path, expected=evidence)
        with decision_store.transaction(conn):
            decision_store.transition_operation(conn, purge_operation_id, "fs_done")
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE operations SET purged_at = CURRENT_TIMESTAMP WHERE operation_id = ?",
                (row["operation_id"],),
            )
            decision_store.transition_operation(conn, purge_operation_id, "db_done")
        with decision_store.transaction(conn):
            decision_store.transition_operation(conn, purge_operation_id, "committed")
        purged.append(str(path))
    return purged


def run(args):
    conn = decision_store.connect_state_db(args.state_db)
    try:
        decision_store.validate_schema(conn)
        if args.command == "doctor":
            issues = decision_store.doctor_issues(conn)
            _json({"ok": not issues, "issue_count": len(issues), "issues": issues})
            return 0 if not issues else 2
        if args.command == "backup":
            if not args.run:
                _json({"dry_run": True, "output": args.output})
                return 0
            output = decision_store.backup_state_db(conn, args.output)
            _json({"dry_run": False, "output": str(output), "integrity_check": "ok"})
            return 0
        if args.command == "recover":
            if args.operation_id is not None and args.group_id is not None:
                raise RuntimeError("--operation-id and --group-id cannot be combined")
            if args.group_id is None:
                group_ids = [row[0] for row in conn.execute(
                    "SELECT group_id FROM operation_groups "
                    "WHERE state IN ('planned', 'fs_done', 'db_done') ORDER BY group_id"
                )] if args.operation_id is None else []
            else:
                group_ids = [args.group_id]
            if args.operation_id is None and args.group_id is None:
                operation_ids = [row[0] for row in conn.execute(
                    "SELECT operation_id FROM operations WHERE state IN ('planned', 'fs_done', 'db_done') "
                    "AND action != 'managed_folder_relocate_item' ORDER BY operation_id"
                )]
            elif args.operation_id is not None:
                operation_ids = [args.operation_id]
            else:
                operation_ids = []
            if not args.run:
                _json({
                    "dry_run": True,
                    "operation_group_ids": group_ids,
                    "operation_ids": operation_ids,
                })
                return 0
            from library_organize import recover_operation_group
            group_outcomes = {
                group_id: recover_operation_group(conn, group_id)
                for group_id in group_ids
            }
            outcomes = {
                operation_id: decision_store.recover_interrupted_operation(conn, operation_id)
                for operation_id in operation_ids
            }
            _json({
                "dry_run": False,
                "operation_group_outcomes": group_outcomes,
                "operation_outcomes": outcomes,
                # Keep the pre-v1.3.3 response field for existing callers.
                "outcomes": outcomes,
            })
            return 0
        if args.command == "enable":
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(f"doctor issues must be zero before enable: {len(issues)}")
            if not args.ack_user_approved:
                raise RuntimeError("--ack-user-approved is required")
            if not args.run:
                _json({"dry_run": True, "backup": args.backup, "enable": True})
                return 0
            from mutation_io import mutation_lock_for_roots
            with mutation_lock_for_roots(args.house, args.temp, "enable-actual-run"):
                backup = decision_store.backup_state_db(conn, args.backup)
                run_id = decision_store.issue_actual_run_token(
                    conn, str(backup), house_dir=args.house, temp_dir=args.temp
                )
            _json({
                "dry_run": False, "backup": str(backup), "enabled": True,
                "approved_run_id": run_id, "one_time": True,
            })
            return 0
        if args.command == "disable":
            if not args.run:
                _json({"dry_run": True, "disabled": False})
                return 0
            from mutation_io import mutation_lock
            with mutation_lock(conn, "disable-actual-run"):
                decision_store.disable_actual_run(conn)
            _json({"dry_run": False, "disabled": True})
            return 0
        if args.command == "purge":
            candidates = _purge_candidates(conn, args.older_than_days)
            preview = [dict(row) for row in candidates]
            if not args.run:
                _json({"dry_run": True, "candidates": preview})
                return 0
            if not args.ack_user_approved:
                raise RuntimeError("--ack-user-approved is required for purge")
            from mutation_io import mutation_lock
            with mutation_lock(conn, "quarantine_purge"):
                purged = _execute_purge(conn, args.older_than_days)
            _json({"dry_run": False, "purged": purged})
            return 0
        if args.command == "ack-missing-review":
            candidates = _missing_review_candidates(conn)
            preview = [
                {
                    "file_id": row["file_id"],
                    "path": row["canonical_path"],
                    "origin_operation_id": row["origin_operation_id"],
                    "origin_action": row["action"],
                }
                for row in candidates
            ]
            if not args.run:
                _json({"dry_run": True, "candidates": preview})
                return 0
            if not args.ack_user_approved:
                raise RuntimeError(
                    "--ack-user-approved is required for missing review acknowledgement"
                )
            backup_dir = Path(args.state_db).parent / "backups"
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            backup_path = backup_dir / f"before_missing_review_ack_{stamp}.sqlite3"
            from mutation_io import mutation_lock
            with mutation_lock(conn, "ack-missing-review"):
                decision_store.backup_state_db(conn, backup_path)
                acknowledged = _ack_missing_reviews(conn)
            remaining = decision_store.doctor_issues(conn)
            if remaining:
                raise RuntimeError(
                    f"doctor failed after acknowledgement: {remaining[0]['kind']}"
                )
            _json({
                "dry_run": False,
                "backup": str(backup_path),
                "acknowledged": [row["file_id"] for row in acknowledged],
            })
            return 0
        raise RuntimeError(f"unsupported command: {args.command}")
    finally:
        conn.close()


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (FileNotFoundError, sqlite3.Error, ValueError, RuntimeError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
