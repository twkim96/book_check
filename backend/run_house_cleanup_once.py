#!/usr/bin/env python3
"""One-time, explicitly approved queueing of existing house review relations."""

import argparse
from collections import defaultdict, deque
import json
import sys
from pathlib import Path

import decision_store
import folderling
from dedup_mutations import HUMAN_REVIEW_CLASSES, house_review_move
from deduplicator import get_better_entry
from mutation_io import mutation_lock_for_roots
from normalizer import analyze_name
from run_folderling_one_button import (
    _prune_folderling_backups,
    _unique_backup_path,
)
from project_paths import FILE_INDEX, FILE_LIST, PROJECT_ROOT, STATE_DB


DEFAULT_DB = STATE_DB


def _entry(row):
    path = Path(row["canonical_path"])
    info = analyze_name(path.name)
    return {
        "file_id": row["file_id"], "path": str(path), "name": path.name,
        "size": row["size"], "ext": info["ext"],
        "effective_max": info.get("effective_max", 0),
        "unit": info.get("unit", "미상"), "complete": info["complete"],
        "span_ambiguous": info.get("span_ambiguous", False),
        "protected": bool(row["protected"]),
    }


QUEUEABLE = {"text_equivalent", "near_identical", "contained_exact", "contained_version"}


def build_plan(conn, scope="queueable", review_ids=None):
    if scope not in {"queueable", "all-pending"}:
        raise ValueError(f"unknown house review scope: {scope}")
    classifications = QUEUEABLE if scope == "queueable" else HUMAN_REVIEW_CLASSES
    placeholders = ", ".join("?" for _ in classifications)
    plans = []
    review_ids = tuple(sorted(set(review_ids or ())))
    review_filter = ""
    params = list(sorted(classifications))
    if review_ids:
        review_filter = "AND ri.review_id IN ({})".format(
            ", ".join("?" for _ in review_ids)
        )
        params.extend(review_ids)
    rows = conn.execute(
        f"""
        SELECT ri.review_id, ri.classification, cf.*, rf.file_id AS r_file_id
        FROM review_items AS ri
        JOIN files AS cf ON cf.file_id = ri.candidate_file_id
        JOIN files AS rf ON rf.file_id = ri.reference_file_id
        WHERE ri.classification IN ({placeholders})
          AND ri.state IN ('pending', 'deferred')
          {review_filter}
        ORDER BY ri.review_id
        """,
        tuple(params),
    ).fetchall()
    files = {}
    edges = defaultdict(list)
    for row in rows:
        right_row = conn.execute(
            "SELECT * FROM files WHERE file_id = ?", (row["r_file_id"],)
        ).fetchone()
        if right_row is None:
            continue
        right = dict(right_row)
        if row["source"] != "house" or right["source"] != "house":
            continue
        if not row["active"] or not right["active"]:
            continue
        if scope == "queueable":
            if row["protected"] or right["protected"]:
                continue
            if row["assignment_state"] in {"legacy_unresolved", "decision_required"}:
                continue
            if right.get("assignment_state") in {"legacy_unresolved", "decision_required"}:
                continue
            if not decision_store.coordinates_compatible(row, right):
                continue
        left_entry, right_entry = _entry(row), _entry(right)
        files[left_entry["file_id"]] = left_entry
        files[right_entry["file_id"]] = right_entry
        edge = (row["review_id"], row["classification"])
        edges[left_entry["file_id"]].append((right_entry["file_id"], edge))
        edges[right_entry["file_id"]].append((left_entry["file_id"], edge))

    seen = set()
    for start in sorted(files):
        if start in seen:
            continue
        component = []
        stack = [start]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            component.append(node)
            stack.extend(neighbor for neighbor, _ in edges[node] if neighbor not in seen)
        if len(component) < 2:
            continue
        protected = [files[node] for node in component if files[node]["protected"]]
        keep = protected[0] if protected else files[component[0]]
        candidates = protected[1:] if protected else [files[node] for node in component[1:]]
        for candidate in candidates:
            keep = get_better_entry(keep, candidate)
        root = keep["file_id"]
        parent = {root: None}
        parent_edge = {}
        depth = {root: 0}
        queue = deque([root])
        while queue:
            node = queue.popleft()
            for neighbor, edge in edges[node]:
                if neighbor in parent:
                    continue
                parent[neighbor] = node
                parent_edge[neighbor] = edge
                depth[neighbor] = depth[node] + 1
                queue.append(neighbor)
        for node in sorted((n for n in component if n != root), key=lambda n: -depth[n]):
            if files[node]["protected"]:
                continue
            review_id, classification = parent_edge[node]
            plans.append({
                "review_id": review_id, "classification": classification,
                "keep_file_id": parent[node], "move_file_id": node,
                "component_keep": keep["path"], "keep": files[parent[node]]["path"],
                "move": files[node]["path"],
            })
    return plans


