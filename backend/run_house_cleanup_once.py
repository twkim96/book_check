#!/usr/bin/env python3
"""Explicitly queue strong house review relations with a recovery report."""

import argparse
from collections import defaultdict, deque
from datetime import datetime
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

import decision_store
import folderling
from dedup_mutations import (
    HUMAN_REVIEW_CLASSES,
    apply_strong_equivalent_quarantine,
    house_review_move,
)
from deduplicator import get_better_entry
from mutation_io import (
    ensure_directory_nofollow,
    inspect_regular_file,
    mutation_lock_for_roots,
)
from normalizer import analyze_name
from run_folderling_one_button import (
    _prune_folderling_backups,
    _unique_backup_path,
)
from project_paths import FILE_INDEX, FILE_LIST, PROJECT_ROOT, STATE_DB


DEFAULT_DB = STATE_DB
REPORT_SCHEMA_VERSION = 2


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


def _better_house_entry(left, right):
    """Use content/completeness rules, then prefer the shallower identical name."""
    if left["name"] == right["name"]:
        return min(
            (left, right),
            key=lambda entry: (
                len(Path(entry["path"]).parts),
                len(entry["path"]),
                entry["path"],
            ),
        )
    return get_better_entry(left, right)


QUEUEABLE = {
    "text_equivalent", "epub_equivalent",
    "near_identical", "contained_exact", "contained_version",
    "ordered_body_match", "ordered_body_review",
}
EXACT_EQUIVALENT = {"text_equivalent", "epub_equivalent"}


def _managed_identity_compatible(left, right):
    """Fail closed when persisted work/variant identity forbids a merge."""
    blocked_states = {"legacy_unresolved", "decision_required"}
    if (
        left["assignment_state"] in blocked_states
        or right["assignment_state"] in blocked_states
    ):
        return False
    if (
        left["assignment_state"] == "managed"
        and right["assignment_state"] == "managed"
    ):
        return bool(
            left["variant_id"] is not None
            and left["variant_id"] == right["variant_id"]
        )
    return True


