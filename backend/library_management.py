"""Safe 1.3.2 human relationship and quarantine workflows."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

import decision_store
from dedup_mutations import _ensure_intake_fingerprint, _file_state, user_quarantine
from mutation_io import (
    ensure_directory_nofollow,
    evidence_matches,
    inspect_regular_file,
    mutation_lock,
    mutation_lock_for_roots,
    unlink_owned,
)


RELATIONSHIP_VERDICTS = frozenset({
    "same_content", "same_work_distinct_variant", "distinct_work",
})
RESTORE_VERDICTS = frozenset({"same_work_distinct_variant", "distinct_work"})
QUARANTINE_ACTIONS = frozenset({"exact_quarantine", "human_quarantine", "user_quarantine"})
MAX_PURGE_BATCH = 200


def _hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _backup_path(state_db: Path, label: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return state_db.parent / "backups" / f"before_{label}_{stamp}_{uuid.uuid4().hex[:8]}.sqlite3"


def _file_row(conn, file_id: str, *, active: bool | None = None):
    clause = "" if active is None else " AND f.active = ?"
    params = (file_id,) if active is None else (file_id, 1 if active else 0)
    row = conn.execute(
        """
        SELECT f.*, a.core_title, a.readable_title, a.author,
               v.work_bucket_id, v.variant_kind, v.label AS variant_label,
               CASE WHEN r.file_id IS NULL THEN 0 ELSE 1 END AS representative,
               fp.raw_sha256, fp.normalized_sha256, fp.normalized_length,
               fp.status AS fingerprint_status
        FROM files AS f
        LEFT JOIN file_analysis AS a ON a.file_id = f.file_id
        LEFT JOIN variants AS v ON v.variant_id = f.variant_id
        LEFT JOIN representatives AS r ON r.file_id = f.file_id
        LEFT JOIN fingerprints AS fp ON fp.fingerprint_id = f.current_fingerprint_id
        WHERE f.file_id = ?
        """ + clause,
        params,
    ).fetchone()
    if row is None:
        raise KeyError(file_id)
    return row


def _public_file(row) -> dict:
    path = Path(str(row["canonical_path"]))
    return {
        "file_id": row["file_id"], "name": path.name, "canonical_path": str(path),
        "source": row["source"], "active": bool(row["active"]), "size": row["size"],
        "mtime_ns": row["mtime_ns"], "dev": row["dev"], "ino": row["ino"],
        "ctime_ns": row["ctime_ns"], "current_fingerprint_id": row["current_fingerprint_id"],
        "raw_sha256": row["raw_sha256"], "normalized_sha256": row["normalized_sha256"],
        "normalized_length": row["normalized_length"], "fingerprint_status": row["fingerprint_status"],
        "core_title": row["core_title"], "readable_title": row["readable_title"],
        "author": row["author"], "assignment_state": row["assignment_state"],
        "variant_id": row["variant_id"], "work_bucket_id": row["work_bucket_id"],
        "variant_kind": row["variant_kind"], "variant_label": row["variant_label"],
        "protected": bool(row["protected"]), "representative": bool(row["representative"]),
        "coordinate_kind": row["coordinate_kind"], "coordinate_raw": row["coordinate_raw"],
        "part_num": row["part_num"], "part_den": row["part_den"],
        "volume_num": row["volume_num"], "volume_den": row["volume_den"],
        "coordinate_symbol": row["coordinate_symbol"],
        "episode_start": row["episode_start"], "episode_end": row["episode_end"],
    }


def _require_current_file(
    row, *, source: str = "house", require_fingerprint: bool = True,
) -> None:
    if not row["active"] or row["source"] != source:
        raise RuntimeError(f"file must be an active {source} file: {row['file_id']}")
    if require_fingerprint and row["current_fingerprint_id"] is None:
        raise RuntimeError(f"current fingerprint is missing: {row['file_id']}")
    evidence = inspect_regular_file(row["canonical_path"])
    expected = (row["dev"], row["ino"], row["ctime_ns"], row["size"], row["mtime_ns"])
    actual = (evidence.dev, evidence.ino, evidence.ctime_ns, evidence.size, evidence.mtime_ns)
    if expected != actual:
        raise RuntimeError(f"file snapshot is stale: {row['file_id']}")


def _prepare_current_fingerprint(conn, file_id: str):
    """Attach a current raw fingerprint after backup, reusing immutable evidence."""
    source = _file_state(conn, file_id)
    if source["current_fingerprint_id"] is not None:
        return source
    _require_current_file(_file_row(conn, file_id, active=True), require_fingerprint=False)
    evidence = inspect_regular_file(source["canonical_path"])
    existing = conn.execute(
        """
        SELECT fingerprint_id, raw_sha256
        FROM fingerprints
        WHERE file_id = ? AND canonical_path = ? AND size = ? AND mtime_ns = ?
          AND raw_sha256 IS NOT NULL
        ORDER BY fingerprint_id DESC
        """,
        (source["file_id"], source["canonical_path"], source["size"], source["mtime_ns"]),
    ).fetchall()
    reusable = next((row for row in existing if row["raw_sha256"] == evidence.sha256), None)
    if reusable is not None:
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE files SET current_fingerprint_id = ? WHERE file_id = ? "
                "AND current_fingerprint_id IS NULL",
                (reusable["fingerprint_id"], source["file_id"]),
            )
        return _file_state(conn, file_id)
    return _ensure_intake_fingerprint(conn, source)


def relationship_preview(
    state_db: Path, *, left_file_id: str, right_file_id: str,
    verdict: str, variant_kind: str = "other", note: str = "",
) -> dict:
    if verdict not in RELATIONSHIP_VERDICTS:
        raise ValueError("unknown relationship verdict")
    if variant_kind not in {"base", "revision", "adult", "translation", "other"}:
        raise ValueError("unknown variant kind")
    if not left_file_id or not right_file_id or left_file_id == right_file_id:
        raise ValueError("two distinct file IDs are required")
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        left = _file_row(conn, left_file_id, active=True)
        right = _file_row(conn, right_file_id, active=True)
        blockers = []
        for row in (left, right):
            if row["source"] != "house":
                blockers.append(f"outside_house:{row['file_id']}")
            if row["current_fingerprint_id"] is None:
                blockers.append(f"missing_fingerprint:{row['file_id']}")
        ordered = sorted((left_file_id, right_file_id))
        existing = conn.execute(
            "SELECT * FROM decisions WHERE left_file_id = ? AND right_file_id = ? AND active = 1",
            ordered,
        ).fetchone()
        payload = {
            "left_file_id": left_file_id, "right_file_id": right_file_id,
            "left_fingerprint_id": left["current_fingerprint_id"],
            "right_fingerprint_id": right["current_fingerprint_id"],
            "verdict": verdict, "variant_kind": variant_kind, "note": note.strip(),
            "existing_decision_id": existing["decision_id"] if existing else None,
            "existing_verdict": existing["verdict"] if existing else None,
        }
        if existing and existing["verdict"] == verdict:
            blockers.append("same_active_decision")
        return {
            "version": "1.3.2", "kind": "relationship", "item_count": 2,
            "left": _public_file(left), "right": _public_file(right),
            "verdict": verdict, "variant_kind": variant_kind, "note": note.strip(),
            "existing_decision": dict(existing) if existing else None,
            "mode": "correction" if existing else "new",
            "blocked_reasons": blockers, "apply_available": not blockers,
            "plan_sha256": _hash(payload), "readonly": True,
        }
    finally:
        conn.close()


def apply_relationship(
    state_db: Path, *, house_dir: Path, temp_dir: Path,
    left_file_id: str, right_file_id: str, verdict: str,
    variant_kind: str, note: str, confirm_count: int, confirm_plan_sha256: str,
) -> dict:
    with mutation_lock_for_roots(house_dir, temp_dir, "library-relationship-1.3.2"):
        plan = relationship_preview(
            state_db, left_file_id=left_file_id, right_file_id=right_file_id,
            verdict=verdict, variant_kind=variant_kind, note=note,
        )
        if not plan["apply_available"]:
            raise RuntimeError("relationship plan is blocked: " + ",".join(plan["blocked_reasons"]))
        if confirm_count != plan["item_count"] or confirm_plan_sha256 != plan["plan_sha256"]:
            raise RuntimeError("relationship confirmation is stale")
        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(f"doctor failed before relationship decision: {issues[0]['kind']}")
            backup = decision_store.backup_state_db(conn, _backup_path(state_db, "relationship_decision"))
            existing = plan["existing_decision"]
            if existing:
                decision_id = decision_store.correct_decision(
                    conn, decision_id=existing["decision_id"], verdict=verdict,
                    variant_kind=variant_kind, note=note.strip() or None,
                )
            else:
                with decision_store.transaction(conn):
                    left = decision_store._active_file_with_fingerprint(conn, left_file_id)
                    right = decision_store._active_file_with_fingerprint(conn, right_file_id)
                    if (
                        left["current_fingerprint_id"] != plan["left"]["current_fingerprint_id"]
                        or right["current_fingerprint_id"] != plan["right"]["current_fingerprint_id"]
                    ):
                        raise RuntimeError("relationship fingerprint changed")
                    evidence = json.dumps({
                        "actor": "local_user", "source": "library_ui",
                        "plan_sha256": plan["plan_sha256"],
                    }, ensure_ascii=False, sort_keys=True)
                    review_id = conn.execute(
                        """
                        INSERT INTO review_items(
                            candidate_file_id, reference_file_id,
                            left_fingerprint_id, right_fingerprint_id,
                            classification, state, evidence_json
                        ) VALUES (?, ?, ?, ?, 'user_pair_review', 'pending', ?)
                        """,
                        (left_file_id, right_file_id, left["current_fingerprint_id"],
                         right["current_fingerprint_id"], evidence),
                    ).lastrowid
                    decision_id = decision_store.apply_decision(
                        conn, review_id=review_id, candidate_file_id=left_file_id,
                        reference_file_id=right_file_id, verdict=verdict,
                        variant_kind=variant_kind, note=note.strip() or None,
                    )
            with decision_store.transaction(conn):
                conn.execute(
                    "UPDATE decisions SET evidence_json = ? WHERE decision_id = ?",
                    (json.dumps({
                        "actor": "local_user", "source": "library_ui",
                        "plan_sha256": plan["plan_sha256"], "mode": plan["mode"],
                    }, ensure_ascii=False, sort_keys=True), decision_id),
                )
            remaining = decision_store.doctor_issues(conn)
            if remaining:
                raise RuntimeError(f"doctor failed after relationship decision: {remaining[0]['kind']}")
            return {"decision_id": decision_id, "backup_path": str(backup), "plan_sha256": plan["plan_sha256"]}
        finally:
            conn.close()


def cancel_relationship(state_db: Path, *, house_dir: Path, temp_dir: Path, decision_id: int) -> dict:
    with mutation_lock_for_roots(house_dir, temp_dir, "cancel-relationship-1.3.2"):
        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(
                    f"doctor failed before decision cancel: {issues[0]['kind']}"
                )
            backup = decision_store.backup_state_db(conn, _backup_path(state_db, "cancel_relationship"))
            review_id = decision_store.cancel_decision(conn, int(decision_id))
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(f"doctor failed after decision cancel: {issues[0]['kind']}")
            return {"decision_id": int(decision_id), "review_id": review_id, "backup_path": str(backup)}
        finally:
            conn.close()


def quarantine_preview(
    state_db: Path, *, temp_dir: Path, source_file_id: str,
    keep_file_id: str | None = None,
) -> dict:
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        source = _file_row(conn, source_file_id, active=True)
        blockers = []
        try:
            _require_current_file(source, require_fingerprint=False)
        except RuntimeError as exc:
            blockers.append(str(exc))
        keep = None
        if keep_file_id:
            if keep_file_id == source_file_id:
                blockers.append("keep_matches_source")
            else:
                try:
                    keep = _file_row(conn, keep_file_id, active=True)
                    _require_current_file(keep, require_fingerprint=False)
                except (KeyError, RuntimeError) as exc:
                    blockers.append(f"invalid_keep:{exc}")
        replacement = None
        remaining_variant_files = 0
        retired_variant = False
        retired_work = False
        if source["representative"] and source["variant_id"] is not None:
            candidates = conn.execute(
                """
                SELECT f.file_id FROM files AS f
                WHERE f.variant_id = ? AND f.active = 1 AND f.source = 'house' AND f.file_id != ?
                ORDER BY CASE WHEN f.file_id = ? THEN 0 ELSE 1 END, f.protected DESC, f.canonical_path
                """,
                (source["variant_id"], source_file_id, keep_file_id or ""),
            ).fetchall()
            remaining_variant_files = len(candidates)
            if candidates:
                replacement = _file_row(conn, candidates[0]["file_id"], active=True)
                try:
                    _require_current_file(replacement, require_fingerprint=False)
                except RuntimeError as exc:
                    blockers.append(f"invalid_replacement:{exc}")
            else:
                retired_variant = True
                if source["work_bucket_id"] is not None:
                    active_work_files = conn.execute(
                        """
                        SELECT COUNT(*) FROM files AS f JOIN variants AS v ON v.variant_id = f.variant_id
                        WHERE v.work_bucket_id = ? AND f.active = 1 AND f.file_id != ?
                        """,
                        (source["work_bucket_id"], source_file_id),
                    ).fetchone()[0]
                    retired_work = active_work_files == 0
        destination_root = Path(temp_dir).resolve() / "trash_bin" / "user_approved_discard"
        payload = {
            "source_file_id": source_file_id, "source_fingerprint_id": source["current_fingerprint_id"],
            "source_path": source["canonical_path"], "keep_file_id": keep_file_id,
            "keep_fingerprint_id": keep["current_fingerprint_id"] if keep else None,
            "replacement_file_id": replacement["file_id"] if replacement else None,
            "destination_root": str(destination_root),
        }
        preparation_ids = {
            row["file_id"] for row in (source, keep, replacement)
            if row is not None and row["current_fingerprint_id"] is None
        }
        return {
            "version": "1.3.2", "kind": "user_quarantine", "item_count": 1,
            "source": _public_file(source), "keep": _public_file(keep) if keep else None,
            "replacement_representative": _public_file(replacement) if replacement else None,
            "remaining_variant_files": remaining_variant_files,
            "retired_variant": retired_variant, "retired_work": retired_work,
            "fingerprint_preparation_count": len(preparation_ids),
            "destination_root": str(destination_root),
            "blocked_reasons": blockers, "apply_available": not blockers,
            "plan_sha256": _hash(payload), "readonly": True,
        }
    finally:
        conn.close()


def apply_quarantine(
    state_db: Path, *, house_dir: Path, temp_dir: Path, index_path: Path,
    source_file_id: str, keep_file_id: str | None,
    confirm_count: int, confirm_plan_sha256: str, progress=None,
) -> dict:
    from library_review import _refresh_review_index
    with mutation_lock_for_roots(house_dir, temp_dir, "user-quarantine-1.3.2"):
        plan = quarantine_preview(
            state_db, temp_dir=temp_dir, source_file_id=source_file_id,
            keep_file_id=keep_file_id,
        )
        if not plan["apply_available"]:
            raise RuntimeError("quarantine plan is blocked: " + ",".join(plan["blocked_reasons"]))
        if confirm_count != 1 or confirm_plan_sha256 != plan["plan_sha256"]:
            raise RuntimeError("quarantine confirmation is stale")
        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(f"doctor failed before quarantine: {issues[0]['kind']}")
            backup = decision_store.backup_state_db(conn, _backup_path(state_db, "user_quarantine"))
            preparation_ids = [source_file_id]
            for key in ("keep", "replacement_representative"):
                item = plan[key]
                if item and item["file_id"] not in preparation_ids:
                    preparation_ids.append(item["file_id"])
            for file_id in preparation_ids:
                _prepare_current_fingerprint(conn, file_id)
            decision_store.issue_actual_run_token(conn, str(backup), house_dir=house_dir, temp_dir=temp_dir)
        finally:
            conn.close()
        paths = [plan["source"]["canonical_path"]]
        for key in ("keep", "replacement_representative"):
            if plan[key] and plan[key]["canonical_path"] not in paths:
                paths.append(plan[key]["canonical_path"])
        run_id, manifest_path = decision_store.prepare_actual_run(
            state_db, house_dir, temp_dir, manifest_paths=paths
        )
        try:
            conn = decision_store.connect_state_db(state_db)
            try:
                ensure_directory_nofollow(plan["destination_root"])
                result = user_quarantine(
                    conn, source_file_id=source_file_id, keep_file_id=keep_file_id,
                    replacement_file_id=(plan["replacement_representative"] or {}).get("file_id"),
                    quarantine_dir=plan["destination_root"], run_id=run_id,
                )
                decision_store.finish_actual_run(conn, run_id, success=True)
            finally:
                conn.close()
        except BaseException as exc:
            conn = decision_store.connect_state_db(state_db)
            try:
                decision_store.finish_actual_run(conn, run_id, success=False, error=str(exc))
            finally:
                conn.close()
            raise
        if progress:
            progress(1, 1, plan["source"]["name"])
        try:
            index = _refresh_review_index(state_db=state_db, house_dir=house_dir, index_path=index_path)
        except Exception as exc:
            index = {"index_updated": False, "house_index_synced": False,
                     "warning": f"격리는 완료됐지만 index 갱신에 실패했습니다: {exc}"}
        return {**result, "run_id": run_id, "manifest_path": manifest_path,
                "backup_path": str(backup), "impact": {
                    "retired_variant": plan["retired_variant"], "retired_work": plan["retired_work"],
                    "replacement_representative": plan["replacement_representative"],
                }, **index}


def _quarantine_origin(conn, operation_id: int):
    row = conn.execute(
        """
        SELECT o.*, f.canonical_path AS file_path, f.source AS file_source,
               f.active AS file_active, f.current_fingerprint_id AS file_fingerprint_id,
               f.variant_id AS file_variant_id
        FROM operations AS o JOIN files AS f ON f.file_id = o.file_id
        WHERE o.operation_id = ?
        """,
        (int(operation_id),),
    ).fetchone()
    if row is None:
        raise KeyError(operation_id)
    return row


def restore_preview(
    state_db: Path, *, house_dir: Path, operation_id: int,
    reference_file_id: str | None, verdict: str, note: str = "",
) -> dict:
    if verdict not in RESTORE_VERDICTS:
        raise ValueError("restore verdict must preserve distinct content")
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        origin = _quarantine_origin(conn, operation_id)
        blockers = []
        if origin["action"] not in QUARANTINE_ACTIONS or origin["state"] != "committed":
            blockers.append("not_committed_quarantine")
        if origin["purged_at"]:
            blockers.append("already_purged")
        if origin["file_active"] or origin["file_source"] != "quarantine":
            blockers.append("file_not_quarantined")
        quarantine_path = Path(origin["quarantine_path"] or origin["dest_path"] or "")
        destination = Path(origin["source_path"]).resolve()
        try:
            destination.relative_to(Path(house_dir).resolve())
        except ValueError:
            blockers.append("original_path_outside_house")
        if not quarantine_path.is_file() or quarantine_path.is_symlink():
            blockers.append("quarantine_bytes_missing")
        else:
            evidence = inspect_regular_file(quarantine_path)
            expected = (
                origin["destination_dev"], origin["destination_ino"],
                origin["destination_ctime_ns"], origin["destination_size"],
                origin["destination_mtime_ns"], origin["destination_sha256"],
            )
            actual = (
                evidence.dev, evidence.ino, evidence.ctime_ns, evidence.size,
                evidence.mtime_ns, evidence.sha256,
            )
            if expected != actual:
                blockers.append("quarantine_identity_stale")
        if destination.exists() or destination.is_symlink():
            blockers.append("original_destination_occupied")
        reference_file_id = reference_file_id or origin["keep_file_id"]
        reference = None
        if not reference_file_id:
            blockers.append("reference_file_required")
        else:
            try:
                reference = _file_row(conn, reference_file_id, active=True)
                _require_current_file(reference)
            except (KeyError, RuntimeError) as exc:
                blockers.append(f"invalid_reference:{exc}")
        file_row = _file_row(conn, origin["file_id"], active=False)
        payload = {
            "operation_id": int(operation_id), "file_id": origin["file_id"],
            "fingerprint_id": origin["file_fingerprint_id"],
            "reference_file_id": reference_file_id,
            "reference_fingerprint_id": reference["current_fingerprint_id"] if reference else None,
            "verdict": verdict, "destination": str(destination), "note": note.strip(),
        }
        return {
            "version": "1.3.2", "kind": "quarantine_restore", "item_count": 1,
            "operation_id": int(operation_id), "source": _public_file(file_row),
            "reference": _public_file(reference) if reference else None,
            "quarantine_path": str(quarantine_path), "destination_path": str(destination),
            "verdict": verdict, "note": note.strip(), "blocked_reasons": blockers,
            "apply_available": not blockers, "plan_sha256": _hash(payload), "readonly": True,
        }
    finally:
        conn.close()


def _restore_quarantine_file(conn, *, plan: Mapping[str, object], run_id: str) -> dict:
    actual_run = decision_store.assert_active_actual_run(conn, run_id)
    origin = _quarantine_origin(conn, int(plan["operation_id"]))
    source = Path(str(plan["quarantine_path"]))
    destination = Path(str(plan["destination_path"]))
    reference = _file_row(conn, plan["reference"]["file_id"], active=True)
    decision_store.assert_actual_run_path(actual_run, source, "temp_root")
    decision_store.assert_actual_run_path(actual_run, destination, "house_root")
    decision_store.assert_actual_run_path(actual_run, reference["canonical_path"], "house_root")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    ensure_directory_nofollow(destination.parent)
    source_evidence = inspect_regular_file(source)
    reference_evidence = inspect_regular_file(reference["canonical_path"])
    decision_store.assert_manifest_source(actual_run, source, "temp_root", source_evidence)
    decision_store.assert_manifest_source(actual_run, reference["canonical_path"], "house_root", reference_evidence)
    with decision_store.transaction(conn):
        operation_id = decision_store.create_operation(
            conn, run_id=run_id, action="user_quarantine_restore",
            source_path=str(source), dest_path=str(destination), file_id=origin["file_id"],
            keep_file_id=reference["file_id"], expected_size=source_evidence.size,
            expected_mtime_ns=source_evidence.mtime_ns,
            expected_fingerprint_id=origin["file_fingerprint_id"],
            expected_keep_fingerprint_id=reference["current_fingerprint_id"],
            parent_operation_id=origin["operation_id"], source_dev=source_evidence.dev,
            source_ino=source_evidence.ino, source_ctime_ns=source_evidence.ctime_ns,
            source_sha256=source_evidence.sha256,
        )
    def restore_guard():
        decision_store.assert_active_actual_run(conn, run_id)
        current_origin = _quarantine_origin(conn, int(plan["operation_id"]))
        if (
            current_origin["file_active"]
            or current_origin["file_source"] != "quarantine"
            or current_origin["file_fingerprint_id"]
            != plan["source"]["current_fingerprint_id"]
        ):
            raise RuntimeError("quarantine source changed before restore")
        current_reference = _file_row(conn, reference["file_id"], active=True)
        if (
            current_reference["current_fingerprint_id"]
            != reference["current_fingerprint_id"]
            or not evidence_matches(
                inspect_regular_file(reference["canonical_path"]),
                reference_evidence,
            )
        ):
            raise RuntimeError("restore reference changed before consume")

    destination_evidence = decision_store.copy_record_consume_operation(
        conn, operation_id, source, destination, source_evidence,
        guard=restore_guard,
    )
    with decision_store.transaction(conn):
        current = _quarantine_origin(conn, int(plan["operation_id"]))
        if current["file_fingerprint_id"] != plan["source"]["current_fingerprint_id"]:
            raise RuntimeError("quarantine fingerprint changed")
        conn.execute(
            """
            UPDATE files SET canonical_path = ?, source = 'house', active = 1,
                variant_id = NULL, assignment_state = 'unassigned', assignment_origin = NULL,
                protected = 0, dev = ?, ino = ?, ctime_ns = ?, size = ?, mtime_ns = ?
            WHERE file_id = ? AND active = 0
            """,
            (str(destination), destination_evidence.dev, destination_evidence.ino,
             destination_evidence.ctime_ns, destination_evidence.size,
             destination_evidence.mtime_ns, origin["file_id"]),
        )
        decision_store.supersede_open_reviews_for_file(
            conn, origin["file_id"], reason="user_selected_restore"
        )
        review_id = conn.execute(
            """
            INSERT INTO review_items(
                candidate_file_id, reference_file_id, left_fingerprint_id,
                right_fingerprint_id, classification, state, evidence_json
            ) VALUES (?, ?, ?, ?, 'user_restore_review', 'pending', ?)
            """,
            (origin["file_id"], reference["file_id"], origin["file_fingerprint_id"],
             reference["current_fingerprint_id"], json.dumps({
                 "actor": "local_user", "human_disposition": "user_selected_restore",
                 "origin_operation_id": origin["operation_id"],
                 "plan_sha256": plan["plan_sha256"],
             }, ensure_ascii=False, sort_keys=True)),
        ).lastrowid
        decision_id = decision_store.apply_decision(
            conn, review_id=review_id, candidate_file_id=origin["file_id"],
            reference_file_id=reference["file_id"], verdict=plan["verdict"],
            variant_kind="other", note=plan["note"] or None,
            allowed_active_run_id=run_id,
        )
        conn.execute(
            "UPDATE decisions SET evidence_json = ? WHERE decision_id = ?",
            (json.dumps({
                "actor": "local_user", "source": "quarantine_restore",
                "origin_operation_id": origin["operation_id"],
                "plan_sha256": plan["plan_sha256"],
            }, ensure_ascii=False, sort_keys=True), decision_id),
        )
        decision_store.transition_operation(conn, operation_id, "db_done")
    with decision_store.transaction(conn):
        decision_store.transition_operation(conn, operation_id, "committed")
    return {"operation_id": operation_id, "decision_id": decision_id,
            "source_path": str(source), "dest_path": str(destination)}


def apply_restore(
    state_db: Path, *, house_dir: Path, temp_dir: Path, index_path: Path,
    operation_id: int, reference_file_id: str | None, verdict: str, note: str,
    confirm_count: int, confirm_plan_sha256: str, progress=None,
) -> dict:
    from library_review import _refresh_review_index
    with mutation_lock_for_roots(house_dir, temp_dir, "quarantine-restore-1.3.2"):
        plan = restore_preview(
            state_db, house_dir=house_dir, operation_id=operation_id,
            reference_file_id=reference_file_id, verdict=verdict, note=note,
        )
        if not plan["apply_available"]:
            raise RuntimeError("restore plan is blocked: " + ",".join(plan["blocked_reasons"]))
        if confirm_count != 1 or confirm_plan_sha256 != plan["plan_sha256"]:
            raise RuntimeError("restore confirmation is stale")
        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(f"doctor failed before restore: {issues[0]['kind']}")
            backup = decision_store.backup_state_db(conn, _backup_path(state_db, "quarantine_restore"))
            decision_store.issue_actual_run_token(conn, str(backup), house_dir=house_dir, temp_dir=temp_dir)
        finally:
            conn.close()
        run_id, manifest_path = decision_store.prepare_actual_run(
            state_db, house_dir, temp_dir,
            manifest_paths=[plan["quarantine_path"], plan["reference"]["canonical_path"]],
        )
        try:
            conn = decision_store.connect_state_db(state_db)
            try:
                result = _restore_quarantine_file(conn, plan=plan, run_id=run_id)
                decision_store.finish_actual_run(conn, run_id, success=True)
            finally:
                conn.close()
        except BaseException as exc:
            conn = decision_store.connect_state_db(state_db)
            try:
                decision_store.finish_actual_run(conn, run_id, success=False, error=str(exc))
            finally:
                conn.close()
            raise
        if progress:
            progress(1, 1, plan["source"]["name"])
        try:
            index = _refresh_review_index(state_db=state_db, house_dir=house_dir, index_path=index_path)
        except Exception as exc:
            index = {"index_updated": False, "house_index_synced": False,
                     "warning": f"복원은 완료됐지만 index 갱신에 실패했습니다: {exc}"}
        return {**result, "run_id": run_id, "manifest_path": manifest_path,
                "backup_path": str(backup), **index}


def purge_preview(state_db: Path, *, operation_ids: Sequence[int]) -> dict:
    from dedup_recover import _validate_purge_candidate
    selected = sorted({int(value) for value in operation_ids})
    if not selected or len(selected) > MAX_PURGE_BATCH:
        raise ValueError(f"purge batch must contain 1..{MAX_PURGE_BATCH} operations")
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        placeholders = ",".join("?" for _ in selected)
        rows = conn.execute(
            f"""
            SELECT o.operation_id, o.action, o.quarantine_path, o.dest_path,
                   o.created_at, o.keep_file_id, o.expected_fingerprint_id,
                   o.expected_keep_fingerprint_id, o.expected_size,
                   o.expected_mtime_ns, o.state, o.purged_at,
                   o.destination_dev AS parent_dev, o.destination_ino AS parent_ino,
                   o.destination_ctime_ns AS parent_ctime_ns,
                   o.destination_size AS parent_size,
                   o.destination_mtime_ns AS parent_mtime_ns,
                   o.destination_sha256 AS parent_sha256,
                   f.file_id, f.protected, f.canonical_path AS file_path,
                   f.active AS file_active, f.source AS file_source,
                   keep.canonical_path AS keep_path
            FROM operations AS o JOIN files AS f ON f.file_id = o.file_id
            LEFT JOIN files AS keep ON keep.file_id = o.keep_file_id
            WHERE o.operation_id IN ({placeholders})
            ORDER BY o.operation_id
            """,
            selected,
        ).fetchall()
        blockers = []
        if len(rows) != len(selected):
            blockers.append("operation_not_found")
        items, total_size = [], 0
        for row in rows:
            path = Path(row["quarantine_path"] or row["dest_path"] or "")
            item_blockers = []
            if row["action"] not in QUARANTINE_ACTIONS or row["state"] != "committed":
                item_blockers.append("not_committed_quarantine")
            if row["purged_at"]:
                item_blockers.append("already_purged")
            if row["file_active"] or row["file_source"] != "quarantine":
                item_blockers.append("file_not_quarantined")
            if not path.is_file() or path.is_symlink():
                item_blockers.append("quarantine_bytes_missing")
                size = 0
            else:
                evidence = inspect_regular_file(path)
                expected = (row["parent_dev"], row["parent_ino"], row["parent_ctime_ns"],
                            row["parent_size"], row["parent_mtime_ns"], row["parent_sha256"])
                actual = (evidence.dev, evidence.ino, evidence.ctime_ns, evidence.size,
                          evidence.mtime_ns, evidence.sha256)
                if expected != actual:
                    item_blockers.append("quarantine_identity_stale")
                size = evidence.size
                if not item_blockers:
                    try:
                        _validate_purge_candidate(conn, row)
                    except RuntimeError as exc:
                        item_blockers.append(f"safety_revalidation_failed:{exc}")
            blockers.extend(f"{row['operation_id']}:{value}" for value in item_blockers)
            total_size += size
            items.append({"operation_id": row["operation_id"], "file_id": row["file_id"],
                          "name": path.name, "path": str(path), "size": size,
                          "keep_path": row["keep_path"],
                          "age_days": max(0, int((datetime.now().timestamp() - path.stat().st_mtime) // 86400)) if path.is_file() else None,
                          "blocked_reasons": item_blockers})
        payload = {"operation_ids": selected, "items": [(item["operation_id"], item["size"]) for item in items]}
        return {"version": "1.3.2", "kind": "quarantine_purge", "item_count": len(items),
                "total_size": total_size, "items": items,
                "blocked_reasons": blockers, "apply_available": not blockers,
                "plan_sha256": _hash(payload), "irreversible": True, "readonly": True}
    finally:
        conn.close()


def apply_purge(
    state_db: Path, *, house_dir: Path, temp_dir: Path, operation_ids: Sequence[int],
    confirm_count: int, confirm_plan_sha256: str, progress=None,
) -> dict:
    with mutation_lock_for_roots(house_dir, temp_dir, "quarantine-purge-1.3.2"):
        plan = purge_preview(state_db, operation_ids=operation_ids)
        if not plan["apply_available"]:
            raise RuntimeError("purge plan is blocked: " + ",".join(plan["blocked_reasons"]))
        if confirm_count != plan["item_count"] or confirm_plan_sha256 != plan["plan_sha256"]:
            raise RuntimeError("purge confirmation is stale or incomplete")
        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(f"doctor failed before purge: {issues[0]['kind']}")
            backup = decision_store.backup_state_db(conn, _backup_path(state_db, "quarantine_purge"))
            decision_store.issue_actual_run_token(conn, str(backup), house_dir=house_dir, temp_dir=temp_dir)
        finally:
            conn.close()
        manifest_paths = [item["path"] for item in plan["items"]]
        manifest_paths.extend(
            item["keep_path"] for item in plan["items"]
            if item.get("keep_path") and item["keep_path"] not in manifest_paths
        )
        run_id, manifest_path = decision_store.prepare_actual_run(
            state_db, house_dir, temp_dir, manifest_paths=manifest_paths
        )
        purged = []
        try:
            current_plan = purge_preview(state_db, operation_ids=operation_ids)
            if not current_plan["apply_available"] or current_plan["plan_sha256"] != plan["plan_sha256"]:
                raise RuntimeError("purge safety evidence changed after activation")
            conn = decision_store.connect_state_db(state_db)
            try:
                with mutation_lock(conn, f"quarantine_purge:{run_id}", run_id=run_id):
                    actual_run = decision_store.assert_active_actual_run(conn, run_id)
                    for index, item in enumerate(plan["items"], start=1):
                        parent = _quarantine_origin(conn, item["operation_id"])
                        path = Path(item["path"])
                        evidence = inspect_regular_file(path)
                        decision_store.assert_actual_run_path(actual_run, path, "temp_root")
                        decision_store.assert_manifest_source(actual_run, path, "temp_root", evidence)
                        if item.get("keep_path"):
                            keep_evidence = inspect_regular_file(item["keep_path"])
                            decision_store.assert_actual_run_path(actual_run, item["keep_path"], "house_root")
                            decision_store.assert_manifest_source(
                                actual_run, item["keep_path"], "house_root", keep_evidence
                            )
                        with decision_store.transaction(conn):
                            purge_operation_id = decision_store.create_operation(
                                conn, run_id=run_id, action="quarantine_purge",
                                source_path=str(path), file_id=parent["file_id"],
                                keep_file_id=parent["keep_file_id"], expected_size=evidence.size,
                                expected_mtime_ns=evidence.mtime_ns,
                                expected_fingerprint_id=parent["expected_fingerprint_id"],
                                expected_keep_fingerprint_id=parent["expected_keep_fingerprint_id"],
                                parent_operation_id=parent["operation_id"], source_dev=evidence.dev,
                                source_ino=evidence.ino, source_ctime_ns=evidence.ctime_ns,
                                source_sha256=evidence.sha256,
                            )
                        unlink_owned(path, expected=evidence)
                        with decision_store.transaction(conn):
                            decision_store.transition_operation(conn, purge_operation_id, "fs_done")
                        with decision_store.transaction(conn):
                            conn.execute("UPDATE operations SET purged_at = CURRENT_TIMESTAMP WHERE operation_id = ?", (parent["operation_id"],))
                            decision_store.transition_operation(conn, purge_operation_id, "db_done")
                        with decision_store.transaction(conn):
                            decision_store.transition_operation(conn, purge_operation_id, "committed")
                        purged.append({"operation_id": parent["operation_id"], "path": str(path), "size": item["size"]})
                        if progress:
                            progress(index, plan["item_count"], item["name"])
                decision_store.finish_actual_run(conn, run_id, success=True)
            finally:
                conn.close()
        except BaseException as exc:
            conn = decision_store.connect_state_db(state_db)
            try:
                decision_store.finish_actual_run(conn, run_id, success=False, error=str(exc))
            finally:
                conn.close()
            raise
        return {"run_id": run_id, "manifest_path": manifest_path, "backup_path": str(backup),
                "purged": purged, "purged_count": len(purged), "released_bytes": sum(item["size"] for item in purged)}
