#!/usr/bin/env python3
"""Plan-bound, journaled cleanup for the 1.4.4 quarantine audit.

The plan is intentionally data-only.  Dry-run validates every current source,
reference, hash and destination.  Actual mode consumes two one-time mutation
capabilities: one for restore/representative changes and one for irreversible
purge.  The terminal JSON report is the operator audit trail.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import decision_store
import folderling
from dedup_mutations import (
    record_user_approved_purge_revalidation,
    refresh_user_approved_snapshot,
    user_quarantine,
    user_queue_accept_to_house,
)
from library_management import (
    _backup_path,
    _quarantine_origin,
    _restore_quarantine_file,
    purge_preview,
    restore_preview,
)
from mutation_io import (
    ensure_directory_nofollow,
    inspect_regular_file,
    mutation_lock,
    mutation_lock_for_roots,
    unlink_owned,
)
from project_paths import FILE_INDEX, FILE_LIST, PROJECT_ROOT, STATE_DB
from run_folderling_one_button import _prune_folderling_backups


KIND = "quarantine_cleanup_1_4_4"
SCHEMA_VERSION = 1
PURGE_CHUNK = 200


def _canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _hash(value):
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _atomic_json(path, value):
    path = Path(path)
    ensure_directory_nofollow(path.parent)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _load_plan(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != KIND
    ):
        raise ValueError("cleanup plan schema/kind mismatch")
    return payload


def _file(conn, file_id, *, active=None):
    clause = "" if active is None else " AND active = ?"
    params = (file_id,) if active is None else (file_id, int(bool(active)))
    row = conn.execute(
        "SELECT * FROM files WHERE file_id = ?" + clause, params
    ).fetchone()
    if row is None:
        raise RuntimeError(f"file snapshot not found: {file_id}")
    return row


def _assert_hash(path, expected, label):
    if not isinstance(expected, str) or len(expected) != 64:
        raise ValueError(f"{label} requires SHA-256")
    evidence = inspect_regular_file(path)
    if evidence.sha256 != expected.lower():
        raise RuntimeError(f"{label} SHA-256 changed: {path}")
    return evidence


def _safe_destination(house, relative):
    relative = Path(str(relative))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise RuntimeError(f"unsafe house destination: {relative}")
    destination = (Path(house).resolve() / relative).resolve()
    destination.relative_to(Path(house).resolve())
    if destination.exists() or destination.is_symlink():
        raise RuntimeError(f"house destination already exists: {destination}")
    return destination


def _origin(conn, operation_id):
    row = _quarantine_origin(conn, int(operation_id))
    if (
        row["state"] != "committed"
        or row["purged_at"] is not None
        or row["file_active"]
        or row["file_source"] != "quarantine"
    ):
        raise RuntimeError(f"quarantine origin is not live: {operation_id}")
    return row


def build_preview(plan, *, state_db, house, temp):
    plan_sha256 = _hash(plan)
    conn = decision_store.connect_state_db_readonly(state_db)
    manifest_paths = []
    restore_plans = []
    try:
        issues = decision_store.doctor_issues(conn)
        if issues:
            raise RuntimeError(f"doctor failed: {issues[0]['kind']}")

        restore_ids = set()
        for item in plan.get("restore", []):
            operation_id = int(item["operation_id"])
            if operation_id in restore_ids:
                raise RuntimeError(f"duplicate restore operation: {operation_id}")
            restore_ids.add(operation_id)
            preview = restore_preview(
                state_db,
                house_dir=Path(house),
                operation_id=operation_id,
                reference_file_id=item["reference_file_id"],
                verdict=item["verdict"],
                note=item.get("note", ""),
                destination_rel=item["destination_rel"],
            )
            if not preview["apply_available"]:
                raise RuntimeError(
                    f"restore {operation_id} blocked: {preview['blocked_reasons']}"
                )
            _assert_hash(
                preview["quarantine_path"],
                item["expected_source_sha256"],
                f"restore {operation_id} source",
            )
            _assert_hash(
                preview["reference"]["canonical_path"],
                item["expected_reference_sha256"],
                f"restore {operation_id} reference",
            )
            restore_plans.append(preview)
            manifest_paths.extend(
                (
                    preview["quarantine_path"],
                    preview["reference"]["canonical_path"],
                )
            )

        queue_ids = set()
        for section in ("queue_discard", "queue_upgrade"):
            for item in plan.get(section, []):
                file_id = item["file_id"]
                if file_id in queue_ids:
                    raise RuntimeError(f"duplicate queue action: {file_id}")
                queue_ids.add(file_id)
                source = _file(conn, file_id, active=True)
                keep = _file(conn, item["keep_file_id"], active=True)
                if source["source"] != "queue" or keep["source"] != "house":
                    raise RuntimeError(f"queue action endpoints changed: {file_id}")
                _assert_hash(
                    source["canonical_path"], item["expected_source_sha256"],
                    f"{section} source",
                )
                _assert_hash(
                    keep["canonical_path"], item["expected_keep_sha256"],
                    f"{section} keep",
                )
                manifest_paths.extend(
                    (source["canonical_path"], keep["canonical_path"])
                )
                if section == "queue_upgrade":
                    _safe_destination(house, item["destination_rel"])

        for item in plan.get("untracked_queue_discard", []):
            source = Path(item["path"]).resolve()
            source.relative_to(Path(temp).resolve())
            keep = _file(conn, item["keep_file_id"], active=True)
            if keep["source"] != "house":
                raise RuntimeError("untracked discard keep left house")
            _assert_hash(
                source, item["expected_source_sha256"], "untracked queue source"
            )
            _assert_hash(
                keep["canonical_path"], item["expected_keep_sha256"],
                "untracked queue keep",
            )
            manifest_paths.extend((str(source), keep["canonical_path"]))

        for item in plan.get("metadata_cleanup", []):
            path = Path(item["path"]).resolve()
            path.relative_to(Path(temp).resolve() / "trash_bin")
            _assert_hash(path, item["expected_sha256"], "metadata cleanup")
            manifest_paths.append(str(path))

        revalidation_ids = set()
        for item in plan.get("purge_revalidation", []):
            operation_id = int(item["operation_id"])
            if operation_id in revalidation_ids or operation_id in restore_ids:
                raise RuntimeError(
                    f"conflicting purge revalidation: {operation_id}"
                )
            revalidation_ids.add(operation_id)
            origin = _origin(conn, operation_id)
            keep = _file(conn, item["keep_file_id"], active=True)
            if keep["source"] != "house":
                raise RuntimeError(f"revalidation keep left house: {operation_id}")
            source_path = origin["quarantine_path"] or origin["dest_path"]
            _assert_hash(
                source_path, item["expected_source_sha256"],
                f"revalidation {operation_id} source",
            )
            _assert_hash(
                keep["canonical_path"], item["expected_keep_sha256"],
                f"revalidation {operation_id} keep",
            )
            manifest_paths.extend((source_path, keep["canonical_path"]))

        purge_ids = [int(value) for value in plan.get("purge_operation_ids", [])]
        if len(purge_ids) != len(set(purge_ids)):
            raise RuntimeError("duplicate purge operation id")
        if restore_ids & set(purge_ids):
            raise RuntimeError("restore operation is also scheduled for purge")
        for operation_id in purge_ids:
            _origin(conn, operation_id)
        if not revalidation_ids.issubset(set(purge_ids)):
            raise RuntimeError("every revalidation must be scheduled for purge")

        missing_ids = set()
        for item in plan.get("missing_quarantine_ack", []):
            operation_id = int(item["operation_id"])
            if (
                operation_id in missing_ids
                or operation_id in restore_ids
                or operation_id in revalidation_ids
                or operation_id in purge_ids
            ):
                raise RuntimeError(
                    f"conflicting missing-quarantine acknowledgement: {operation_id}"
                )
            missing_ids.add(operation_id)
            origin = _origin(conn, operation_id)
            path = Path(item["expected_missing_path"])
            if path.exists() or path.is_symlink():
                raise RuntimeError(
                    f"missing quarantine exists again: {operation_id}"
                )
            if (
                origin["file_id"] != item["file_id"]
                or str(path) != origin["file_path"]
                or origin["destination_sha256"]
                != item["historical_destination_sha256"]
                or any(
                    origin[key] is None for key in (
                        "destination_dev", "destination_ino",
                        "destination_ctime_ns", "destination_size",
                        "destination_mtime_ns", "destination_sha256",
                    )
                )
            ):
                raise RuntimeError(
                    f"missing quarantine history changed: {operation_id}"
                )

        for item in plan.get("upgrade_restored", []):
            if int(item["restore_operation_id"]) not in restore_ids:
                raise RuntimeError("upgrade_restored references an unknown restore")
            old_keep = _file(conn, item["old_keep_file_id"], active=True)
            if old_keep["source"] != "house":
                raise RuntimeError("upgrade old keep left house")
            _assert_hash(
                old_keep["canonical_path"], item["expected_old_keep_sha256"],
                "restored upgrade old keep",
            )
            manifest_paths.append(old_keep["canonical_path"])

        manifest_paths = list(dict.fromkeys(str(path) for path in manifest_paths))
        return {
            "kind": KIND,
            "schema_version": SCHEMA_VERSION,
            "plan_sha256": plan_sha256,
            "restore_count": len(restore_plans),
            "queue_discard_count": len(plan.get("queue_discard", [])),
            "queue_upgrade_count": len(plan.get("queue_upgrade", [])),
            "untracked_discard_count": len(
                plan.get("untracked_queue_discard", [])
            ),
            "revalidation_count": len(revalidation_ids),
            "missing_ack_count": len(missing_ids),
            "existing_purge_count": len(purge_ids),
            "manifest_paths": manifest_paths,
            "restore_plans": restore_plans,
        }
    finally:
        conn.close()


def _issue_run(state_db, house, temp, label, manifest_paths):
    conn = decision_store.connect_state_db(state_db)
    try:
        backup = decision_store.backup_state_db(
            conn, _backup_path(Path(state_db), label)
        )
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()
    run_id, manifest_path = decision_store.prepare_actual_run(
        state_db, house, temp, manifest_paths=manifest_paths
    )
    return run_id, manifest_path, str(backup)


def _refresh_indexes(state_db, house, temp):
    if not folderling.generate_file_list(
        [house], str(FILE_LIST), str(FILE_INDEX),
        state_db_path=state_db, temp_root=temp,
    ):
        raise RuntimeError("file list/index generation failed")
    folderling.sync_house_index(str(FILE_INDEX), house)
    folderling.sync_extension_index(str(FILE_INDEX), str(PROJECT_ROOT))


def _apply_dispositions(plan, preview, *, state_db, house, temp):
    run_id, manifest_path, backup = _issue_run(
        state_db, house, temp, "quarantine_cleanup_1_4_4_disposition",
        preview["manifest_paths"],
    )
    conn = decision_store.connect_state_db(state_db)
    results = {
        "run_id": run_id, "manifest_path": manifest_path,
        "backup_path": backup, "revalidated": [], "restored": [],
        "queue_discarded": [], "upgrades": [], "untracked_discarded": [],
        "missing_acknowledged": [],
        "metadata_removed": [],
        "new_purge_operation_ids": [],
    }
    finished = False
    created_group_ids = []
    try:
        group_id = None
        if plan.get("purge_revalidation"):
            with decision_store.transaction(conn):
                group_id = decision_store.create_operation_group(
                    conn,
                    run_id=run_id,
                    action="quarantine_cleanup_1_4_4_revalidation",
                    plan_sha256=preview["plan_sha256"],
                    item_count=len(plan["purge_revalidation"]),
                    manifest_path=manifest_path,
                    source_manifest_json=_canonical_json(plan),
                )
            created_group_ids.append(group_id)
            for item in plan["purge_revalidation"]:
                result = record_user_approved_purge_revalidation(
                    conn,
                    origin_operation_id=item["operation_id"],
                    keep_file_id=item["keep_file_id"],
                    run_id=run_id,
                    operation_group_id=group_id,
                )
                results["revalidated"].append(result)
            with decision_store.transaction(conn):
                decision_store.transition_operation_group(conn, group_id, "fs_done")
                decision_store.transition_operation_group(conn, group_id, "db_done")
                decision_store.transition_operation_group(conn, group_id, "committed")
            results["revalidation_group_id"] = group_id

        if plan.get("missing_quarantine_ack"):
            with decision_store.transaction(conn):
                missing_group_id = decision_store.create_operation_group(
                    conn,
                    run_id=run_id,
                    action="quarantine_cleanup_1_4_4_missing_ack",
                    plan_sha256=preview["plan_sha256"],
                    item_count=len(plan["missing_quarantine_ack"]),
                    manifest_path=manifest_path,
                    source_manifest_json=_canonical_json(plan),
                )
                created_group_ids.append(missing_group_id)
                for item in plan["missing_quarantine_ack"]:
                    origin = _origin(conn, item["operation_id"])
                    operation_id = decision_store.create_operation(
                        conn,
                        run_id=run_id,
                        action="quarantine_purge",
                        source_path=item["expected_missing_path"],
                        file_id=origin["file_id"],
                        keep_file_id=origin["keep_file_id"],
                        expected_size=origin["destination_size"],
                        expected_mtime_ns=origin["destination_mtime_ns"],
                        expected_fingerprint_id=origin["expected_fingerprint_id"],
                        expected_keep_fingerprint_id=origin[
                            "expected_keep_fingerprint_id"
                        ],
                        parent_operation_id=origin["operation_id"],
                        operation_group_id=missing_group_id,
                        source_dev=origin["destination_dev"],
                        source_ino=origin["destination_ino"],
                        source_ctime_ns=origin["destination_ctime_ns"],
                        source_sha256=origin["destination_sha256"],
                    )
                    decision_store.transition_operation(
                        conn, operation_id, "fs_done"
                    )
                    conn.execute(
                        "UPDATE operations SET purged_at = CURRENT_TIMESTAMP "
                        "WHERE operation_id = ?",
                        (origin["operation_id"],),
                    )
                    decision_store.transition_operation(
                        conn, operation_id, "db_done"
                    )
                    decision_store.transition_operation(
                        conn, operation_id, "committed"
                    )
                    results["missing_acknowledged"].append({
                        "origin_operation_id": origin["operation_id"],
                        "purge_operation_id": operation_id,
                        "path": item["expected_missing_path"],
                    })
                decision_store.transition_operation_group(
                    conn, missing_group_id, "fs_done"
                )
                decision_store.transition_operation_group(
                    conn, missing_group_id, "db_done"
                )
                decision_store.transition_operation_group(
                    conn, missing_group_id, "committed"
                )
            results["missing_ack_group_id"] = missing_group_id

        restored_by_origin = {}
        for restore_plan in preview["restore_plans"]:
            result = _restore_quarantine_file(
                conn, plan=restore_plan, run_id=run_id
            )
            restored_by_origin[int(restore_plan["operation_id"])] = result
            results["restored"].append(result)

        for item in plan.get("upgrade_restored", []):
            restored = restored_by_origin[int(item["restore_operation_id"])]
            result = user_quarantine(
                conn,
                source_file_id=item["old_keep_file_id"],
                keep_file_id=item["restored_file_id"],
                quarantine_dir=(
                    Path(temp) / "trash_bin" / "superseded_versions"
                ),
                run_id=run_id,
                reason="v1_4_4_restored_longer_version_upgrade",
                keep_origin_operation_id=restored["operation_id"],
            )
            results["upgrades"].append(result)
            results["new_purge_operation_ids"].append(result["operation_id"])

        for item in plan.get("queue_discard", []):
            result = user_quarantine(
                conn,
                source_file_id=item["file_id"],
                keep_file_id=item["keep_file_id"],
                quarantine_dir=(
                    Path(temp) / "trash_bin" / "user_discard_quarantine"
                ),
                run_id=run_id,
                reason="v1_4_4_audited_queue_duplicate",
            )
            results["queue_discarded"].append(result)
            results["new_purge_operation_ids"].append(result["operation_id"])

        for item in plan.get("queue_upgrade", []):
            destination = _safe_destination(house, item["destination_rel"])
            accepted = user_queue_accept_to_house(
                conn,
                file_id=item["file_id"],
                destination=destination,
                run_id=run_id,
            )
            discarded = user_quarantine(
                conn,
                source_file_id=item["keep_file_id"],
                keep_file_id=item["file_id"],
                quarantine_dir=(
                    Path(temp) / "trash_bin" / "superseded_versions"
                ),
                run_id=run_id,
                reason="v1_4_4_queue_longer_version_upgrade",
                keep_origin_operation_id=accepted["operation_id"],
            )
            results["upgrades"].append(
                {"accepted": accepted, "discarded": discarded}
            )
            results["new_purge_operation_ids"].append(
                discarded["operation_id"]
            )

        for item in plan.get("untracked_queue_discard", []):
            source = Path(item["path"]).resolve()
            with decision_store.transaction(conn):
                reconciled = decision_store.reconcile_file_metadata(
                    conn, source, source="queue"
                )
            file_id = reconciled["file_id"]
            refresh_user_approved_snapshot(conn, file_id)
            result = user_quarantine(
                conn,
                source_file_id=file_id,
                keep_file_id=item["keep_file_id"],
                quarantine_dir=(
                    Path(temp) / "trash_bin" / "user_discard_quarantine"
                ),
                run_id=run_id,
                reason="v1_4_4_audited_untracked_queue_duplicate",
            )
            results["untracked_discarded"].append(result)
            results["new_purge_operation_ids"].append(result["operation_id"])

        actual_run = decision_store.assert_active_actual_run(conn, run_id)
        for item in plan.get("metadata_cleanup", []):
            path = Path(item["path"])
            evidence = _assert_hash(
                path, item["expected_sha256"], "metadata cleanup"
            )
            decision_store.assert_actual_run_path(
                actual_run, path, "temp_root"
            )
            decision_store.assert_manifest_source(
                actual_run, path, "temp_root", evidence
            )
            unlink_owned(path, expected=evidence)
            results["metadata_removed"].append(str(path))

        _refresh_indexes(state_db, house, temp)
        decision_store.finish_actual_run(conn, run_id, success=True)
        finished = True
        return results
    except BaseException as exc:
        with decision_store.transaction(conn):
            for group_id in created_group_ids:
                row = conn.execute(
                    "SELECT state FROM operation_groups WHERE group_id = ?",
                    (group_id,),
                ).fetchone()
                if row is not None and row["state"] in {
                    "planned", "fs_done", "db_done"
                }:
                    decision_store.transition_operation_group(
                        conn, group_id, "failed", error=str(exc)
                    )
        if not finished:
            decision_store.finish_actual_run(
                conn, run_id, success=False, error=str(exc)
            )
        raise
    finally:
        conn.close()


def _chunks(values, size=PURGE_CHUNK):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _purge_rows_preview(state_db, operation_ids):
    plans = []
    manifest_paths = []
    total_size = 0
    for chunk in _chunks(operation_ids):
        plan = purge_preview(Path(state_db), operation_ids=chunk)
        if not plan["apply_available"]:
            raise RuntimeError(
                "purge safety validation failed: "
                + ",".join(plan["blocked_reasons"][:10])
            )
        plans.append(plan)
        total_size += plan["total_size"]
        for item in plan["items"]:
            manifest_paths.append(item["path"])
            if item.get("keep_path"):
                manifest_paths.append(item["keep_path"])
    return {
        "plans": plans,
        "manifest_paths": list(dict.fromkeys(manifest_paths)),
        "item_count": sum(plan["item_count"] for plan in plans),
        "total_size": total_size,
    }


def _apply_purge(state_db, house, temp, operation_ids):
    preview = _purge_rows_preview(state_db, operation_ids)
    run_id, manifest_path, backup = _issue_run(
        state_db, house, temp, "quarantine_cleanup_1_4_4_purge",
        preview["manifest_paths"],
    )
    conn = decision_store.connect_state_db(state_db)
    purged = []
    finished = False
    try:
        with mutation_lock(conn, f"quarantine_cleanup_purge:{run_id}", run_id=run_id):
            actual_run = decision_store.assert_active_actual_run(conn, run_id)
            for expected in preview["plans"]:
                current = purge_preview(
                    Path(state_db),
                    operation_ids=[
                        item["operation_id"] for item in expected["items"]
                    ],
                )
                if (
                    not current["apply_available"]
                    or current["plan_sha256"] != expected["plan_sha256"]
                ):
                    raise RuntimeError("purge evidence changed after activation")
                for item in current["items"]:
                    parent = _quarantine_origin(conn, item["operation_id"])
                    path = Path(item["path"])
                    evidence = inspect_regular_file(path)
                    decision_store.assert_actual_run_path(
                        actual_run, path, "temp_root"
                    )
                    decision_store.assert_manifest_source(
                        actual_run, path, "temp_root", evidence
                    )
                    if item.get("keep_path"):
                        keep_evidence = inspect_regular_file(item["keep_path"])
                        decision_store.assert_actual_run_path(
                            actual_run, item["keep_path"], "house_root"
                        )
                        decision_store.assert_manifest_source(
                            actual_run, item["keep_path"], "house_root",
                            keep_evidence,
                        )
                    with decision_store.transaction(conn):
                        purge_operation_id = decision_store.create_operation(
                            conn,
                            run_id=run_id,
                            action="quarantine_purge",
                            source_path=str(path),
                            file_id=parent["file_id"],
                            keep_file_id=parent["keep_file_id"],
                            expected_size=evidence.size,
                            expected_mtime_ns=evidence.mtime_ns,
                            expected_fingerprint_id=parent[
                                "expected_fingerprint_id"
                            ],
                            expected_keep_fingerprint_id=parent[
                                "expected_keep_fingerprint_id"
                            ],
                            parent_operation_id=parent["operation_id"],
                            source_dev=evidence.dev,
                            source_ino=evidence.ino,
                            source_ctime_ns=evidence.ctime_ns,
                            source_sha256=evidence.sha256,
                        )
                    unlink_owned(path, expected=evidence)
                    with decision_store.transaction(conn):
                        decision_store.transition_operation(
                            conn, purge_operation_id, "fs_done"
                        )
                        conn.execute(
                            "UPDATE operations SET purged_at = CURRENT_TIMESTAMP "
                            "WHERE operation_id = ?",
                            (parent["operation_id"],),
                        )
                        decision_store.transition_operation(
                            conn, purge_operation_id, "db_done"
                        )
                        decision_store.transition_operation(
                            conn, purge_operation_id, "committed"
                        )
                    purged.append(
                        {
                            "operation_id": parent["operation_id"],
                            "purge_operation_id": purge_operation_id,
                            "path": str(path),
                            "size": item["size"],
                        }
                    )
        decision_store.finish_actual_run(conn, run_id, success=True)
        finished = True
        return {
            "run_id": run_id,
            "manifest_path": manifest_path,
            "backup_path": backup,
            "purged_count": len(purged),
            "purged_bytes": sum(item["size"] for item in purged),
            "purged": purged,
        }
    except BaseException as exc:
        if not finished:
            decision_store.finish_actual_run(
                conn, run_id, success=False, error=str(exc)
            )
        raise
    finally:
        conn.close()


def _verification(state_db, plan, purge_ids):
    conn = decision_store.connect_state_db(state_db)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        issues = decision_store.doctor_issues(conn)
        remaining = 0
        for chunk in _chunks(purge_ids, 500):
            remaining += conn.execute(
                """
                SELECT COUNT(*) FROM operations AS o
                JOIN files AS f ON f.file_id = o.file_id
                WHERE o.operation_id IN ({}) AND o.purged_at IS NULL
                  AND f.active = 0 AND f.source = 'quarantine'
                """.format(",".join("?" for _ in chunk)),
                tuple(chunk),
            ).fetchone()[0]
        restored = []
        for item in plan.get("restore", []):
            row = conn.execute(
                "SELECT canonical_path, active, source FROM files WHERE file_id = ?",
                (item["file_id"],),
            ).fetchone()
            restored.append(dict(row) if row is not None else None)
        return {
            "integrity_check": integrity,
            "doctor_issue_count": len(issues),
            "doctor_issues": issues,
            "scheduled_quarantine_remaining": remaining,
            "restored": restored,
        }
    finally:
        conn.close()


def _resume_disposition_evidence(state_db, run_id, plan_sha256, plan):
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        actual = conn.execute(
            "SELECT * FROM actual_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if actual is None or actual["state"] != "finished":
            raise RuntimeError("resume disposition run is not finished")
        groups = conn.execute(
            """
            SELECT group_id, action, state, item_count, plan_sha256
            FROM operation_groups WHERE run_id = ? ORDER BY group_id
            """,
            (run_id,),
        ).fetchall()
        expected_groups = {
            "quarantine_cleanup_1_4_4_revalidation": len(
                plan.get("purge_revalidation", [])
            ),
            "quarantine_cleanup_1_4_4_missing_ack": len(
                plan.get("missing_quarantine_ack", [])
            ),
        }
        for action, item_count in expected_groups.items():
            matches = [row for row in groups if row["action"] == action]
            if (
                len(matches) != 1
                or matches[0]["state"] != "committed"
                or matches[0]["item_count"] != item_count
                or matches[0]["plan_sha256"] != plan_sha256
            ):
                raise RuntimeError(f"resume disposition group is stale: {action}")
        rows = conn.execute(
            """
            SELECT action, COUNT(*) AS item_count
            FROM operations WHERE run_id = ? AND state = 'committed'
            GROUP BY action ORDER BY action
            """,
            (run_id,),
        ).fetchall()
        counts = {row["action"]: row["item_count"] for row in rows}
        expected_counts = {
            "user_approved_purge_revalidation": len(
                plan.get("purge_revalidation", [])
            ),
            "user_quarantine_restore": len(plan.get("restore", [])),
            "user_queue_accept": len(plan.get("queue_upgrade", [])),
            "user_quarantine": (
                len(plan.get("queue_discard", []))
                + len(plan.get("queue_upgrade", []))
                + len(plan.get("untracked_queue_discard", []))
                + len(plan.get("upgrade_restored", []))
            ),
            "quarantine_purge": len(plan.get("missing_quarantine_ack", [])),
        }
        if any(counts.get(action) != count for action, count in expected_counts.items()):
            raise RuntimeError(
                f"resume disposition operation counts changed: {counts}"
            )
        new_purge_ids = [row[0] for row in conn.execute(
            """
            SELECT operation_id FROM operations
            WHERE run_id = ? AND action = 'user_quarantine'
              AND state = 'committed' AND purged_at IS NULL
            ORDER BY operation_id
            """,
            (run_id,),
        )]
        return {
            "run_id": run_id,
            "backup_path": actual["backup_path"],
            "manifest_path": actual["manifest_path"],
            "operation_counts": counts,
            "group_ids": [row["group_id"] for row in groups],
            "new_purge_operation_ids": new_purge_ids,
        }
    finally:
        conn.close()


def run(args):
    plan = _load_plan(args.plan)
    if getattr(args, "resume_after_disposition_run", None):
        plan_sha256 = _hash(plan)
        if not args.execute:
            raise RuntimeError("resume requires --execute")
        if args.confirm_plan_sha256 != plan_sha256:
            raise RuntimeError("cleanup plan confirmation is stale")
        dispositions = _resume_disposition_evidence(
            args.state_db, args.resume_after_disposition_run, plan_sha256, plan
        )
        purge_ids = sorted(set(
            [int(value) for value in plan["purge_operation_ids"]]
            + dispositions["new_purge_operation_ids"]
        ))
        report_path = Path(args.report_path) if args.report_path else (
            Path(args.temp) / "dedup_logs" /
            f"quarantine_cleanup_1_4_4_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
        )
        with mutation_lock_for_roots(
            args.house, args.temp, "quarantine-cleanup-1.4.4-resume-purge"
        ):
            purge = _apply_purge(
                args.state_db, args.house, args.temp, purge_ids
            )
            verification = _verification(args.state_db, plan, purge_ids)
            if (
                verification["integrity_check"] != "ok"
                or verification["doctor_issue_count"]
                or verification["scheduled_quarantine_remaining"]
            ):
                raise RuntimeError(
                    f"terminal verification failed: {verification}"
                )
            _prune_folderling_backups(
                args.state_db, Path(args.state_db).parent / "backups"
            )
            result = {
                "kind": KIND,
                "schema_version": SCHEMA_VERSION,
                "completed_at": datetime.now().isoformat(timespec="seconds"),
                "plan_path": str(Path(args.plan).resolve()),
                "plan_sha256": plan_sha256,
                "resumed_after_disposition": dispositions,
                "purge": purge,
                "verification": verification,
            }
            _atomic_json(report_path, result)
        print(json.dumps({
            "dry_run": False,
            "resumed": True,
            "report_path": str(report_path),
            "plan_sha256": plan_sha256,
            "purged_count": purge["purged_count"],
            "purged_bytes": purge["purged_bytes"],
            "verification": verification,
        }, ensure_ascii=False, indent=2))
        return 0
    preview = build_preview(
        plan, state_db=args.state_db, house=args.house, temp=args.temp
    )
    public_preview = {
        key: value for key, value in preview.items()
        if key not in {"manifest_paths", "restore_plans"}
    }
    if not args.execute:
        print(json.dumps({"dry_run": True, **public_preview}, ensure_ascii=False, indent=2))
        return 0
    if args.confirm_plan_sha256 != preview["plan_sha256"]:
        raise RuntimeError("cleanup plan confirmation is stale")

    started_at = datetime.now().isoformat(timespec="seconds")
    report_path = Path(args.report_path) if args.report_path else (
        Path(args.temp) / "dedup_logs" /
        f"quarantine_cleanup_1_4_4_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    )
    with mutation_lock_for_roots(
        args.house, args.temp, "quarantine-cleanup-1.4.4"
    ):
        dispositions = _apply_dispositions(
            plan, preview,
            state_db=args.state_db, house=args.house, temp=args.temp,
        )
        purge_ids = list(plan["purge_operation_ids"])
        purge_ids.extend(dispositions["new_purge_operation_ids"])
        purge_ids = sorted(set(int(value) for value in purge_ids))
        purge = _apply_purge(
            args.state_db, args.house, args.temp, purge_ids
        )
        verification = _verification(args.state_db, plan, purge_ids)
        if (
            verification["integrity_check"] != "ok"
            or verification["doctor_issue_count"]
            or verification["scheduled_quarantine_remaining"]
        ):
            raise RuntimeError(f"terminal verification failed: {verification}")
        _prune_folderling_backups(
            args.state_db, Path(args.state_db).parent / "backups"
        )
        result = {
            "kind": KIND,
            "schema_version": SCHEMA_VERSION,
            "started_at": started_at,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "plan_path": str(Path(args.plan).resolve()),
            "plan_sha256": preview["plan_sha256"],
            "preview": public_preview,
            "dispositions": dispositions,
            "purge": purge,
            "verification": verification,
        }
        _atomic_json(report_path, result)
    print(json.dumps({
        "dry_run": False,
        "report_path": str(report_path),
        "plan_sha256": preview["plan_sha256"],
        "restored_count": len(dispositions["restored"]),
        "upgraded_count": len(dispositions["upgrades"]),
        "purged_count": purge["purged_count"],
        "purged_bytes": purge["purged_bytes"],
        "verification": verification,
    }, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--state-db", default=str(STATE_DB))
    parser.add_argument("--house", default=str(Path.home() / "Documents" / "txt_house"))
    parser.add_argument("--temp", default=str(Path.home() / "Documents" / "txt_temp"))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-plan-sha256")
    parser.add_argument("--report-path")
    parser.add_argument("--resume-after-disposition-run")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
