#!/usr/bin/env python3
"""One-button managed Folderling entry point for the terminal control server."""

import argparse
import sqlite3
import stat
import sys
import uuid
from datetime import datetime
from pathlib import Path

import decision_store
import folderling
import review_actions
from mutation_io import ensure_directory_nofollow, mutation_lock_for_roots
from project_paths import PROJECT_ROOT, STATE_DB


DEFAULT_STATE_DB = STATE_DB
FOLDERLING_BACKUP_RETENTION = 10


def build_parser():
    parser = argparse.ArgumentParser(
        description="backup, one-time approval, and Folderling actual run"
    )
    parser.add_argument("--src", default=folderling.DEFAULT_SRC_DIR)
    parser.add_argument("--dst", default=folderling.DEFAULT_DST_DIR)
    parser.add_argument("--state-db", default=str(DEFAULT_STATE_DB))
    return parser


def _unique_backup_path(directory, label):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return directory / f"{label}_{stamp}_{uuid.uuid4().hex[:8]}.sqlite3"


def _backup_unmigrated_db(db_path, output):
    ensure_directory_nofollow(output.parent)
    source = sqlite3.connect(str(db_path))
    destination = sqlite3.connect(str(output))
    try:
        source.backup(destination)
        integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"pre-migration backup integrity failed: {integrity}")
    finally:
        destination.close()
        source.close()
    return output


def _schema_version(db_path):
    connection = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    try:
        return connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()


def _protected_backup_paths(conn):
    rows = conn.execute(
        """
        SELECT DISTINCT ar.backup_path
        FROM actual_runs AS ar
        WHERE ar.state IN ('approved', 'active')
           OR EXISTS (
               SELECT 1 FROM operations AS op
               WHERE op.run_id = ar.run_id
                 AND op.state IN ('planned', 'fs_done', 'db_done')
           )
        """
    )
    return {str(Path(row[0]).resolve()) for row in rows if row[0]}


def _prune_folderling_backups(state_db_path, backup_dir, keep_latest=FOLDERLING_BACKUP_RETENTION):
    """Bound completed-run backups without touching live recovery evidence."""
    conn = decision_store.connect_state_db(state_db_path)
    try:
        protected = _protected_backup_paths(conn)
    finally:
        conn.close()

    candidates = []
    try:
        entries = list(Path(backup_dir).iterdir())
    except FileNotFoundError:
        return []
    for path in entries:
        if not path.name.startswith("before_folderling_") or path.suffix != ".sqlite3":
            continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            continue
        candidates.append((info.st_mtime_ns, path))
    candidates.sort(reverse=True)
    keep = {str(path.resolve()) for _, path in candidates[:keep_latest]} | protected
    removed = []
    for _, path in candidates[keep_latest:]:
        if str(path.resolve()) in keep:
            continue
        path.unlink()
        removed.append(path)
    return removed


def _close_unfinished_approval(state_db_path, reason):
    conn = decision_store.connect_state_db(state_db_path)
    try:
        active = conn.execute(
            "SELECT run_id FROM actual_runs WHERE state = 'active' LIMIT 1"
        ).fetchone()
        if active is not None:
            decision_store.finish_actual_run(
                conn, active["run_id"], success=False, error=reason
            )
            return "failed"
        approved = conn.execute(
            "SELECT run_id FROM actual_runs WHERE state = 'approved' LIMIT 1"
        ).fetchone()
        if approved is not None:
            decision_store.disable_actual_run(conn)
            return "cancelled"
        return None
    finally:
        conn.close()


def run(src_dir, dst_dir, state_db_path):
    state_db = Path(state_db_path)
    if not state_db.is_file():
        raise RuntimeError(f"managed state DB is missing: {state_db}")
    backup_dir = state_db.parent / "backups"
    script_dir = str(PROJECT_ROOT)

    with mutation_lock_for_roots(dst_dir, src_dir, "folderling-one-button"):
        ensure_directory_nofollow(backup_dir)
        original_schema = _schema_version(state_db)
        pre_migration = None
        if original_schema < decision_store.SCHEMA_VERSION:
            pre_migration = _backup_unmigrated_db(
                state_db, _unique_backup_path(backup_dir, "before_schema")
            )
        approval_started = False
        try:
            conn = decision_store.initialize_state_db(state_db)
            try:
                # A user moves managed queue files into these inboxes outside the
                # program.  Back up first, then bind those renames to the stable
                # file IDs before doctor judges the old queue paths as missing.
                review_actions.ensure_action_directories(src_dir)
                run_backup = decision_store.backup_state_db(
                    conn, _unique_backup_path(backup_dir, "before_folderling")
                )
                claimed_actions = review_actions.claim_external_action_moves(
                    conn, src_dir
                )
                issues = decision_store.doctor_issues(conn)
                if issues:
                    first = issues[0]
                    raise RuntimeError(
                        "doctor failed before Folderling; run disable/recover/doctor manually: "
                        f"{len(issues)} issue(s), first={first['kind']}"
                    )
                approval_started = True
                run_id = decision_store.issue_actual_run_token(
                    conn, str(run_backup), house_dir=dst_dir, temp_dir=src_dir
                )
            finally:
                conn.close()
            schema_message = str(pre_migration) if pre_migration else "불필요(schema current)"
            print(f"🔐 schema/doctor 준비 완료, migration 백업: {schema_message}")
            if claimed_actions:
                print(f"📥 검토 처리함 입력 {len(claimed_actions)}개 확인")
            print(f"🔐 일회성 actual 승인 발급: {run_id}")
            return folderling._process_items_with_lock_held(
                src_dir, dst_dir, script_dir, state_db_path=str(state_db)
            )
        except BaseException as exc:
            if approval_started:
                if isinstance(exc, KeyboardInterrupt):
                    reason = "Folderling interrupted by SIGINT/KeyboardInterrupt"
                else:
                    reason = f"Folderling startup failed: {exc}"
                _close_unfinished_approval(state_db, reason)
            raise
        finally:
            try:
                _prune_folderling_backups(state_db, backup_dir)
            except Exception as exc:
                sys.stderr.write(f"⚠️ Folderling backup retention failed: {exc}\n")


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = run(args.src, args.dst, args.state_db)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"❌ Folderling 원버튼 실행 실패: {exc}", file=sys.stderr)
        return 2
    return 2 if result.get("failure_count", 0) else 0


if __name__ == "__main__":
    sys.exit(main())
