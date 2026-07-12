#!/usr/bin/env python3
"""Apply a human queue disposition list with journaled restore/quarantine.

The delete list names files currently in ``house_human_review``.  Every other
active file in that queue is restored to its original house path.  Optional
explicit duplicate pairs can quarantine already-resident house files while
preserving the named keep file.  Actual mode is fail-closed and requires an
explicit acknowledgement.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import unicodedata
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import decision_store
import folderling
from dedup_mutations import (
    refresh_user_approved_snapshot,
    user_quarantine,
    user_queue_restore,
)
from mutation_io import mutation_lock_for_roots
from run_folderling_one_button import _prune_folderling_backups
from project_paths import FILE_INDEX, FILE_LIST, PROJECT_ROOT, STATE_DB


DEFAULT_DB = STATE_DB
DEFAULT_EXTRA_PLAN = PROJECT_ROOT / "manual_duplicate_plan_20260712.json"
QUEUE_FRAGMENT = "/trash_bin/house_human_review/"


def _key(value):
    value = unicodedata.normalize("NFC", str(value).strip()).casefold()
    return value[:-4] if value.endswith(".txt") else value


def _read_delete_list(path):
    path = Path(path)
    if path.suffix.casefold() == ".rtf":
        completed = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        text = completed.stdout
    else:
        text = path.read_text(encoding="utf-8")
    lines = [unicodedata.normalize("NFC", line.strip()) for line in text.splitlines()]
    return [line for line in lines if line]


def _active_files(conn):
    return conn.execute(
        """
        SELECT f.*, CASE WHEN r.file_id IS NULL THEN 0 ELSE 1 END AS representative
        FROM files AS f LEFT JOIN representatives AS r ON r.file_id = f.file_id
        WHERE f.active = 1
        """
    ).fetchall()


def _exact_title_index(rows):
    index = defaultdict(list)
    for row in rows:
        index[_key(Path(row["canonical_path"]).stem)].append(row)
    return index


def _one(index, title, label):
    rows = index.get(_key(title), [])
    if len(rows) != 1:
        raise RuntimeError(f"{label} must match exactly one active file: {title!r} ({len(rows)})")
    return rows[0]


def _root_keep(conn, file_id, deleting):
    seen = set()
    current = file_id
    while current in deleting:
        if current in seen:
            raise RuntimeError(f"house review keep chain cycle: {file_id}")
        seen.add(current)
        operation = conn.execute(
            """
            SELECT keep_file_id FROM operations
            WHERE file_id = ? AND action = 'house_review_move' AND state = 'committed'
            ORDER BY operation_id DESC LIMIT 1
            """,
            (current,),
        ).fetchone()
        if operation is None or operation["keep_file_id"] is None:
            raise RuntimeError(f"house review keep chain missing: {file_id}")
        current = operation["keep_file_id"]
    keep = conn.execute(
        "SELECT file_id, canonical_path, active FROM files WHERE file_id = ?",
        (current,),
    ).fetchone()
    if keep is None or not keep["active"]:
        raise RuntimeError(f"house review keep is inactive: {file_id}")
    return keep


def build_plan(conn, delete_list, extra_plan):
    rows = _active_files(conn)
    queue_rows = [row for row in rows if QUEUE_FRAGMENT in row["canonical_path"]]
    queue_index = _exact_title_index(queue_rows)
    matched = []
    for title in delete_list:
        matched.append(_one(queue_index, title, "delete-list title"))
    delete_ids = {row["file_id"] for row in matched}
    if len(delete_ids) != len(delete_list):
        raise RuntimeError("delete list contains duplicate normalized titles")

    restore_rows = [row for row in queue_rows if row["file_id"] not in delete_ids]
    delete_rows = []
    for row in matched:
        keep = _root_keep(conn, row["file_id"], delete_ids)
        delete_rows.append({
            "file_id": row["file_id"],
            "path": row["canonical_path"],
            "keep_file_id": keep["file_id"],
            "keep_path": keep["canonical_path"],
            "reason": "rtf_delete_list",
        })

    all_index = _exact_title_index(rows)
    explicit = []
    blocked = []
    for item in extra_plan:
        delete = _one(all_index, item["delete"], "explicit delete title")
        keep = _one(all_index, item["keep"], "explicit keep title")
        payload = {
            "file_id": delete["file_id"],
            "path": delete["canonical_path"],
            "keep_file_id": keep["file_id"],
            "keep_path": keep["canonical_path"],
            "reason": item.get("reason", "user_confirmed_duplicate"),
        }
        if item.get("blocked"):
            blocked.append(payload)
        else:
            explicit.append(payload)

    all_discard_ids = delete_ids | {item["file_id"] for item in explicit}
    for item in explicit:
        if item["keep_file_id"] in all_discard_ids:
            raise RuntimeError(f"explicit keep is also scheduled for discard: {item['keep_path']}")
    return {
        "queue_total": len(queue_rows),
        "queue_delete": delete_rows,
        "queue_restore": [
            {"file_id": row["file_id"], "path": row["canonical_path"]}
            for row in restore_rows
        ],
        "explicit_delete": explicit,
        "blocked": blocked,
    }


def _backup_path(state_db):
    directory = Path(state_db).parent / "backups"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return directory / f"before_review_resolution_{stamp}_{uuid.uuid4().hex[:8]}.sqlite3"


def _issue_run(state_db, backup, house, temp):
    conn = decision_store.connect_state_db(state_db)
    try:
        issues = decision_store.doctor_issues(conn)
        if issues:
            raise RuntimeError(f"doctor failed before actual run: {issues[0]}")
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()
    return decision_store.prepare_actual_run(state_db, house, temp)[0]


def execute(plan, *, state_db, house, temp):
    with mutation_lock_for_roots(house, temp, "resolve-house-review-batch"):
        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            target_ids = {
                item["file_id"]
                for item in (
                    plan["queue_restore"] + plan["queue_delete"]
                    + plan["explicit_delete"]
                )
            } | {
                item["keep_file_id"]
                for item in plan["queue_delete"] + plan["explicit_delete"]
            }
            refreshable = {"stale_identity", "stale_snapshot"}
            blocking = [issue for issue in issues if issue["kind"] not in refreshable]
            if blocking:
                raise RuntimeError(
                    f"non-refreshable doctor issue before resolution: {blocking[0]}"
                )
            refresh_ids = target_ids | {
                issue["file_id"] for issue in issues if issue.get("file_id")
            }
            backup = decision_store.backup_state_db(conn, _backup_path(state_db))
            for file_id in sorted(refresh_ids):
                refresh_user_approved_snapshot(conn, file_id)
            remaining = decision_store.doctor_issues(conn)
            if remaining:
                raise RuntimeError(
                    f"doctor failed after approved rebaseline: {remaining[0]}"
                )
        finally:
            conn.close()

        restore_run_id = _issue_run(state_db, backup, house, temp)
        restored = []
        conn = decision_store.connect_state_db(state_db)
        try:
            try:
                for item in plan["queue_restore"]:
                    restored.append(user_queue_restore(
                        conn, file_id=item["file_id"], run_id=restore_run_id
                    ))
                decision_store.finish_actual_run(
                    conn, restore_run_id, success=True
                )
            except BaseException as exc:
                decision_store.finish_actual_run(
                    conn, restore_run_id, success=False, error=str(exc)
                )
                raise
        finally:
            conn.close()

        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(f"doctor failed after restores: {issues[0]}")
            discard_backup = decision_store.backup_state_db(
                conn, _backup_path(state_db)
            )
        finally:
            conn.close()
        discard_run_id = _issue_run(
            state_db, discard_backup, house, temp
        )

        quarantined = []
        conn = decision_store.connect_state_db(state_db)
        try:
            try:
                for item in plan["queue_delete"] + plan["explicit_delete"]:
                    quarantined.append(user_quarantine(
                        conn,
                        source_file_id=item["file_id"],
                        keep_file_id=item["keep_file_id"],
                        quarantine_dir=Path(temp) / "trash_bin" / "user_discard_quarantine",
                        run_id=discard_run_id,
                        reason=item["reason"],
                    ))
                with decision_store.transaction(conn):
                    for item in plan["queue_restore"]:
                        decision_store.supersede_open_reviews_for_file(
                            conn, item["file_id"], reason="user_selected_restore"
                        )
                decision_store.stamp_superseded_human_disposition_snapshots(conn)
                decision_store.finish_actual_run(
                    conn, discard_run_id, success=True
                )
            except BaseException as exc:
                decision_store.finish_actual_run(
                    conn, discard_run_id, success=False, error=str(exc)
                )
                raise
        finally:
            conn.close()

        folderling.generate_file_list(
            [house], str(FILE_LIST),
            str(FILE_INDEX), state_db_path=state_db,
        )
        folderling.sync_house_index(str(FILE_INDEX), house)
        folderling.sync_extension_index(
            str(FILE_INDEX), str(PROJECT_ROOT)
        )
        _prune_folderling_backups(state_db, Path(state_db).parent / "backups")
        return {
            "restore_run_id": restore_run_id,
            "discard_run_id": discard_run_id,
            "backup": str(backup),
            "discard_backup": str(discard_backup),
            "restored": restored,
            "quarantined": quarantined,
            "blocked": plan["blocked"],
        }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete-list", required=True)
    parser.add_argument("--extra-plan", default=str(DEFAULT_EXTRA_PLAN))
    parser.add_argument("--state-db", default=str(DEFAULT_DB))
    parser.add_argument("--house", default=folderling.DEFAULT_DST_DIR)
    parser.add_argument("--temp", default=folderling.DEFAULT_SRC_DIR)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--ack-user-approved", action="store_true")
    args = parser.parse_args(argv)
    if args.run and not args.ack_user_approved:
        parser.error("--run requires --ack-user-approved")

    delete_list = _read_delete_list(args.delete_list)
    extra_plan = json.loads(Path(args.extra_plan).read_text(encoding="utf-8"))
    conn = decision_store.connect_state_db_readonly(args.state_db)
    try:
        plan = build_plan(conn, delete_list, extra_plan)
    finally:
        conn.close()
    if not args.run:
        print(json.dumps({"dry_run": True, **plan}, ensure_ascii=False, indent=2))
        return 0
    result = execute(
        plan, state_db=args.state_db, house=args.house, temp=args.temp
    )
    print(json.dumps({"dry_run": False, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