def _materialize_component_rebound_reviews(conn, plans):
    """Bind weak evidence to the surviving strong-component representatives."""
    for plan in plans:
        if not plan.get("review_rebind_required"):
            continue
        source = conn.execute(
            "SELECT * FROM review_items WHERE review_id = ?",
            (plan["source_review_id"],),
        ).fetchone()
        if (
            source is None
            or source["state"] not in {"pending", "deferred"}
            or source["classification"] != plan["classification"]
            or {
                source["candidate_file_id"], source["reference_file_id"]
            } != set(plan["source_pair_file_ids"])
        ):
            raise RuntimeError("component rebind source review is stale")
        move = conn.execute(
            "SELECT current_fingerprint_id FROM files "
            "WHERE file_id = ? AND active = 1 AND source = 'house'",
            (plan["move_file_id"],),
        ).fetchone()
        keep = conn.execute(
            "SELECT current_fingerprint_id FROM files "
            "WHERE file_id = ? AND active = 1 AND source = 'house'",
            (plan["keep_file_id"],),
        ).fetchone()
        if (
            move is None
            or keep is None
            or move["current_fingerprint_id"] is None
            or keep["current_fingerprint_id"] is None
        ):
            raise RuntimeError("component rebind endpoint is not current")
        existing = conn.execute(
            """
            SELECT review_id, evidence_json FROM review_items
            WHERE state IN ('pending', 'deferred') AND classification = ?
              AND (
                (candidate_file_id = ? AND reference_file_id = ?
                 AND left_fingerprint_id = ? AND right_fingerprint_id = ?)
                OR
                (candidate_file_id = ? AND reference_file_id = ?
                 AND left_fingerprint_id = ? AND right_fingerprint_id = ?)
              )
            ORDER BY review_id DESC LIMIT 1
            """,
            (
                plan["classification"],
                plan["move_file_id"], plan["keep_file_id"],
                move["current_fingerprint_id"], keep["current_fingerprint_id"],
                plan["keep_file_id"], plan["move_file_id"],
                keep["current_fingerprint_id"], move["current_fingerprint_id"],
            ),
        ).fetchone()
        if existing is not None:
            review_id = existing["review_id"]
        else:
            evidence = dict(plan.get("review_evidence") or {})
            evidence["strong_component_rebind"] = {
                "version": "1.4.10",
                "source_review_id": plan["source_review_id"],
                "source_pair_file_ids": list(plan["source_pair_file_ids"]),
                "final_pair_file_ids": [
                    plan["move_file_id"], plan["keep_file_id"]
                ],
            }
            review_id = decision_store.add_review_item(
                conn,
                candidate_file_id=plan["move_file_id"],
                reference_file_id=plan["keep_file_id"],
                classification=plan["classification"],
                evidence_json=json.dumps(
                    evidence, ensure_ascii=False, sort_keys=True
                ),
            )
            plan["review_evidence"] = evidence
        plan["review_id"] = review_id
        plan["rebound_review_id"] = review_id


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
        SELECT ri.review_id, ri.classification,
               ri.left_fingerprint_id, ri.right_fingerprint_id,
               ri.evidence_json AS review_evidence_json,
               cf.*, rf.file_id AS r_file_id
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
    edge_records = []
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
        if (
            (row["protected"] or right["protected"])
            and row["classification"] not in EXACT_EQUIVALENT
        ):
            continue
        if not _managed_identity_compatible(row, right):
            continue
        if not decision_store.coordinates_compatible(row, right):
            continue
        left_entry, right_entry = _entry(row), _entry(right)
        files[left_entry["file_id"]] = left_entry
        files[right_entry["file_id"]] = right_entry
        try:
            review_evidence = json.loads(row["review_evidence_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            review_evidence = {
                "previous_evidence": row["review_evidence_json"],
            }
        edge = {
            "review_id": row["review_id"],
            "classification": row["classification"],
            "left_fingerprint_id": row["left_fingerprint_id"],
            "right_fingerprint_id": row["right_fingerprint_id"],
            "review_evidence": review_evidence,
            "left_file_id": left_entry["file_id"],
            "right_file_id": right_entry["file_id"],
        }
        edge_records.append((left_entry["file_id"], right_entry["file_id"], edge))

    union_parent = {file_id: file_id for file_id in files}

    def find(node):
        while union_parent[node] != node:
            union_parent[node] = union_parent[union_parent[node]]
            node = union_parent[node]
        return node

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            union_parent[max(left_root, right_root)] = min(left_root, right_root)

    strong_adjacency = defaultdict(list)
    for left, right, edge in edge_records:
        if edge["classification"] not in EXACT_EQUIVALENT:
            continue
        union(left, right)
        strong_adjacency[left].append((right, edge))
        strong_adjacency[right].append((left, edge))

    strong_components = defaultdict(list)
    for file_id in files:
        strong_components[find(file_id)].append(file_id)

    representative = {}
    representative_protected = {}
    strong_plans = []

    def plan_record(edge, move_id, keep_id, component_keep, phase):
        record = {
            **edge,
            "phase": phase,
            "keep_file_id": keep_id,
            "move_file_id": move_id,
            "component_keep": component_keep,
            "keep": files[keep_id]["path"],
            "move": files[move_id]["path"],
        }
        source_pair = (edge["left_file_id"], edge["right_file_id"])
        if set(source_pair) != {move_id, keep_id}:
            record["source_review_id"] = edge["review_id"]
            record["source_pair_file_ids"] = source_pair
            record["review_rebind_required"] = True
        else:
            record["review_rebind_required"] = False
        return record

    for component in sorted(strong_components.values(), key=lambda value: min(value)):
        protected = [node for node in component if files[node]["protected"]]
        if len(protected) > 1:
            for node in component:
                representative[node] = node
                representative_protected[node] = True
            continue
        keep = files[protected[0]] if protected else files[component[0]]
        if not protected:
            for node in component[1:]:
                keep = _better_house_entry(keep, files[node])
        root = keep["file_id"]
        for node in component:
            representative[node] = root
        representative_protected[root] = bool(protected)
        if len(component) < 2:
            continue
        parent = {root: None}
        parent_edge = {}
        depth = {root: 0}
        queue = deque([root])
        while queue:
            node = queue.popleft()
            for neighbor, edge in sorted(
                strong_adjacency[node],
                key=lambda value: (value[0], value[1]["review_id"]),
            ):
                if neighbor in parent:
                    continue
                parent[neighbor] = node
                parent_edge[neighbor] = edge
                depth[neighbor] = depth[node] + 1
                queue.append(neighbor)
        for node in sorted(
            (value for value in component if value != root),
            key=lambda value: (-depth[value], value),
        ):
            strong_plans.append(plan_record(
                parent_edge[node], node, parent[node], keep["path"], "strong"
            ))

    weak_adjacency = defaultdict(list)
    for left, right, edge in edge_records:
        if edge["classification"] in EXACT_EQUIVALENT:
            continue
        mapped_left = representative[left]
        mapped_right = representative[right]
        if mapped_left == mapped_right:
            continue
        if (
            representative_protected.get(mapped_left, False)
            or representative_protected.get(mapped_right, False)
        ):
            continue
        weak_adjacency[mapped_left].append((mapped_right, edge))
        weak_adjacency[mapped_right].append((mapped_left, edge))

    weak_plans = []
    seen = set()
    for start in sorted(weak_adjacency):
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
            stack.extend(
                neighbor for neighbor, _edge in weak_adjacency[node]
                if neighbor not in seen
            )
        if len(component) < 2:
            continue
        keep = files[component[0]]
        for node in component[1:]:
            keep = _better_house_entry(keep, files[node])
        root = keep["file_id"]
        parent = {root: None}
        parent_edge = {}
        depth = {root: 0}
        queue = deque([root])
        while queue:
            node = queue.popleft()
            for neighbor, edge in sorted(
                weak_adjacency[node],
                key=lambda value: (value[0], value[1]["review_id"]),
            ):
                if neighbor in parent:
                    continue
                parent[neighbor] = node
                parent_edge[neighbor] = edge
                depth[neighbor] = depth[node] + 1
                queue.append(neighbor)
        for node in sorted(
            (value for value in component if value != root),
            key=lambda value: (-depth[value], value),
        ):
            weak_plans.append(plan_record(
                parent_edge[node], node, parent[node], keep["path"], "weak"
            ))
    return [*strong_plans, *weak_plans]


def run(
    state_db, house, temp, execute=False, scope="queueable", review_ids=None,
    *, report_path=None, intent_report_path=None,
):
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
        if intent_report_path is None:
            intent_report_path = _intent_report_path(temp)
        if report_path is not None and Path(report_path) == Path(intent_report_path):
            raise RuntimeError("intent and terminal report paths must differ")
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
            intent = write_intent_report(
                intent_report_path,
                plans=plans,
                scope=scope,
                review_ids=review_ids,
                state_db=state_db,
                house=house,
                temp=temp,
                backup=backup,
            )
            decision_store.issue_actual_run_token(
                conn, str(backup), house_dir=house, temp_dir=temp
            )
        finally:
            conn.close()
        manifest_paths = [intent["path"]]
        for plan in plans:
            manifest_paths.extend((plan["move"], plan["keep"]))
        manifest_paths = list(dict.fromkeys(manifest_paths))
        run_id, manifest_path = decision_store.prepare_actual_run(
            state_db, house, temp, manifest_paths=manifest_paths
        )
        conn = decision_store.connect_state_db(state_db)
        moved = []
        run_finished = False
        try:
            _materialize_component_rebound_reviews(conn, plans)
            for plan in plans:
                if scope == "all-pending":
                    queue_name = "house_human_review"
                else:
                    queue_name = (
                        "house_cleanup_review"
                        if plan["classification"] in {"text_equivalent", "epub_equivalent"}
                        else "house_cleanup_warning"
                    )
                if (
                    scope != "all-pending"
                    and plan["classification"] in EXACT_EQUIVALENT
                ):
                    result = apply_strong_equivalent_quarantine(
                        conn,
                        review_id=plan["review_id"],
                        discard_file_id=plan["move_file_id"],
                        keep_file_id=plan["keep_file_id"],
                        classification=plan["classification"],
                        quarantine_dir=(
                            Path(temp) / "trash_bin" /
                            "strong_equivalent_duplicates"
                        ),
                        run_id=run_id,
                    )
                else:
                    result = house_review_move(
                        conn, review_id=plan["review_id"],
                        move_file_id=plan["move_file_id"],
                        keep_file_id=plan["keep_file_id"],
                        classification=plan["classification"],
                        queue_dir=Path(temp) / "trash_bin" / queue_name,
                        run_id=run_id,
                    )
                moved.append({**plan, **result})
            index_ok = folderling.generate_file_list(
                [house], str(FILE_LIST),
                str(FILE_INDEX), state_db_path=state_db, temp_root=temp,
            )
            if not index_ok:
                raise RuntimeError("file list/index generation failed")
            folderling.sync_house_index(str(FILE_INDEX), house)
            folderling.sync_extension_index(
                str(FILE_INDEX), str(PROJECT_ROOT)
            )
            _prune_folderling_backups(
                state_db, Path(state_db).parent / "backups"
            )
            decision_store.finish_actual_run(conn, run_id, success=True)
            run_finished = True
        except BaseException as exc:
            if not run_finished:
                decision_store.finish_actual_run(
                    conn, run_id, success=False, error=str(exc)
                )
            raise
        finally:
            conn.close()

        result = {
            "dry_run": False,
            "scope": scope,
            "review_ids": sorted(set(review_ids or ())),
            "run_id": run_id,
            "manifest_path": manifest_path,
            "backup_path": str(backup),
            "intent_report_path": intent["path"],
            "intent_report_sha256": intent["sha256"],
            "moved": moved,
        }
        if report_path is not None:
            result["report_path"] = str(report_path)
            write_execution_report(
                report_path,
                result=result,
                state_db=state_db,
            )
        return result


def _report_path(temp_dir, requested=None):
    lexical_root = Path(temp_dir).resolve() / "dedup_logs"
    ensure_directory_nofollow(lexical_root)
    root = lexical_root.resolve()
    if requested:
        target = Path(requested).expanduser().resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                "--report-path must resolve inside <temp>/dedup_logs"
            ) from exc
        if target.parent != root:
            raise ValueError("--report-path must be a direct child of <temp>/dedup_logs")
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"report path already exists: {target}")
        return target
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = root / f"house_cleanup_1_4_0_{stamp}.json"
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"report path already exists: {target}")
    return target