def run(state_db, house, temp, execute=False, scope="queueable", review_ids=None):
    if not execute:
        conn = decision_store.connect_state_db_readonly(state_db)
        try:
            plans = build_plan(conn, scope=scope, review_ids=review_ids)
            return {
                "dry_run": True,
                "scope": scope,
                "review_ids": sorted(set(review_ids or ())),
                "planned_file_moves": len(plans),
                "plans": plans,
            }
        finally:
            conn.close()

    with mutation_lock_for_roots(house, temp, "house-cleanup-once"):
        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(f"doctor failed: {issues[0]}")
            plans = build_plan(conn, scope=scope, review_ids=review_ids)
            if not plans:
                raise RuntimeError("no eligible house review pairs")
            backup = decision_store.backup_state_db(
                conn, _unique_backup_path(Path(state_db).parent / "backups", "before_house_cleanup")
            )
            decision_store.issue_actual_run_token(
                conn, str(backup), house_dir=house, temp_dir=temp
            )
        finally:
            conn.close()
        run_id, _ = decision_store.prepare_actual_run(state_db, house, temp)
        conn = decision_store.connect_state_db(state_db)
        moved = []
        try:
            for plan in plans:
                if scope == "all-pending":
                    queue_name = "house_human_review"
                else:
                    queue_name = (
                        "house_cleanup_review" if plan["classification"] == "text_equivalent"
                        else "house_cleanup_warning"
                    )
                result = house_review_move(
                    conn, review_id=plan["review_id"],
                    move_file_id=plan["move_file_id"], keep_file_id=plan["keep_file_id"],
                    classification=plan["classification"],
                    queue_dir=Path(temp) / "trash_bin" / queue_name, run_id=run_id,
                )
                moved.append({**plan, **result})
            decision_store.finish_actual_run(conn, run_id, success=True)
        except BaseException as exc:
            decision_store.finish_actual_run(conn, run_id, success=False, error=str(exc))
            raise
        finally:
            conn.close()
            _prune_folderling_backups(state_db, Path(state_db).parent / "backups")

        folderling.generate_file_list(
            [house], str(FILE_LIST),
            str(FILE_INDEX), state_db_path=state_db,
        )
        folderling.sync_house_index(str(FILE_INDEX), house)
        folderling.sync_extension_index(
            str(FILE_INDEX), str(PROJECT_ROOT)
        )
        return {
            "dry_run": False,
            "scope": scope,
            "review_ids": sorted(set(review_ids or ())),
            "run_id": run_id,
            "moved": moved,
        }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-db", default=str(DEFAULT_DB))
    parser.add_argument("--house", default=folderling.DEFAULT_DST_DIR)
    parser.add_argument("--temp", default=folderling.DEFAULT_SRC_DIR)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--ack-user-approved", action="store_true")
    parser.add_argument(
        "--scope",
        choices=("queueable", "all-pending"),
        default="queueable",
        help="all-pending은 report-only 관계까지 최초 사람 검토 큐에 포함합니다.",
    )
    parser.add_argument(
        "--review-id", type=int, action="append", default=[],
        help="지정한 pending/deferred review만 처리합니다. 여러 번 지정할 수 있습니다.",
    )
    args = parser.parse_args(argv)
    if args.run and not args.ack_user_approved:
        parser.error("--run requires --ack-user-approved")
    print(json.dumps(
        run(
            args.state_db, args.house, args.temp, args.run,
            scope=args.scope, review_ids=args.review_id,
        ),
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
