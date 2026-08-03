"""SQLite source of truth for dedup decisions and immutable fingerprints.

The store is deliberately fail-closed.  Creating a valid schema does not enable
library mutations; the integration phases must finish and explicitly open that
gate after doctor/recovery checks exist.
"""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import stat
import unicodedata
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Optional, Tuple

from file_analysis_repository import (
    build_effective_file_analysis,
    build_file_analysis,
    file_analysis_snapshot_is_current,
    file_analysis_sync_status,
    migrate_catalog_title_keys,
    prune_file_analysis_projection,
    reconcile_file_metadata,
    resolve_current_file_analysis,
    sync_active_file_analysis,
    sync_contextual_bare_volume_metadata as _sync_contextual_bare_volume_metadata,
    upsert_file_analysis,
)
from state_repository import (
    DEFAULT_BUSY_TIMEOUT_MS,
    _connection_main_path,
    canonicalize_path,
    canonicalize_real_path,
    connect_state_db,
    connect_state_db_readonly,
    initialize_state_db as _initialize_state_db,
    retire_legacy_title_requeue_path_owners,
    retired_canonical_path,
    transaction,
    validate_schema,
)
from state_schema import (
    ASSIGNMENT_STATES,
    CATALOG_SCHEMA_SQL,
    FILE_ANALYSIS_SCHEMA_SQL,
    FINAL_VERDICTS,
    OPERATION_STATES,
    REQUIRED_TABLES,
    REQUIRED_VIEWS,
    REVIEW_STATES,
    SCHEMA_SQL,
    SCHEMA_VERSION,
)
from volume_policy import (
    canonical_rational,
    canonical_symbol,
    coordinate_fields_from_name,
    coordinate_sort_token,
    coordinates_compatible,
)


def initialize_state_db(
    path: os.PathLike | str,
    *,
    migrate: bool = False,
    check_integrity: bool = True,
) -> sqlite3.Connection:
    """Compatibility facade preserving decision_store validation hooks."""
    return _initialize_state_db(
        path,
        migrate=migrate,
        check_integrity=check_integrity,
        _validate_schema=validate_schema,
    )


def sync_contextual_bare_volume_metadata(conn, **kwargs):
    """Compatibility facade preserving current analysis monkeypatch hooks."""
    return _sync_contextual_bare_volume_metadata(
        conn,
        _build_file_analysis=build_file_analysis,
        _upsert_file_analysis=upsert_file_analysis,
        **kwargs,
    )


# Do not narrow legacy ``from decision_store import *`` behavior with
# ``__all__``.  Existing callers may import the actual-run, journal, recovery,
# and Doctor API that intentionally remains implemented in this facade.
_COMPATIBILITY_FACADE_EXPORTS = (
    ASSIGNMENT_STATES,
    CATALOG_SCHEMA_SQL,
    FILE_ANALYSIS_SCHEMA_SQL,
    REQUIRED_TABLES,
    REQUIRED_VIEWS,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    build_effective_file_analysis,
    file_analysis_snapshot_is_current,
    file_analysis_sync_status,
    migrate_catalog_title_keys,
    prune_file_analysis_projection,
    reconcile_file_metadata,
    resolve_current_file_analysis,
    sync_active_file_analysis,
    connect_state_db_readonly,
    retire_legacy_title_requeue_path_owners,
    canonical_rational,
    canonical_symbol,
    coordinate_sort_token,
    coordinates_compatible,
)


ACTUAL_RUN_RECOVERY_STATES = frozenset({"cancelled", "failed", "finished"})
_ACTUAL_RUN_TRANSITIONS = {
    "approved": {"active", "failed", "cancelled"},
    "active": {"finished", "failed", "cancelled"},
    "finished": set(),
    "failed": set(),
    "cancelled": set(),
}

_PREFLIGHT_RECEIPT_SECRET = object()
_STATE_INTEGRITY_RECEIPT_SECRET = object()
_PREFLIGHT_VALIDATION_RECEIPTS = {}
_PREFLIGHT_VALIDATION_RECEIPT_LIMIT = 32


class _PreflightValidationReceipt:
    __slots__ = ("run_id", "state_db", "house_root", "temp_root", "secret")

    def __init__(self, run_id, state_db, house_root, temp_root):
        self.run_id = run_id
        self.state_db = state_db
        self.house_root = house_root
        self.temp_root = temp_root
        self.secret = _PREFLIGHT_RECEIPT_SECRET


class _StateIntegrityReceipt:
    __slots__ = ("run_id", "state_db", "storage_identity", "secret")

    def __init__(self, run_id, state_db, storage_identity):
        self.run_id = run_id
        self.state_db = state_db
        self.storage_identity = storage_identity
        self.secret = _STATE_INTEGRITY_RECEIPT_SECRET


_ALLOWED_OPERATION_TRANSITIONS = {
    "planned": {"fs_done", "rolled_back", "stale", "failed"},
    "fs_done": {"db_done", "rolled_back", "failed"},
    "db_done": {"committed", "rolled_back", "stale", "failed"},
    "committed": set(),
    "rolled_back": set(),
    "stale": set(),
    "failed": set(),
}


def _state_db_storage_identity(path: os.PathLike | str) -> tuple:
    db_path = Path(path).expanduser().resolve()

    def identity(candidate: Path):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"state DB storage path is not regular: {candidate}")
        return (
            info.st_dev,
            info.st_ino,
            info.st_ctime_ns,
            info.st_size,
            info.st_mtime_ns,
        )

    return tuple(
        (suffix, identity(Path(f"{db_path}{suffix}")))
        for suffix in ("", "-wal", "-journal")
    )


def issue_state_integrity_receipt(path, *, run_id):
    """Bind a just-completed full integrity check to unchanged DB storage."""
    state_db = str(Path(path).expanduser().resolve())
    return _StateIntegrityReceipt(
        run_id, state_db, _state_db_storage_identity(state_db)
    )


def state_integrity_receipt_is_current(receipt, path, *, run_id) -> bool:
    """Return whether a same-process full-check receipt still covers this DB."""
    state_db = str(Path(path).expanduser().resolve())
    return bool(
        isinstance(receipt, _StateIntegrityReceipt)
        and receipt.secret is _STATE_INTEGRITY_RECEIPT_SECRET
        and receipt.run_id == run_id
        and receipt.state_db == state_db
        and receipt.storage_identity == _state_db_storage_identity(state_db)
    )


def _validated_receipt_run_id(
    receipt, path, *, house_dir=None, temp_dir=None
) -> str | None:
    if receipt is None:
        return None
    if (
        not isinstance(receipt, _PreflightValidationReceipt)
        or receipt.secret is not _PREFLIGHT_RECEIPT_SECRET
    ):
        raise RuntimeError("invalid preflight validation receipt")
    if receipt.state_db != str(Path(path).expanduser().resolve()):
        raise RuntimeError("preflight receipt state DB does not match")
    if (
        house_dir is not None
        and receipt.house_root != canonicalize_real_path(house_dir)
    ):
        raise RuntimeError("preflight receipt house root does not match")
    if (
        temp_dir is not None
        and receipt.temp_root != canonicalize_real_path(temp_dir)
    ):
        raise RuntimeError("preflight receipt temp root does not match")
    return receipt.run_id