def _intent_report_path(temp_dir):
    lexical_root = Path(temp_dir).resolve() / "dedup_logs"
    ensure_directory_nofollow(lexical_root)
    root = lexical_root.resolve()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = root / f"house_cleanup_1_4_0_{stamp}.json"
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"intent report path already exists: {target}")
    return target


def _cleanup_plan_sha256(*, plans, scope, review_ids):
    payload = {
        "scope": scope,
        "review_ids": sorted(set(review_ids or ())),
        "plans": plans,
    }
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def write_intent_report(
    path, *, plans, scope, review_ids, state_db, house, temp, backup
):
    backup_evidence = inspect_regular_file(backup)
    plan_sha256 = _cleanup_plan_sha256(
        plans=plans, scope=scope, review_ids=review_ids
    )
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "house_cleanup_intent_1_4_0",
        "phase": "intent",
        "generated_at": datetime.now().astimezone().isoformat(),
        "plan_sha256": plan_sha256,
        "plan": {
            "scope": scope,
            "review_ids": sorted(set(review_ids or ())),
            "plans": plans,
        },
        "run_preparation": {
            "state_db": str(Path(state_db).resolve()),
            "house_root": str(Path(house).resolve()),
            "temp_root": str(Path(temp).resolve()),
            "backup": {
                "path": str(Path(backup).resolve()),
                "sha256": backup_evidence.sha256,
                "size": backup_evidence.size,
                "mtime_ns": backup_evidence.mtime_ns,
            },
        },
    }
    target = Path(path)
    temporary = target.with_name(target.name + f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.link(temporary, target, follow_symlinks=False)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    evidence = inspect_regular_file(target)
    return {
        "path": str(target),
        "sha256": evidence.sha256,
        "plan_sha256": plan_sha256,
    }


def write_execution_report(path, *, result, state_db, index_path=FILE_INDEX):
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        final_snapshot = {
            "doctor_issues": decision_store.doctor_issues(conn),
            "active_house": conn.execute(
                "SELECT COUNT(*) FROM files WHERE active=1 AND source='house'"
            ).fetchone()[0],
            "active_queue": conn.execute(
                "SELECT COUNT(*) FROM files WHERE active=1 AND source='queue'"
            ).fetchone()[0],
            "unfinished_operations": conn.execute(
                "SELECT COUNT(*) FROM operations "
                "WHERE state IN ('planned', 'fs_done', 'db_done')"
            ).fetchone()[0],
            "active_runs": conn.execute(
                "SELECT COUNT(*) FROM actual_runs WHERE state='active'"
            ).fetchone()[0],
        }
    finally:
        conn.close()
    try:
        index = json.loads(Path(index_path).read_text(encoding="utf-8"))
        final_snapshot["index"] = {
            "generation_id": index.get("generation_id"),
            "generated_at": index.get("generated_at"),
            "entries": len(index.get("entries") or []),
        }
    except (OSError, ValueError, TypeError):
        final_snapshot["index"] = {"read_error": True}
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "house_cleanup_1_4_0",
        "generated_at": datetime.now().astimezone().isoformat(),
        "result": result,
        "final_snapshot": final_snapshot,
    }
    target = Path(path)
    temporary = target.with_name(target.name + f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.link(temporary, target, follow_symlinks=False)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return str(target)


def _recovery_context(state_db, limit=4):
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        runs = [dict(row) for row in conn.execute(
            """
            SELECT run_id, state, backup_path, manifest_path, error,
                   approved_at, activated_at, finished_at
            FROM actual_runs ORDER BY rowid DESC LIMIT ?
            """,
            (limit,),
        )]
        run_ids = [row["run_id"] for row in runs]
        operations = []
        if run_ids:
            placeholders = ", ".join("?" for _ in run_ids)
            operations = [dict(row) for row in conn.execute(
                f"""
                SELECT operation_id, run_id, action, state, file_id,
                       source_path, dest_path, error
                FROM operations WHERE run_id IN ({placeholders})
                ORDER BY operation_id
                """,
                tuple(run_ids),
            )]
        return {"actual_runs": runs, "operations": operations}
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-db", default=str(DEFAULT_DB))
    parser.add_argument("--house", default=folderling.DEFAULT_DST_DIR)
    parser.add_argument("--temp", default=folderling.DEFAULT_SRC_DIR)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--ack-user-approved", action="store_true")
    parser.add_argument(
        "--report-path",
        help="actual-run JSON log path below <temp>/dedup_logs",
    )
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
    report_target = (
        _report_path(args.temp, args.report_path) if args.run else None
    )
    intent_target = _intent_report_path(args.temp) if args.run else None
    try:
        result = run(
            args.state_db, args.house, args.temp, args.run,
            scope=args.scope, review_ids=args.review_id,
            report_path=report_target,
            intent_report_path=intent_target,
        )
    except BaseException as exc:
        if report_target is not None:
            failure = {
                "dry_run": False,
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "report_path": str(report_target),
                "recovery": _recovery_context(args.state_db),
            }
            if intent_target is not None and intent_target.is_file():
                intent_evidence = inspect_regular_file(intent_target)
                failure["intent_report_path"] = str(intent_target)
                failure["intent_report_sha256"] = intent_evidence.sha256
            try:
                write_execution_report(
                    report_target,
                    result=failure,
                    state_db=args.state_db,
                )
            except BaseException as report_exc:
                print(
                    f"failed to write recovery report: {report_exc}",
                    file=sys.stderr,
                )
        raise
    if args.run and "report_path" not in result:
        result["report_path"] = write_execution_report(
            report_target,
            result=result,
            state_db=args.state_db,
        )
    print(json.dumps(
        result,
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
