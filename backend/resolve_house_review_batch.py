#!/usr/bin/env python3
"""Apply a human queue disposition list with journaled restore/quarantine.

The delete list names files currently in ``house_human_review``.  Every other
active file in that queue is restored to its original house path.  Optional
explicit duplicate pairs can quarantine already-resident house files while
preserving the named keep file.  Version 1.4.0 plans may also restore confirmed
false-positive queue files as distinct works.  Actual mode is fail-closed,
requires an explicit acknowledgement, and writes a structured recovery log.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
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
    user_queue_accept_to_house,
    user_queue_restore,
)
from mutation_io import (
    ensure_directory_nofollow,
    evidence_matches,
    inspect_regular_file,
    mutation_lock_for_roots,
)
from run_folderling_one_button import _prune_folderling_backups
from project_paths import FILE_INDEX, FILE_LIST, PROJECT_ROOT, STATE_DB


DEFAULT_DB = STATE_DB
QUEUE_FRAGMENT = "/trash_bin/house_human_review/"
REPORT_SCHEMA_VERSION = 2


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


def _one_selector(rows, title_index, item, prefix, label):
    file_id = item.get(f"{prefix}_file_id")
    rel_path = item.get(f"{prefix}_rel_path")
    if file_id:
        matches = [row for row in rows if row["file_id"] == file_id]
    elif rel_path:
        suffix = "/" + unicodedata.normalize("NFC", str(rel_path)).lstrip("/")
        matches = [
            row for row in rows
            if unicodedata.normalize("NFC", row["canonical_path"]).endswith(suffix)
        ]
    else:
        title = item.get(prefix)
        if not title:
            raise ValueError(
                f"{label} requires {prefix}, {prefix}_rel_path, or {prefix}_file_id"
            )
        matches = title_index.get(_key(title), [])
    if len(matches) != 1:
        selector = file_id or rel_path or item.get(prefix)
        raise RuntimeError(
            f"{label} must match exactly one active file: {selector!r} ({len(matches)})"
        )
    return matches[0]


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


def _restore_relative_path(item):
    value = unicodedata.normalize("NFC", str(item.get("destination_rel") or ""))
    relative = Path(value)
    if not value or relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise RuntimeError(
            f"unsafe explicit restore destination: {item.get('destination_rel')}"
        )
    return relative


def _assert_disjoint_actions(action_items):
    action_ids = {}
    for name, items in action_items.items():
        identifiers = [item["file_id"] for item in items]
        if len(identifiers) != len(set(identifiers)):
            raise RuntimeError(f"duplicate file in {name} actions")
        action_ids[name] = set(identifiers)
    names = list(action_ids)
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            overlap = action_ids[left] & action_ids[right]
            if overlap:
                raise RuntimeError(
                    f"file scheduled for conflicting actions: {left}/{right}: "
                    + ",".join(sorted(overlap))
                )


def _file_snapshot(row):
    evidence = inspect_regular_file(row["canonical_path"])
    return {
        "file_id": row["file_id"],
        "canonical_path": row["canonical_path"],
        "source": row["source"],
        "assignment_state": row["assignment_state"],
        "variant_id": row["variant_id"],
        "protected": int(row["protected"]),
        "representative": int(row["representative"]),
        "current_fingerprint_id": row["current_fingerprint_id"],
        "dev": evidence.dev,
        "ino": evidence.ino,
        "ctime_ns": evidence.ctime_ns,
        "size": evidence.size,
        "mtime_ns": evidence.mtime_ns,
        "raw_sha256": evidence.sha256,
    }


def _approved_snapshot(row, item, prefix, *, required=True):
    snapshot = _file_snapshot(row)
    expected = item.get(f"{prefix}_expected_snapshot") or {}
    if not isinstance(expected, dict):
        raise ValueError(f"{prefix}_expected_snapshot must be an object")
    explicit_sha = item.get(f"{prefix}_expected_sha256")
    snapshot_sha = expected.get("raw_sha256")
    if required and explicit_sha is None and snapshot_sha is None:
        raise RuntimeError(
            f"{prefix} approval requires an expected SHA-256"
        )
    for value in (explicit_sha, snapshot_sha):
        if value is None:
            continue
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in value)
        ):
            raise ValueError(f"{prefix}_expected_sha256 must be a SHA-256 hex digest")
    if explicit_sha is not None:
        explicit_sha = explicit_sha.lower()
        if snapshot_sha is not None and snapshot_sha.lower() != explicit_sha:
            raise ValueError(f"conflicting {prefix} expected SHA-256 values")
        expected = {**expected, "raw_sha256": explicit_sha}
    elif snapshot_sha is not None:
        expected = {**expected, "raw_sha256": snapshot_sha.lower()}
    for field, value in expected.items():
        if field not in snapshot:
            raise ValueError(f"unsupported {prefix} expected snapshot field: {field}")
        if snapshot[field] != value:
            raise RuntimeError(
                f"{prefix} expected snapshot mismatch: "
                f"{row['canonical_path']}: {field}"
            )
    return snapshot


def _assert_explicit_plan_snapshots(conn, plan, *, resumed_file_ids=()):
    resumed_file_ids = set(resumed_file_ids)
    rows = {row["file_id"]: row for row in _active_files(conn)}
    checks = []
    for item in plan.get("explicit_delete", []):
        checks.extend((
            (item["file_id"], item.get("delete_snapshot"), "delete"),
            (item["keep_file_id"], item.get("keep_snapshot"), "keep"),
        ))
    for item in plan.get("explicit_restore", []):
        checks.extend((
            (item["file_id"], item.get("restore_snapshot"), "restore"),
            (
                item["reference_file_id"],
                item.get("reference_snapshot"),
                "reference",
            ),
        ))
    current_by_file = {}
    for file_id, expected, label in checks:
        if not isinstance(expected, dict):
            raise RuntimeError(f"explicit {label} approval snapshot is required")
        row = rows.get(file_id)
        if row is None:
            raise RuntimeError(f"explicit {label} file is no longer active: {file_id}")
        if file_id not in current_by_file:
            current_by_file[file_id] = _file_snapshot(row)
        current = current_by_file[file_id]
        if file_id in resumed_file_ids and label == "restore":
            if (
                current["file_id"] != expected.get("file_id")
                or current["raw_sha256"] != expected.get("raw_sha256")
            ):
                raise RuntimeError(
                    f"explicit restore approval bytes changed: {file_id}"
                )
            continue
        if current != expected:
            changed = sorted(
                key for key in set(current) | set(expected)
                if current.get(key) != expected.get(key)
            )
            raise RuntimeError(
                f"explicit {label} approval snapshot changed: {file_id}: "
                + ",".join(changed)
            )


def _resume_operation_for_plan(conn, row, item):
    """Return one terminal journal proving this file was accepted to destination."""
    if row["source"] != "house":
        return None
    expected_sha = (
        item.get("restore_expected_sha256")
        or (item.get("restore_expected_snapshot") or {}).get("raw_sha256")
    )
    if not expected_sha:
        return None
    relative = _restore_relative_path(item)
    candidates = conn.execute(
        """
        SELECT o.*, ar.state AS run_state,
               ar.house_root AS run_house_root, ar.temp_root AS run_temp_root
        FROM operations AS o
        JOIN actual_runs AS ar ON ar.run_id = o.run_id
        WHERE o.file_id = ? AND o.action = 'user_queue_accept'
          AND o.state IN ('db_done', 'committed')
          AND ar.state IN ('failed', 'finished', 'cancelled')
        ORDER BY o.operation_id DESC
        """,
        (row["file_id"],),
    ).fetchall()
    matches = []
    for operation in candidates:
        destination = (Path(operation["run_house_root"]) / relative).resolve()
        source = Path(operation["source_path"]).resolve()
        temp_root = Path(operation["run_temp_root"]).resolve()
        try:
            source.relative_to(temp_root)
        except ValueError:
            continue
        if (
            str(destination) == row["canonical_path"]
            and operation["dest_path"] == row["canonical_path"]
            and operation["source_sha256"] == expected_sha.lower()
            and operation["destination_sha256"] == expected_sha.lower()
        ):
            matches.append(operation)
    if len(matches) > 1:
        raise RuntimeError(
            f"ambiguous explicit restore resume journals: {row['file_id']}"
        )
    return matches[0] if matches else None


def _peek_restore_review_snapshot(
    conn, candidate_file_id, reference_file_id, *, allow_decided
):
    states = "'pending', 'deferred', 'decided'" if allow_decided else "'pending', 'deferred'"
    review = conn.execute(
        f"""
        SELECT ri.review_id, ri.candidate_file_id, ri.reference_file_id,
               ri.left_fingerprint_id, ri.right_fingerprint_id,
               ri.classification
        FROM review_items AS ri
        JOIN files AS candidate ON candidate.file_id = ri.candidate_file_id
        JOIN files AS reference ON reference.file_id = ri.reference_file_id
        WHERE ri.state IN ({states})
          AND ri.left_fingerprint_id = candidate.current_fingerprint_id
          AND ri.right_fingerprint_id = reference.current_fingerprint_id
          AND ((candidate_file_id = ? AND reference_file_id = ?)
            OR (candidate_file_id = ? AND reference_file_id = ?))
        ORDER BY CASE ri.state WHEN 'decided' THEN 0 ELSE 1 END,
                 ri.review_id DESC
        LIMIT 1
        """,
        (
            candidate_file_id, reference_file_id,
            reference_file_id, candidate_file_id,
        ),
    ).fetchone()
    if review is None:
        return None
    return dict(review)


def build_plan(conn, delete_list, extra_plan):
    include_legacy_queue = delete_list is not None
    delete_list = list(delete_list or [])
    if not isinstance(extra_plan, dict):
        raise ValueError("manual plan must be a schema_version 2 object")
    schema_version = extra_plan.get("schema_version")
    if type(schema_version) is not int or schema_version < 2:
        raise ValueError("manual plan schema_version must be at least 2")
    explicit_quarantine_plan = extra_plan.get("quarantine", [])
    explicit_restore_plan = extra_plan.get("restore_distinct", [])
    plan_metadata = {
        key: extra_plan[key]
        for key in (
            "schema_version",
            "kind",
            "source_audit",
            "preserve_relationships",
            "preserve_results",
            "preserved_unresolved",
        )
        if key in extra_plan
    }
    if not isinstance(explicit_quarantine_plan, list):
        raise ValueError("manual quarantine plan must be a list")
    if not isinstance(explicit_restore_plan, list):
        raise ValueError("manual restore_distinct plan must be a list")
    rows = _active_files(conn)
    queue_rows = [row for row in rows if QUEUE_FRAGMENT in row["canonical_path"]]
    queue_index = _exact_title_index(queue_rows)
    matched = []
    for title in delete_list:
        matched.append(_one(queue_index, title, "delete-list title"))
    delete_ids = {row["file_id"] for row in matched}
    if len(delete_ids) != len(delete_list):
        raise RuntimeError("delete list contains duplicate normalized titles")

    restore_rows = (
        [row for row in queue_rows if row["file_id"] not in delete_ids]
        if include_legacy_queue else []
    )
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
    for item in explicit_quarantine_plan:
        delete = _one_selector(
            rows, all_index, item, "delete", "explicit delete selector"
        )
        keep = _one_selector(
            rows, all_index, item, "keep", "explicit keep selector"
        )
        blocked_reason = None
        if delete["source"] not in {"house", "queue"}:
            blocked_reason = "explicit_delete_source_not_house_or_queue"
        elif keep["source"] != "house":
            blocked_reason = "explicit_keep_source_not_house"
        approval_required = not item.get("blocked", False) and blocked_reason is None
        payload = {
            "file_id": delete["file_id"],
            "path": delete["canonical_path"],
            "keep_file_id": keep["file_id"],
            "keep_path": keep["canonical_path"],
            "reason": item.get("reason", "user_confirmed_duplicate"),
            "classification": item.get("classification", "manual_duplicate"),
            "evidence": item.get("evidence", {}),
            "selection_policy": item.get(
                "selection_policy", "explicit_user_approved"
            ),
            "delete_snapshot": _approved_snapshot(
                delete, item, "delete", required=approval_required
            ),
            "keep_snapshot": _approved_snapshot(
                keep, item, "keep", required=approval_required
            ),
        }
        if blocked_reason:
            payload["blocked_reason"] = blocked_reason
        if item.get("blocked") or payload.get("blocked_reason"):
            blocked.append(payload)
        else:
            explicit.append(payload)

    explicit_restore = []
    for item in explicit_restore_plan:
        restore = _one_selector(
            rows, all_index, item, "restore", "explicit restore selector"
        )
        reference = _one_selector(
            rows, all_index, item, "reference", "restore reference selector"
        )
        resume_operation = _resume_operation_for_plan(conn, restore, item)
        blocked_reason = None
        if restore["source"] != "queue" and resume_operation is None:
            blocked_reason = "restore_source_not_queue"
        elif reference["source"] != "house":
            blocked_reason = "restore_reference_not_house"
        approval_required = not item.get("blocked", False) and blocked_reason is None
        payload = {
            "file_id": restore["file_id"],
            "path": restore["canonical_path"],
            "reference_file_id": reference["file_id"],
            "reference_path": reference["canonical_path"],
            "destination_rel": str(_restore_relative_path(item)),
            "verdict": "distinct_work",
            "reason": item.get("reason", "user_selected_restore"),
            "note": item.get("note", "v1.4.0 false-positive restoration"),
            "evidence": item.get("evidence", {}),
            "resume_operation_id": (
                resume_operation["operation_id"] if resume_operation else None
            ),
            "restore_snapshot": _approved_snapshot(
                restore, item, "restore", required=approval_required
            ),
            "reference_snapshot": _approved_snapshot(
                reference, item, "reference", required=approval_required
            ),
        }
        if approval_required:
            review_snapshot = _peek_restore_review_snapshot(
                conn,
                payload["file_id"],
                payload["reference_file_id"],
                allow_decided=resume_operation is not None,
            )
            if review_snapshot is not None:
                payload["review_snapshot"] = review_snapshot
        if blocked_reason:
            payload["blocked_reason"] = blocked_reason
        if item.get("blocked") or payload.get("blocked_reason"):
            blocked.append(payload)
        else:
            explicit_restore.append(payload)

    queue_restore = [
        {"file_id": row["file_id"], "path": row["canonical_path"]}
        for row in restore_rows
    ]
    action_items = {
        "queue_restore": queue_restore,
        "queue_delete": delete_rows,
        "explicit_delete": explicit,
        "explicit_restore": explicit_restore,
    }
    _assert_disjoint_actions(action_items)
    destination_keys = [
        unicodedata.normalize("NFC", item["destination_rel"]).casefold()
        for item in explicit_restore
    ]
    if len(destination_keys) != len(set(destination_keys)):
        raise RuntimeError("explicit restore destinations must be unique")
    recent_names = [
        unicodedata.normalize("NFC", Path(item["destination_rel"]).name).casefold()
        for item in explicit_restore
    ]
    if len(recent_names) != len(set(recent_names)):
        raise RuntimeError("explicit restore recent-link names must be unique")

    all_discard_ids = delete_ids | {item["file_id"] for item in explicit}
    for item in delete_rows + explicit:
        if item["keep_file_id"] in all_discard_ids:
            raise RuntimeError(
                f"keep is also scheduled for discard: {item['keep_path']}"
            )
    for item in explicit_restore:
        if item["reference_file_id"] in all_discard_ids:
            raise RuntimeError(
                "restore reference is also scheduled for discard: "
                f"{item['reference_path']}"
            )
    return {
        "plan_metadata": plan_metadata,
        "queue_total": len(queue_rows),
        "queue_delete": delete_rows,
        "queue_restore": queue_restore,
        "explicit_delete": explicit,
        "explicit_restore": explicit_restore,
        "blocked": blocked,
    }


def _backup_path(state_db):
    directory = Path(state_db).parent / "backups"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return directory / f"before_review_resolution_{stamp}_{uuid.uuid4().hex[:8]}.sqlite3"


def _issue_run(state_db, backup, house, temp, *, manifest_paths=None):
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
    return decision_store.prepare_actual_run(
        state_db, house, temp, manifest_paths=manifest_paths
    )


def _explicit_restore_destination(house, item):
    relative_path = _restore_relative_path(item)
    root = Path(house).resolve()
    destination = (root / relative_path).resolve()
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(
            "explicit restore destination escapes house: "
            f"{item.get('destination_rel')}"
        ) from exc
    return destination


def _latest_pair_review(conn, candidate_file_id, reference_file_id):
    review = conn.execute(
        """
        SELECT ri.* FROM review_items AS ri
        JOIN files AS candidate ON candidate.file_id = ri.candidate_file_id
        JOIN files AS reference ON reference.file_id = ri.reference_file_id
        WHERE ri.state IN ('pending', 'deferred')
          AND ri.left_fingerprint_id = candidate.current_fingerprint_id
          AND ri.right_fingerprint_id = reference.current_fingerprint_id
          AND ((ri.candidate_file_id = ? AND ri.reference_file_id = ?)
            OR (ri.candidate_file_id = ? AND ri.reference_file_id = ?))
        ORDER BY ri.review_id DESC LIMIT 1
        """,
        (
            candidate_file_id,
            reference_file_id,
            reference_file_id,
            candidate_file_id,
        ),
    ).fetchone()
    if review is None:
        raise RuntimeError(
            "explicit restore requires a current pending/deferred pair review: "
            f"{candidate_file_id}/{reference_file_id}"
        )
    decision_store.preview_decision(
        conn,
        review_id=review["review_id"],
        candidate_file_id=review["candidate_file_id"],
        reference_file_id=review["reference_file_id"],
        verdict="distinct_work",
    )
    endpoints = conn.execute(
        """
        SELECT f.file_id, f.assignment_state, v.work_bucket_id
        FROM files AS f
        LEFT JOIN variants AS v ON v.variant_id = f.variant_id
        WHERE f.file_id IN (?, ?) AND f.active = 1
        """,
        (candidate_file_id, reference_file_id),
    ).fetchall()
    if len(endpoints) != 2:
        raise RuntimeError("explicit restore review endpoint is no longer active")
    if all(row["assignment_state"] == "managed" for row in endpoints):
        works = {row["work_bucket_id"] for row in endpoints}
        if len(works) == 1:
            raise RuntimeError(
                "explicit distinct restore conflicts with an existing managed work"
            )
    return {
        "review_id": review["review_id"],
        "candidate_file_id": review["candidate_file_id"],
        "reference_file_id": review["reference_file_id"],
        "left_fingerprint_id": review["left_fingerprint_id"],
        "right_fingerprint_id": review["right_fingerprint_id"],
        "classification": review["classification"],
        "state": review["state"],
        "decision_id": None,
        "evidence_json": review["evidence_json"],
    }


def _decided_restore_review(conn, item):
    left_file_id, right_file_id = sorted(
        (item["file_id"], item["reference_file_id"])
    )
    decision = conn.execute(
        """
        SELECT * FROM decisions
        WHERE left_file_id = ? AND right_file_id = ? AND active = 1
        """,
        (left_file_id, right_file_id),
    ).fetchone()
    if decision is None:
        return None
    if decision["verdict"] != "distinct_work" or decision["note"] != item["note"]:
        raise RuntimeError("existing restore decision does not match the approved plan")
    review = conn.execute(
        """
        SELECT * FROM review_items
        WHERE decision_id = ? AND state = 'decided'
          AND ((candidate_file_id = ? AND reference_file_id = ?)
            OR (candidate_file_id = ? AND reference_file_id = ?))
        """,
        (
            decision["decision_id"],
            item["file_id"], item["reference_file_id"],
            item["reference_file_id"], item["file_id"],
        ),
    ).fetchone()
    if review is None:
        raise RuntimeError("active restore decision has no matching decided review")
    expected_fingerprints = (
        (review["left_fingerprint_id"], review["right_fingerprint_id"])
        if review["candidate_file_id"] < review["reference_file_id"]
        else (review["right_fingerprint_id"], review["left_fingerprint_id"])
    )
    if expected_fingerprints != (
        decision["left_fingerprint_id"], decision["right_fingerprint_id"]
    ):
        raise RuntimeError("restore decision fingerprint provenance mismatch")
    endpoints = conn.execute(
        """
        SELECT f.file_id, f.assignment_state, f.protected,
               v.work_bucket_id,
               EXISTS(SELECT 1 FROM representatives AS r
                      WHERE r.file_id = f.file_id) AS representative
        FROM files AS f
        LEFT JOIN variants AS v ON v.variant_id = f.variant_id
        WHERE f.file_id IN (?, ?) AND f.active = 1
        """,
        (item["file_id"], item["reference_file_id"]),
    ).fetchall()
    if (
        len(endpoints) != 2
        or any(row["assignment_state"] != "managed" for row in endpoints)
        or len({row["work_bucket_id"] for row in endpoints}) != 2
        or any(not row["protected"] or not row["representative"] for row in endpoints)
    ):
        raise RuntimeError("existing distinct-work restore relation is incomplete")
    return {
        "review_id": review["review_id"],
        "candidate_file_id": review["candidate_file_id"],
        "reference_file_id": review["reference_file_id"],
        "left_fingerprint_id": review["left_fingerprint_id"],
        "right_fingerprint_id": review["right_fingerprint_id"],
        "classification": review["classification"],
        "state": review["state"],
        "decision_id": decision["decision_id"],
        "evidence_json": review["evidence_json"],
    }


def _restore_review_state(conn, item, *, allow_decided):
    if allow_decided:
        decided = _decided_restore_review(conn, item)
        if decided is not None:
            return decided
    return _latest_pair_review(
        conn, item["file_id"], item["reference_file_id"]
    )


def _assert_restore_review_snapshot(item, review):
    expected = item.get("review_snapshot")
    if not isinstance(expected, dict):
        raise RuntimeError("explicit restore review snapshot is required")
    current = {
        key: review[key]
        for key in (
            "review_id", "candidate_file_id", "reference_file_id",
            "left_fingerprint_id", "right_fingerprint_id", "classification",
        )
    }
    if current != expected:
        changed = sorted(
            key for key in set(current) | set(expected)
            if current.get(key) != expected.get(key)
        )
        raise RuntimeError(
            "explicit restore review provenance changed: " + ",".join(changed)
        )
    return review


def _verified_resume_operation(conn, plan, item, destination, house, temp):
    row = conn.execute(
        "SELECT * FROM files WHERE file_id = ? AND active = 1",
        (item["file_id"],),
    ).fetchone()
    if row is None or row["source"] != "house":
        return None
    if row["canonical_path"] != str(destination):
        raise RuntimeError("journaled restore file is not at the approved destination")
    operation = _resume_operation_for_plan(conn, row, {
        "destination_rel": item["destination_rel"],
        "restore_expected_sha256": item["restore_snapshot"]["raw_sha256"],
    })
    if operation is None:
        raise RuntimeError("house restore has no matching journaled queue accept")
    if item.get("resume_operation_id") not in {None, operation["operation_id"]}:
        raise RuntimeError("restore resume journal changed after plan resolution")
    intent = _matching_prior_intent(conn, plan, temp, item, operation=operation)
    if intent is None:
        raise RuntimeError("restore resume has no matching immutable intent report")
    if (
        Path(operation["run_house_root"]).resolve() != Path(house).resolve()
        or Path(operation["run_temp_root"]).resolve() != Path(temp).resolve()
    ):
        raise RuntimeError("restore resume journal roots do not match this invocation")
    if Path(operation["source_path"]).exists():
        raise RuntimeError("restore resume source unexpectedly exists")
    evidence = inspect_regular_file(destination)
    operation_identity = (
        operation["destination_dev"], operation["destination_ino"],
        operation["destination_ctime_ns"], operation["destination_size"],
        operation["destination_mtime_ns"], operation["destination_sha256"],
    )
    current_identity = (
        evidence.dev, evidence.ino, evidence.ctime_ns,
        evidence.size, evidence.mtime_ns, evidence.sha256,
    )
    row_identity = (
        row["dev"], row["ino"], row["ctime_ns"], row["size"], row["mtime_ns"]
    )
    if operation_identity != current_identity or row_identity != current_identity[:5]:
        raise RuntimeError("restore resume destination is not the journal-owned file")
    if evidence.sha256 != item["restore_snapshot"]["raw_sha256"]:
        raise RuntimeError("restore resume bytes differ from the approved SHA-256")
    if operation["state"] not in {"db_done", "committed"}:
        raise RuntimeError("restore resume operation is not recoverable")
    result = dict(operation)
    result["intent_entry"] = intent["entry"]
    result["intent_path"] = intent["path"]
    return result


def _preflight_explicit_restores(conn, plan, house, temp):
    recent_dir = Path(house) / "_최근"
    prepared = {}
    destinations = set()
    recent_names = set()
    for item in plan.get("explicit_restore", []):
        destination = _explicit_restore_destination(house, item)
        destination_key = unicodedata.normalize("NFC", str(destination)).casefold()
        recent_key = unicodedata.normalize("NFC", destination.name).casefold()
        if destination_key in destinations or recent_key in recent_names:
            raise RuntimeError("explicit restore destination/recent link is duplicated")
        resume_operation = _verified_resume_operation(
            conn, plan, item, destination, house, temp
        )
        existing_link_evidence = None
        if resume_operation is None:
            if destination.exists() or destination.is_symlink():
                raise RuntimeError(
                    f"explicit restore destination already exists: {destination}"
                )
            recent_link = recent_dir / destination.name
            if os.path.lexists(recent_link):
                raise RuntimeError(
                    f"explicit restore recent link already exists: {recent_link}"
                )
            folderling.ensure_recent_link_slot(destination.name, str(recent_dir))
        elif os.path.lexists(recent_dir / destination.name):
            recent_link = recent_dir / destination.name
            review = _assert_restore_review_snapshot(
                item,
                _restore_review_state(conn, item, allow_decided=True),
            )
            existing_link_evidence = _verified_existing_recent_link(
                review, recent_link, destination
            )
            if existing_link_evidence is None:
                entry = resume_operation["intent_entry"]
                info = os.lstat(recent_link)
                target = (
                    os.readlink(recent_link) if stat.S_ISLNK(info.st_mode) else None
                )
                if (
                    entry.get("recent_link_path") != str(recent_link)
                    or entry.get("recent_link_target") != str(destination)
                    or target != str(destination)
                ):
                    raise RuntimeError(
                        f"existing recent link has no matching restore intent: {recent_link}"
                    )
                existing_link_evidence = {
                    "dev": info.st_dev,
                    "ino": info.st_ino,
                    "ctime_ns": info.st_ctime_ns,
                    "target": target,
                }
        destinations.add(destination_key)
        recent_names.add(recent_key)
        review = _assert_restore_review_snapshot(
            item,
            _restore_review_state(
                conn, item, allow_decided=resume_operation is not None
            ),
        )
        prepared[item["file_id"]] = {
            "destination": destination,
            "recent_dir": recent_dir,
            "review": review,
            "resume_operation": resume_operation,
            "existing_link_evidence": existing_link_evidence,
        }
    return prepared


def _create_owned_recent_link(destination, recent_dir):
    folderling.create_recent_link(
        str(destination), destination.name, str(recent_dir)
    )
    link_path = recent_dir / destination.name
    info = os.lstat(link_path)
    if not stat.S_ISLNK(info.st_mode):
        raise RuntimeError(f"created recent path is not a symlink: {link_path}")
    return link_path, {
        "dev": info.st_dev,
        "ino": info.st_ino,
        "ctime_ns": info.st_ctime_ns,
        "target": os.readlink(link_path),
    }


def _remove_owned_recent_link(link_path, expected):
    try:
        info = os.lstat(link_path)
    except FileNotFoundError:
        return
    actual = {
        "dev": info.st_dev,
        "ino": info.st_ino,
        "ctime_ns": info.st_ctime_ns,
        "target": os.readlink(link_path) if stat.S_ISLNK(info.st_mode) else None,
    }
    if not stat.S_ISLNK(info.st_mode) or actual != expected:
        raise RuntimeError(
            f"recent link changed; refusing cleanup: {link_path}"
        )
    os.unlink(link_path)


def _restore_disposition_payload(conn, item, review, operation_id, destination):
    candidate = conn.execute(
        "SELECT canonical_path FROM files WHERE file_id = ? AND active = 1",
        (item["file_id"],),
    ).fetchone()
    reference = conn.execute(
        "SELECT canonical_path FROM files WHERE file_id = ? AND active = 1",
        (item["reference_file_id"],),
    ).fetchone()
    if candidate is None or reference is None:
        raise RuntimeError("restore disposition endpoint is no longer active")
    candidate_sha = inspect_regular_file(candidate["canonical_path"]).sha256
    reference_sha = inspect_regular_file(reference["canonical_path"]).sha256
    expected = {
        "reason": "user_selected_restore",
        "file_id": item["file_id"],
        "reference_file_id": item["reference_file_id"],
        "review_id": review["review_id"],
        "decision_id": review["decision_id"],
        "operation_id": operation_id,
        "destination": str(destination),
        "raw_sha256": item["restore_snapshot"]["raw_sha256"],
        "reference_raw_sha256": item["reference_snapshot"]["raw_sha256"],
    }
    if (
        candidate_sha != expected["raw_sha256"]
        or reference_sha != expected["reference_raw_sha256"]
    ):
        raise RuntimeError("restore disposition bytes differ from the approved plan")
    return expected


def _stamp_explicit_restore_disposition(
    conn, item, review, operation_id, destination
):
    if review["state"] != "decided" or review["decision_id"] is None:
        raise RuntimeError("restore disposition requires a decided review")
    expected = _restore_disposition_payload(
        conn, item, review, operation_id, destination
    )
    row = conn.execute(
        "SELECT evidence_json FROM review_items WHERE review_id = ?",
        (review["review_id"],),
    ).fetchone()
    try:
        payload = json.loads(row["evidence_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {"previous_evidence": row["evidence_json"]}
    prior = payload.get("explicit_restore_disposition")
    if prior is not None and {
        key: prior.get(key) for key in expected
    } != expected:
        raise RuntimeError("existing restore disposition does not match this plan")
    payload["explicit_restore_disposition"] = {
        **expected,
        **({"recent_link": prior["recent_link"]} if prior and prior.get("recent_link") else {}),
    }
    with decision_store.transaction(conn):
        updated = conn.execute(
            """
            UPDATE review_items SET evidence_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE review_id = ? AND state = 'decided' AND decision_id = ?
            """,
            (
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                review["review_id"], review["decision_id"],
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("restore disposition review changed")
    return expected


def _stamp_explicit_restore_recent_link(conn, review, link_path, evidence):
    row = conn.execute(
        "SELECT evidence_json FROM review_items WHERE review_id = ?",
        (review["review_id"],),
    ).fetchone()
    try:
        payload = json.loads(row["evidence_json"] or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("restore disposition evidence is unreadable") from exc
    disposition = payload.get("explicit_restore_disposition")
    if not isinstance(disposition, dict):
        raise RuntimeError("restore disposition must be stamped before recent link")
    disposition["recent_link"] = {
        "path": str(link_path),
        **evidence,
    }
    with decision_store.transaction(conn):
        updated = conn.execute(
            """
            UPDATE review_items SET evidence_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE review_id = ? AND state = 'decided' AND decision_id = ?
            """,
            (
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                review["review_id"], review["decision_id"],
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("restore recent-link review changed")


def _verified_existing_recent_link(review, link_path, destination):
    if not os.path.lexists(link_path):
        return None
    try:
        payload = json.loads(review.get("evidence_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    expected = (
        payload.get("explicit_restore_disposition", {}).get("recent_link")
    )
    if expected is None:
        return None
    info = os.lstat(link_path)
    actual = {
        "path": str(link_path),
        "dev": info.st_dev,
        "ino": info.st_ino,
        "ctime_ns": info.st_ctime_ns,
        "target": os.readlink(link_path) if stat.S_ISLNK(info.st_mode) else None,
    }
    if (
        not stat.S_ISLNK(info.st_mode)
        or actual["target"] != str(destination)
        or expected != actual
    ):
        raise RuntimeError(
            f"existing recent link is not owned by this restore: {link_path}"
        )
    return {key: actual[key] for key in ("dev", "ino", "ctime_ns", "target")}


def _complete_explicit_restore(
    conn, item, prepared, *, run_id=None, operation_group_id=None
):
    destination = prepared["destination"]
    review = prepared["review"]
    operation = prepared["resume_operation"]
    recent_dir = prepared["recent_dir"]
    recent_link = recent_dir / destination.name
    existing_link = prepared.get("existing_link_evidence")
    created_link = None
    created_evidence = None
    try:
        if operation is not None and operation["state"] == "db_done":
            recovered = decision_store.recover_interrupted_operation(
                conn, operation["operation_id"]
            )
            if recovered != "committed":
                raise RuntimeError(
                    "restore resume operation did not commit during recovery: "
                    f"{recovered}"
                )
            operation["state"] = recovered
        if operation is None:
            restored_item = user_queue_accept_to_house(
                conn,
                file_id=item["file_id"],
                destination=destination,
                run_id=run_id,
                operation_group_id=operation_group_id,
            )
            operation_id = restored_item["operation_id"]
        else:
            operation_id = operation["operation_id"]
            restored_item = {
                "operation_id": operation_id,
                "action": "user_queue_accept_resume",
                "file_id": item["file_id"],
                "source_path": operation["source_path"],
                "dest_path": operation["dest_path"],
            }
        if review["state"] != "decided":
            decision_id = decision_store.apply_decision(
                conn,
                review_id=review["review_id"],
                candidate_file_id=review["candidate_file_id"],
                reference_file_id=review["reference_file_id"],
                verdict="distinct_work",
                note=item["note"],
                allowed_active_run_id=run_id,
            )
            review = {
                **review,
                "state": "decided",
                "decision_id": decision_id,
            }
        else:
            decision_id = review["decision_id"]
        decision_store.record_human_restore_disposition(
            conn, item["file_id"], reason="user_selected_restore"
        )
        _stamp_explicit_restore_disposition(
            conn, item, review, operation_id, destination
        )
        if existing_link is None:
            created_link, created_evidence = _create_owned_recent_link(
                destination, recent_dir
            )
            recent_link = created_link
        link_evidence = created_evidence or existing_link
        _stamp_explicit_restore_recent_link(
            conn, review, recent_link, link_evidence
        )
    except BaseException:
        if created_link is not None:
            _remove_owned_recent_link(created_link, created_evidence)
        raise
    return (
        {**restored_item, "recent_link": str(recent_link)},
        {
            "file_id": item["file_id"],
            "reference_file_id": item["reference_file_id"],
            "review_id": review["review_id"],
            "decision_id": decision_id,
            "verdict": "distinct_work",
            "resumed": operation is not None,
        },
    )


def _approval_plan_sha256(plan):
    metadata = plan.get("plan_metadata") or {}
    input_plan = metadata.get("input_plan") or {}
    stable = {
        "input_plan_sha256": input_plan.get("sha256"),
        "queue_restore": plan.get("queue_restore", []),
        "queue_delete": plan.get("queue_delete", []),
        "explicit_delete": [
            {
                "file_id": item["file_id"],
                "keep_file_id": item["keep_file_id"],
                "reason": item["reason"],
                "classification": item.get("classification"),
                "selection_policy": item.get("selection_policy"),
                "evidence": item.get("evidence", {}),
                "delete_sha256": item["delete_snapshot"]["raw_sha256"],
                "keep_sha256": item["keep_snapshot"]["raw_sha256"],
            }
            for item in plan.get("explicit_delete", [])
        ],
        "explicit_restore": [
            {
                "file_id": item["file_id"],
                "reference_file_id": item["reference_file_id"],
                "destination_rel": item["destination_rel"],
                "reason": item["reason"],
                "note": item["note"],
                "evidence": item.get("evidence", {}),
                "restore_sha256": item["restore_snapshot"]["raw_sha256"],
                "reference_sha256": item["reference_snapshot"]["raw_sha256"],
                "review_snapshot": item.get("review_snapshot"),
            }
            for item in plan.get("explicit_restore", [])
        ],
    }
    return hashlib.sha256(
        json.dumps(
            stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _intent_restore_entries(plan, house):
    recent_dir = Path(house).resolve() / "_최근"
    entries = []
    for item in plan.get("explicit_restore", []):
        destination = _explicit_restore_destination(house, item)
        entries.append({
            "file_id": item["file_id"],
            "reference_file_id": item["reference_file_id"],
            "source_path": item["restore_snapshot"]["canonical_path"],
            "destination": str(destination),
            "recent_link_path": str(recent_dir / destination.name),
            "recent_link_target": str(destination),
            "restore_sha256": item["restore_snapshot"]["raw_sha256"],
            "reference_sha256": item["reference_snapshot"]["raw_sha256"],
            "note": item["note"],
            "review_snapshot": item.get("review_snapshot"),
        })
    return entries


def write_intent_report(path, *, plan, state_db, house, temp, backup):
    backup_evidence = inspect_regular_file(backup)
    plan_sha256 = _approval_plan_sha256(plan)
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "manual_house_cleanup_intent_1_4_0",
        "phase": "intent",
        "generated_at": datetime.now().astimezone().isoformat(),
        "plan_sha256": plan_sha256,
        "plan": plan,
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
            "restore_entries": _intent_restore_entries(plan, house),
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
    plan.setdefault("plan_metadata", {})["intent_report"] = {
        "path": str(target),
        "sha256": evidence.sha256,
        "plan_sha256": plan_sha256,
    }
    return {
        "path": str(target),
        "sha256": evidence.sha256,
        "plan_sha256": plan_sha256,
        "size": evidence.size,
        "mtime_ns": evidence.mtime_ns,
    }


def _matching_prior_intent(conn, plan, temp, item, *, operation):
    plan_sha256 = _approval_plan_sha256(plan)
    root = Path(temp).resolve() / "dedup_logs"
    group_id = operation["operation_group_id"]
    if not group_id:
        return None
    group = conn.execute(
        "SELECT * FROM operation_groups WHERE group_id = ?", (group_id,)
    ).fetchone()
    if (
        group is None
        or group["run_id"] != operation["run_id"]
        or group["action"] != "manual_house_cleanup_restore"
        or group["state"] not in {
            "planned", "fs_done", "db_done", "committed", "failed"
        }
        or group["plan_sha256"] != plan_sha256
        or not group["manifest_path"]
        or not group["source_manifest_json"]
    ):
        return None
    try:
        provenance = json.loads(group["source_manifest_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    path = Path(group["manifest_path"])
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if path.is_symlink() or resolved.parent != root or str(resolved) != str(path):
        return None
    try:
        evidence = inspect_regular_file(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not evidence_matches(inspect_regular_file(path), evidence):
            return None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if (
        provenance.get("intent_path") != str(path)
        or provenance.get("intent_sha256") != evidence.sha256
        or provenance.get("plan_sha256") != plan_sha256
        or payload.get("kind") != "manual_house_cleanup_intent_1_4_0"
        or payload.get("plan_sha256") != plan_sha256
    ):
        return None
    preparation = payload.get("run_preparation") or {}
    if (
        preparation.get("state_db") != str(Path(conn.execute(
            "PRAGMA database_list"
        ).fetchone()[2]).resolve())
        or preparation.get("temp_root") != str(Path(temp).resolve())
    ):
        return None
    matches = []
    for entry in preparation.get("restore_entries", []):
        if (
            entry.get("file_id") == item["file_id"]
            and entry.get("reference_file_id") == item["reference_file_id"]
            and entry.get("restore_sha256") == item["restore_snapshot"]["raw_sha256"]
            and entry.get("reference_sha256") == item["reference_snapshot"]["raw_sha256"]
            and entry.get("note") == item["note"]
            and entry.get("review_snapshot") == item.get("review_snapshot")
            and entry.get("source_path") == operation["source_path"]
            and entry.get("destination") == operation["dest_path"]
        ):
            matches.append(entry)
    if len(matches) != 1:
        return None
    return {"path": str(path), "entry": matches[0], "group_id": group_id}


def _create_restore_operation_group(
    conn, *, run_id, plan, intent, item_count=None
):
    provenance = {
        "intent_path": intent["path"],
        "intent_sha256": intent["sha256"],
        "plan_sha256": intent["plan_sha256"],
        "input_plan_sha256": (
            plan.get("plan_metadata", {}).get("input_plan", {}).get("sha256")
        ),
    }
    with decision_store.transaction(conn):
        return decision_store.create_operation_group(
            conn,
            run_id=run_id,
            action="manual_house_cleanup_restore",
            plan_sha256=intent["plan_sha256"],
            item_count=(
                len(plan.get("explicit_restore", []))
                if item_count is None else item_count
            ),
            manifest_path=intent["path"],
            source_manifest_json=json.dumps(
                provenance, ensure_ascii=False, sort_keys=True
            ),
        )


def _finish_restore_operation_group(conn, group_id, *, success, error=None):
    if group_id is None:
        return
    with decision_store.transaction(conn):
        current = conn.execute(
            "SELECT state FROM operation_groups WHERE group_id = ?", (group_id,)
        ).fetchone()["state"]
        if success:
            states = ("planned", "fs_done", "db_done", "committed")
            for state in states[states.index(current) + 1:]:
                decision_store.transition_operation_group(conn, group_id, state)
        elif current in {"planned", "fs_done", "db_done"}:
            decision_store.transition_operation_group(
                conn, group_id, "failed", error=error
            )


def _manifest_paths_for_files(conn, file_ids, *, extra_paths=()):
    file_ids = sorted(set(file_ids))
    paths = [str(Path(value).resolve()) for value in extra_paths]
    if not file_ids:
        return paths
    placeholders = ", ".join("?" for _ in file_ids)
    rows = conn.execute(
        f"SELECT file_id, canonical_path FROM files "
        f"WHERE active = 1 AND file_id IN ({placeholders})",
        tuple(file_ids),
    ).fetchall()
    if {row["file_id"] for row in rows} != set(file_ids):
        raise RuntimeError("actual-run manifest endpoint is no longer active")
    paths.extend(row["canonical_path"] for row in rows)
    return list(dict.fromkeys(paths))


def execute(
    plan, *, state_db, house, temp, report_path=None, intent_report_path=None
):
    with mutation_lock_for_roots(house, temp, "resolve-house-review-batch"):
        if intent_report_path is None:
            intent_report_path = _intent_report_path(temp)
        if report_path is not None and Path(report_path) == Path(intent_report_path):
            raise RuntimeError("intent and terminal report paths must differ")
        conn = decision_store.connect_state_db(state_db)
        try:
            source_house_ids = {
                row["file_id"] for row in conn.execute(
                    "SELECT file_id FROM files WHERE active = 1 AND source = 'house'"
                )
            }
            possible_resume_ids = {
                item["file_id"] for item in plan.get("explicit_restore", [])
                if item["file_id"] in source_house_ids
            }
            _assert_explicit_plan_snapshots(
                conn, plan, resumed_file_ids=possible_resume_ids
            )
            issues = decision_store.doctor_issues(conn)
            target_ids = {
                item["file_id"]
                for item in (
                    plan["queue_restore"] + plan["queue_delete"]
                    + plan["explicit_delete"] + plan.get("explicit_restore", [])
                )
            } | {
                item["keep_file_id"]
                for item in plan["queue_delete"] + plan["explicit_delete"]
            } | {
                item["reference_file_id"]
                for item in plan.get("explicit_restore", [])
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
            restore_preflight = _preflight_explicit_restores(
                conn, plan, house, temp
            )
            resumed_ids = {
                file_id for file_id, prepared in restore_preflight.items()
                if prepared["resume_operation"] is not None
            }
            _assert_explicit_plan_snapshots(
                conn, plan, resumed_file_ids=resumed_ids
            )
            intent = write_intent_report(
                intent_report_path,
                plan=plan,
                state_db=state_db,
                house=house,
                temp=temp,
                backup=backup,
            )
            for file_id in sorted(refresh_ids - resumed_ids):
                refresh_user_approved_snapshot(conn, file_id)
            remaining = [
                issue for issue in decision_store.doctor_issues(conn)
                if not (
                    issue.get("file_id") in resumed_ids
                    and issue["kind"] in refreshable
                )
            ]
            if remaining:
                raise RuntimeError(
                    f"doctor failed after approved rebaseline: {remaining[0]}"
                )
            restored = []
            restore_decisions = []
            for item in plan.get("explicit_restore", []):
                prepared = restore_preflight[item["file_id"]]
                if prepared["resume_operation"] is None:
                    continue
                restored_item, decision_item = _complete_explicit_restore(
                    conn, item, prepared
                )
                restored.append(restored_item)
                restore_decisions.append(decision_item)
                fingerprint = conn.execute(
                    """
                    SELECT fp.canonical_path, fp.dev, fp.ino, fp.ctime_ns,
                           f.canonical_path AS file_path, f.dev AS file_dev,
                           f.ino AS file_ino, f.ctime_ns AS file_ctime_ns
                    FROM files AS f
                    LEFT JOIN fingerprints AS fp
                      ON fp.fingerprint_id = f.current_fingerprint_id
                    WHERE f.file_id = ?
                    """,
                    (item["file_id"],),
                ).fetchone()
                if (
                    fingerprint["canonical_path"] != fingerprint["file_path"]
                    or (
                        fingerprint["dev"], fingerprint["ino"],
                        fingerprint["ctime_ns"]
                    ) != (
                        fingerprint["file_dev"], fingerprint["file_ino"],
                        fingerprint["file_ctime_ns"]
                    )
                ):
                    with decision_store.transaction(conn):
                        conn.execute(
                            "UPDATE files SET current_fingerprint_id = NULL "
                            "WHERE file_id = ?",
                            (item["file_id"],),
                        )
                    refresh_user_approved_snapshot(conn, item["file_id"])
            remaining = decision_store.doctor_issues(conn)
            if remaining:
                raise RuntimeError(
                    f"doctor failed after explicit restore resume: {remaining[0]}"
                )
        finally:
            conn.close()

        conn = decision_store.connect_state_db_readonly(state_db)
        try:
            new_explicit = [
                item for item in plan.get("explicit_restore", [])
                if restore_preflight[item["file_id"]]["resume_operation"] is None
            ]
            restore_manifest_ids = {
                item["file_id"] for item in plan["queue_restore"] + new_explicit
            }
            restore_manifest_paths = _manifest_paths_for_files(
                conn, restore_manifest_ids, extra_paths=[intent["path"]]
            )
        finally:
            conn.close()
        restore_run_id, restore_manifest_path = _issue_run(
            state_db, backup, house, temp, manifest_paths=restore_manifest_paths
        )
        conn = decision_store.connect_state_db(state_db)
        restore_group_id = None
        try:
            try:
                if new_explicit:
                    restore_group_id = _create_restore_operation_group(
                        conn,
                        run_id=restore_run_id,
                        plan=plan,
                        intent=intent,
                        item_count=len(new_explicit),
                    )
                for item in plan["queue_restore"]:
                    restored.append(user_queue_restore(
                        conn, file_id=item["file_id"], run_id=restore_run_id
                    ))
                for item in plan.get("explicit_restore", []):
                    prepared = restore_preflight[item["file_id"]]
                    if prepared["resume_operation"] is not None:
                        continue
                    restored_item, decision_item = _complete_explicit_restore(
                        conn,
                        item,
                        prepared,
                        run_id=restore_run_id,
                        operation_group_id=restore_group_id,
                    )
                    restored.append(restored_item)
                    restore_decisions.append(decision_item)
                _finish_restore_operation_group(
                    conn, restore_group_id, success=True
                )
                decision_store.finish_actual_run(
                    conn, restore_run_id, success=True
                )
            except BaseException as exc:
                _finish_restore_operation_group(
                    conn, restore_group_id, success=False, error=str(exc)
                )
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
            discard_ids = {
                endpoint_id
                for item in plan["queue_delete"] + plan["explicit_delete"]
                for endpoint_id in (item["file_id"], item["keep_file_id"])
            }
            discard_manifest_paths = _manifest_paths_for_files(
                conn, discard_ids, extra_paths=[intent["path"]]
            )
        finally:
            conn.close()
        discard_run_id, discard_manifest_path = _issue_run(
            state_db,
            discard_backup,
            house,
            temp,
            manifest_paths=discard_manifest_paths,
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

        result = {
            "restore_run_id": restore_run_id,
            "restore_manifest_path": restore_manifest_path,
            "restore_operation_group_id": restore_group_id,
            "discard_run_id": discard_run_id,
            "discard_manifest_path": discard_manifest_path,
            "backup": str(backup),
            "discard_backup": str(discard_backup),
            "intent_report_path": intent["path"],
            "intent_report_sha256": intent["sha256"],
            "restored": restored,
            "restore_decisions": restore_decisions,
            "quarantined": quarantined,
            "blocked": plan["blocked"],
        }
        if report_path is not None:
            result["report_path"] = str(report_path)
            write_execution_report(
                report_path,
                plan=plan,
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
    target = root / f"manual_house_cleanup_1_4_0_{stamp}.json"
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"report path already exists: {target}")
    return target


def _intent_report_path(temp_dir):
    lexical_root = Path(temp_dir).resolve() / "dedup_logs"
    ensure_directory_nofollow(lexical_root)
    root = lexical_root.resolve()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = root / f"manual_house_cleanup_1_4_0_{stamp}.json"
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"intent report path already exists: {target}")
    return target


def _final_snapshot(state_db, index_path=FILE_INDEX):
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        doctor = decision_store.doctor_issues(conn)
        counts = {
            "active_house": conn.execute(
                "SELECT COUNT(*) FROM files WHERE active = 1 AND source = 'house'"
            ).fetchone()[0],
            "active_queue": conn.execute(
                "SELECT COUNT(*) FROM files WHERE active = 1 AND source = 'queue'"
            ).fetchone()[0],
            "unfinished_operations": conn.execute(
                "SELECT COUNT(*) FROM operations "
                "WHERE state IN ('planned', 'fs_done', 'db_done')"
            ).fetchone()[0],
            "active_runs": conn.execute(
                "SELECT COUNT(*) FROM actual_runs WHERE state = 'active'"
            ).fetchone()[0],
        }
    finally:
        conn.close()
    try:
        payload = json.loads(Path(index_path).read_text(encoding="utf-8"))
        generation = {
            "generated_at": payload.get("generated_at"),
            "generation_id": payload.get("generation_id"),
            "entries": len(payload.get("entries") or []),
        }
    except (OSError, ValueError, TypeError):
        generation = {"read_error": True}
    return {"doctor_issues": doctor, "counts": counts, "index": generation}


def write_execution_report(path, *, plan, result, state_db, index_path=FILE_INDEX):
    target = Path(path)
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "manual_house_cleanup_1_4_0",
        "generated_at": datetime.now().astimezone().isoformat(),
        "plan": plan,
        "result": result,
        "final_snapshot": _final_snapshot(state_db, index_path=index_path),
    }
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
    parser.add_argument(
        "--delete-list",
        help="optional legacy house_human_review delete-title list",
    )
    parser.add_argument("--extra-plan", required=True)
    parser.add_argument("--state-db", default=str(DEFAULT_DB))
    parser.add_argument("--house", default=folderling.DEFAULT_DST_DIR)
    parser.add_argument("--temp", default=folderling.DEFAULT_SRC_DIR)
    parser.add_argument(
        "--report-path",
        help="actual-run JSON log path below <temp>/dedup_logs",
    )
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--ack-user-approved", action="store_true")
    args = parser.parse_args(argv)
    if args.run and not args.ack_user_approved:
        parser.error("--run requires --ack-user-approved")

    delete_list = _read_delete_list(args.delete_list) if args.delete_list else None
    extra_plan_path = Path(args.extra_plan).expanduser().resolve()
    extra_plan_evidence = inspect_regular_file(extra_plan_path)
    extra_plan = json.loads(extra_plan_path.read_text(encoding="utf-8"))
    if not evidence_matches(
        inspect_regular_file(extra_plan_path), extra_plan_evidence
    ):
        raise RuntimeError("manual plan changed while it was read")
    conn = decision_store.connect_state_db_readonly(args.state_db)
    try:
        plan = build_plan(conn, delete_list, extra_plan)
    finally:
        conn.close()
    plan["plan_metadata"]["input_plan"] = {
        "path": str(extra_plan_path),
        "sha256": extra_plan_evidence.sha256,
        "size": extra_plan_evidence.size,
        "mtime_ns": extra_plan_evidence.mtime_ns,
    }
    if not args.run:
        print(json.dumps({"dry_run": True, **plan}, ensure_ascii=False, indent=2))
        return 0
    report_target = _report_path(args.temp, args.report_path)
    intent_target = _intent_report_path(args.temp)
    try:
        result = execute(
            plan,
            state_db=args.state_db,
            house=args.house,
            temp=args.temp,
            report_path=report_target,
            intent_report_path=intent_target,
        )
    except BaseException as exc:
        failure = {
            "dry_run": False,
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "report_path": str(report_target),
            "recovery": _recovery_context(args.state_db),
        }
        intent_metadata = plan.get("plan_metadata", {}).get("intent_report")
        if intent_metadata:
            failure["intent_report_path"] = intent_metadata.get("path")
            failure["intent_report_sha256"] = intent_metadata.get("sha256")
        elif intent_target.is_file():
            intent_evidence = inspect_regular_file(intent_target)
            failure["intent_report_path"] = str(intent_target)
            failure["intent_report_sha256"] = intent_evidence.sha256
        try:
            write_execution_report(
                report_target,
                plan=plan,
                result=failure,
                state_db=args.state_db,
            )
        except BaseException as report_exc:
            print(
                f"failed to write recovery report: {report_exc}",
                file=sys.stderr,
            )
        raise
    report_path = result.get("report_path")
    if report_path is None:
        report_path = write_execution_report(
            report_target,
            plan=plan,
            result=result,
            state_db=args.state_db,
        )
    print(json.dumps(
        {"dry_run": False, **result, "report_path": report_path},
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