def verify_state_db_ready(
    path: os.PathLike | str, *, preflight_receipt=None
) -> Tuple[bool, str]:
    """Read-only readiness check used by the Phase 0 mutation gate."""
    db_path = Path(path)
    if not db_path.is_file():
        return False, "state DB does not exist"

    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    try:
        prevalidated_run_id = _validated_receipt_run_id(
            preflight_receipt, db_path
        )
        conn = sqlite3.connect(uri, uri=True, timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
            # Readiness ends with the full Doctor, which performs the integrity
            # check.  Keep this first pass structural only so an approved run
            # does not execute the same full SQLite integrity scan twice.
            validate_schema(conn, check_integrity=False)
            enabled = conn.execute(
                "SELECT value FROM settings WHERE key = 'actual_mutation_enabled'"
            ).fetchone()
            if enabled is None or enabled[0] != "1":
                return False, "actual mutation gate is disabled"
            token = conn.execute(
                "SELECT value FROM settings WHERE key = 'approved_run_id'"
            ).fetchone()
            if token is None or not token[0]:
                return False, "approved one-time run token is missing"
            if prevalidated_run_id is not None and token[0] != prevalidated_run_id:
                return False, "prevalidated actual run token does not match"
            run = conn.execute(
                "SELECT * FROM actual_runs WHERE run_id = ? AND state = 'approved'",
                (token[0],),
            ).fetchone()
            if run is None:
                return False, "approved actual run record is missing"
            active = conn.execute(
                "SELECT COUNT(*) FROM actual_runs WHERE state = 'active'"
            ).fetchone()[0]
            if active:
                return False, f"active actual runs: {active}"
            try:
                _verify_backup_evidence(run["backup_path"], run["backup_sha256"])
            except RuntimeError as exc:
                return False, str(exc)
            unfinished = conn.execute(
                "SELECT COUNT(*) FROM operations WHERE state IN ('planned', 'fs_done', 'db_done')"
            ).fetchone()[0]
            if unfinished:
                return False, f"unfinished operations: {unfinished}"
            # The one-button entry point already completed a full Doctor under
            # the same roots mutation lock immediately before issuing this
            # exact token. Generic callers still receive the full readiness
            # Doctor; only the matching run-scoped receipt avoids duplication.
            if prevalidated_run_id is None:
                issues = doctor_issues(conn)
                if issues:
                    return False, f"doctor issues: {len(issues)} ({issues[0]['kind']})"
            return True, "ok"
        finally:
            conn.close()
    except (sqlite3.Error, RuntimeError) as exc:
        return False, str(exc)


def sha256_file(path: os.PathLike | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_BACKUP_EVIDENCE_CACHE = {}
_BACKUP_EVIDENCE_CACHE_LIMIT = 32


def _clear_backup_evidence_cache_for_tests() -> None:
    _BACKUP_EVIDENCE_CACHE.clear()


def _verify_backup_evidence(
    backup_path: str, expected_sha256: str | None = None
) -> str:
    path = Path(backup_path)
    try:
        path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"approved backup does not exist: {backup_path}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"approved backup cannot be read: {backup_path}: {exc}"
        ) from exc
    identity = _regular_file_identity(path)
    cache_key = str(path.resolve())
    cached = _BACKUP_EVIDENCE_CACHE.get(cache_key)
    if (
        cached is not None
        and cached["identity"] == identity
        and (expected_sha256 is None or cached["sha256"] == expected_sha256)
    ):
        return cached["sha256"]
    try:
        actual_sha256 = sha256_file(path)
    except OSError as exc:
        raise RuntimeError(f"approved backup cannot be read: {backup_path}: {exc}") from exc
    if expected_sha256 is not None and actual_sha256 != expected_sha256:
        raise RuntimeError(f"approved backup SHA-256 mismatch: {backup_path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
    try:
        backup = sqlite3.connect(uri, uri=True)
        try:
            integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            backup.close()
    except sqlite3.Error as exc:
        raise RuntimeError(f"approved backup cannot be opened: {backup_path}: {exc}") from exc
    if integrity != "ok":
        raise RuntimeError(f"approved backup integrity_check failed: {integrity}")
    if len(_BACKUP_EVIDENCE_CACHE) >= _BACKUP_EVIDENCE_CACHE_LIMIT:
        _BACKUP_EVIDENCE_CACHE.pop(next(iter(_BACKUP_EVIDENCE_CACHE)))
    _BACKUP_EVIDENCE_CACHE[cache_key] = {
        "identity": identity,
        "sha256": actual_sha256,
    }
    return actual_sha256


def _regular_file_identity(path: os.PathLike | str) -> tuple:
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError(f"run evidence file is missing or unreadable: {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"run evidence path is not a regular file: {path}")
    return info.st_dev, info.st_ino, info.st_ctime_ns, info.st_size, info.st_mtime_ns


def _stored_identity(row, prefix):
    return tuple(
        row[f"{prefix}_{field}"]
        for field in ("dev", "ino", "ctime_ns", "size", "mtime_ns")
    )


def issue_actual_run_token(
    conn: sqlite3.Connection,
    backup_path: str,
    *,
    house_dir: os.PathLike | str,
    temp_dir: os.PathLike | str,
) -> str:
    state_db_path = _connection_main_path(conn)
    _PREFLIGHT_VALIDATION_RECEIPTS.pop(state_db_path, None)
    backup_path = canonicalize_path(backup_path)
    backup_sha256 = _verify_backup_evidence(backup_path)
    run_id = f"actual-{uuid.uuid4()}"
    with transaction(conn):
        # A maintenance process may cold-archive an otherwise unreferenced
        # backup between the first (potentially expensive) verification and
        # this writer transaction.  Revalidate while holding SQLite's writer
        # reservation so the archive path can make its final reference check
        # under the same serialization boundary.  The evidence cache keeps
        # this to an identity check when the file is unchanged.
        _verify_backup_evidence(backup_path, backup_sha256)
        if conn.execute(
            "SELECT 1 FROM actual_runs WHERE state IN ('approved', 'active') LIMIT 1"
        ).fetchone():
            raise RuntimeError("an approved or active actual run already exists")
        conn.execute(
            """
            INSERT INTO actual_runs(
                run_id, state, house_root, temp_root, backup_path, backup_sha256
            ) VALUES (?, 'approved', ?, ?, ?, ?)
            """,
            (
                run_id, canonicalize_real_path(house_dir), canonicalize_real_path(temp_dir),
                backup_path, backup_sha256,
            ),
        )
        for key, value in (
            ("actual_mutation_enabled", "1"),
            ("approved_run_id", run_id),
            ("approved_backup", str(backup_path)),
        ):
            conn.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )
    return run_id


def issue_prevalidated_actual_run_token(
    conn: sqlite3.Connection,
    backup_path: str,
    *,
    house_dir: os.PathLike | str,
    temp_dir: os.PathLike | str,
):
    """Run the full preflight Doctor and issue an opaque same-process receipt."""
    issues = doctor_issues(conn)
    if issues:
        first = issues[0]
        raise RuntimeError(
            "doctor failed before Folderling; run disable/recover/doctor manually: "
            f"{len(issues)} issue(s), first={first['kind']}"
        )
    run_id = issue_actual_run_token(
        conn,
        backup_path,
        house_dir=house_dir,
        temp_dir=temp_dir,
    )
    receipt = _PreflightValidationReceipt(
        run_id,
        _connection_main_path(conn),
        canonicalize_real_path(house_dir),
        canonicalize_real_path(temp_dir),
    )
    if (
        receipt.state_db not in _PREFLIGHT_VALIDATION_RECEIPTS
        and len(_PREFLIGHT_VALIDATION_RECEIPTS)
        >= _PREFLIGHT_VALIDATION_RECEIPT_LIMIT
    ):
        _PREFLIGHT_VALIDATION_RECEIPTS.pop(
            next(iter(_PREFLIGHT_VALIDATION_RECEIPTS))
        )
    _PREFLIGHT_VALIDATION_RECEIPTS[receipt.state_db] = receipt
    return run_id, receipt


def consume_preflight_validation_receipt(path, house_dir, temp_dir):
    """Return the opaque receipt issued for this same-process actual run."""
    state_db = str(Path(path).expanduser().resolve())
    receipt = _PREFLIGHT_VALIDATION_RECEIPTS.pop(state_db, None)
    if receipt is None:
        return None
    _validated_receipt_run_id(
        receipt,
        state_db,
        house_dir=house_dir,
        temp_dir=temp_dir,
    )
    return receipt


def disable_actual_run(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE settings SET value = '0', updated_at = CURRENT_TIMESTAMP "
            "WHERE key = 'actual_mutation_enabled'"
        )
        conn.execute("DELETE FROM settings WHERE key = 'approved_run_id'")
        run_ids = [row[0] for row in conn.execute(
            "SELECT run_id FROM actual_runs WHERE state IN ('approved', 'active')"
        )]
        for run_id in run_ids:
            transition_actual_run(
                conn, run_id, "cancelled", error="disabled by operator"
            )


def _manifest_relative_path_key(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError("actual run manifest contains an invalid relative path")
    return unicodedata.normalize("NFC", value)


def _manifest_lookup_from_records(records):
    lookup = {}
    for record in records:
        source = record.get("source")
        if source not in {"house", "temp"}:
            raise RuntimeError("actual run manifest contains an invalid source")
        key = (source, _manifest_relative_path_key(record.get("rel_path")))
        if key in lookup:
            raise RuntimeError(
                f"actual run manifest normalized path collision: {source}/{key[1]}"
            )
        lookup[key] = record
    return lookup


def prepare_actual_run(
    path, house_dir, temp_dir, *, manifest_paths=None,
    preflight_receipt=None,
):
    """Consume one approval before any filesystem mutation and record its manifest."""
    _validated_receipt_run_id(
        preflight_receipt,
        path,
        house_dir=house_dir,
        temp_dir=temp_dir,
    )
    if preflight_receipt is None:
        ready, reason = verify_state_db_ready(path)
    else:
        ready, reason = verify_state_db_ready(
            path, preflight_receipt=preflight_receipt
        )
    if not ready:
        raise RuntimeError(reason)
    for label, root in (("house", house_dir), ("temp", temp_dir)):
        symlink = _symlink_component(root)
        if symlink is not None:
            raise RuntimeError(f"approved {label} root contains symlink component: {symlink}")
    expected_house = canonicalize_real_path(house_dir)
    expected_temp = canonicalize_real_path(temp_dir)
    claim_id = str(uuid.uuid4())
    conn = connect_state_db(path)
    try:
        with transaction(conn):
            row = conn.execute(
                """
                SELECT ar.* FROM actual_runs AS ar
                JOIN settings AS s ON s.key = 'approved_run_id' AND s.value = ar.run_id
                WHERE ar.state = 'approved' AND ar.activation_claim IS NULL
                """
            ).fetchone()
            if row is None:
                raise RuntimeError("approved one-time run record is missing or already claimed")
            if row["house_root"] != expected_house or row["temp_root"] != expected_temp:
                raise RuntimeError("approved actual run roots do not match this invocation")
            _verify_backup_evidence(row["backup_path"], row["backup_sha256"])
            run_id = row["run_id"]
            claimed = conn.execute(
                """
                UPDATE actual_runs SET activation_claim = ?
                WHERE run_id = ? AND state = 'approved' AND activation_claim IS NULL
                """,
                (claim_id, run_id),
            )
            if claimed.rowcount != 1:
                raise RuntimeError("actual run approval claim lost")
            conn.execute(
                "UPDATE settings SET value = '0', updated_at = CURRENT_TIMESTAMP "
                "WHERE key = 'actual_mutation_enabled'"
            )
            conn.execute("DELETE FROM settings WHERE key = 'approved_run_id'")
    finally:
        conn.close()

    manifest_path = None
    manifest_created = False
    manifest_created_evidence = None
    try:
        records = []
        manifest_keys = set()
        roots = (("house", house_dir), ("temp", temp_dir))
        selected = None
        if manifest_paths is not None:
            selected = []
            for value in manifest_paths:
                item = Path(value).expanduser().resolve()
                matched = None
                for label, root_value in roots:
                    root = Path(root_value).resolve()
                    try:
                        item.relative_to(root)
                    except ValueError:
                        continue
                    matched = (label, root, item)
                    break
                if matched is None:
                    raise RuntimeError(f"targeted manifest path is outside approved roots: {item}")
                selected.append(matched)
        for label, root_value in roots:
            root = Path(root_value).resolve()
            if not root.exists():
                continue
            items = (
                sorted(item for item_label, _, item in selected if item_label == label)
                if selected is not None else sorted(root.rglob("*"))
            )
            for item in items:
                if item.is_file() and not item.is_symlink():
                    item_stat = item.stat()
                    raw_rel_path = item.relative_to(root).as_posix()
                    rel_path = _manifest_relative_path_key(raw_rel_path)
                    manifest_key = (label, rel_path)
                    if manifest_key in manifest_keys:
                        raise RuntimeError(
                            "actual run manifest normalized path collision: "
                            f"{label}/{rel_path}"
                        )
                    manifest_keys.add(manifest_key)
                    record = {
                        "source": label,
                        "rel_path": rel_path,
                        "dev": item_stat.st_dev,
                        "ino": item_stat.st_ino,
                        "ctime_ns": item_stat.st_ctime_ns,
                        "size": item_stat.st_size,
                        "mtime_ns": item_stat.st_mtime_ns,
                    }
                    if raw_rel_path != rel_path:
                        record["raw_rel_path"] = raw_rel_path
                    records.append(record)
        manifest_dir = Path(path).resolve().parent / "manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / f"{run_id}-{claim_id}.json"
        with open(manifest_path, "x", encoding="utf-8") as manifest:
            json.dump(
                {"run_id": run_id, "files": records}, manifest,
                ensure_ascii=False, indent=2,
            )
        manifest_created = True
        from mutation_io import inspect_regular_file
        manifest_created_evidence = inspect_regular_file(manifest_path)
        manifest_sha256 = manifest_created_evidence.sha256

        conn = connect_state_db(path)
        try:
            with transaction(conn):
                current = conn.execute(
                    "SELECT * FROM actual_runs WHERE run_id = ? AND activation_claim = ?",
                    (run_id, claim_id),
                ).fetchone()
                if current is None or current["state"] != "approved":
                    raise RuntimeError("actual run approval was already consumed")
                if current["house_root"] != expected_house or current["temp_root"] != expected_temp:
                    raise RuntimeError("approved actual run roots changed")
                _verify_backup_evidence(current["backup_path"], current["backup_sha256"])
                backup_identity = _regular_file_identity(current["backup_path"])
                manifest_identity = _regular_file_identity(manifest_path)
                transition_actual_run(conn, run_id, "active")
                conn.execute(
                    """
                    UPDATE actual_runs SET activated_at = CURRENT_TIMESTAMP,
                        manifest_path = ?, manifest_sha256 = ?,
                        backup_dev = ?, backup_ino = ?, backup_ctime_ns = ?,
                        backup_size = ?, backup_mtime_ns = ?,
                        manifest_dev = ?, manifest_ino = ?, manifest_ctime_ns = ?,
                        manifest_size = ?, manifest_mtime_ns = ?
                    WHERE run_id = ? AND activation_claim = ?
                    """,
                    (
                        str(manifest_path), manifest_sha256,
                        *backup_identity, *manifest_identity, run_id, claim_id,
                    ),
                )
        finally:
            conn.close()
    except Exception as exc:
        state_record_error = None
        try:
            conn = connect_state_db(path)
            try:
                with transaction(conn):
                    current = conn.execute(
                        "SELECT state, activation_claim FROM actual_runs WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()
                    if (
                        current
                        and current["activation_claim"] == claim_id
                        and current["state"] in {"approved", "active"}
                    ):
                        transition_actual_run(
                            conn, run_id, "failed", error=f"activation failed: {exc}"
                        )
            finally:
                conn.close()
        except Exception as record_exc:
            # The activation exception remains primary; record failures are appended
            # below together with any manifest cleanup failure.
            state_record_error = record_exc

        cleanup_error = None
        try:
            if manifest_created and manifest_path is not None:
                from mutation_io import unlink_owned
                if manifest_created_evidence is None:
                    raise OSError("activation manifest evidence unavailable for cleanup")
                unlink_owned(manifest_path, expected=manifest_created_evidence)
        except FileNotFoundError:
            pass
        except OSError as unlink_exc:
            cleanup_error = unlink_exc

        if state_record_error is not None or cleanup_error is not None:
            details = []
            if state_record_error is not None:
                details.append(f"activation state record failed: {state_record_error}")
            if cleanup_error is not None:
                details.append(f"activation manifest cleanup failed: {cleanup_error}")
            conn = connect_state_db(path)
            try:
                with transaction(conn):
                    current = conn.execute(
                        "SELECT error FROM actual_runs WHERE run_id = ?", (run_id,)
                    ).fetchone()
                    prior = current["error"] if current else None
                    combined = "; ".join(filter(None, [prior, *details]))
                    conn.execute(
                        "UPDATE actual_runs SET error = ?, manifest_path = ? WHERE run_id = ?",
                        (
                            combined,
                            str(manifest_path) if cleanup_error is not None else None,
                            run_id,
                        ),
                    )
            finally:
                conn.close()
        raise
    return run_id, str(manifest_path)


def assert_active_actual_run(
    conn, run_id, *, house_dir=None, temp_dir=None, full_evidence=False
):
    if not run_id:
        raise RuntimeError("active actual run ID is required")
    row = conn.execute(
        "SELECT * FROM actual_runs WHERE run_id = ? AND state = 'active'", (run_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("actual run is not active")
    if house_dir is not None and row["house_root"] != canonicalize_real_path(house_dir):
        raise RuntimeError("actual run house root mismatch")
    if temp_dir is not None and row["temp_root"] != canonicalize_real_path(temp_dir):
        raise RuntimeError("actual run temp root mismatch")
    if not row["manifest_path"] or not row["manifest_sha256"]:
        raise RuntimeError("actual run manifest evidence is missing")
    try:
        if _regular_file_identity(row["backup_path"]) != _stored_identity(row, "backup"):
            raise RuntimeError("actual run backup identity is stale")
        if _regular_file_identity(row["manifest_path"]) != _stored_identity(row, "manifest"):
            raise RuntimeError("actual run manifest identity is stale")
        if full_evidence:
            _verify_backup_evidence(row["backup_path"], row["backup_sha256"])
            if sha256_file(row["manifest_path"]) != row["manifest_sha256"]:
                raise RuntimeError("actual run manifest SHA-256 mismatch")
    except RuntimeError as exc:
        with transaction(conn):
            transition_actual_run(
                conn, run_id, "failed", error=f"run evidence failed: {exc}"
            )
        raise
    return row


def assert_actual_run_path(run, path, root_field):
    symlink = _symlink_component(path)
    if symlink is not None:
        raise RuntimeError(f"actual run path contains symlink component: {symlink}")
    candidate = canonicalize_real_path(path)
    root = run[root_field]
    try:
        inside = os.path.commonpath((candidate, root)) == root
    except ValueError:
        inside = False
    if not inside:
        raise RuntimeError(f"actual run {root_field} does not authorize path: {candidate}")


def assert_actual_run_path_any(run, path, root_fields):
    errors = []
    for root_field in root_fields:
        try:
            assert_actual_run_path(run, path, root_field)
            return
        except RuntimeError as exc:
            errors.append(str(exc))
    raise RuntimeError("actual run does not authorize recovery path: " + " | ".join(errors))


_MANIFEST_LOOKUP_CACHE = {}


def _actual_run_manifest_lookup(run):
    from mutation_io import evidence_matches, FileEvidence, read_json_with_evidence
    identity = _regular_file_identity(run["manifest_path"])
    key = (run["manifest_path"], identity, run["manifest_sha256"])
    cached = _MANIFEST_LOOKUP_CACHE.get(key)
    if cached is not None:
        return cached
    evidence, payload = read_json_with_evidence(run["manifest_path"])
    expected = FileEvidence(
        run["manifest_dev"], run["manifest_ino"], run["manifest_ctime_ns"],
        run["manifest_size"], run["manifest_mtime_ns"], run["manifest_sha256"],
    )
    if not evidence_matches(evidence, expected):
        raise RuntimeError("actual run manifest identity or SHA-256 mismatch")
    if payload.get("run_id") != run["run_id"]:
        raise RuntimeError("actual run manifest run_id mismatch")
    lookup = _manifest_lookup_from_records(payload.get("files", []))
    _MANIFEST_LOOKUP_CACHE.clear()
    _MANIFEST_LOOKUP_CACHE[key] = lookup
    return lookup


def assert_manifest_source(run, path, root_field, evidence) -> None:
    source = "house" if root_field == "house_root" else "temp"
    root = Path(run[root_field])
    candidate = Path(canonicalize_real_path(path))
    try:
        rel_path = _manifest_relative_path_key(
            candidate.relative_to(root).as_posix()
        )
    except ValueError as exc:
        raise RuntimeError(f"manifest source is outside {root_field}: {candidate}") from exc
    record = _actual_run_manifest_lookup(run).get((source, rel_path))
    if record is None:
        raise RuntimeError(f"actual run manifest does not authorize source: {candidate}")
    expected = (
        record.get("dev"), record.get("ino"), record.get("ctime_ns"),
        record.get("size"), record.get("mtime_ns"),
    )
    current = (
        evidence.dev, evidence.ino, evidence.ctime_ns, evidence.size, evidence.mtime_ns,
    )
    if expected != current:
        raise RuntimeError(f"actual run manifest source identity is stale: {candidate}")


def assert_manifest_or_same_run_house_source(
    conn: sqlite3.Connection, run, path, evidence
) -> None:
    """Authorize an original house file or an exact house result of this run.

    Folderling captures its manifest before review actions and temp intake.  Its
    final series pass must therefore be able to consume a file that a committed
    operation in the *same* still-active run just placed in house.  The fallback
    is deliberately narrow: exact destination path, exact durable destination
    identity/SHA, a small allowlist of house-producing actions, and the same
    run_id are all required.  A stale original manifest entry is never accepted
    merely because the pathname still exists.
    """

    try:
        assert_manifest_source(run, path, "house_root", evidence)
        return
    except RuntimeError as manifest_error:
        if not str(manifest_error).startswith(
            "actual run manifest does not authorize source:"
        ):
            raise
        candidate = str(Path(canonicalize_path(path)))
        rows = conn.execute(
            """
            SELECT * FROM operations
            WHERE run_id = ? AND state = 'committed' AND dest_path = ?
              AND action IN ('house_ingest', 'user_queue_accept', 'user_queue_restore')
            ORDER BY operation_id DESC
            """,
            (run["run_id"], candidate),
        ).fetchall()
        from mutation_io import evidence_matches

        for row in rows:
            expected = _operation_evidence(row, "destination")
            if expected is not None and evidence_matches(evidence, expected):
                return
        raise manifest_error


def _actual_run_for_operation(conn, run_id):
    row = conn.execute("SELECT * FROM actual_runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise RuntimeError("operation has no persistent actual run authorization")
    if row["state"] not in ACTUAL_RUN_RECOVERY_STATES:
        raise RuntimeError(f"operation actual run state cannot authorize recovery: {row['state']}")
    return row


def transition_actual_run(conn, run_id, new_state, *, error=None):
    row = conn.execute(
        "SELECT state FROM actual_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        raise KeyError(run_id)
    current = row["state"]
    if new_state not in _ACTUAL_RUN_TRANSITIONS.get(current, set()):
        if current == new_state:
            return
        raise RuntimeError(f"invalid actual run transition: {current} -> {new_state}")
    cursor = conn.execute(
        """
        UPDATE actual_runs SET state = ?, error = ?,
            finished_at = CASE WHEN ? IN ('finished', 'failed', 'cancelled')
                               THEN CURRENT_TIMESTAMP ELSE finished_at END
        WHERE run_id = ? AND state = ?
        """,
        (new_state, error, new_state, run_id, current),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"actual run transition lost: {current} -> {new_state}")


def finish_actual_run(conn, run_id, *, success: bool, error: Optional[str] = None) -> None:
    target = "finished" if success else "failed"
    with transaction(conn):
        current = conn.execute(
            "SELECT state FROM actual_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if current is not None and current["state"] == "cancelled":
            return
        transition_actual_run(conn, run_id, target, error=error)


STATE_BACKUP_RETENTION = 10


def _is_state_backup(path: Path) -> bool:
    """Return whether *path* is a managed SQLite backup file."""
    return path.suffix == ".sqlite3"


def protected_state_backup_paths(conn: sqlite3.Connection) -> set[str]:
    """Return backups that are still required by an unfinished actual run."""
    protected = {
        str(Path(row[0]).resolve())
        for row in conn.execute(
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
        if row[0]
    }
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "operation_groups" in tables:
        protected.update(
            str(Path(row[0]).resolve())
            for row in conn.execute(
                """
                SELECT DISTINCT ar.backup_path
                FROM actual_runs AS ar
                JOIN operation_groups AS og ON og.run_id = ar.run_id
                WHERE og.state IN ('planned', 'fs_done', 'db_done')
                """
            )
            if row[0]
        )
    return protected


def prune_state_backups(
    conn: sqlite3.Connection,
    backup_dir: os.PathLike | str,
    *,
    keep_latest: int = STATE_BACKUP_RETENTION,
) -> list[Path]:
    """Keep a bounded global set of completed state DB backups.

    Backups referenced by an unfinished actual run are retained even when that
    exceeds ``keep_latest``. Symlinks and multiply-linked files are never removed.
    """
    if keep_latest < 1:
        raise ValueError("keep_latest must be at least 1")

    protected = protected_state_backup_paths(conn)
    try:
        entries = list(Path(backup_dir).iterdir())
    except FileNotFoundError:
        return []

    candidates: list[tuple[int, Path]] = []
    for path in entries:
        if not _is_state_backup(path):
            continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            continue
        candidates.append((info.st_mtime_ns, path))

    candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    retained = {str(path.resolve()) for _, path in candidates[:keep_latest]} | protected
    removed: list[Path] = []
    for _, path in candidates[keep_latest:]:
        if str(path.resolve()) in retained:
            continue
        path.unlink()
        removed.append(path)
    return removed


def backup_state_db(conn: sqlite3.Connection, backup_path: os.PathLike | str) -> Path:
    target = Path(backup_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(target)

    destination = sqlite3.connect(str(target))
    try:
        conn.backup(destination)
        integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"backup integrity_check failed: {integrity}")
    except Exception:
        destination.close()
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        raise
    else:
        destination.close()
    if _is_state_backup(target) and target.parent.name == "backups":
        prune_state_backups(conn, target.parent)
    return target


def create_operation(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    action: str,
    source_path: str,
    file_id: str,
    expected_size: int,
    expected_mtime_ns: int,
    expected_fingerprint_id: int,
    dest_path: Optional[str] = None,
    quarantine_path: Optional[str] = None,
    keep_file_id: Optional[str] = None,
    expected_keep_fingerprint_id: Optional[int] = None,
    parent_operation_id: Optional[int] = None,
    operation_group_id: Optional[int] = None,
    source_dev: Optional[int] = None,
    source_ino: Optional[int] = None,
    source_ctime_ns: Optional[int] = None,
    source_sha256: Optional[str] = None,
) -> int:
    if source_dev is None:
        try:
            from mutation_io import inspect_regular_file
            source_evidence = inspect_regular_file(source_path)
        except (FileNotFoundError, OSError, RuntimeError):
            source_evidence = None
        if source_evidence is not None:
            source_dev = source_evidence.dev
            source_ino = source_evidence.ino
            source_ctime_ns = source_evidence.ctime_ns
            source_sha256 = source_evidence.sha256
    cursor = conn.execute(
        """
        INSERT INTO operations(
            run_id, action, source_path, dest_path, quarantine_path, file_id,
            keep_file_id, expected_size, expected_mtime_ns, expected_fingerprint_id,
            expected_keep_fingerprint_id, parent_operation_id,
            operation_group_id, source_dev, source_ino, source_ctime_ns,
            source_sha256, state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned')
        """,
        (
            run_id,
            action,
            source_path,
            dest_path,
            quarantine_path,
            file_id,
            keep_file_id,
            expected_size,
            expected_mtime_ns,
            expected_fingerprint_id,
            expected_keep_fingerprint_id,
            parent_operation_id,
            operation_group_id,
            source_dev,
            source_ino,
            source_ctime_ns,
            source_sha256,
        ),
    )
    return cursor.lastrowid


def create_operation_group(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    action: str,
    plan_sha256: str,
    source_path: Optional[str] = None,
    dest_path: Optional[str] = None,
    item_count: int = 0,
    manifest_path: Optional[str] = None,
    source_manifest_json: Optional[str] = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO operation_groups(
            run_id, action, state, source_path, dest_path, item_count,
            plan_sha256, manifest_path, source_manifest_json
        ) VALUES (?, ?, 'planned', ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, action, source_path, dest_path, int(item_count),
            plan_sha256, manifest_path, source_manifest_json,
        ),
    )
    return int(cursor.lastrowid)


def transition_operation_group(
    conn: sqlite3.Connection,
    group_id: int,
    new_state: str,
    *,
    error: Optional[str] = None,
) -> None:
    if new_state not in OPERATION_STATES:
        raise ValueError(f"unknown operation group state: {new_state}")
    row = conn.execute(
        "SELECT state FROM operation_groups WHERE group_id = ?", (int(group_id),)
    ).fetchone()
    if row is None:
        raise KeyError(group_id)
    current = row["state"]
    if new_state not in _ALLOWED_OPERATION_TRANSITIONS[current]:
        raise RuntimeError(f"invalid operation group transition: {current} -> {new_state}")
    conn.execute(
        """
        UPDATE operation_groups SET state = ?, error = ?, updated_at = CURRENT_TIMESTAMP
        WHERE group_id = ? AND state = ?
        """,
        (new_state, error, int(group_id), current),
    )


def record_operation_destination(conn, operation_id, evidence) -> None:
    conn.execute(
        """
        UPDATE operations SET
            destination_dev = ?, destination_ino = ?, destination_ctime_ns = ?,
            destination_size = ?, destination_mtime_ns = ?, destination_sha256 = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE operation_id = ?
        """,
        (
            evidence.dev, evidence.ino, evidence.ctime_ns, evidence.size,
            evidence.mtime_ns, evidence.sha256, operation_id,
        ),
    )


def copy_record_consume_operation(
    conn, operation_id, source, destination, source_evidence, *, guard=None
):
    """Copy, durably journal destination evidence, then consume the source.

    If source consumption completed but the following fs_done transaction
    fails, the operation remains planned with destination evidence so recovery
    can resolve it instead of treating it as a terminal failure.
    """
    from mutation_io import (
        SourceIdentityChanged,
        consume_copied_source,
        copy_no_clobber,
        evidence_matches,
        inspect_regular_file,
        unlink_owned,
    )

    copied = None
    try:
        copied = copy_no_clobber(source, destination, expected=source_evidence)
        with transaction(conn):
            record_operation_destination(conn, operation_id, copied.destination_evidence)
        consume_copied_source(copied, guard=guard)
        try:
            with transaction(conn):
                transition_operation(conn, operation_id, "fs_done")
        except Exception as exc:
            with transaction(conn):
                conn.execute(
                    "UPDATE operations SET error = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE operation_id = ? AND state = 'planned'",
                    (f"post-consume fs_done failed: {exc}", operation_id),
                )
            raise
        return copied.destination_evidence
    except Exception as exc:
        if copied is not None:
            try:
                source_owned = evidence_matches(
                    inspect_regular_file(source), copied.source_evidence
                )
            except (FileNotFoundError, OSError, RuntimeError):
                source_owned = False
            if not source_owned:
                with transaction(conn):
                    conn.execute(
                        "UPDATE operations SET error = ?, updated_at = CURRENT_TIMESTAMP "
                        "WHERE operation_id = ? AND state = 'planned'",
                        (f"source consumed; recovery required: {exc}", operation_id),
                    )
                raise
            try:
                unlink_owned(destination, expected=copied.destination_evidence)
            except (FileNotFoundError, OSError, RuntimeError) as cleanup_exc:
                try:
                    destination_owned = evidence_matches(
                        inspect_regular_file(destination), copied.destination_evidence
                    )
                except FileNotFoundError:
                    destination_owned = False
                except (OSError, RuntimeError):
                    destination_owned = True
                if destination_owned:
                    with transaction(conn):
                        conn.execute(
                            "UPDATE operations SET error = ?, updated_at = CURRENT_TIMESTAMP "
                            "WHERE operation_id = ? AND state = 'planned'",
                            (
                                f"destination cleanup failed; recovery required: "
                                f"{cleanup_exc}; original error: {exc}",
                                operation_id,
                            ),
                        )
                    raise exc
        terminal = "stale" if isinstance(exc, SourceIdentityChanged) else "failed"
        with transaction(conn):
            row = conn.execute(
                "SELECT state FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if row is not None and row["state"] == "planned":
                transition_operation(conn, operation_id, terminal, error=str(exc))
        raise


def transition_operation(
    conn: sqlite3.Connection,
    operation_id: int,
    new_state: str,
    *,
    error: Optional[str] = None,
) -> None:
    if new_state not in OPERATION_STATES:
        raise ValueError(f"unknown operation state: {new_state}")
    row = conn.execute(
        "SELECT state FROM operations WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    if row is None:
        raise KeyError(operation_id)
    current = row[0]
    if new_state not in _ALLOWED_OPERATION_TRANSITIONS[current]:
        raise RuntimeError(f"invalid operation transition: {current} -> {new_state}")
    if new_state == "fs_done":
        evidence_row = conn.execute(
            "SELECT action, dest_path, quarantine_path, destination_dev FROM operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if (
            evidence_row["action"] != "quarantine_purge"
            and evidence_row["destination_dev"] is None
        ):
            raise RuntimeError("fs_done requires durable destination evidence")
    conn.execute(
        """
        UPDATE operations
        SET state = ?, error = ?, updated_at = CURRENT_TIMESTAMP
        WHERE operation_id = ? AND state = ?
        """,
        (new_state, error, operation_id, current),
    )


def _operation_evidence(row, prefix):
    from mutation_io import FileEvidence
    dev = row[f"{prefix}_dev"]
    if dev is None:
        return None
    size_field = "expected_size" if prefix == "source" else "destination_size"
    mtime_field = "expected_mtime_ns" if prefix == "source" else "destination_mtime_ns"
    return FileEvidence(
        dev=dev,
        ino=row[f"{prefix}_ino"],
        ctime_ns=row[f"{prefix}_ctime_ns"],
        size=row[size_field],
        mtime_ns=row[mtime_field],
        sha256=row[f"{prefix}_sha256"],
    )


def _owned_operation_path(row, path, prefix):
    from mutation_io import evidence_matches, inspect_regular_file
    expected = _operation_evidence(row, prefix)
    if expected is None:
        return False
    try:
        return evidence_matches(inspect_regular_file(path), expected)
    except (FileNotFoundError, OSError, RuntimeError):
        return False


def _clone_fingerprint_for_recovered_file(conn, file_id, canonical_path, evidence):
    current = conn.execute(
        "SELECT current_fingerprint_id FROM files WHERE file_id = ?", (file_id,)
    ).fetchone()
    if current is None or current["current_fingerprint_id"] is None:
        return None
    fingerprint_id = conn.execute(
        """
        INSERT INTO fingerprints(
            file_id, canonical_path, size, mtime_ns, dev, ino, ctime_ns,
            normalizer_version, fingerprint_version, analysis_policy_hash,
            raw_sha256, normalized_sha256, normalized_length, encoding, status,
            front_anchor, tail_anchor, anchors_json
        )
        SELECT file_id, ?, ?, ?, ?, ?, ?, normalizer_version,
               fingerprint_version || ?, analysis_policy_hash, raw_sha256,
               normalized_sha256, normalized_length, encoding, status,
               front_anchor, tail_anchor, anchors_json
        FROM fingerprints WHERE fingerprint_id = ? AND file_id = ?
        """,
        (
            str(canonical_path), evidence.size, evidence.mtime_ns,
            evidence.dev, evidence.ino, evidence.ctime_ns,
            f":recovery:{evidence.dev}:{evidence.ino}:{evidence.ctime_ns}",
            current["current_fingerprint_id"], file_id,
        ),
    ).lastrowid
    return fingerprint_id or None


def _rollback_owned_destination(conn, row, destination, source, source_bucket):
    from mutation_io import copy_no_clobber, consume_copied_source
    copied = copy_no_clobber(
        destination, source, expected=_operation_evidence(row, "destination")
    )
    with transaction(conn):
        conn.execute(
            """
            UPDATE operations SET source_dev = ?, source_ino = ?, source_ctime_ns = ?,
                source_sha256 = ?, expected_size = ?, expected_mtime_ns = ?,
                updated_at = CURRENT_TIMESTAMP WHERE operation_id = ?
            """,
            (
                copied.destination_evidence.dev, copied.destination_evidence.ino,
                copied.destination_evidence.ctime_ns,
                copied.destination_evidence.sha256, copied.destination_evidence.size,
                copied.destination_evidence.mtime_ns, row["operation_id"],
            ),
        )
    consume_copied_source(copied)
    with transaction(conn):
        fingerprint_id = _clone_fingerprint_for_recovered_file(
            conn, row["file_id"], source, copied.destination_evidence
        )
        conn.execute(
            """
            UPDATE files SET canonical_path = ?, source = ?, active = 1,
                dev = ?, ino = ?, ctime_ns = ?, size = ?, mtime_ns = ?,
                current_fingerprint_id = ? WHERE file_id = ?
            """,
            (
                str(source), source_bucket, copied.destination_evidence.dev,
                copied.destination_evidence.ino, copied.destination_evidence.ctime_ns,
                copied.destination_evidence.size, copied.destination_evidence.mtime_ns,
                fingerprint_id, row["file_id"],
            ),
        )
        transition_operation(conn, row["operation_id"], "rolled_back")
    return copied.destination_evidence


def _finalize_existing_source_rollback(conn, row, source, source_bucket):
    from mutation_io import inspect_regular_file
    evidence = inspect_regular_file(source)
    expected = _operation_evidence(row, "source")
    if expected is None or evidence != expected:
        raise RuntimeError("rollback source ownership mismatch")
    file_row = conn.execute(
        "SELECT canonical_path, source, dev, ino, ctime_ns, size, mtime_ns "
        "FROM files WHERE file_id = ?",
        (row["file_id"],),
    ).fetchone()
    current_identity = (
        file_row["dev"], file_row["ino"], file_row["ctime_ns"],
        file_row["size"], file_row["mtime_ns"],
    ) if file_row else None
    expected_identity = (
        evidence.dev, evidence.ino, evidence.ctime_ns, evidence.size, evidence.mtime_ns,
    )
    with transaction(conn):
        if (
            file_row is None
            or file_row["canonical_path"] != str(source)
            or file_row["source"] != source_bucket
            or current_identity != expected_identity
        ):
            fingerprint_id = _clone_fingerprint_for_recovered_file(
                conn, row["file_id"], source, evidence
            )
            conn.execute(
                """
                UPDATE files SET canonical_path = ?, source = ?, active = 1,
                    dev = ?, ino = ?, ctime_ns = ?, size = ?, mtime_ns = ?,
                    current_fingerprint_id = ? WHERE file_id = ?
                """,
                (
                    str(source), source_bucket, evidence.dev, evidence.ino,
                    evidence.ctime_ns, evidence.size, evidence.mtime_ns,
                    fingerprint_id, row["file_id"],
                ),
            )
        transition_operation(conn, row["operation_id"], "rolled_back")


def _complete_legacy_title_path_house_ingest(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    destination: Path,
) -> bool:
    """Finish a proven fs_done intake blocked by one legacy title tombstone.

    Old title-requeue rows could keep their former real path after becoming
    inactive.  A later full Scanner fallback may also reactivate that historical
    row after the incoming file has already been copied there.  Complete the
    intake only when the operation owns the destination bytes and the competing
    row has committed title-requeue provenance proving that it released the
    path.  Every other collision continues through the conservative rollback.
    """
    if row["action"] != "house_ingest" or row["state"] != "fs_done":
        return False
    owner = conn.execute(
        """
        SELECT f.*,
               CASE WHEN rep.file_id IS NULL THEN 0 ELSE 1 END AS representative
        FROM files AS f
        LEFT JOIN representatives AS rep ON rep.file_id = f.file_id
        WHERE f.canonical_path = ? AND f.file_id != ?
        """,
        (str(destination), row["file_id"]),
    ).fetchone()
    if owner is None:
        return False
    released = conn.execute(
        """
        SELECT operation_id
        FROM operations
        WHERE file_id = ?
          AND action IN ('title_cleanup_requeue', 'user_title_requeue')
          AND state = 'committed'
          AND source_path = ?
        ORDER BY operation_id DESC
        LIMIT 1
        """,
        (owner["file_id"], str(destination)),
    ).fetchone()
    if released is None:
        return False
    if (
        owner["source"] != "house"
        or owner["protected"]
        or owner["representative"]
        or owner["variant_id"] is not None
        or owner["assignment_state"]
        not in {"unassigned", "legacy_unresolved", "decision_required"}
    ):
        raise RuntimeError(
            "legacy title path owner gained protected or managed state; "
            f"manual recovery required: file_id={owner['file_id']}"
        )
    owner_unfinished = conn.execute(
        """
        SELECT operation_id
        FROM operations
        WHERE file_id = ? AND state IN ('planned', 'fs_done', 'db_done')
        LIMIT 1
        """,
        (owner["file_id"],),
    ).fetchone()
    if owner_unfinished is not None:
        raise RuntimeError(
            "legacy title path owner has an unfinished operation: "
            f"file_id={owner['file_id']}, operation_id={owner_unfinished[0]}"
        )
    incoming = conn.execute(
        """
        SELECT canonical_path, source, active, current_fingerprint_id,
               size, mtime_ns
        FROM files WHERE file_id = ?
        """,
        (row["file_id"],),
    ).fetchone()
    if (
        incoming is None
        or incoming["canonical_path"] != row["source_path"]
        or incoming["source"] != "temp"
        or incoming["active"] != 1
        or incoming["current_fingerprint_id"] != row["expected_fingerprint_id"]
        or incoming["size"] != row["expected_size"]
        or incoming["mtime_ns"] != row["expected_mtime_ns"]
    ):
        raise RuntimeError("fs_done house intake source DB state no longer matches journal")

    from mutation_io import evidence_matches, inspect_regular_file

    destination_evidence = inspect_regular_file(destination)
    expected_destination = _operation_evidence(row, "destination")
    if expected_destination is None or not evidence_matches(
        destination_evidence, expected_destination
    ):
        raise RuntimeError("fs_done house intake destination evidence changed")
    coordinates = coordinate_fields_from_name(destination.name)
    current_fingerprint_id = (
        incoming["current_fingerprint_id"]
        if destination_evidence.size == incoming["size"]
        and destination_evidence.mtime_ns == incoming["mtime_ns"]
        else None
    )
    retired_path = retired_canonical_path(
        conn, owner["file_id"], destination
    )
    with transaction(conn):
        conn.execute(
            """
            UPDATE files
            SET canonical_path = ?, active = 0, protected = 0,
                last_seen_at = CURRENT_TIMESTAMP
            WHERE file_id = ? AND canonical_path = ?
            """,
            (retired_path, owner["file_id"], str(destination)),
        )
        conn.execute(
            """
            UPDATE files
            SET canonical_path = ?, source = 'house', active = 1,
                size = ?, mtime_ns = ?, dev = ?, ino = ?, ctime_ns = ?,
                current_fingerprint_id = ?, last_seen_at = CURRENT_TIMESTAMP,
                coordinate_kind = ?, part_num = ?, part_den = ?,
                volume_num = ?, volume_den = ?, coordinate_symbol = ?,
                coordinate_sort_key = ?, episode_start = ?, episode_end = ?,
                coordinate_raw = ?, span_ambiguous = ?
            WHERE file_id = ?
            """,
            (
                str(destination), destination_evidence.size,
                destination_evidence.mtime_ns, destination_evidence.dev,
                destination_evidence.ino, destination_evidence.ctime_ns,
                current_fingerprint_id,
                coordinates["coordinate_kind"], coordinates["part_num"],
                coordinates["part_den"], coordinates["volume_num"],
                coordinates["volume_den"], coordinates["coordinate_symbol"],
                coordinates["coordinate_sort_key"], coordinates["episode_start"],
                coordinates["episode_end"], coordinates["coordinate_raw"],
                coordinates["span_ambiguous"], row["file_id"],
            ),
        )
        upsert_file_analysis(
            conn,
            row["file_id"],
            destination,
            stat_result=os.stat(destination, follow_symlinks=False),
        )
        transition_operation(conn, row["operation_id"], "db_done")
    with transaction(conn):
        transition_operation(conn, row["operation_id"], "committed")
    return True


def _recover_interrupted_exact_operation(
    conn: sqlite3.Connection,
    operation_id: int,
) -> str:
    """Safely resolve one interrupted exact-quarantine journal entry.

    Before DB application (`planned`/`fs_done`) recovery favors restoring the
    source.  Once the DB already reflects quarantine (`db_done`), recovery
    finishes the commit.  Conflicting paths are never overwritten.
    """
    row = conn.execute(
        """
        SELECT *
        FROM operations WHERE operation_id = ?
        """,
        (operation_id,),
    ).fetchone()
    if row is None:
        raise KeyError(operation_id)
    if row["action"] not in {
        "exact_quarantine", "human_quarantine", "user_quarantine"
    }:
        raise ValueError("operation is not a supported quarantine action")
    if row["state"] not in {"planned", "fs_done", "db_done"}:
        return row["state"]

    source = Path(row["source_path"])
    quarantine_value = row["quarantine_path"]
    if not quarantine_value:
        with transaction(conn):
            transition_operation(conn, operation_id, "failed", error="missing quarantine_path")
        return "failed"
    quarantine = Path(quarantine_value)
    actual_run = _actual_run_for_operation(conn, row["run_id"])
    assert_actual_run_path_any(actual_run, source, ("temp_root", "house_root"))
    assert_actual_run_path(actual_run, quarantine, "temp_root")
    source_exists = source.exists()
    quarantine_exists = quarantine.exists()

    if row["state"] in {"planned", "fs_done"}:
        if source_exists and not _owned_operation_path(row, source, "source"):
            with transaction(conn):
                transition_operation(conn, operation_id, "stale", error="source identity mismatch")
            return "stale"
        if quarantine_exists and not _owned_operation_path(row, quarantine, "destination"):
            with transaction(conn):
                transition_operation(conn, operation_id, "stale", error="quarantine identity mismatch")
            return "stale"
        if source_exists and quarantine_exists:
            from mutation_io import unlink_owned
            unlink_owned(quarantine, expected=_operation_evidence(row, "destination"))
            with transaction(conn):
                transition_operation(conn, operation_id, "rolled_back")
            return "rolled_back"
        if not source_exists and quarantine_exists:
            source_bucket = (
                "house" if str(source).startswith(actual_run["house_root"] + os.sep)
                else "temp"
            )
            _rollback_owned_destination(
                conn, row, quarantine, source, source_bucket
            )
            return "rolled_back"
        if source_exists and not quarantine_exists:
            source_bucket = (
                "house" if str(source).startswith(actual_run["house_root"] + os.sep)
                else "temp"
            )
            _finalize_existing_source_rollback(
                conn, row, source, source_bucket
            )
            return "rolled_back"
        with transaction(conn):
            transition_operation(
                conn, operation_id, "failed", error="source and quarantine are both missing"
            )
        return "failed"

    file_row = conn.execute(
        "SELECT canonical_path, active FROM files WHERE file_id = ?", (row["file_id"],)
    ).fetchone()
    db_committed = (
        file_row is not None
        and file_row["canonical_path"] == str(quarantine)
        and file_row["active"] == 0
    )
    if db_committed and quarantine_exists and not source_exists:
        if not _owned_operation_path(row, quarantine, "destination"):
            with transaction(conn):
                transition_operation(
                    conn, operation_id, "stale", error="db_done quarantine ownership mismatch"
                )
            return "stale"
        with transaction(conn):
            transition_operation(conn, operation_id, "committed")
        return "committed"

    with transaction(conn):
        transition_operation(
            conn, operation_id, "failed", error="db_done state does not match quarantine"
        )
    return "failed"


def _recover_interrupted_queue_operation(conn: sqlite3.Connection, operation_id: int) -> str:
    row = conn.execute(
        """
        SELECT *
        FROM operations WHERE operation_id = ?
        """,
        (operation_id,),
    ).fetchone()
    if row is None:
        raise KeyError(operation_id)
    if row["action"] not in {
        "suspected_move", "warning_move", "house_review_move", "queue_restore",
        "house_ingest", "user_queue_restore", "user_queue_accept",
        "title_cleanup_requeue", "user_title_requeue", "volume_group_merge",
        "library_file_relocate",
        "volume_coordinate_hold",
    }:
        raise ValueError("operation is not a queue move")
    if row["state"] not in {"planned", "fs_done", "db_done"}:
        return row["state"]
    source = Path(row["source_path"])
    destination = Path(row["dest_path"])
    actual_run = _actual_run_for_operation(conn, row["run_id"])
    if row["action"] in {
        "house_ingest", "user_queue_restore", "user_queue_accept"
    }:
        assert_actual_run_path(actual_run, source, "temp_root")
        assert_actual_run_path(actual_run, destination, "house_root")
    elif row["action"] in {
        "house_review_move", "title_cleanup_requeue", "user_title_requeue"
    }:
        assert_actual_run_path(actual_run, source, "house_root")
        assert_actual_run_path(actual_run, destination, "temp_root")
    elif row["action"] in {"volume_group_merge", "library_file_relocate"}:
        assert_actual_run_path(actual_run, source, "house_root")
        assert_actual_run_path(actual_run, destination, "house_root")
    else:
        assert_actual_run_path(actual_run, source, "temp_root")
        assert_actual_run_path(actual_run, destination, "temp_root")
    source_exists, destination_exists = source.exists(), destination.exists()
    if row["state"] in {"planned", "fs_done"}:
        if source_exists and not _owned_operation_path(row, source, "source"):
            with transaction(conn):
                transition_operation(conn, operation_id, "stale", error="queue source identity mismatch")
            return "stale"
        if destination_exists and not _owned_operation_path(row, destination, "destination"):
            with transaction(conn):
                transition_operation(conn, operation_id, "stale", error="queue destination identity mismatch")
            return "stale"
        if source_exists and destination_exists:
            from mutation_io import unlink_owned
            unlink_owned(destination, expected=_operation_evidence(row, "destination"))
            with transaction(conn):
                transition_operation(conn, operation_id, "rolled_back")
            return "rolled_back"
        if not source_exists and destination_exists:
            if _complete_legacy_title_path_house_ingest(
                conn, row, destination
            ):
                return "committed"
            source_bucket = {
                "queue_restore": "queue", "user_queue_restore": "queue",
                "user_queue_accept": "queue",
                "house_review_move": "house",
                "title_cleanup_requeue": "house",
                "user_title_requeue": "house",
                "volume_group_merge": "house",
                "library_file_relocate": "house",
            }.get(row["action"], "temp")
            _rollback_owned_destination(
                conn, row, destination, source, source_bucket
            )
            return "rolled_back"
        if source_exists and not destination_exists:
            source_bucket = {
                "queue_restore": "queue", "user_queue_restore": "queue",
                "user_queue_accept": "queue",
                "house_review_move": "house",
                "title_cleanup_requeue": "house",
                "user_title_requeue": "house",
                "volume_group_merge": "house",
                "library_file_relocate": "house",
            }.get(row["action"], "temp")
            _finalize_existing_source_rollback(
                conn, row, source, source_bucket
            )
            return "rolled_back"
        with transaction(conn):
            transition_operation(conn, operation_id, "failed", error="both queue paths missing")
        return "failed"
    file_row = conn.execute(
        "SELECT canonical_path, source, active FROM files WHERE file_id = ?",
        (row["file_id"],),
    ).fetchone()
    if row["action"] in {"title_cleanup_requeue", "user_title_requeue"}:
        retired_path = retired_canonical_path(conn, row["file_id"], source)
        db_committed = (
            file_row is not None
            and file_row["canonical_path"] in {str(source), retired_path}
            and file_row["source"] == "house"
            and file_row["active"] == 0
        )
        if db_committed and destination_exists and not source_exists:
            if not _owned_operation_path(row, destination, "destination"):
                with transaction(conn):
                    transition_operation(
                        conn, operation_id, "stale",
                        error="db_done title requeue destination ownership mismatch",
                    )
                return "stale"
            with transaction(conn):
                transition_operation(conn, operation_id, "committed")
            return "committed"
        with transaction(conn):
            transition_operation(
                conn, operation_id, "failed",
                error="db_done title requeue state mismatch",
            )
        return "failed"
    if row["action"] in {"volume_group_merge", "library_file_relocate"}:
        db_committed = (
            file_row is not None
            and file_row["canonical_path"] == str(destination)
            and file_row["source"] == "house"
            and file_row["active"] == 1
        )
        if db_committed and destination_exists and not source_exists:
            if not _owned_operation_path(row, destination, "destination"):
                with transaction(conn):
                    transition_operation(
                        conn, operation_id, "stale",
                        error="db_done volume destination ownership mismatch",
                    )
                return "stale"
            with transaction(conn):
                transition_operation(conn, operation_id, "committed")
            return "committed"
        with transaction(conn):
            transition_operation(
                conn, operation_id, "failed",
                error="db_done volume group state mismatch",
            )
        return "failed"
    expected_source = {
        "queue_restore": "temp",
        "house_ingest": "house",
        "user_queue_restore": "house",
        "user_queue_accept": "house",
    }.get(row["action"], "queue")
    if (
        file_row is not None
        and file_row["canonical_path"] == str(destination)
        and file_row["source"] == expected_source
        and destination_exists
        and not source_exists
    ):
        if not _owned_operation_path(row, destination, "destination"):
            with transaction(conn):
                transition_operation(
                    conn, operation_id, "stale", error="db_done destination ownership mismatch"
                )
            return "stale"
        with transaction(conn):
            transition_operation(conn, operation_id, "committed")
        return "committed"
    with transaction(conn):
        transition_operation(conn, operation_id, "failed", error="db_done queue state mismatch")
    return "failed"


def _recover_interrupted_purge_operation(conn: sqlite3.Connection, operation_id: int) -> str:
    row = conn.execute(
        """
        SELECT *
        FROM operations WHERE operation_id = ?
        """,
        (operation_id,),
    ).fetchone()
    if row is None:
        raise KeyError(operation_id)
    if row["action"] != "quarantine_purge" or row["parent_operation_id"] is None:
        raise ValueError("operation is not a purge journal entry")
    if row["state"] not in {"planned", "fs_done", "db_done"}:
        return row["state"]
    path = Path(row["source_path"])

    if row["state"] == "planned":
        if path.exists() or path.is_symlink():
            if not _owned_operation_path(row, path, "source"):
                with transaction(conn):
                    transition_operation(conn, operation_id, "stale", error="purge source identity mismatch")
                return "stale"
            with transaction(conn):
                transition_operation(conn, operation_id, "rolled_back")
            return "rolled_back"
        with transaction(conn):
            transition_operation(conn, operation_id, "fs_done")
        row = conn.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()

    if row["state"] == "fs_done":
        if path.exists() or path.is_symlink():
            with transaction(conn):
                transition_operation(conn, operation_id, "rolled_back")
            return "rolled_back"
        with transaction(conn):
            conn.execute(
                "UPDATE operations SET purged_at = COALESCE(purged_at, CURRENT_TIMESTAMP) "
                "WHERE operation_id = ? AND action IN "
                "('exact_quarantine', 'human_quarantine', 'user_quarantine')",
                (row["parent_operation_id"],),
            )
            transition_operation(conn, operation_id, "db_done")
        row = conn.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()

    parent = conn.execute(
        "SELECT purged_at FROM operations WHERE operation_id = ?",
        (row["parent_operation_id"],),
    ).fetchone()
    if row["state"] == "db_done" and parent and parent["purged_at"] and not path.exists():
        with transaction(conn):
            transition_operation(conn, operation_id, "committed")
        return "committed"
    with transaction(conn):
        transition_operation(conn, operation_id, "failed", error="purge recovery state mismatch")
    return "failed"


def _recover_interrupted_quarantine_restore(conn: sqlite3.Connection, operation_id: int) -> str:
    row = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if row is None:
        raise KeyError(operation_id)
    if row["action"] != "user_quarantine_restore":
        raise ValueError("operation is not a quarantine restore")
    if row["state"] not in {"planned", "fs_done", "db_done"}:
        return row["state"]
    source = Path(row["source_path"])
    destination = Path(row["dest_path"])
    actual_run = _actual_run_for_operation(conn, row["run_id"])
    assert_actual_run_path(actual_run, source, "temp_root")
    assert_actual_run_path(actual_run, destination, "house_root")
    source_exists, destination_exists = source.exists(), destination.exists()
    if row["state"] in {"planned", "fs_done"}:
        if source_exists and not _owned_operation_path(row, source, "source"):
            with transaction(conn):
                transition_operation(conn, operation_id, "stale", error="restore source identity mismatch")
            return "stale"
        if destination_exists and not _owned_operation_path(row, destination, "destination"):
            with transaction(conn):
                transition_operation(conn, operation_id, "stale", error="restore destination identity mismatch")
            return "stale"
        if source_exists and destination_exists:
            from mutation_io import unlink_owned
            unlink_owned(destination, expected=_operation_evidence(row, "destination"))
            with transaction(conn):
                transition_operation(conn, operation_id, "rolled_back")
            return "rolled_back"
        if not source_exists and destination_exists:
            _rollback_owned_destination(conn, row, destination, source, "quarantine")
            with transaction(conn):
                conn.execute("UPDATE files SET active = 0 WHERE file_id = ?", (row["file_id"],))
            return "rolled_back"
        if source_exists and not destination_exists:
            _finalize_existing_source_rollback(conn, row, source, "quarantine")
            with transaction(conn):
                conn.execute("UPDATE files SET active = 0 WHERE file_id = ?", (row["file_id"],))
            return "rolled_back"
        with transaction(conn):
            transition_operation(conn, operation_id, "failed", error="both restore paths missing")
        return "failed"
    file_row = conn.execute(
        "SELECT canonical_path, source, active FROM files WHERE file_id = ?", (row["file_id"],)
    ).fetchone()
    if (
        file_row is not None and file_row["canonical_path"] == str(destination)
        and file_row["source"] == "house" and file_row["active"] == 1
        and destination_exists and not source_exists
        and _owned_operation_path(row, destination, "destination")
    ):
        with transaction(conn):
            transition_operation(conn, operation_id, "committed")
        return "committed"
    with transaction(conn):
        transition_operation(conn, operation_id, "failed", error="db_done restore state mismatch")
    return "failed"


def recover_interrupted_operation(conn: sqlite3.Connection, operation_id: int) -> str:
    from mutation_io import mutation_lock
    row = conn.execute(
        "SELECT run_id FROM operations WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    if row is None:
        raise KeyError(operation_id)
    with mutation_lock(conn, f"recovery:{operation_id}", run_id=row["run_id"]):
        return _recover_interrupted_operation(conn, operation_id)


def _recover_interrupted_operation(conn: sqlite3.Connection, operation_id: int) -> str:
    row = conn.execute(
        "SELECT action FROM operations WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    if row is None:
        raise KeyError(operation_id)
    if row["action"] in {
        "exact_quarantine", "human_quarantine", "user_quarantine"
    }:
        return _recover_interrupted_exact_operation(conn, operation_id)
    if row["action"] in {
        "suspected_move", "warning_move", "house_review_move", "queue_restore",
        "house_ingest", "user_queue_restore", "user_queue_accept",
        "title_cleanup_requeue", "user_title_requeue", "volume_group_merge",
        "library_file_relocate",
        "volume_coordinate_hold",
    }:
        return _recover_interrupted_queue_operation(conn, operation_id)
    if row["action"] == "quarantine_purge":
        return _recover_interrupted_purge_operation(conn, operation_id)
    if row["action"] == "user_quarantine_restore":
        return _recover_interrupted_quarantine_restore(conn, operation_id)
    raise ValueError(f"unsupported recovery action: {row['action']}")


def doctor_issues(
    conn: sqlite3.Connection,
    *,
    allowed_active_run_id=None,
    verify_files: bool = True,
    check_integrity: bool = True,
):
    """Return operational, schema, and optionally filesystem doctor issues.

    Defaults preserve the existing full fail-closed Doctor.  The local web UI
    uses the lightweight mode for status rendering only; Folderling and every
    mutation path continue to use both full integrity and file identity checks.
    """
    issues = []
    try:
        validate_schema(conn, check_integrity=check_integrity)
    except RuntimeError as exc:
        return [{"kind": "schema", "detail": str(exc)}]
    for row in conn.execute(
        "SELECT run_id, activated_at FROM actual_runs WHERE state = 'active'"
    ):
        if row["run_id"] != allowed_active_run_id:
            issues.append({
                "kind": "active_actual_run",
                "run_id": row["run_id"],
                "activated_at": row["activated_at"],
            })
    for row in conn.execute(
        """
        SELECT run_id, activation_claim FROM actual_runs
        WHERE state = 'approved' AND activation_claim IS NOT NULL
        """
    ):
        issues.append({
            "kind": "claimed_actual_run",
            "run_id": row["run_id"],
            "activation_claim": row["activation_claim"],
        })
    for row in conn.execute(
        "SELECT operation_id, state FROM operations WHERE state IN ('planned', 'fs_done', 'db_done')"
    ):
        issues.append({
            "kind": "unfinished_operation",
            "operation_id": row["operation_id"],
            "state": row["state"],
        })
    for row in conn.execute(
        "SELECT group_id, action, state FROM operation_groups "
        "WHERE state IN ('planned', 'fs_done', 'db_done')"
    ):
        issues.append({
            "kind": "unfinished_operation_group",
            "group_id": row["group_id"],
            "action": row["action"],
            "state": row["state"],
        })
    for row in conn.execute(
        """
        SELECT operation_id, quarantine_path FROM operations
        WHERE action = 'exact_quarantine' AND state = 'committed'
          AND purged_at IS NULL AND destination_dev IS NULL
        """
    ):
        issues.append({
            "kind": "legacy_unowned_quarantine",
            "operation_id": row["operation_id"],
            "path": row["quarantine_path"],
        })
    for row in conn.execute(
        """
        SELECT run_id, manifest_path, error FROM actual_runs
        WHERE manifest_path IS NOT NULL
          AND error LIKE '%activation manifest cleanup failed%'
        """
    ):
        if Path(row["manifest_path"]).exists():
            issues.append({
                "kind": "orphan_activation_manifest",
                "run_id": row["run_id"],
                "path": row["manifest_path"],
                "error": row["error"],
            })
    if verify_files:
        for row in conn.execute(
            """
            SELECT file_id, canonical_path, size, mtime_ns, dev, ino, ctime_ns, assignment_state,
                   current_fingerprint_id
            FROM files WHERE active = 1
            """
        ):
            path = Path(row["canonical_path"])
            if not path.is_file():
                issues.append({"kind": "missing_file", "file_id": row["file_id"], "path": str(path)})
                continue
            stat = path.stat()
            if stat.st_size != row["size"] or stat.st_mtime_ns != row["mtime_ns"]:
                issues.append({"kind": "stale_snapshot", "file_id": row["file_id"], "path": str(path)})
            identity_changed = row["dev"] is not None and (
                row["dev"] != stat.st_dev or row["ino"] != stat.st_ino
            )
            managed_ctime_changed = (
                row["assignment_state"] == "managed"
                and row["dev"] is not None
                and row["ctime_ns"] != stat.st_ctime_ns
            )
            # chmod/xattr 같은 macOS 메타데이터 변경은 inode·size·mtime를 그대로 둔 채
            # ctime만 바꿀 수 있다. 아직 mutation 권한의 근거가 아닌 미배정 파일은 이
            # 차이만으로 Folderling을 막지 않는다. 반면 실제 identity(dev/ino) 교체와
            # managed 파일의 ctime 변화는 기존 fail-closed 검사를 유지한다.
            if identity_changed or managed_ctime_changed:
                issues.append({"kind": "stale_identity", "file_id": row["file_id"], "path": str(path)})
            if row["current_fingerprint_id"] is None and row["assignment_state"] == "managed":
                issues.append({"kind": "missing_fingerprint", "file_id": row["file_id"]})
        for row in conn.execute(
            """
            SELECT folder_id, canonical_path, dev, ino, ctime_ns
            FROM work_folders WHERE state = 'active'
            """
        ):
            path = Path(row["canonical_path"])
            if not path.is_dir() or path.is_symlink():
                issues.append({
                    "kind": "managed_folder_missing",
                    "folder_id": row["folder_id"],
                    "path": str(path),
                })
                continue
            info = os.stat(path, follow_symlinks=False)
            if row["dev"] is not None and (
                row["dev"] != info.st_dev
                or row["ino"] != info.st_ino
            ):
                issues.append({
                    "kind": "managed_folder_identity_stale",
                    "folder_id": row["folder_id"],
                    "path": str(path),
                })
    for row in conn.execute(
        """
        SELECT r.variant_id, r.file_id, f.protected, f.active, f.assignment_state,
               v.status AS variant_status, w.status AS work_status
        FROM representatives AS r
        JOIN files AS f ON f.file_id = r.file_id
        JOIN variants AS v ON v.variant_id = r.variant_id
        JOIN works AS w ON w.work_bucket_id = v.work_bucket_id
        """
    ):
        if (
            not row["protected"] or not row["active"]
            or row["assignment_state"] != "managed"
            or row["variant_status"] != "active"
            or row["work_status"] != "active"
        ):
            issues.append({
                "kind": "invalid_representative",
                "variant_id": row["variant_id"],
                "file_id": row["file_id"],
            })
    for row in conn.execute(
        """
        SELECT v.variant_id, w.work_bucket_id, COUNT(f.file_id) AS managed_file_count
        FROM variants AS v
        JOIN works AS w ON w.work_bucket_id = v.work_bucket_id
        JOIN files AS f ON f.variant_id = v.variant_id
        LEFT JOIN representatives AS r ON r.variant_id = v.variant_id
        WHERE v.status = 'active' AND w.status = 'active'
          AND f.active = 1 AND f.assignment_state = 'managed'
          AND r.variant_id IS NULL
        GROUP BY v.variant_id, w.work_bucket_id
        """
    ):
        issues.append({
            "kind": "active_managed_variant_missing_representative",
            "variant_id": row["variant_id"],
            "work_bucket_id": row["work_bucket_id"],
            "managed_file_count": row["managed_file_count"],
        })
    for row in conn.execute(
        """
        SELECT f.file_id, f.variant_id, v.status AS variant_status,
               w.work_bucket_id, w.status AS work_status
        FROM files AS f
        JOIN variants AS v ON v.variant_id = f.variant_id
        JOIN works AS w ON w.work_bucket_id = v.work_bucket_id
        WHERE f.active = 1 AND f.assignment_state = 'managed'
          AND (v.status != 'active' OR w.status != 'active')
        """
    ):
        issues.append({
            "kind": "managed_file_in_retired_relation",
            "file_id": row["file_id"],
            "variant_id": row["variant_id"],
            "work_bucket_id": row["work_bucket_id"],
        })
    for row in conn.execute(
        """
        SELECT wa.alias_id, wa.work_bucket_id, wa.preferred_folder_id,
               w.status AS work_status, wf.work_bucket_id AS folder_work_id,
               wf.state AS folder_state
        FROM work_aliases AS wa
        JOIN works AS w ON w.work_bucket_id = wa.work_bucket_id
        LEFT JOIN work_folders AS wf ON wf.folder_id = wa.preferred_folder_id
        WHERE wa.active = 1
        """
    ):
        if row["work_status"] != "active":
            issues.append({
                "kind": "active_alias_on_retired_work",
                "alias_id": row["alias_id"],
                "work_bucket_id": row["work_bucket_id"],
            })
        if row["preferred_folder_id"] is not None and (
            row["folder_state"] != "active"
            or row["folder_work_id"] != row["work_bucket_id"]
        ):
            issues.append({
                "kind": "alias_route_invalid",
                "alias_id": row["alias_id"],
                "folder_id": row["preferred_folder_id"],
            })
    for row in conn.execute(
        """
        SELECT w.work_bucket_id
        FROM works AS w
        WHERE w.status = 'retired' AND (
            EXISTS (SELECT 1 FROM variants AS v
                    WHERE v.work_bucket_id = w.work_bucket_id AND v.status = 'active')
            OR EXISTS (SELECT 1 FROM work_folders AS wf
                       WHERE wf.work_bucket_id = w.work_bucket_id AND wf.state = 'active')
            OR EXISTS (SELECT 1 FROM work_aliases AS wa
                       WHERE wa.work_bucket_id = w.work_bucket_id AND wa.active = 1)
        )
        """
    ):
        issues.append({
            "kind": "retired_work_has_active_relations",
            "work_bucket_id": row["work_bucket_id"],
        })
    for row in conn.execute(
        """
        SELECT d.decision_id, d.verdict,
               lv.variant_id AS left_variant_id, lv.work_bucket_id AS left_work_id,
               rv.variant_id AS right_variant_id, rv.work_bucket_id AS right_work_id
        FROM decisions AS d
        JOIN variants AS lv ON lv.variant_id = d.left_variant_id
        JOIN variants AS rv ON rv.variant_id = d.right_variant_id
        WHERE d.active = 1
        """
    ):
        if row["verdict"] == "same_content":
            valid = row["left_variant_id"] == row["right_variant_id"]
        elif row["verdict"] == "same_work_distinct_variant":
            valid = (
                row["left_work_id"] == row["right_work_id"]
                and row["left_variant_id"] != row["right_variant_id"]
            )
        else:
            valid = row["left_work_id"] != row["right_work_id"]
        if not valid:
            issues.append({
                "kind": "active_decision_relation_conflict",
                "decision_id": row["decision_id"],
                "verdict": row["verdict"],
            })
    return issues


def restore_committed_queue_file(conn: sqlite3.Connection, file_id: str):
    from mutation_io import mutation_lock
    origin = conn.execute(
        """
        SELECT run_id FROM operations
        WHERE file_id = ? AND action IN ('suspected_move', 'warning_move', 'house_review_move')
          AND state = 'committed' ORDER BY operation_id DESC LIMIT 1
        """,
        (file_id,),
    ).fetchone()
    if origin is None:
        raise RuntimeError("committed queue origin not found")
    with mutation_lock(
        conn, f"queue_restore:{file_id}", run_id=origin["run_id"]
    ):
        return _restore_committed_queue_file(conn, file_id)


def _restore_committed_queue_file(conn: sqlite3.Connection, file_id: str):
    file_row = conn.execute(
        """
        SELECT f.*, CASE WHEN r.file_id IS NULL THEN 0 ELSE 1 END AS representative
        FROM files AS f LEFT JOIN representatives AS r ON r.file_id = f.file_id
        WHERE f.file_id = ? AND f.active = 1
        """,
        (file_id,),
    ).fetchone()
    if file_row is None or file_row["source"] != "queue":
        raise RuntimeError("file is not an active queue item")
    same_content = conn.execute(
        """
        SELECT 1 FROM decisions
        WHERE active = 1 AND verdict = 'same_content'
          AND (left_file_id = ? OR right_file_id = ?)
        LIMIT 1
        """,
        (file_id, file_id),
    ).fetchone()
    if same_content is not None and not file_row["representative"]:
        raise RuntimeError("same_content queue file must be quarantined, not restored")
    original = conn.execute(
        """
        SELECT * FROM operations
        WHERE file_id = ? AND action IN ('suspected_move', 'warning_move', 'house_review_move')
          AND state = 'committed'
        ORDER BY operation_id DESC LIMIT 1
        """,
        (file_id,),
    ).fetchone()
    if original is None:
        raise RuntimeError("committed queue origin not found")
    queue_path = Path(file_row["canonical_path"])
    destination = Path(original["source_path"])
    actual_run = _actual_run_for_operation(conn, original["run_id"])
    assert_actual_run_path(actual_run, queue_path, "temp_root")
    if original["action"] == "house_review_move":
        assert_actual_run_path(actual_run, destination, "house_root")
    else:
        assert_actual_run_path(actual_run, destination, "temp_root")
    if not queue_path.is_file() or destination.exists():
        raise RuntimeError("queue restore paths are stale or destination already exists")
    if not _owned_operation_path(original, queue_path, "destination"):
        raise RuntimeError("queue restore identity mismatch")
    stat = queue_path.stat()
    if stat.st_size != file_row["size"] or stat.st_mtime_ns != file_row["mtime_ns"]:
        raise RuntimeError("queue restore snapshot is stale")
    with transaction(conn):
        operation_id = create_operation(
            conn,
            run_id=original["run_id"],
            action="queue_restore",
            source_path=str(queue_path),
            dest_path=str(destination),
            file_id=file_id,
            expected_size=file_row["size"],
            expected_mtime_ns=file_row["mtime_ns"],
            expected_fingerprint_id=file_row["current_fingerprint_id"],
        )
    try:
        destination_evidence = copy_record_consume_operation(
            conn,
            operation_id,
            queue_path,
            destination,
            _operation_evidence(original, "destination"),
        )
    except Exception:
        raise
    with transaction(conn):
        restore_source = "house" if original["action"] == "house_review_move" else "temp"
        if file_row["assignment_origin"] == "strong_match":
            conn.execute(
                """
                UPDATE files
                SET canonical_path = ?, source = ?, assignment_state = 'decision_required',
                    assignment_origin = NULL,
                    dev = ?, ino = ?, ctime_ns = ?, size = ?, mtime_ns = ?
                WHERE file_id = ?
                """,
                (
                    str(destination), restore_source, destination_evidence.dev, destination_evidence.ino,
                    destination_evidence.ctime_ns, destination_evidence.size,
                    destination_evidence.mtime_ns, file_id,
                ),
            )
        else:
            conn.execute(
                """UPDATE files SET canonical_path = ?, source = ?,
                    dev = ?, ino = ?, ctime_ns = ?, size = ?, mtime_ns = ?
                    WHERE file_id = ?""",
                (
                    str(destination), restore_source, destination_evidence.dev, destination_evidence.ino,
                    destination_evidence.ctime_ns, destination_evidence.size,
                    destination_evidence.mtime_ns, file_id,
                ),
            )
        conn.execute(
            """
            UPDATE review_items SET queue_path = NULL,
                state = CASE WHEN state = 'deferred' THEN 'pending' ELSE state END,
                updated_at = CURRENT_TIMESTAMP
            WHERE candidate_file_id = ? AND queue_path = ?
            """,
            (file_id, str(queue_path)),
        )
        transition_operation(conn, operation_id, "db_done")
    with transaction(conn):
        transition_operation(conn, operation_id, "committed")
    return {
        "operation_id": operation_id,
        "source_path": str(queue_path),
        "dest_path": str(destination),
    }


def _decided_same_content_pair(conn: sqlite3.Connection, review_id: int):
    row = conn.execute(
        """
        SELECT ri.review_id, ri.state, ri.decision_id,
               ri.candidate_file_id, ri.reference_file_id,
               d.verdict, d.left_file_id, d.right_file_id,
               d.left_fingerprint_id, d.right_fingerprint_id
        FROM review_items AS ri
        JOIN decisions AS d ON d.decision_id = ri.decision_id AND d.active = 1
        WHERE ri.review_id = ?
        """,
        (review_id,),
    ).fetchone()
    if row is None or row["state"] != "decided" or row["verdict"] != "same_content":
        raise RuntimeError("human quarantine requires an active same_content decision")
    pair = (row["candidate_file_id"], row["reference_file_id"])
    files = conn.execute(
        """
        SELECT f.*, CASE WHEN r.file_id IS NULL THEN 0 ELSE 1 END AS representative
        FROM files AS f LEFT JOIN representatives AS r ON r.file_id = f.file_id
        WHERE f.file_id IN (?, ?) AND f.active = 1
        """,
        pair,
    ).fetchall()
    if len(files) != 2:
        raise RuntimeError("same_content decision pair is not fully active")
    representatives = [file_row for file_row in files if file_row["representative"]]
    if len(representatives) != 1:
        raise RuntimeError("same_content decision must have exactly one representative")
    keep = representatives[0]
    discard = next(file_row for file_row in files if file_row["file_id"] != keep["file_id"])
    if keep["variant_id"] != discard["variant_id"]:
        raise RuntimeError("same_content decision files no longer share a variant")
    expected = {
        row["left_file_id"]: row["left_fingerprint_id"],
        row["right_file_id"]: row["right_fingerprint_id"],
    }
    if (
        keep["current_fingerprint_id"] != expected.get(keep["file_id"])
        or discard["current_fingerprint_id"] != expected.get(discard["file_id"])
    ):
        raise RuntimeError("same_content decision fingerprint is stale")
    if discard["protected"]:
        raise RuntimeError("protected non-representative cannot be quarantined")
    open_relations = conn.execute(
        """
        SELECT COUNT(*) FROM review_items
        WHERE review_id != ? AND state IN ('pending', 'deferred')
          AND (candidate_file_id = ? OR reference_file_id = ?)
        """,
        (review_id, discard["file_id"], discard["file_id"]),
    ).fetchone()[0]
    if open_relations:
        raise RuntimeError(
            f"discard file still has {open_relations} open review relation(s)"
        )
    return row, discard, keep


def preview_decided_review_disposition(conn: sqlite3.Connection, review_id: int):
    _, discard, keep = _decided_same_content_pair(conn, review_id)
    return {
        "action": "human_quarantine",
        "review_id": review_id,
        "discard_file_id": discard["file_id"],
        "discard_path": discard["canonical_path"],
        "keep_file_id": keep["file_id"],
        "keep_path": keep["canonical_path"],
    }


def quarantine_decided_review(conn: sqlite3.Connection, review_id: int):
    from mutation_io import mutation_lock

    _assert_no_active_actual_run(conn)
    with mutation_lock(conn, f"human_quarantine:{review_id}"):
        return _quarantine_decided_review(conn, review_id)


def _quarantine_decided_review(conn: sqlite3.Connection, review_id: int):
    from dedup_mutations import _unique_destination
    from mutation_io import evidence_matches, inspect_regular_file

    _, discard, keep = _decided_same_content_pair(conn, review_id)
    origin = conn.execute(
        """
        SELECT * FROM operations
        WHERE file_id IN (?, ?) AND action IN (
            'suspected_move', 'warning_move', 'house_review_move'
        ) AND state = 'committed'
        ORDER BY operation_id DESC LIMIT 1
        """,
        (discard["file_id"], keep["file_id"]),
    ).fetchone()
    if origin is None:
        raise RuntimeError("human quarantine requires a managed review-queue origin")
    actual_run = _actual_run_for_operation(conn, origin["run_id"])
    source_path = Path(discard["canonical_path"])
    keep_path = Path(keep["canonical_path"])
    source_root = "house_root" if discard["source"] == "house" else "temp_root"
    keep_root = "house_root" if keep["source"] == "house" else "temp_root"
    assert_actual_run_path(actual_run, source_path, source_root)
    assert_actual_run_path(actual_run, keep_path, keep_root)
    quarantine_dir = Path(actual_run["temp_root"]) / "trash_bin" / "human_quarantine"
    assert_actual_run_path(actual_run, quarantine_dir, "temp_root")
    if not source_path.is_file() or not keep_path.is_file():
        raise RuntimeError("same_content disposition paths are stale")
    source_evidence = inspect_regular_file(source_path)
    keep_evidence = inspect_regular_file(keep_path)
    if (
        source_evidence.size != discard["size"]
        or source_evidence.mtime_ns != discard["mtime_ns"]
    ):
        raise RuntimeError("same_content discard snapshot is stale")
    destination = _unique_destination(quarantine_dir, source_path.name)
    with transaction(conn):
        operation_id = create_operation(
            conn,
            run_id=origin["run_id"],
            action="human_quarantine",
            source_path=str(source_path),
            quarantine_path=str(destination),
            file_id=discard["file_id"],
            keep_file_id=keep["file_id"],
            expected_size=discard["size"],
            expected_mtime_ns=discard["mtime_ns"],
            expected_fingerprint_id=discard["current_fingerprint_id"],
            expected_keep_fingerprint_id=keep["current_fingerprint_id"],
            source_dev=source_evidence.dev,
            source_ino=source_evidence.ino,
            source_ctime_ns=source_evidence.ctime_ns,
            source_sha256=source_evidence.sha256,
        )

    def guard():
        _, current_discard, current_keep = _decided_same_content_pair(conn, review_id)
        if (
            current_discard["file_id"] != discard["file_id"]
            or current_keep["file_id"] != keep["file_id"]
            or not evidence_matches(inspect_regular_file(keep_path), keep_evidence)
        ):
            raise RuntimeError("same_content disposition guard changed")

    destination_evidence = copy_record_consume_operation(
        conn, operation_id, source_path, destination, source_evidence, guard=guard
    )
    with transaction(conn):
        conn.execute(
            """
            UPDATE files SET canonical_path = ?, source = 'quarantine', active = 0,
                dev = ?, ino = ?, ctime_ns = ?, size = ?, mtime_ns = ?
            WHERE file_id = ?
            """,
            (
                str(destination), destination_evidence.dev, destination_evidence.ino,
                destination_evidence.ctime_ns, destination_evidence.size,
                destination_evidence.mtime_ns, discard["file_id"],
            ),
        )
        transition_operation(conn, operation_id, "db_done")
    with transaction(conn):
        transition_operation(conn, operation_id, "committed")
    return {
        "operation_id": operation_id,
        "action": "human_quarantine",
        "discard_file_id": discard["file_id"],
        "keep_file_id": keep["file_id"],
        "dest_path": str(destination),
    }


def find_external_rename_candidate(
    conn: sqlite3.Connection,
    *,
    raw_sha256: str,
    size: int,
) -> Optional[str]:
    """Return one missing active file id, never guess among same-hash copies."""
    rows = conn.execute(
        """
        SELECT f.file_id, f.canonical_path
        FROM files AS f
        JOIN fingerprints AS fp ON fp.fingerprint_id = f.current_fingerprint_id
        WHERE f.active = 1 AND f.size = ? AND fp.raw_sha256 = ?
        """,
        (size, raw_sha256),
    ).fetchall()
    missing = [row[0] for row in rows if not os.path.exists(row[1])]
    return missing[0] if len(missing) == 1 else None


def _symlink_component(path: os.PathLike | str):
    current = Path(os.path.abspath(os.fspath(path)))
    for component in (current, *current.parents):
        if component.is_symlink():
            return component
    return None


def _active_file_with_fingerprint(conn, file_id: str):
    row = conn.execute(
        """
        SELECT f.*, fp.fingerprint_id
        FROM files AS f
        LEFT JOIN fingerprints AS fp ON fp.fingerprint_id = f.current_fingerprint_id
        WHERE f.file_id = ? AND f.active = 1
        """,
        (file_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"active file not found: {file_id}")
    if row["current_fingerprint_id"] is None:
        raise ValueError(f"current fingerprint missing: {file_id}")
    if not os.path.isfile(row["canonical_path"]):
        raise ValueError(f"file path missing: {row['canonical_path']}")
    stat = os.stat(row["canonical_path"], follow_symlinks=False)
    if (
        stat.st_size != row["size"]
        or stat.st_mtime_ns != row["mtime_ns"]
        or (row["dev"] is not None and stat.st_dev != row["dev"])
        or (row["ino"] is not None and stat.st_ino != row["ino"])
        or (row["ctime_ns"] is not None and stat.st_ctime_ns != row["ctime_ns"])
    ):
        raise ValueError(f"file snapshot is stale: {file_id}")
    return row


def add_review_item(
    conn: sqlite3.Connection,
    *,
    candidate_file_id: str,
    reference_file_id: str,
    classification: str,
    queue_path: Optional[str] = None,
    evidence_json: Optional[str] = None,
) -> int:
    if candidate_file_id == reference_file_id:
        raise ValueError("candidate and reference must differ")
    with transaction(conn):
        candidate = _active_file_with_fingerprint(conn, candidate_file_id)
        reference = _active_file_with_fingerprint(conn, reference_file_id)
        cursor = conn.execute(
            """
            INSERT INTO review_items(
                candidate_file_id, reference_file_id,
                left_fingerprint_id, right_fingerprint_id,
                classification, state, queue_path, evidence_json
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                candidate_file_id,
                reference_file_id,
                candidate["current_fingerprint_id"],
                reference["current_fingerprint_id"],
                classification,
                queue_path,
                evidence_json,
            ),
        )
        return cursor.lastrowid


def set_review_state(conn, review_id: int, state: str) -> None:
    if state not in {"pending", "deferred"}:
        raise ValueError("manual review state must be pending or deferred")
    with transaction(conn):
        row = conn.execute(
            "SELECT state, decision_id FROM review_items WHERE review_id = ?", (review_id,)
        ).fetchone()
        if row is None:
            raise KeyError(review_id)
        if row["state"] in {"decided", "superseded"}:
            raise RuntimeError(f"closed review cannot be reopened directly: {row['state']}")
        conn.execute(
            """
            UPDATE review_items SET state = ?, updated_at = CURRENT_TIMESTAMP
            WHERE review_id = ?
            """,
            (state, review_id),
        )


def supersede_open_reviews_for_file(
    conn: sqlite3.Connection, file_id: str, *, reason: str
) -> int:
    """Close open review edges after an explicit file-level human disposition."""
    rows = conn.execute(
        """
        SELECT review_id, evidence_json FROM review_items
        WHERE state IN ('pending', 'deferred')
          AND (candidate_file_id = ? OR reference_file_id = ?)
        """,
        (file_id, file_id),
    ).fetchall()
    for row in rows:
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            evidence = {"previous_evidence": row["evidence_json"]}
        evidence["human_disposition"] = reason
        conn.execute(
            """
            UPDATE review_items SET state = 'superseded', decision_id = NULL,
                evidence_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE review_id = ?
            """,
            (json.dumps(evidence, ensure_ascii=False, sort_keys=True), row["review_id"]),
        )
    return len(rows)


def supersede_open_reviews_for_inactive_file(
    conn: sqlite3.Connection, file_id: str, *, reason: str
) -> int:
    """Close review edges that cannot be acted on after an automatic quarantine."""
    rows = conn.execute(
        """
        SELECT review_id, evidence_json FROM review_items
        WHERE state IN ('pending', 'deferred')
          AND (candidate_file_id = ? OR reference_file_id = ?)
        """,
        (file_id, file_id),
    ).fetchall()
    for row in rows:
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            evidence = {"previous_evidence": row["evidence_json"]}
        evidence["automatic_suppression"] = {
            "reason": reason,
            "file_id": file_id,
        }
        conn.execute(
            """
            UPDATE review_items SET state = 'superseded', decision_id = NULL,
                evidence_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE review_id = ?
            """,
            (json.dumps(evidence, ensure_ascii=False, sort_keys=True), row["review_id"]),
        )
    return len(rows)


def record_human_restore_disposition(
    conn: sqlite3.Connection, file_id: str,
    *, reason: str = "user_selected_restore",
) -> int:
    """Atomically close current review edges with their approved raw bytes.

    This is the post-mutation form used by Folderling's action inbox.  All
    filesystem evidence is collected and checked before any review state is
    changed, so a partial batch cannot leave a superseded edge without its
    byte-bound suppression snapshot.
    """
    from mutation_io import inspect_regular_file

    rows = conn.execute(
        """
        SELECT ri.review_id, ri.evidence_json,
               ri.candidate_file_id, ri.reference_file_id,
               c.canonical_path AS candidate_path,
               c.dev AS candidate_dev, c.ino AS candidate_ino,
               c.ctime_ns AS candidate_ctime_ns, c.size AS candidate_size,
               c.mtime_ns AS candidate_mtime_ns,
               r.canonical_path AS reference_path,
               r.dev AS reference_dev, r.ino AS reference_ino,
               r.ctime_ns AS reference_ctime_ns, r.size AS reference_size,
               r.mtime_ns AS reference_mtime_ns
        FROM review_items AS ri
        JOIN files AS c ON c.file_id = ri.candidate_file_id AND c.active = 1
        JOIN files AS r ON r.file_id = ri.reference_file_id AND r.active = 1
        WHERE ri.state IN ('pending', 'deferred')
          AND (ri.candidate_file_id = ? OR ri.reference_file_id = ?)
        """,
        (file_id, file_id),
    ).fetchall()
    prepared = []
    evidence_by_file = {}
    for row in rows:
        for prefix, endpoint_id in (
            ("candidate", row["candidate_file_id"]),
            ("reference", row["reference_file_id"]),
        ):
            if endpoint_id in evidence_by_file:
                continue
            evidence = inspect_regular_file(row[f"{prefix}_path"])
            expected = (
                row[f"{prefix}_dev"], row[f"{prefix}_ino"],
                row[f"{prefix}_ctime_ns"], row[f"{prefix}_size"],
                row[f"{prefix}_mtime_ns"],
            )
            actual = (
                evidence.dev, evidence.ino, evidence.ctime_ns,
                evidence.size, evidence.mtime_ns,
            )
            if expected != actual:
                raise RuntimeError(
                    f"human disposition endpoint is stale: {row[f'{prefix}_path']}"
                )
            evidence_by_file[endpoint_id] = evidence.sha256
        try:
            payload = json.loads(row["evidence_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {"previous_evidence": row["evidence_json"]}
        payload["human_disposition"] = reason
        payload["human_disposition_raw_sha256"] = {
            row["candidate_file_id"]: evidence_by_file[row["candidate_file_id"]],
            row["reference_file_id"]: evidence_by_file[row["reference_file_id"]],
        }
        prepared.append((row["review_id"], payload))

    with transaction(conn):
        for review_id, payload in prepared:
            updated = conn.execute(
                """
                UPDATE review_items SET state = 'superseded', decision_id = NULL,
                    evidence_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE review_id = ? AND state IN ('pending', 'deferred')
                """,
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), review_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"human disposition review changed: {review_id}")
    return len(prepared)


def stamp_superseded_human_disposition_snapshots(
    conn: sqlite3.Connection, *, reason: str = "user_selected_restore"
) -> int:
    """Bind a file-pair human disposition to the currently approved bytes.

    Queue-to-house restore changes path/inode identity, so a later auditor run
    legitimately creates new immutable fingerprint rows.  The human decision
    remains reusable only while both stable file IDs still have the exact raw
    bytes approved here.
    """
    from mutation_io import inspect_regular_file

    rows = conn.execute(
        """
        SELECT ri.review_id, ri.candidate_file_id, ri.reference_file_id,
               ri.evidence_json,
               c.canonical_path AS candidate_path,
               c.size AS candidate_size, c.mtime_ns AS candidate_mtime_ns,
               c.dev AS candidate_dev, c.ino AS candidate_ino,
               c.ctime_ns AS candidate_ctime_ns,
               r.canonical_path AS reference_path,
               r.size AS reference_size, r.mtime_ns AS reference_mtime_ns,
               r.dev AS reference_dev, r.ino AS reference_ino,
               r.ctime_ns AS reference_ctime_ns
        FROM review_items AS ri
        JOIN files AS c ON c.file_id = ri.candidate_file_id AND c.active = 1
        JOIN files AS r ON r.file_id = ri.reference_file_id AND r.active = 1
        WHERE ri.state = 'superseded'
        """
    ).fetchall()
    approved = []
    evidence_by_file = {}
    for row in rows:
        try:
            payload = json.loads(row["evidence_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if payload.get("human_disposition") != reason:
            continue
        approved.append((row, payload))
        for prefix, file_id in (
            ("candidate", row["candidate_file_id"]),
            ("reference", row["reference_file_id"]),
        ):
            if file_id in evidence_by_file:
                continue
            current = inspect_regular_file(row[f"{prefix}_path"])
            expected = (
                row[f"{prefix}_dev"], row[f"{prefix}_ino"],
                row[f"{prefix}_ctime_ns"], row[f"{prefix}_size"],
                row[f"{prefix}_mtime_ns"],
            )
            actual = (
                current.dev, current.ino, current.ctime_ns,
                current.size, current.mtime_ns,
            )
            if expected != actual:
                raise RuntimeError(
                    f"human disposition snapshot is stale: {row[f'{prefix}_path']}"
                )
            evidence_by_file[file_id] = current.sha256

    with transaction(conn):
        for row, payload in approved:
            payload["human_disposition_raw_sha256"] = {
                row["candidate_file_id"]: evidence_by_file[row["candidate_file_id"]],
                row["reference_file_id"]: evidence_by_file[row["reference_file_id"]],
            }
            conn.execute(
                """
                UPDATE review_items SET evidence_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE review_id = ?
                """,
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), row["review_id"]),
            )
    return len(approved)


def human_disposition_suppresses_review(
    conn: sqlite3.Connection,
    *,
    candidate_file_id: str,
    reference_file_id: str,
    candidate_raw_sha256: Optional[str],
    reference_raw_sha256: Optional[str],
) -> bool:
    """Return true only for the same stable pair and the approved raw bytes."""
    if not candidate_raw_sha256 or not reference_raw_sha256:
        return False
    rows = conn.execute(
        """
        SELECT evidence_json FROM review_items
        WHERE state = 'superseded'
          AND ((candidate_file_id = ? AND reference_file_id = ?)
            OR (candidate_file_id = ? AND reference_file_id = ?))
        """,
        (
            candidate_file_id, reference_file_id,
            reference_file_id, candidate_file_id,
        ),
    ).fetchall()
    current = {
        candidate_file_id: candidate_raw_sha256,
        reference_file_id: reference_raw_sha256,
    }
    for row in rows:
        try:
            payload = json.loads(row["evidence_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if payload.get("human_disposition") != "user_selected_restore":
            continue
        if payload.get("human_disposition_raw_sha256") == current:
            return True
    return False


def list_review_items(
    conn,
    state: Optional[str] = None,
    classification: Optional[str] = None,
    file_id: Optional[str] = None,
):
    if state is not None and state not in REVIEW_STATES:
        raise ValueError(f"unknown review state: {state}")
    query = """
        SELECT ri.*,
               cf.canonical_path AS candidate_path,
               cf.source AS candidate_source,
               cf.active AS candidate_active,
               rf.canonical_path AS reference_path,
               rf.source AS reference_source,
               rf.active AS reference_active
        FROM review_items AS ri
        JOIN files AS cf ON cf.file_id = ri.candidate_file_id
        JOIN files AS rf ON rf.file_id = ri.reference_file_id
    """
    clauses = []
    params = []
    if state is not None:
        clauses.append("ri.state = ?")
        params.append(state)
    if classification is not None:
        clauses.append("ri.classification = ?")
        params.append(classification)
    if file_id is not None:
        clauses.append("(ri.candidate_file_id = ? OR ri.reference_file_id = ?)")
        params.extend((file_id, file_id))
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY ri.review_id"
    return conn.execute(query, tuple(params)).fetchall()


def preview_review_pair(conn, candidate_file_id: str, reference_file_id: str):
    if candidate_file_id == reference_file_id:
        raise ValueError("candidate and reference must differ")
    candidate = _active_file_with_fingerprint(conn, candidate_file_id)
    reference = _active_file_with_fingerprint(conn, reference_file_id)
    return {
        "candidate_file_id": candidate_file_id,
        "candidate_path": candidate["canonical_path"],
        "candidate_fingerprint_id": candidate["current_fingerprint_id"],
        "candidate_state": candidate["assignment_state"],
        "reference_file_id": reference_file_id,
        "reference_path": reference["canonical_path"],
        "reference_fingerprint_id": reference["current_fingerprint_id"],
        "reference_state": reference["assignment_state"],
    }


def _validate_review_for_decision(
    conn,
    review_id,
    candidate_file_id,
    reference_file_id,
):
    review = conn.execute(
        "SELECT * FROM review_items WHERE review_id = ?", (review_id,)
    ).fetchone()
    if review is None:
        raise KeyError(review_id)
    if review["state"] not in {"pending", "deferred"}:
        raise RuntimeError(f"review is already closed: {review['state']}")
    if (
        review["candidate_file_id"] != candidate_file_id
        or review["reference_file_id"] != reference_file_id
    ):
        raise ValueError("review pair does not match candidate/reference")
    candidate = _active_file_with_fingerprint(conn, candidate_file_id)
    reference = _active_file_with_fingerprint(conn, reference_file_id)
    if candidate["current_fingerprint_id"] != review["left_fingerprint_id"]:
        raise ValueError("candidate fingerprint changed after review")
    if reference["current_fingerprint_id"] != review["right_fingerprint_id"]:
        raise ValueError("reference fingerprint changed after review")
    return review, candidate, reference


def _work_for_variant(conn, variant_id):
    row = conn.execute(
        "SELECT work_bucket_id FROM variants WHERE variant_id = ?", (variant_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"variant not found: {variant_id}")
    return row[0]


def _new_work_variant(conn, title, variant_kind="base"):
    work_id = conn.execute(
        "INSERT INTO works(display_title) VALUES (?)", (title,)
    ).lastrowid
    variant_id = conn.execute(
        "INSERT INTO variants(work_bucket_id, variant_kind) VALUES (?, ?)",
        (work_id, variant_kind),
    ).lastrowid
    return work_id, variant_id


def _make_managed(conn, file_id, variant_id, *, protected):
    conn.execute(
        """
        UPDATE files
        SET variant_id = ?, assignment_state = 'managed',
            assignment_origin = 'human_decision', protected = ?
        WHERE file_id = ?
        """,
        (variant_id, 1 if protected else 0, file_id),
    )


def _ensure_representative(conn, variant_id, file_id):
    current = conn.execute(
        "SELECT file_id FROM representatives WHERE variant_id = ?", (variant_id,)
    ).fetchone()
    if current is None:
        conn.execute(
            "INSERT INTO representatives(variant_id, file_id) VALUES (?, ?)",
            (variant_id, file_id),
        )
    elif current[0] != file_id:
        raise RuntimeError(f"variant already has another representative: {variant_id}")


def _ensure_collision_members(conn, reference_variant_id, candidate_variant_id, core_key):
    row = conn.execute(
        """
        SELECT group_id FROM collision_members
        WHERE variant_id = ? ORDER BY group_id LIMIT 1
        """,
        (reference_variant_id,),
    ).fetchone()
    if row is None:
        group_id = conn.execute(
            "INSERT INTO collision_groups(core_key) VALUES (?)", (core_key,)
        ).lastrowid
        conn.execute(
            "INSERT INTO collision_members(group_id, variant_id, display_disambig) VALUES (?, ?, 1)",
            (group_id, reference_variant_id),
        )
    else:
        group_id = row[0]
    exists = conn.execute(
        "SELECT 1 FROM collision_members WHERE group_id = ? AND variant_id = ?",
        (group_id, candidate_variant_id),
    ).fetchone()
    if exists is None:
        next_display = conn.execute(
            "SELECT COALESCE(MAX(display_disambig), 0) + 1 FROM collision_members WHERE group_id = ?",
            (group_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO collision_members(group_id, variant_id, display_disambig) VALUES (?, ?, ?)",
            (group_id, candidate_variant_id, next_display),
        )
    return group_id


def _managed_relationship_matches(conn, candidate, reference, verdict):
    candidate_variant = candidate["variant_id"]
    reference_variant = reference["variant_id"]
    if candidate_variant is None or reference_variant is None:
        return False
    candidate_work = _work_for_variant(conn, candidate_variant)
    reference_work = _work_for_variant(conn, reference_variant)
    if verdict == "same_content":
        return candidate_variant == reference_variant
    if verdict == "same_work_distinct_variant":
        return candidate_work == reference_work and candidate_variant != reference_variant
    return candidate_work != reference_work


def _assert_no_active_actual_run(conn, *, allowed_active_run_id=None):
    row = conn.execute(
        "SELECT run_id FROM actual_runs WHERE state = 'active' LIMIT 1"
    ).fetchone()
    if row is not None and row["run_id"] != allowed_active_run_id:
        raise RuntimeError(
            f"human decision state cannot change during active run: {row['run_id']}"
        )


def apply_decision(
    conn: sqlite3.Connection,
    *,
    review_id: int,
    candidate_file_id: str,
    reference_file_id: str,
    verdict: str,
    variant_kind: str = "other",
    note: Optional[str] = None,
    supersedes_decision_id: Optional[int] = None,
    allowed_active_run_id: Optional[str] = None,
) -> int:
    _assert_no_active_actual_run(conn, allowed_active_run_id=allowed_active_run_id)
    if verdict not in FINAL_VERDICTS:
        raise ValueError(f"unknown final verdict: {verdict}")
    if variant_kind not in {"base", "revision", "adult", "translation", "other"}:
        raise ValueError(f"unknown variant kind: {variant_kind}")

    transaction_scope = nullcontext(conn) if conn.in_transaction else transaction(conn)
    with transaction_scope:
        review, candidate, reference = _validate_review_for_decision(
            conn, review_id, candidate_file_id, reference_file_id
        )
        candidate_state = candidate["assignment_state"]
        reference_state = reference["assignment_state"]

        if candidate_state == "managed" and reference_state == "managed":
            refreshed_reference = conn.execute(
                "SELECT * FROM files WHERE file_id = ?", (reference_file_id,)
            ).fetchone()
            if not _managed_relationship_matches(conn, candidate, refreshed_reference, verdict):
                raise RuntimeError("verdict conflicts with existing managed identities")
            candidate_variant = candidate["variant_id"]
            candidate_work = _work_for_variant(conn, candidate_variant)
            reference_variant = reference["variant_id"]
            reference_work = _work_for_variant(conn, reference_variant)
        else:
            if candidate_state == "managed":
                anchor, anchor_file_id = candidate, candidate_file_id
                subject, subject_file_id = reference, reference_file_id
                subject_is_candidate = False
            else:
                anchor, anchor_file_id = reference, reference_file_id
                subject, subject_file_id = candidate, candidate_file_id
                subject_is_candidate = True

            if anchor["assignment_state"] == "managed":
                representative = conn.execute(
                    """
                    SELECT r.variant_id FROM representatives AS r
                    JOIN files AS f ON f.file_id = r.file_id
                    WHERE r.variant_id = ? AND f.active = 1
                      AND f.assignment_state = 'managed'
                    """,
                    (anchor["variant_id"],),
                ).fetchone()
                if representative is None:
                    raise RuntimeError("managed anchor variant has no active representative")
                anchor_variant = anchor["variant_id"]
                anchor_work = _work_for_variant(conn, anchor_variant)
            else:
                anchor_title = Path(anchor["canonical_path"]).stem
                anchor_work, anchor_variant = _new_work_variant(
                    conn, anchor_title, "base"
                )
                _make_managed(conn, anchor_file_id, anchor_variant, protected=True)
                _ensure_representative(conn, anchor_variant, anchor_file_id)

            if verdict == "same_content":
                subject_work, subject_variant = anchor_work, anchor_variant
                _make_managed(conn, subject_file_id, subject_variant, protected=False)
            elif verdict == "same_work_distinct_variant":
                subject_work = anchor_work
                subject_variant = conn.execute(
                    "INSERT INTO variants(work_bucket_id, variant_kind) VALUES (?, ?)",
                    (subject_work, variant_kind),
                ).lastrowid
                _make_managed(conn, subject_file_id, subject_variant, protected=True)
                _ensure_representative(conn, subject_variant, subject_file_id)
            else:
                subject_title = Path(subject["canonical_path"]).stem
                subject_work, subject_variant = _new_work_variant(
                    conn, subject_title, "base"
                )
                _make_managed(conn, subject_file_id, subject_variant, protected=True)
                _ensure_representative(conn, subject_variant, subject_file_id)

            if subject_is_candidate:
                candidate_work, candidate_variant = subject_work, subject_variant
                reference_work, reference_variant = anchor_work, anchor_variant
            else:
                candidate_work, candidate_variant = anchor_work, anchor_variant
                reference_work, reference_variant = subject_work, subject_variant

        if verdict in {"same_work_distinct_variant", "distinct_work"}:
            from normalizer import analyze_name

            core_key = analyze_name(Path(reference["canonical_path"]).name)["core_title"]
            _ensure_collision_members(
                conn, reference_variant, candidate_variant, core_key or "collision"
            )

        if candidate_file_id < reference_file_id:
            left_file, right_file = candidate_file_id, reference_file_id
            left_fp, right_fp = review["left_fingerprint_id"], review["right_fingerprint_id"]
            left_work, right_work = candidate_work, reference_work
            left_variant, right_variant = candidate_variant, reference_variant
        else:
            left_file, right_file = reference_file_id, candidate_file_id
            left_fp, right_fp = review["right_fingerprint_id"], review["left_fingerprint_id"]
            left_work, right_work = reference_work, candidate_work
            left_variant, right_variant = reference_variant, candidate_variant

        previous = conn.execute(
            """
            SELECT decision_id FROM decisions
            WHERE left_file_id = ? AND right_file_id = ? AND active = 1
            """,
            (left_file, right_file),
        ).fetchone()
        supersedes = supersedes_decision_id if supersedes_decision_id is not None else (
            previous[0] if previous else None
        )
        if previous:
            conn.execute(
                "UPDATE decisions SET active = 0 WHERE decision_id = ?", (previous[0],)
            )
        decision_id = conn.execute(
            """
            INSERT INTO decisions(
                left_work_id, left_variant_id, right_work_id, right_variant_id,
                left_file_id, right_file_id, left_fingerprint_id, right_fingerprint_id,
                verdict, note, supersedes_decision_id, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                left_work,
                left_variant,
                right_work,
                right_variant,
                left_file,
                right_file,
                left_fp,
                right_fp,
                verdict,
                note,
                supersedes,
            ),
        ).lastrowid
        conn.execute(
            """
            UPDATE review_items
            SET state = 'decided', decision_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE review_id = ?
            """,
            (decision_id, review_id),
        )
        return decision_id


def _reset_isolated_decision_pair(conn, decision_id: int):
    decision = conn.execute(
        "SELECT * FROM decisions WHERE decision_id = ? AND active = 1", (decision_id,)
    ).fetchone()
    if decision is None:
        raise ValueError("active decision not found")
    pair = (decision["left_file_id"], decision["right_file_id"])
    disposition = conn.execute(
        """
        SELECT 1 FROM operations
        WHERE action = 'human_quarantine' AND state = 'committed'
          AND file_id IN (?, ?)
        LIMIT 1
        """,
        pair,
    ).fetchone()
    if disposition is not None:
        raise RuntimeError(
            "decision correction is blocked after committed human quarantine"
        )
    placeholders = ",".join("?" for _ in pair)
    related = conn.execute(
        f"""
        SELECT COUNT(*) FROM decisions
        WHERE NOT (left_file_id = ? AND right_file_id = ?) AND (
            left_file_id IN ({placeholders}) OR right_file_id IN ({placeholders})
        )
        """,
        (*pair, *pair, *pair),
    ).fetchone()[0]
    if related:
        raise RuntimeError("decision correction requires an isolated first-decision pair")
    files = conn.execute(
        f"SELECT file_id, variant_id FROM files WHERE file_id IN ({placeholders})", pair
    ).fetchall()
    if len(files) != 2 or any(row["variant_id"] is None for row in files):
        raise RuntimeError("decision pair identities are incomplete")
    variants = sorted({row["variant_id"] for row in files})
    variant_marks = ",".join("?" for _ in variants)
    outsiders = conn.execute(
        f"SELECT COUNT(*) FROM files WHERE variant_id IN ({variant_marks}) AND file_id NOT IN ({placeholders})",
        (*variants, *pair),
    ).fetchone()[0]
    if outsiders:
        raise RuntimeError("decision identities are shared with other files")

    group_ids = [row[0] for row in conn.execute(
        f"SELECT DISTINCT group_id FROM collision_members WHERE variant_id IN ({variant_marks})",
        variants,
    )]
    conn.execute(
        f"DELETE FROM collision_members WHERE variant_id IN ({variant_marks})", variants
    )
    conn.execute(f"DELETE FROM representatives WHERE variant_id IN ({variant_marks})", variants)
    conn.execute(
        f"""
        UPDATE files SET variant_id = NULL, assignment_state = 'unassigned',
            assignment_origin = NULL, protected = 0
        WHERE file_id IN ({placeholders})
        """,
        pair,
    )
    for group_id in group_ids:
        conn.execute(
            "DELETE FROM collision_groups WHERE group_id = ? AND NOT EXISTS "
            "(SELECT 1 FROM collision_members WHERE group_id = ?)",
            (group_id, group_id),
        )
    conn.execute("UPDATE decisions SET active = 0 WHERE decision_id = ?", (decision_id,))
    review = conn.execute(
        "SELECT review_id FROM review_items WHERE decision_id = ? ORDER BY review_id DESC LIMIT 1",
        (decision_id,),
    ).fetchone()
    if review is None:
        raise RuntimeError("decision provenance review is missing")
    conn.execute(
        "UPDATE review_items SET state = 'pending', decision_id = NULL, updated_at = CURRENT_TIMESTAMP "
        "WHERE review_id = ?",
        (review["review_id"],),
    )
    return decision, review["review_id"]


def cancel_decision(conn: sqlite3.Connection, decision_id: int) -> int:
    """Cancel an isolated first decision without erasing its immutable history."""
    _assert_no_active_actual_run(conn)
    with transaction(conn):
        _, review_id = _reset_isolated_decision_pair(conn, decision_id)
        return review_id


def correct_decision(
    conn: sqlite3.Connection,
    *,
    decision_id: int,
    verdict: str,
    variant_kind: str = "other",
    note: Optional[str] = None,
) -> int:
    """Atomically replace an isolated first decision and link its history."""
    _assert_no_active_actual_run(conn)
    with transaction(conn):
        old, review_id = _reset_isolated_decision_pair(conn, decision_id)
        return apply_decision(
            conn,
            review_id=review_id,
            candidate_file_id=conn.execute(
                "SELECT candidate_file_id FROM review_items WHERE review_id = ?", (review_id,)
            ).fetchone()[0],
            reference_file_id=conn.execute(
                "SELECT reference_file_id FROM review_items WHERE review_id = ?", (review_id,)
            ).fetchone()[0],
            verdict=verdict,
            variant_kind=variant_kind,
            note=note,
            supersedes_decision_id=old["decision_id"],
        )


def preview_decision(
    conn: sqlite3.Connection,
    *,
    review_id: int,
    candidate_file_id: str,
    reference_file_id: str,
    verdict: str,
):
    if verdict not in FINAL_VERDICTS:
        raise ValueError(f"unknown final verdict: {verdict}")
    review, candidate, reference = _validate_review_for_decision(
        conn, review_id, candidate_file_id, reference_file_id
    )
    return {
        "review_id": review_id,
        "classification": review["classification"],
        "verdict": verdict,
        "candidate_file_id": candidate_file_id,
        "candidate_path": candidate["canonical_path"],
        "candidate_state": candidate["assignment_state"],
        "reference_file_id": reference_file_id,
        "reference_path": reference["canonical_path"],
        "reference_state": reference["assignment_state"],
        "candidate_fingerprint_id": candidate["current_fingerprint_id"],
        "reference_fingerprint_id": reference["current_fingerprint_id"],
    }


def set_file_protected(conn, file_id: str, protected: bool) -> None:
    _assert_no_active_actual_run(conn)
    with transaction(conn):
        row = conn.execute(
            "SELECT assignment_state FROM files WHERE file_id = ? AND active = 1", (file_id,)
        ).fetchone()
        if row is None:
            raise KeyError(file_id)
        if row["assignment_state"] != "managed":
            raise RuntimeError("only managed files can change protected state")
        representative = conn.execute(
            "SELECT 1 FROM representatives WHERE file_id = ?", (file_id,)
        ).fetchone()
        if representative and not protected:
            raise RuntimeError("representative protection cannot be removed")
        conn.execute(
            "UPDATE files SET protected = ? WHERE file_id = ?",
            (1 if protected else 0, file_id),
        )


def replace_representative(conn, variant_id: int, new_file_id: str) -> None:
    _assert_no_active_actual_run(conn)
    with transaction(conn):
        row = conn.execute(
            """
            SELECT assignment_state, variant_id FROM files
            WHERE file_id = ? AND active = 1
            """,
            (new_file_id,),
        ).fetchone()
        if row is None or row["assignment_state"] != "managed" or row["variant_id"] != variant_id:
            raise RuntimeError("new representative must be an active managed file in the variant")
        cursor = conn.execute(
            "UPDATE representatives SET file_id = ?, updated_at = CURRENT_TIMESTAMP WHERE variant_id = ?",
            (new_file_id, variant_id),
        )
        if cursor.rowcount == 0:
            raise KeyError(variant_id)
        conn.execute("UPDATE files SET protected = 1 WHERE file_id = ?", (new_file_id,))
