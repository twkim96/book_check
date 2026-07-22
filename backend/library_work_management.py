"""Human-approved work, variant, alias, and routing management for v1.3.4."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

import decision_store
from mutation_io import mutation_lock_for_roots
from normalizer import extract_core_title


ALIAS_KINDS = frozenset({"core_title", "readable_title", "folder_name"})


def _hash(payload: Mapping[str, object]) -> str:
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _backup_path(state_db: Path, label: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return (
        Path(state_db).resolve().parent / "backups" /
        f"before_{label}_{stamp}_{uuid.uuid4().hex[:8]}.sqlite3"
    )


def _descendant_pattern(path: str) -> str:
    value = str(path).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return value + os.sep + "%"


def normalize_alias_key(alias_kind: str, value: str) -> tuple[str, str]:
    alias_kind = str(alias_kind or "").strip()
    if alias_kind not in ALIAS_KINDS:
        raise ValueError("alias kind must be core_title, readable_title, or folder_name")
    display = unicodedata.normalize("NFC", str(value or "").strip())
    if not display or "\x00" in display:
        raise ValueError("alias value is required")
    if alias_kind == "core_title":
        key = extract_core_title(display)
    else:
        key = re.sub(r"\s+", " ", display).casefold()
    if not key:
        raise ValueError("alias key is empty after normalization")
    return key, display


def _work_row(conn, work_bucket_id: int, *, active: bool = True):
    row = conn.execute(
        "SELECT * FROM works WHERE work_bucket_id = ?",
        (int(work_bucket_id),),
    ).fetchone()
    if row is None:
        raise KeyError(work_bucket_id)
    if active and row["status"] != "active":
        raise RuntimeError(f"work is not active: {work_bucket_id}")
    return row


def _work_snapshot(conn, work_bucket_id: int) -> dict:
    work = _work_row(conn, work_bucket_id, active=False)
    variants = [dict(row) for row in conn.execute(
        """
        SELECT v.variant_id, v.variant_kind, v.label, v.status,
               COUNT(CASE WHEN f.active = 1 THEN 1 END) AS active_file_count,
               r.file_id AS representative_file_id
        FROM variants AS v
        LEFT JOIN files AS f ON f.variant_id = v.variant_id
        LEFT JOIN representatives AS r ON r.variant_id = v.variant_id
        WHERE v.work_bucket_id = ?
        GROUP BY v.variant_id
        ORDER BY v.variant_id
        """,
        (int(work_bucket_id),),
    )]
    folders = [dict(row) for row in conn.execute(
        """
        SELECT folder_id, canonical_path, role, state
        FROM work_folders WHERE work_bucket_id = ?
        ORDER BY state, role, canonical_path
        """,
        (int(work_bucket_id),),
    )]
    aliases = [dict(row) for row in conn.execute(
        """
        SELECT alias_id, alias_kind, alias_key, alias_display,
               preferred_folder_id, origin, active
        FROM work_aliases WHERE work_bucket_id = ?
        ORDER BY active DESC, alias_kind, alias_key
        """,
        (int(work_bucket_id),),
    )]
    files = [dict(row) for row in conn.execute(
        """
        SELECT f.file_id, f.canonical_path, f.variant_id, f.active, f.source,
               f.size, f.coordinate_kind, f.coordinate_raw,
               CASE WHEN r.file_id IS NULL THEN 0 ELSE 1 END AS representative
        FROM files AS f
        JOIN variants AS v ON v.variant_id = f.variant_id
        LEFT JOIN representatives AS r ON r.file_id = f.file_id
        WHERE v.work_bucket_id = ? AND f.active = 1
        ORDER BY f.variant_id, f.canonical_path
        """,
        (int(work_bucket_id),),
    )]
    return {
        "work": dict(work),
        "variants": variants,
        "folders": folders,
        "aliases": aliases,
        "files": files,
    }


def work_detail(state_db: Path, work_bucket_id: int) -> dict:
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        snapshot = _work_snapshot(conn, work_bucket_id)
        events = [dict(row) for row in conn.execute(
            """
            SELECT * FROM work_management_events
            WHERE source_work_id = ? OR target_work_id = ?
            ORDER BY event_id DESC LIMIT 100
            """,
            (int(work_bucket_id), int(work_bucket_id)),
        )]
        return {**snapshot, "events": events, "readonly": True}
    finally:
        conn.close()


def alias_preview(
    state_db: Path,
    *,
    alias_kind: str,
    alias_value: str,
    work_bucket_id: int,
    preferred_folder_id: int | None = None,
    replace_alias_id: int | None = None,
) -> dict:
    key, display = normalize_alias_key(alias_kind, alias_value)
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        work = _work_row(conn, work_bucket_id)
        blockers: list[str] = []
        folder = None
        if preferred_folder_id is not None:
            folder = conn.execute(
                "SELECT * FROM work_folders WHERE folder_id = ? AND state = 'active'",
                (int(preferred_folder_id),),
            ).fetchone()
            if folder is None:
                blockers.append("preferred_folder_missing")
            elif int(folder["work_bucket_id"]) != int(work_bucket_id):
                blockers.append("preferred_folder_belongs_to_other_work")
        existing = conn.execute(
            "SELECT * FROM work_aliases WHERE alias_kind = ? AND alias_key = ? AND active = 1",
            (alias_kind, key),
        ).fetchone()
        if existing is not None:
            same = (
                int(existing["work_bucket_id"]) == int(work_bucket_id)
                and existing["preferred_folder_id"] == preferred_folder_id
                and existing["alias_display"] == display
            )
            if same:
                blockers.append("no_changes")
            elif replace_alias_id != int(existing["alias_id"]):
                blockers.append("alias_conflict_requires_explicit_replacement")
        elif replace_alias_id is not None:
            blockers.append("replacement_alias_is_not_current")
        payload = {
            "alias_kind": alias_kind,
            "alias_key": key,
            "alias_display": display,
            "work_bucket_id": int(work_bucket_id),
            "preferred_folder_id": preferred_folder_id,
            "existing_alias_id": int(existing["alias_id"]) if existing else None,
            "replace_alias_id": replace_alias_id,
        }
        return {
            "version": "1.3.4",
            "kind": "work_alias_upsert",
            "item_count": 1,
            "alias_kind": alias_kind,
            "alias_key": key,
            "alias_display": display,
            "work": dict(work),
            "preferred_folder": dict(folder) if folder else None,
            "existing_alias": dict(existing) if existing else None,
            "replace_alias_id": replace_alias_id,
            "blocked_reasons": blockers,
            "apply_available": not blockers,
            "plan_sha256": _hash(payload),
            "readonly": True,
        }
    finally:
        conn.close()


def _record_event(
    conn,
    *,
    action: str,
    plan_sha256: str,
    payload: Mapping[str, object],
    source_work_id: int | None = None,
    target_work_id: int | None = None,
    supersedes_event_id: int | None = None,
) -> int:
    return int(conn.execute(
        """
        INSERT INTO work_management_events(
            action, source_work_id, target_work_id, plan_sha256,
            payload_json, supersedes_event_id
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            action, source_work_id, target_work_id, plan_sha256,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            supersedes_event_id,
        ),
    ).lastrowid)


def apply_alias(
    state_db: Path,
    *,
    house_dir: Path,
    temp_dir: Path,
    alias_kind: str,
    alias_value: str,
    work_bucket_id: int,
    preferred_folder_id: int | None,
    replace_alias_id: int | None,
    confirm_count: int,
    confirm_plan_sha256: str,
) -> dict:
    with mutation_lock_for_roots(house_dir, temp_dir, "work-alias-1.3.4"):
        plan = alias_preview(
            state_db,
            alias_kind=alias_kind,
            alias_value=alias_value,
            work_bucket_id=work_bucket_id,
            preferred_folder_id=preferred_folder_id,
            replace_alias_id=replace_alias_id,
        )
        if not plan["apply_available"]:
            raise RuntimeError("alias plan is blocked: " + ",".join(plan["blocked_reasons"]))
        if confirm_count != 1 or confirm_plan_sha256 != plan["plan_sha256"]:
            raise RuntimeError("alias confirmation is stale")
        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(f"doctor failed before alias update: {issues[0]['kind']}")
            backup = decision_store.backup_state_db(
                conn, _backup_path(state_db, "work_alias")
            )
            with decision_store.transaction(conn):
                if plan["existing_alias"]:
                    conn.execute(
                        "UPDATE work_aliases SET active = 0, updated_at = CURRENT_TIMESTAMP "
                        "WHERE alias_id = ? AND active = 1",
                        (int(plan["existing_alias"]["alias_id"]),),
                    )
                alias_id = int(conn.execute(
                    """
                    INSERT INTO work_aliases(
                        alias_kind, alias_key, alias_display, work_bucket_id,
                        preferred_folder_id, supersedes_alias_id
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        plan["alias_kind"], plan["alias_key"], plan["alias_display"],
                        int(work_bucket_id), preferred_folder_id,
                        int(plan["existing_alias"]["alias_id"])
                        if plan["existing_alias"] else None,
                    ),
                ).lastrowid)
                event_id = _record_event(
                    conn,
                    action="alias_upsert",
                    plan_sha256=plan["plan_sha256"],
                    payload={
                        "alias_id": alias_id,
                        "alias_kind": plan["alias_kind"],
                        "alias_key": plan["alias_key"],
                        "preferred_folder_id": preferred_folder_id,
                        "supersedes_alias_id": (
                            int(plan["existing_alias"]["alias_id"])
                            if plan["existing_alias"] else None
                        ),
                    },
                    target_work_id=int(work_bucket_id),
                )
                remaining = decision_store.doctor_issues(conn)
                if remaining:
                    raise RuntimeError(
                        f"doctor failed after alias update: {remaining[0]['kind']}"
                    )
            return {
                "alias_id": alias_id,
                "event_id": event_id,
                "backup_path": str(backup),
                "plan_sha256": plan["plan_sha256"],
            }
        finally:
            conn.close()


def alias_retire_preview(state_db: Path, *, alias_id: int) -> dict:
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        alias = conn.execute(
            "SELECT * FROM work_aliases WHERE alias_id = ?",
            (int(alias_id),),
        ).fetchone()
        if alias is None:
            raise KeyError(alias_id)
        blockers = [] if alias["active"] else ["alias_already_inactive"]
        payload = {"alias": dict(alias), "action": "retire"}
        return {
            "version": "1.3.4",
            "kind": "work_alias_retire",
            "item_count": 1,
            "alias": dict(alias),
            "blocked_reasons": blockers,
            "apply_available": not blockers,
            "plan_sha256": _hash(payload),
            "readonly": True,
        }
    finally:
        conn.close()


def apply_alias_retire(
    state_db: Path,
    *,
    house_dir: Path,
    temp_dir: Path,
    alias_id: int,
    confirm_count: int,
    confirm_plan_sha256: str,
) -> dict:
    with mutation_lock_for_roots(house_dir, temp_dir, "work-alias-retire-1.3.4"):
        plan = alias_retire_preview(state_db, alias_id=alias_id)
        if not plan["apply_available"]:
            raise RuntimeError(
                "alias retire is blocked: " + ",".join(plan["blocked_reasons"])
            )
        if confirm_count != 1 or confirm_plan_sha256 != plan["plan_sha256"]:
            raise RuntimeError("alias retire confirmation is stale")
        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(
                    f"doctor failed before alias retire: {issues[0]['kind']}"
                )
            backup = decision_store.backup_state_db(
                conn, _backup_path(state_db, "work_alias_retire")
            )
            with decision_store.transaction(conn):
                conn.execute(
                    "UPDATE work_aliases SET active = 0, updated_at = CURRENT_TIMESTAMP "
                    "WHERE alias_id = ? AND active = 1",
                    (int(alias_id),),
                )
                event_id = _record_event(
                    conn,
                    action="alias_retire",
                    plan_sha256=plan["plan_sha256"],
                    payload={"alias_before": plan["alias"]},
                    source_work_id=int(plan["alias"]["work_bucket_id"]),
                )
                remaining = decision_store.doctor_issues(conn)
                if remaining:
                    raise RuntimeError(
                        f"doctor failed after alias retire: {remaining[0]['kind']}"
                    )
            return {
                "alias_id": int(alias_id),
                "event_id": event_id,
                "backup_path": str(backup),
                "plan_sha256": plan["plan_sha256"],
            }
        finally:
            conn.close()


def resolve_work_route(
    conn,
    *,
    core_title: str | None = None,
    readable_title: str | None = None,
    folder_name: str | None = None,
) -> dict:
    candidates = []
    for priority, (kind, value) in enumerate((
        ("core_title", core_title),
        ("readable_title", readable_title),
        ("folder_name", folder_name),
    )):
        if not value:
            continue
        key, _ = normalize_alias_key(kind, value)
        row = conn.execute(
            """
            SELECT wa.*, w.status AS work_status
            FROM work_aliases AS wa
            JOIN works AS w ON w.work_bucket_id = wa.work_bucket_id
            WHERE wa.alias_kind = ? AND wa.alias_key = ? AND wa.active = 1
            """,
            (kind, key),
        ).fetchone()
        if row:
            candidates.append((priority, row))
    if not candidates:
        return {"status": "no_alias", "matched": False}
    _, alias = sorted(candidates, key=lambda item: item[0])[0]
    if alias["work_status"] != "active":
        return {
            "status": "retired_work",
            "matched": False,
            "alias_id": int(alias["alias_id"]),
        }
    folder = None
    if alias["preferred_folder_id"] is not None:
        folder = conn.execute(
            "SELECT * FROM work_folders WHERE folder_id = ? AND state = 'active'",
            (int(alias["preferred_folder_id"]),),
        ).fetchone()
    if folder is None:
        folder = conn.execute(
            """
            SELECT * FROM work_folders
            WHERE work_bucket_id = ? AND role = 'primary' AND state = 'active'
            """,
            (int(alias["work_bucket_id"]),),
        ).fetchone()
    if folder is None:
        return {
            "status": "work_without_route",
            "matched": True,
            "alias_id": int(alias["alias_id"]),
            "work_bucket_id": int(alias["work_bucket_id"]),
            "target_folder": None,
        }
    return {
        "status": "target",
        "matched": True,
        "alias_id": int(alias["alias_id"]),
        "alias_kind": alias["alias_kind"],
        "work_bucket_id": int(alias["work_bucket_id"]),
        "folder_id": int(folder["folder_id"]),
        "target_folder": folder["canonical_path"],
    }


def attach_routed_file(
    conn,
    *,
    file_id: str,
    work_bucket_id: int,
    alias_id: int,
) -> dict:
    """Attach one alias-routed intake as a distinct variant of the selected work."""
    work = _work_row(conn, work_bucket_id)
    file = conn.execute(
        "SELECT * FROM files WHERE file_id = ? AND active = 1 AND source = 'house'",
        (str(file_id),),
    ).fetchone()
    if file is None:
        raise RuntimeError("routed file must be an active house file")
    if file["current_fingerprint_id"] is None:
        raise RuntimeError("routed file fingerprint is missing")
    alias = conn.execute(
        "SELECT * FROM work_aliases WHERE alias_id = ? AND active = 1",
        (int(alias_id),),
    ).fetchone()
    if alias is None or int(alias["work_bucket_id"]) != int(work_bucket_id):
        raise RuntimeError("routing alias is stale")
    if file["variant_id"] is not None:
        current = conn.execute(
            "SELECT work_bucket_id FROM variants WHERE variant_id = ? AND status = 'active'",
            (int(file["variant_id"]),),
        ).fetchone()
        if current is None or int(current["work_bucket_id"]) != int(work_bucket_id):
            raise RuntimeError("routed file already belongs to another work")
        return {
            "work_bucket_id": int(work_bucket_id),
            "variant_id": int(file["variant_id"]),
            "alias_id": int(alias_id),
            "existing": True,
        }
    variant_id = int(conn.execute(
        "INSERT INTO variants(work_bucket_id, variant_kind, label) "
        "VALUES (?, 'base', ?)",
        (int(work_bucket_id), f"alias-route:{file_id}"),
    ).lastrowid)
    conn.execute(
        "UPDATE files SET variant_id = ?, assignment_state = 'managed', "
        "assignment_origin = 'human_decision', protected = 1 WHERE file_id = ?",
        (variant_id, str(file_id)),
    )
    conn.execute(
        "INSERT INTO representatives(variant_id, file_id) VALUES (?, ?)",
        (variant_id, str(file_id)),
    )
    _record_event(
        conn,
        action="routed_intake",
        plan_sha256=_hash({
            "file_id": str(file_id),
            "fingerprint_id": int(file["current_fingerprint_id"]),
            "work_bucket_id": int(work_bucket_id),
            "alias_id": int(alias_id),
        }),
        payload={
            "file_id": str(file_id),
            "variant_id": variant_id,
            "alias_id": int(alias_id),
            "work_display_title": work["display_title"],
        },
        target_work_id=int(work_bucket_id),
    )
    return {
        "work_bucket_id": int(work_bucket_id),
        "variant_id": variant_id,
        "alias_id": int(alias_id),
        "existing": False,
    }


def work_merge_preview(
    state_db: Path, *, source_work_id: int, target_work_id: int
) -> dict:
    if int(source_work_id) == int(target_work_id):
        raise ValueError("source and target work must differ")
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        source = _work_snapshot(conn, source_work_id)
        target = _work_snapshot(conn, target_work_id)
        blockers: list[str] = []
        if source["work"]["status"] != "active":
            blockers.append("source_work_not_active")
        if target["work"]["status"] != "active":
            blockers.append("target_work_not_active")
        active_variants = [v for v in source["variants"] if v["status"] == "active"]
        if not active_variants:
            blockers.append("source_has_no_active_variants")
        source_primary = [
            f for f in source["folders"] if f["state"] == "active" and f["role"] == "primary"
        ]
        target_primary = [
            f for f in target["folders"] if f["state"] == "active" and f["role"] == "primary"
        ]
        demoted_folder_ids = [
            int(row["folder_id"]) for row in source_primary
        ] if target_primary else []
        payload = {
            "source": source,
            "target": target,
            "demoted_folder_ids": demoted_folder_ids,
        }
        return {
            "version": "1.3.4",
            "kind": "work_merge",
            "item_count": len(active_variants),
            "source": source,
            "target": target,
            "demoted_folder_ids": demoted_folder_ids,
            "blocked_reasons": blockers,
            "apply_available": not blockers,
            "plan_sha256": _hash(payload),
            "readonly": True,
        }
    finally:
        conn.close()


def apply_work_merge(
    state_db: Path,
    *,
    house_dir: Path,
    temp_dir: Path,
    source_work_id: int,
    target_work_id: int,
    confirm_count: int,
    confirm_plan_sha256: str,
) -> dict:
    with mutation_lock_for_roots(house_dir, temp_dir, "work-merge-1.3.4"):
        plan = work_merge_preview(
            state_db, source_work_id=source_work_id, target_work_id=target_work_id
        )
        if not plan["apply_available"]:
            raise RuntimeError("work merge is blocked: " + ",".join(plan["blocked_reasons"]))
        if confirm_count != plan["item_count"] or confirm_plan_sha256 != plan["plan_sha256"]:
            raise RuntimeError("work merge confirmation is stale")
        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(f"doctor failed before work merge: {issues[0]['kind']}")
            backup = decision_store.backup_state_db(
                conn, _backup_path(state_db, "work_merge")
            )
            with decision_store.transaction(conn):
                for folder_id in plan["demoted_folder_ids"]:
                    conn.execute(
                        "UPDATE work_folders SET role = 'edition', updated_at = CURRENT_TIMESTAMP "
                        "WHERE folder_id = ? AND state = 'active'",
                        (int(folder_id),),
                    )
                conn.execute(
                    "UPDATE variants SET work_bucket_id = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE work_bucket_id = ?",
                    (int(target_work_id), int(source_work_id)),
                )
                conn.execute(
                    "UPDATE work_folders SET work_bucket_id = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE work_bucket_id = ? AND state = 'active'",
                    (int(target_work_id), int(source_work_id)),
                )
                conn.execute(
                    "UPDATE work_aliases SET work_bucket_id = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE work_bucket_id = ? AND active = 1",
                    (int(target_work_id), int(source_work_id)),
                )
                conn.execute(
                    "UPDATE works SET status = 'retired', updated_at = CURRENT_TIMESTAMP "
                    "WHERE work_bucket_id = ?",
                    (int(source_work_id),),
                )
                event_id = _record_event(
                    conn,
                    action="work_merge",
                    plan_sha256=plan["plan_sha256"],
                    payload={
                        "source_before": plan["source"],
                        "target_before": plan["target"],
                        "demoted_folder_ids": plan["demoted_folder_ids"],
                    },
                    source_work_id=int(source_work_id),
                    target_work_id=int(target_work_id),
                )
                remaining = decision_store.doctor_issues(conn)
                if remaining:
                    raise RuntimeError(
                        f"doctor failed after work merge: {remaining[0]['kind']}"
                    )
            return {
                "event_id": event_id,
                "source_work_id": int(source_work_id),
                "target_work_id": int(target_work_id),
                "backup_path": str(backup),
                "plan_sha256": plan["plan_sha256"],
            }
        finally:
            conn.close()


def work_split_preview(
    state_db: Path,
    *,
    source_work_id: int,
    variant_ids: Sequence[int],
    display_title: str,
    folder_ids: Sequence[int] = (),
    alias_ids: Sequence[int] = (),
) -> dict:
    selected_variants = sorted({int(value) for value in variant_ids})
    selected_folders = sorted({int(value) for value in folder_ids})
    selected_aliases = sorted({int(value) for value in alias_ids})
    title = unicodedata.normalize("NFC", str(display_title or "").strip())
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        source = _work_snapshot(conn, source_work_id)
        blockers: list[str] = []
        if source["work"]["status"] != "active":
            blockers.append("source_work_not_active")
        active_variant_ids = {
            int(row["variant_id"]) for row in source["variants"] if row["status"] == "active"
        }
        if not selected_variants:
            blockers.append("no_variants_selected")
        if not set(selected_variants).issubset(active_variant_ids):
            blockers.append("variant_not_in_source_work")
        if set(selected_variants) == active_variant_ids and active_variant_ids:
            blockers.append("all_variants_selected_use_work_rename")
        source_folder_ids = {
            int(row["folder_id"]) for row in source["folders"] if row["state"] == "active"
        }
        if not set(selected_folders).issubset(source_folder_ids):
            blockers.append("folder_not_in_source_work")
        for folder_id in selected_folders:
            folder = next(
                (row for row in source["folders"] if int(row["folder_id"]) == folder_id),
                None,
            )
            if folder is None:
                continue
            contained_variants = {
                int(row[0])
                for row in conn.execute(
                    """
                    SELECT DISTINCT variant_id FROM files
                    WHERE active = 1 AND source = 'house' AND variant_id IS NOT NULL
                      AND canonical_path LIKE ? ESCAPE '\\'
                    """,
                    (_descendant_pattern(folder["canonical_path"]),),
                )
            }
            if not contained_variants.issubset(set(selected_variants)):
                blockers.append(f"folder_contains_unselected_variants:{folder_id}")
        source_alias_ids = {
            int(row["alias_id"]) for row in source["aliases"] if row["active"]
        }
        if not set(selected_aliases).issubset(source_alias_ids):
            blockers.append("alias_not_in_source_work")
        if not title:
            blockers.append("display_title_required")
        cleared_alias_routes = sorted(
            int(row["alias_id"])
            for row in source["aliases"]
            if row["preferred_folder_id"] is not None
            and (
                (
                    int(row["alias_id"]) in selected_aliases
                    and int(row["preferred_folder_id"]) not in selected_folders
                )
                or (
                    int(row["alias_id"]) not in selected_aliases
                    and int(row["preferred_folder_id"]) in selected_folders
                )
            )
        )
        payload = {
            "source": source,
            "variant_ids": selected_variants,
            "folder_ids": selected_folders,
            "alias_ids": selected_aliases,
            "display_title": title,
            "cleared_alias_routes": cleared_alias_routes,
        }
        return {
            "version": "1.3.4",
            "kind": "work_split",
            "item_count": len(selected_variants),
            "source": source,
            "variant_ids": selected_variants,
            "folder_ids": selected_folders,
            "alias_ids": selected_aliases,
            "display_title": title,
            "cleared_alias_routes": cleared_alias_routes,
            "blocked_reasons": blockers,
            "apply_available": not blockers,
            "plan_sha256": _hash(payload),
            "readonly": True,
        }
    finally:
        conn.close()


def apply_work_split(
    state_db: Path,
    *,
    house_dir: Path,
    temp_dir: Path,
    source_work_id: int,
    variant_ids: Sequence[int],
    display_title: str,
    folder_ids: Sequence[int],
    alias_ids: Sequence[int],
    confirm_count: int,
    confirm_plan_sha256: str,
) -> dict:
    with mutation_lock_for_roots(house_dir, temp_dir, "work-split-1.3.4"):
        plan = work_split_preview(
            state_db,
            source_work_id=source_work_id,
            variant_ids=variant_ids,
            display_title=display_title,
            folder_ids=folder_ids,
            alias_ids=alias_ids,
        )
        if not plan["apply_available"]:
            raise RuntimeError("work split is blocked: " + ",".join(plan["blocked_reasons"]))
        if confirm_count != plan["item_count"] or confirm_plan_sha256 != plan["plan_sha256"]:
            raise RuntimeError("work split confirmation is stale")
        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(f"doctor failed before work split: {issues[0]['kind']}")
            backup = decision_store.backup_state_db(
                conn, _backup_path(state_db, "work_split")
            )
            with decision_store.transaction(conn):
                new_work_id = int(conn.execute(
                    "INSERT INTO works(display_title) VALUES (?)",
                    (plan["display_title"],),
                ).lastrowid)
                marks = ",".join("?" for _ in plan["variant_ids"])
                conn.execute(
                    f"UPDATE variants SET work_bucket_id = ?, updated_at = CURRENT_TIMESTAMP "
                    f"WHERE variant_id IN ({marks})",
                    (new_work_id, *plan["variant_ids"]),
                )
                if plan["folder_ids"]:
                    folder_marks = ",".join("?" for _ in plan["folder_ids"])
                    conn.execute(
                        f"UPDATE work_folders SET work_bucket_id = ?, updated_at = CURRENT_TIMESTAMP "
                        f"WHERE folder_id IN ({folder_marks})",
                        (new_work_id, *plan["folder_ids"]),
                    )
                if plan["alias_ids"]:
                    alias_marks = ",".join("?" for _ in plan["alias_ids"])
                    conn.execute(
                        f"UPDATE work_aliases SET work_bucket_id = ?, "
                        f"updated_at = CURRENT_TIMESTAMP WHERE alias_id IN ({alias_marks})",
                        (new_work_id, *plan["alias_ids"]),
                    )
                for alias_id in plan["cleared_alias_routes"]:
                    conn.execute(
                        "UPDATE work_aliases SET preferred_folder_id = NULL, "
                        "updated_at = CURRENT_TIMESTAMP WHERE alias_id = ?",
                        (int(alias_id),),
                    )
                event_id = _record_event(
                    conn,
                    action="work_split",
                    plan_sha256=plan["plan_sha256"],
                    payload={
                        "source_before": plan["source"],
                        "variant_ids": plan["variant_ids"],
                        "folder_ids": plan["folder_ids"],
                        "alias_ids": plan["alias_ids"],
                        "cleared_alias_routes": plan["cleared_alias_routes"],
                        "display_title": plan["display_title"],
                    },
                    source_work_id=int(source_work_id),
                    target_work_id=new_work_id,
                )
                remaining = decision_store.doctor_issues(conn)
                if remaining:
                    raise RuntimeError(
                        f"doctor failed after work split: {remaining[0]['kind']}"
                    )
            return {
                "event_id": event_id,
                "source_work_id": int(source_work_id),
                "new_work_id": new_work_id,
                "backup_path": str(backup),
                "plan_sha256": plan["plan_sha256"],
            }
        finally:
            conn.close()


def representative_preview(state_db: Path, *, variant_id: int, file_id: str) -> dict:
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        variant = conn.execute(
            "SELECT * FROM variants WHERE variant_id = ? AND status = 'active'",
            (int(variant_id),),
        ).fetchone()
        if variant is None:
            raise KeyError(variant_id)
        file = conn.execute(
            "SELECT file_id, canonical_path, variant_id, active, source, assignment_state "
            "FROM files WHERE file_id = ?",
            (str(file_id),),
        ).fetchone()
        blockers: list[str] = []
        if file is None:
            blockers.append("file_missing")
        elif (
            not file["active"] or file["source"] != "house"
            or file["assignment_state"] != "managed"
            or int(file["variant_id"] or -1) != int(variant_id)
        ):
            blockers.append("file_is_not_active_managed_variant_member")
        current = conn.execute(
            "SELECT file_id FROM representatives WHERE variant_id = ?",
            (int(variant_id),),
        ).fetchone()
        if current and current["file_id"] == file_id:
            blockers.append("no_changes")
        payload = {
            "variant_id": int(variant_id),
            "file_id": str(file_id),
            "current_file_id": current["file_id"] if current else None,
        }
        return {
            "version": "1.3.4",
            "kind": "representative_replace",
            "item_count": 1,
            "variant": dict(variant),
            "file": dict(file) if file else None,
            "current_file_id": current["file_id"] if current else None,
            "blocked_reasons": blockers,
            "apply_available": not blockers,
            "plan_sha256": _hash(payload),
            "readonly": True,
        }
    finally:
        conn.close()


def apply_representative(
    state_db: Path,
    *,
    house_dir: Path,
    temp_dir: Path,
    variant_id: int,
    file_id: str,
    confirm_count: int,
    confirm_plan_sha256: str,
) -> dict:
    with mutation_lock_for_roots(house_dir, temp_dir, "representative-replace-1.3.4"):
        plan = representative_preview(state_db, variant_id=variant_id, file_id=file_id)
        if not plan["apply_available"]:
            raise RuntimeError(
                "representative plan is blocked: " + ",".join(plan["blocked_reasons"])
            )
        if confirm_count != 1 or confirm_plan_sha256 != plan["plan_sha256"]:
            raise RuntimeError("representative confirmation is stale")
        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(
                    f"doctor failed before representative update: {issues[0]['kind']}"
                )
            backup = decision_store.backup_state_db(
                conn, _backup_path(state_db, "representative")
            )
            with decision_store.transaction(conn):
                if plan["current_file_id"] is None:
                    conn.execute(
                        "INSERT INTO representatives(variant_id, file_id) VALUES (?, ?)",
                        (int(variant_id), str(file_id)),
                    )
                else:
                    conn.execute(
                        "UPDATE representatives SET file_id = ?, updated_at = CURRENT_TIMESTAMP "
                        "WHERE variant_id = ?",
                        (str(file_id), int(variant_id)),
                    )
                event_id = _record_event(
                    conn,
                    action="representative_replace",
                    plan_sha256=plan["plan_sha256"],
                    payload={
                        "variant_id": int(variant_id),
                        "before_file_id": plan["current_file_id"],
                        "after_file_id": str(file_id),
                    },
                    target_work_id=int(plan["variant"]["work_bucket_id"]),
                )
                remaining = decision_store.doctor_issues(conn)
                if remaining:
                    raise RuntimeError(
                        f"doctor failed after representative update: {remaining[0]['kind']}"
                    )
            return {
                "event_id": event_id,
                "variant_id": int(variant_id),
                "file_id": str(file_id),
                "backup_path": str(backup),
                "plan_sha256": plan["plan_sha256"],
            }
        finally:
            conn.close()
