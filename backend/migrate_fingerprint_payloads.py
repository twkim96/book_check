#!/usr/bin/env python3
"""Plan, apply, and resume the guarded schema-v19 payload migration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import decision_store
from build_info import resolve_build_info
from fingerprint_payloads import canonical_anchor_payload
from mutation_io import mutation_lock_for_roots
from project_paths import HOUSE_DIR, PROJECT_ROOT, STATE_DB, TEMP_DIR
from state_repository import _migrate_anchor_payload_schema_connection


TARGET_VERSION = "1.5.2"
JOURNAL_PREFIX = "fingerprint_payload_migration_1_5_2"
_PHASES = {
    "preparing": 0,
    "prepared": 1,
    "logical_committed": 2,
    "compacted": 3,
    "verified": 4,
    "reported": 5,
}


def _migration_failpoint(_name: str) -> None:
    """Monkeypatchable crash/failure boundary used by migration fault tests."""


def _tables(conn) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _metadata_digest(conn) -> str:
    excluded = {
        "front_anchor",
        "tail_anchor",
        "anchor_payload_state",
        "anchor_payload_hash",
    }
    columns = [
        row[1]
        for row in conn.execute("PRAGMA table_info(fingerprints)")
        if row[1] not in excluded
    ]
    digest = hashlib.sha256()
    query = "SELECT " + ", ".join(columns) + " FROM fingerprints ORDER BY fingerprint_id"
    for row in conn.execute(query):
        payload = json.dumps(
            list(row), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _git_status_porcelain(project_root: Path) -> bytes:
    try:
        return subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=project_root,
            check=True,
            capture_output=True,
            timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("unable to inspect migration source status") from exc


def _git_diff_sha256(project_root: Path) -> str:
    try:
        tracked = subprocess.run(
            ["git", "diff", "--binary", "HEAD", "--"],
            cwd=project_root,
            check=True,
            capture_output=True,
            timeout=15,
        ).stdout
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=project_root,
            check=True,
            capture_output=True,
            timeout=15,
        ).stdout.split(b"\0")
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("unable to capture migration source diff") from exc
    digest = hashlib.sha256()
    digest.update(len(tracked).to_bytes(8, "big"))
    digest.update(tracked)
    for raw_path in sorted(item for item in untracked if item):
        path = project_root / os.fsdecode(raw_path)
        if not path.is_file():
            continue
        digest.update(len(raw_path).to_bytes(8, "big"))
        digest.update(raw_path)
        digest.update(bytes.fromhex(_sha256(path)))
    return digest.hexdigest()


def _source_provenance(*, allow_dirty: bool) -> dict:
    build = resolve_build_info(str(PROJECT_ROOT))
    try:
        worktree_dirty = bool(_git_status_porcelain(PROJECT_ROOT))
    except RuntimeError:
        dirty = build.get("build_dirty")
    else:
        # The embedded/runtime build marker and the checkout are independent
        # provenance signals.  Never let a clean checkout erase an explicitly
        # dirty build marker; either source being dirty must fail closed.
        dirty = worktree_dirty or build.get("build_dirty") is True
    commit = str(build.get("build_commit") or "unknown")
    if dirty is None and not allow_dirty:
        raise RuntimeError(
            "migration build cleanliness is unknown; use a clean release checkout"
        )
    if dirty is True and not allow_dirty:
        raise RuntimeError(
            "migration refuses a dirty working tree; commit the release or pass "
            "--allow-dirty to record an emergency source diff"
        )
    if commit == "unknown" and not allow_dirty:
        raise RuntimeError("migration build commit is unknown")
    source_diff_sha256 = _git_diff_sha256(PROJECT_ROOT) if dirty else None
    return {
        "build_commit": commit,
        "build_dirty": dirty,
        "allow_dirty": bool(allow_dirty),
        "source_diff_sha256": source_diff_sha256,
        "migration_script_sha256": _sha256(Path(__file__).resolve()),
        "schema_module_sha256": _sha256(
            Path(__file__).resolve().with_name("state_schema.py")
        ),
        "python_version": platform.python_version(),
        "sqlite_version": sqlite3.sqlite_version,
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return path


def _read_json(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return None


def _journal_path(state_db: Path) -> Path:
    identity = hashlib.sha256(str(state_db).encode("utf-8")).hexdigest()[:12]
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", state_db.stem)[:48] or "state"
    return state_db.parent / "reports" / (
        f"{JOURNAL_PREFIX}_{stem}_{identity}_journal.json"
    )


def _artifact_paths(state_db: Path, migration_id: str) -> tuple[Path, Path]:
    backup = state_db.parent / "backups" / (
        f"before_fingerprint_payload_v19_{migration_id}.sqlite3"
    )
    report = state_db.parent / "reports" / (
        f"fingerprint_payload_migration_1_5_2_{migration_id}.json"
    )
    return backup, report


def _validate_journal_identity(journal: dict, state_db: Path) -> None:
    if journal.get("kind") != "fingerprint_payload_migration_journal":
        raise RuntimeError("migration journal kind is invalid")
    if journal.get("state_db") != str(state_db):
        raise RuntimeError("migration journal belongs to another state DB")
    if journal.get("target_version") != TARGET_VERSION:
        raise RuntimeError("migration journal targets another release")
    if journal.get("target_schema") != decision_store.SCHEMA_VERSION:
        raise RuntimeError("migration journal targets another schema")
    if journal.get("phase") not in _PHASES:
        raise RuntimeError("migration journal phase is invalid")
    migration_id = journal.get("migration_id")
    if not isinstance(migration_id, str) or not re.fullmatch(
        r"[0-9a-f]{32}", migration_id
    ):
        raise RuntimeError("migration journal ID is invalid")
    backup, report = _artifact_paths(state_db, migration_id)
    if journal.get("backup_path") != str(backup):
        raise RuntimeError("migration journal backup path is invalid")
    if journal.get("backup_partial_path") != str(backup) + ".partial":
        raise RuntimeError("migration journal partial backup path is invalid")
    if journal.get("report_path") != str(report):
        raise RuntimeError("migration journal report path is invalid")


def _archive_stale_journal(path: Path, journal: dict) -> Path:
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archived = path.with_name(
        f"{path.stem}.stale-{suffix}-{journal['migration_id'][:8]}{path.suffix}"
    )
    os.replace(path, archived)
    _fsync_directory(path.parent)
    return archived


def _phase_at_least(journal: dict, phase: str) -> bool:
    return _PHASES[journal["phase"]] >= _PHASES[phase]


def _write_journal(path: Path, journal: dict, phase: str, **updates) -> dict:
    if phase not in _PHASES:
        raise ValueError(phase)
    updated = dict(journal)
    updated.update(updates)
    updated["phase"] = phase
    updated["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(path, updated)
    return updated


def _storage_extent(conn, state_db: Path) -> dict:
    main_bytes = state_db.stat().st_size
    wal_path = Path(f"{state_db}-wal")
    journal_path = Path(f"{state_db}-journal")
    wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
    journal_bytes = journal_path.stat().st_size if journal_path.exists() else 0
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    logical_page_bytes = page_size * page_count
    source_extent = max(
        main_bytes + wal_bytes + journal_bytes,
        logical_page_bytes,
    )
    safety_margin = 256 * 1024 * 1024
    return {
        "database_bytes": main_bytes,
        "main_bytes": main_bytes,
        "wal_bytes": wal_bytes,
        "journal_bytes": journal_bytes,
        "page_size": page_size,
        "page_count": page_count,
        "logical_page_bytes": logical_page_bytes,
        "source_extent_bytes": source_extent,
        "backup_reserve_bytes": source_extent,
        "migration_wal_reserve_bytes": source_extent,
        "vacuum_reserve_bytes": source_extent,
        "safety_margin_bytes": safety_margin,
        "required_free_bytes": source_extent * 3 + safety_margin,
    }


def _build_plan_from_connection(
    conn,
    state_db: Path,
    legacy_anchor_backup: Path | None,
    *,
    allow_dirty: bool = False,
) -> dict:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > decision_store.SCHEMA_VERSION:
        raise RuntimeError(f"state DB schema is newer than this program: {version}")
    tables = _tables(conn)
    blockers = []
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        blockers.append(f"integrity_check:{integrity}")
    foreign_key_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_rows:
        blockers.append(f"foreign_key_check:{len(foreign_key_rows)}")
    if version in (17, 18) and integrity == "ok" and not foreign_key_rows:
        doctor = decision_store.doctor_issues(
            conn,
            verify_files=True,
            check_integrity=False,
            _allow_legacy_payload_schema=True,
        )
        blockers.extend(
            f"doctor:{issue.get('kind', 'unknown')}" for issue in doctor
        )
    try:
        provenance = _source_provenance(allow_dirty=allow_dirty)
    except RuntimeError as exc:
        provenance = {"error": str(exc), "allow_dirty": bool(allow_dirty)}
        blockers.append(f"build_provenance:{exc}")
    fingerprint_count = int(
        conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
    )
    legacy = conn.execute(
        """
        SELECT COUNT(*) AS rows,
               COALESCE(SUM(
                   LENGTH(CAST(COALESCE(front_anchor, '') AS BLOB)) +
                   LENGTH(CAST(COALESCE(tail_anchor, '') AS BLOB))
               ), 0) AS bytes
        FROM fingerprints
        WHERE COALESCE(front_anchor, '') != ''
           OR COALESCE(tail_anchor, '') != ''
        """
    ).fetchone()
    if "actual_runs" in tables:
        blockers.extend(
            f"actual_run:{row[0]}:{row[1]}"
            for row in conn.execute(
                "SELECT run_id, state FROM actual_runs "
                "WHERE state IN ('approved', 'active')"
            )
        )
    if "operations" in tables:
        blockers.extend(
            f"operation:{row[0]}:{row[1]}"
            for row in conn.execute(
                "SELECT operation_id, state FROM operations "
                "WHERE state IN ('planned', 'fs_done', 'db_done')"
            )
        )
    if "operation_groups" in tables:
        blockers.extend(
            f"operation_group:{row[0]}:{row[1]}"
            for row in conn.execute(
                "SELECT group_id, state FROM operation_groups "
                "WHERE state IN ('planned', 'fs_done', 'db_done')"
            )
        )
    if version == 18 and fingerprint_count:
        if legacy_anchor_backup is None:
            blockers.append("legacy_anchor_backup_required")
        elif not legacy_anchor_backup.is_file():
            blockers.append(f"legacy_anchor_backup_missing:{legacy_anchor_backup}")
    extent = _storage_extent(conn, state_db)
    free_bytes = shutil.disk_usage(state_db.parent).free
    if free_bytes < extent["required_free_bytes"]:
        blockers.append("insufficient_free_space")
    current_stats = None
    if {"anchor_payload_objects", "fingerprint_anchor_refs"} <= tables:
        current_stats = decision_store.anchor_payload_storage_stats(conn)
    return {
        "kind": "fingerprint_payload_migration_plan",
        "target_version": TARGET_VERSION,
        "target_schema": decision_store.SCHEMA_VERSION,
        "state_db": str(state_db),
        "current_schema": version,
        **extent,
        "free_bytes": free_bytes,
        "fingerprint_count": fingerprint_count,
        "fingerprint_metadata_sha256": _metadata_digest(conn),
        "legacy_anchor_rows": int(legacy["rows"]),
        "legacy_anchor_bytes": int(legacy["bytes"]),
        "current_payload_stats": current_stats,
        "legacy_anchor_backup": (
            str(legacy_anchor_backup) if legacy_anchor_backup is not None else None
        ),
        "source_integrity": integrity,
        "source_foreign_key_issues": len(foreign_key_rows),
        "source_provenance": provenance,
        "blockers": list(dict.fromkeys(blockers)),
        "apply_available": version in (17, 18) and not blockers,
    }


def build_plan(
    state_db: Path,
    *,
    legacy_anchor_backup: Path | None = None,
    allow_dirty: bool = False,
) -> dict:
    state_db = Path(state_db).expanduser().resolve()
    if not state_db.is_file():
        raise FileNotFoundError(state_db)
    evidence = (
        Path(legacy_anchor_backup).expanduser().resolve()
        if legacy_anchor_backup is not None
        else None
    )
    conn = sqlite3.connect(str(state_db), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    try:
        return _build_plan_from_connection(
            conn, state_db, evidence, allow_dirty=allow_dirty
        )
    finally:
        conn.close()


def _validate_snapshot(path: Path, plan: dict) -> None:
    for attempt in range(3):
        # A WAL-mode backup may have no sidecars yet.  On macOS, making a
        # mode=ro connection its first opener can fail before query_only is set.
        conn = sqlite3.connect(str(path.resolve()), timeout=5)
        conn.execute("PRAGMA query_only = ON")
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"migration backup integrity failed: {integrity}")
            foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_keys:
                raise RuntimeError(
                    f"migration backup foreign_key_check failed: {foreign_keys[0]}"
                )
            count = int(
                conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
            )
            if count != plan["fingerprint_count"]:
                raise RuntimeError("migration backup fingerprint count differs from plan")
            if _metadata_digest(conn) != plan["fingerprint_metadata_sha256"]:
                raise RuntimeError("migration backup fingerprint digest differs from plan")
            return
        except sqlite3.OperationalError:
            if attempt == 2:
                raise
            time.sleep((0.1, 0.25)[attempt])
        finally:
            conn.close()


def _create_verified_backup(
    state_db: Path,
    partial_path: Path,
    final_path: Path,
    plan: dict,
) -> tuple[Path, str]:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.exists() or partial_path.exists():
        raise RuntimeError(
            "pre-commit migration backup already exists; a resumed writer epoch "
            "must discard it before taking a fresh snapshot"
        )

    source = sqlite3.connect(
        f"file:{state_db.as_posix()}?mode=ro", uri=True, timeout=5
    )
    destination = sqlite3.connect(str(partial_path))
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()
    with partial_path.open("rb") as stream:
        os.fsync(stream.fileno())
    _validate_snapshot(partial_path, plan)
    _migration_failpoint("backup_fsynced")
    os.replace(partial_path, final_path)
    _fsync_directory(final_path.parent)
    return final_path, _sha256(final_path)


def _discard_precommit_backup(journal: dict) -> None:
    parent = Path(journal["backup_path"]).parent
    parent.mkdir(parents=True, exist_ok=True)
    for key in ("backup_partial_path", "backup_path"):
        path = Path(journal[key])
        try:
            path.unlink()
        except FileNotFoundError:
            continue
    _fsync_directory(parent)


def _legacy_expectations(path: Path) -> tuple[dict[int, str | None], dict]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != 17:
            raise RuntimeError(
                f"legacy anchor evidence must be schema v17, got {version}"
            )
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"legacy anchor backup integrity failed: {integrity}")
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise RuntimeError(
                f"legacy anchor backup foreign_key_check failed: {foreign_keys[0]}"
            )
        expectations: dict[int, str | None] = {}
        mapping_digest = hashlib.sha256()
        nonempty = 0
        for row in conn.execute(
            "SELECT fingerprint_id, front_anchor, tail_anchor "
            "FROM fingerprints ORDER BY fingerprint_id"
        ):
            front = row["front_anchor"] or ""
            tail = row["tail_anchor"] or ""
            expected = None
            if front or tail:
                expected = hashlib.sha256(
                    canonical_anchor_payload(front, tail)
                ).hexdigest()
                nonempty += 1
            fingerprint_id = int(row["fingerprint_id"])
            expectations[fingerprint_id] = expected
            mapping_digest.update(fingerprint_id.to_bytes(8, "big", signed=False))
            mapping_digest.update((expected or "none").encode("ascii"))
        info = {
            "path": str(path),
            "sha256": _sha256(path),
            "schema": version,
            "fingerprint_count": len(expectations),
            "nonempty_anchor_count": nonempty,
            "mapping_sha256": mapping_digest.hexdigest(),
        }
        return expectations, info
    finally:
        conn.close()


def _verify_current(
    state_db: Path,
    plan: dict,
    *,
    allow_active_migration_gate: bool = False,
) -> dict:
    conn = decision_store.connect_state_db(state_db)
    try:
        doctor = decision_store.doctor_issues(
            conn,
            verify_files=True,
            check_integrity=True,
            _allow_active_migration_gate=allow_active_migration_gate,
        )
        if doctor:
            raise RuntimeError(
                "payload migration operational Doctor failed: "
                f"{doctor[0].get('kind', 'unknown')}"
            )
        count = int(conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0])
        digest = _metadata_digest(conn)
        if count != plan["fingerprint_count"]:
            raise RuntimeError("fingerprint count changed during payload migration")
        if digest != plan["fingerprint_metadata_sha256"]:
            raise RuntimeError("fingerprint metadata changed during payload migration")
        legacy_rows = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM fingerprints
                WHERE front_anchor IS NOT NULL OR tail_anchor IS NOT NULL
                """
            ).fetchone()[0]
        )
        foreign_key_issues = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        if legacy_rows or foreign_key_issues:
            raise RuntimeError("payload migration left legacy anchors or FK issues")
        return {
            "fingerprint_count": count,
            "fingerprint_metadata_sha256": digest,
            "legacy_anchor_rows_after": legacy_rows,
            "payload_stats": decision_store.anchor_payload_storage_stats(conn),
            "foreign_key_issues": foreign_key_issues,
            "doctor_issue_count": 0,
        }
    finally:
        conn.close()


def _checkpoint_truncate(conn: sqlite3.Connection) -> None:
    result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if result is not None and int(result[0]) != 0:
        raise RuntimeError(
            "WAL checkpoint is busy; stop all database readers/writers and resume "
            "the migration"
        )


def _compact_current(state_db: Path) -> None:
    conn = decision_store.connect_state_db(state_db)
    try:
        _checkpoint_truncate(conn)
        _migration_failpoint("before_vacuum")
        conn.execute("VACUUM")
        _checkpoint_truncate(conn)
    finally:
        conn.close()


def _upsert_setting(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        """
        INSERT INTO settings(key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, str(value)),
    )


def _record_logical_migration(
    conn: sqlite3.Connection,
    journal: dict,
    *,
    source_schema: int,
    backup_sha256: str,
    evidence_info: dict,
) -> None:
    provenance = journal["plan"]["source_provenance"]
    values = {
        "fingerprint_payload_migration_id": journal["migration_id"],
        "fingerprint_payload_migration_status": "logical_committed",
        "fingerprint_payload_migration_source_schema": source_schema,
        "fingerprint_payload_migration_source_snapshot_sha256": backup_sha256,
        "fingerprint_payload_rollback_backup": journal["backup_path"],
        "fingerprint_payload_rollback_sha256": backup_sha256,
        "fingerprint_payload_legacy_evidence_backup": evidence_info["path"],
        "fingerprint_payload_legacy_evidence_sha256": evidence_info["sha256"],
        "fingerprint_payload_migration_build_commit": provenance["build_commit"],
        "fingerprint_payload_migration_build_dirty": provenance["build_dirty"],
        "fingerprint_payload_migration_script_sha256": provenance[
            "migration_script_sha256"
        ],
        "fingerprint_payload_migration_schema_sha256": provenance[
            "schema_module_sha256"
        ],
    }
    for key, value in values.items():
        _upsert_setting(conn, key, value)
    conn.execute(
        "DELETE FROM settings WHERE key IN ("
        "'fingerprint_payload_migration_report_path', "
        "'fingerprint_payload_migration_report_sha256'"
        ")"
    )


def _migration_settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): str(row[1])
        for row in conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE "
            "'fingerprint_payload_migration_%' "
            "OR key LIKE 'fingerprint_payload_rollback_%' "
            "OR key LIKE 'fingerprint_payload_legacy_evidence_%'"
        )
    }


def _assert_db_migration_marker(
    conn: sqlite3.Connection,
    journal: dict,
    *,
    report_sha256: str | None = None,
) -> None:
    settings = _migration_settings(conn)
    expected = {
        "fingerprint_payload_migration_id": journal["migration_id"],
        "fingerprint_payload_migration_source_schema": str(
            journal["plan"]["current_schema"]
        ),
        "fingerprint_payload_migration_source_snapshot_sha256": journal[
            "backup_sha256"
        ],
        "fingerprint_payload_rollback_backup": journal["backup_path"],
        "fingerprint_payload_rollback_sha256": journal["backup_sha256"],
        "fingerprint_payload_legacy_evidence_backup": journal[
            "legacy_anchor_evidence"
        ]["path"],
        "fingerprint_payload_legacy_evidence_sha256": journal[
            "legacy_anchor_evidence"
        ]["sha256"],
    }
    mismatches = [
        key for key, value in expected.items() if settings.get(key) != value
    ]
    if mismatches:
        raise RuntimeError(
            "migration journal does not match the current DB marker: "
            + ", ".join(mismatches)
        )
    status = settings.get("fingerprint_payload_migration_status")
    if report_sha256 is None:
        if status not in {"logical_committed", "reported"}:
            raise RuntimeError("current DB has no matching logical migration marker")
        return
    if status != "reported":
        raise RuntimeError("current DB migration marker is not reported")
    if settings.get("fingerprint_payload_migration_report_path") != journal[
        "report_path"
    ]:
        raise RuntimeError("current DB migration report path differs from journal")
    if settings.get("fingerprint_payload_migration_report_sha256") != report_sha256:
        raise RuntimeError("current DB migration report checksum differs from journal")
    if decision_store.fingerprint_payload_migration_state(conn) is not None:
        raise RuntimeError("reported migration DB still has an active writer gate")


def _mark_migration_reported(
    state_db: Path, journal: dict, report_sha256: str
) -> None:
    conn = decision_store.connect_state_db(state_db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _assert_db_migration_marker(conn, journal)
        _upsert_setting(
            conn,
            "fingerprint_payload_migration_report_path",
            journal["report_path"],
        )
        _upsert_setting(
            conn,
            "fingerprint_payload_migration_report_sha256",
            report_sha256,
        )
        _upsert_setting(
            conn, "fingerprint_payload_migration_status", "reported"
        )
        conn.execute(
            "DELETE FROM settings WHERE key = 'fingerprint_payload_migration_gate'"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _already_current(state_db: Path) -> dict:
    plan = {
        "fingerprint_count": _current_fingerprint_count(state_db),
        "fingerprint_metadata_sha256": _current_metadata_digest(state_db),
    }
    verified = _verify_current(state_db, plan)
    return {
        "kind": "fingerprint_payload_migration_result",
        "status": "already_current",
        "state_db": str(state_db),
        "schema": decision_store.SCHEMA_VERSION,
        **verified,
    }


def _current_fingerprint_count(state_db: Path) -> int:
    conn = sqlite3.connect(str(state_db))
    try:
        conn.execute("PRAGMA query_only = ON")
        return int(conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0])
    finally:
        conn.close()


def _current_metadata_digest(state_db: Path) -> str:
    conn = sqlite3.connect(str(state_db))
    try:
        conn.execute("PRAGMA query_only = ON")
        return _metadata_digest(conn)
    finally:
        conn.close()


def _load_validated_report(journal: dict, state_db: Path) -> tuple[dict, str]:
    report_path = Path(journal["report_path"])
    report = _read_json(report_path)
    if report is None:
        raise RuntimeError("reported migration result is missing")
    report_sha256 = _sha256(report_path)
    if journal.get("report_sha256") not in (None, report_sha256):
        raise RuntimeError("reported migration result checksum mismatch")
    expected = {
        "kind": "fingerprint_payload_migration_result",
        "migration_id": journal["migration_id"],
        "target_version": TARGET_VERSION,
        "state_db": str(state_db),
        "schema_before": journal["plan"]["current_schema"],
        "schema_after": decision_store.SCHEMA_VERSION,
        "fingerprint_count": journal["plan"]["fingerprint_count"],
        "fingerprint_metadata_sha256": journal["plan"][
            "fingerprint_metadata_sha256"
        ],
        "backup_path": journal["backup_path"],
        "backup_sha256": journal["backup_sha256"],
    }
    mismatches = [
        key for key, value in expected.items() if report.get(key) != value
    ]
    report_evidence = report.get("legacy_anchor_evidence") or {}
    journal_evidence = journal["legacy_anchor_evidence"]
    for key in ("path", "sha256", "mapping_sha256", "fingerprint_count"):
        if report_evidence.get(key) != journal_evidence.get(key):
            mismatches.append(f"legacy_anchor_evidence.{key}")
    if mismatches:
        raise RuntimeError(
            "existing migration report differs from journal: "
            + ", ".join(mismatches)
        )
    return report, report_sha256


def apply_migration(
    state_db: Path,
    *,
    legacy_anchor_backup: Path | None = None,
    house_dir: Path = HOUSE_DIR,
    temp_dir: Path = TEMP_DIR,
    allow_dirty: bool = False,
    reapply: bool = False,
) -> dict:
    state_db = Path(state_db).expanduser().resolve()
    if not state_db.is_file():
        raise FileNotFoundError(state_db)
    evidence_arg = (
        Path(legacy_anchor_backup).expanduser().resolve()
        if legacy_anchor_backup is not None
        else None
    )
    journal_path = _journal_path(state_db)
    with mutation_lock_for_roots(
        house_dir, temp_dir, "fingerprint-payload-schema-v19"
    ):
        journal = _read_json(journal_path)
        if journal is not None:
            _validate_journal_identity(journal, state_db)

        version_probe = sqlite3.connect(str(state_db))
        try:
            version_probe.execute("PRAGMA query_only = ON")
            current_version = int(
                version_probe.execute("PRAGMA user_version").fetchone()[0]
            )
        finally:
            version_probe.close()

        if journal is not None and journal["phase"] == "reported":
            if current_version != decision_store.SCHEMA_VERSION:
                if not reapply:
                    raise RuntimeError(
                        "reported migration journal does not match the current DB; "
                        "the DB appears rolled back. Re-run with --reapply after "
                        "confirming the schema-v17 evidence backup."
                    )
                if current_version == 18 and evidence_arg is None:
                    raise RuntimeError(
                        "--reapply for schema v18 requires --legacy-anchor-backup"
                    )
                _archive_stale_journal(journal_path, journal)
                journal = None
            else:
                report, report_sha256 = _load_validated_report(journal, state_db)
                marker_conn = decision_store.connect_state_db(state_db)
                try:
                    _assert_db_migration_marker(
                        marker_conn, journal, report_sha256=report_sha256
                    )
                finally:
                    marker_conn.close()
                verified = _verify_current(state_db, journal["plan"])
                if (
                    verified["fingerprint_count"] != report["fingerprint_count"]
                    or verified["fingerprint_metadata_sha256"]
                    != report["fingerprint_metadata_sha256"]
                ):
                    raise RuntimeError(
                        "reported migration result differs from the current DB"
                    )
                return report

        if (
            journal is not None
            and current_version in (17, 18)
            and _phase_at_least(journal, "logical_committed")
        ):
            if not reapply:
                raise RuntimeError(
                    "migration journal records a logical commit but the DB is older; "
                    "pass --reapply only after confirming the rollback and evidence"
                )
            if current_version == 18 and evidence_arg is None:
                raise RuntimeError(
                    "--reapply for schema v18 requires --legacy-anchor-backup"
                )
            _archive_stale_journal(journal_path, journal)
            journal = None

        if journal is None and current_version == decision_store.SCHEMA_VERSION:
            return _already_current(state_db)
        if journal is None and current_version not in (17, 18):
            raise RuntimeError(
                f"fingerprint payload migration requires schema 17 or 18, got {current_version}"
            )

        if current_version in (17, 18):
            lock_conn = decision_store.connect_state_db(state_db)
            try:
                lock_conn.execute("BEGIN IMMEDIATE")
                locked_version = int(
                    lock_conn.execute("PRAGMA user_version").fetchone()[0]
                )
                if locked_version != current_version:
                    raise RuntimeError(
                        "state DB schema changed before the migration writer epoch"
                    )
                resume_evidence = evidence_arg
                if resume_evidence is None and journal is not None:
                    stored_evidence = journal.get(
                        "legacy_anchor_evidence", {}
                    ).get("path") or journal.get("plan", {}).get(
                        "legacy_anchor_backup"
                    )
                    if stored_evidence:
                        resume_evidence = Path(stored_evidence)
                plan = _build_plan_from_connection(
                    lock_conn,
                    state_db,
                    resume_evidence,
                    allow_dirty=allow_dirty,
                )
                if not plan["apply_available"]:
                    raise RuntimeError(
                        "fingerprint payload migration is blocked: "
                        + (", ".join(plan["blockers"]) or "schema preflight failed")
                    )
                if journal is None:
                    migration_id = uuid.uuid4().hex
                    backup_path, report_path = _artifact_paths(
                        state_db, migration_id
                    )
                    journal = {
                        "kind": "fingerprint_payload_migration_journal",
                        "migration_id": migration_id,
                        "target_version": TARGET_VERSION,
                        "target_schema": decision_store.SCHEMA_VERSION,
                        "state_db": str(state_db),
                        "plan": plan,
                        "backup_path": str(backup_path),
                        "backup_partial_path": str(backup_path) + ".partial",
                        "report_path": str(report_path),
                        "writer_epoch": 1,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    journal = _write_journal(
                        journal_path, journal, "preparing"
                    )
                else:
                    if journal["plan"]["current_schema"] != current_version:
                        raise RuntimeError(
                            "migration journal source schema differs from current DB"
                        )
                    journal = dict(journal)
                    for key in (
                        "backup_sha256",
                        "legacy_anchor_evidence",
                        "logical_migration",
                        "verified",
                    ):
                        journal.pop(key, None)
                    journal["plan"] = plan
                    journal["writer_epoch"] = int(
                        journal.get("writer_epoch", 1)
                    ) + 1
                    journal = _write_journal(
                        journal_path,
                        journal,
                        "preparing",
                        resumed_precommit=True,
                    )
                    # The previous writer lock was released.  A fingerprint-only
                    # digest cannot prove rollback-backup identity, so always take
                    # a fresh full snapshot in this new epoch.
                    _discard_precommit_backup(journal)

                backup, backup_sha256 = _create_verified_backup(
                    state_db,
                    Path(journal["backup_partial_path"]),
                    Path(journal["backup_path"]),
                    plan,
                )
                evidence_path = backup if current_version == 17 else resume_evidence
                if evidence_path is None:
                    raise RuntimeError(
                        "schema v18 migration requires --legacy-anchor-backup"
                    )
                expectations, evidence_info = _legacy_expectations(evidence_path)
                journal = _write_journal(
                    journal_path,
                    journal,
                    "prepared",
                    backup_path=str(backup),
                    backup_sha256=backup_sha256,
                    legacy_anchor_evidence=evidence_info,
                )
                if _source_provenance(allow_dirty=allow_dirty) != plan[
                    "source_provenance"
                ]:
                    raise RuntimeError(
                        "migration source changed during the locked writer epoch"
                    )
                final_source_doctor = decision_store.doctor_issues(
                    lock_conn,
                    verify_files=True,
                    check_integrity=False,
                    _allow_legacy_payload_schema=True,
                )
                if final_source_doctor:
                    raise RuntimeError(
                        "source operational Doctor changed during backup: "
                        f"{final_source_doctor[0].get('kind', 'unknown')}"
                    )
                migration = _migrate_anchor_payload_schema_connection(
                    lock_conn,
                    current_version,
                    legacy_anchor_expectations=expectations,
                    activate_writer_gate=True,
                )
                if (
                    int(lock_conn.execute(
                        "SELECT COUNT(*) FROM fingerprints"
                    ).fetchone()[0])
                    != plan["fingerprint_count"]
                    or _metadata_digest(lock_conn)
                    != plan["fingerprint_metadata_sha256"]
                ):
                    raise RuntimeError(
                        "fingerprint identity changed inside logical migration"
                    )
                _record_logical_migration(
                    lock_conn,
                    journal,
                    source_schema=current_version,
                    backup_sha256=backup_sha256,
                    evidence_info=evidence_info,
                )
                lock_conn.commit()
                current_version = decision_store.SCHEMA_VERSION
                journal = _write_journal(
                    journal_path,
                    journal,
                    "logical_committed",
                    logical_migration=migration,
                )
                _migration_failpoint("logical_committed")
            except Exception:
                if lock_conn.in_transaction:
                    lock_conn.rollback()
                raise
            finally:
                lock_conn.close()
        elif current_version == decision_store.SCHEMA_VERSION:
            if journal is None:
                return _already_current(state_db)
            plan = journal["plan"]
            marker_conn = decision_store.connect_state_db(state_db)
            try:
                _assert_db_migration_marker(marker_conn, journal)
            finally:
                marker_conn.close()
            if not _phase_at_least(journal, "logical_committed"):
                journal = _write_journal(
                    journal_path,
                    journal,
                    "logical_committed",
                    recovered_after_commit=True,
                )
        else:
            raise RuntimeError(
                f"cannot resume migration from schema {current_version}"
            )

        marker_conn = decision_store.connect_state_db(state_db)
        try:
            _assert_db_migration_marker(marker_conn, journal)
        finally:
            marker_conn.close()
        plan = journal["plan"]
        if not _phase_at_least(journal, "compacted"):
            _compact_current(state_db)
            journal = _write_journal(journal_path, journal, "compacted")
        if not _phase_at_least(journal, "verified"):
            _migration_failpoint("before_post_validation")
            verified = _verify_current(
                state_db, plan, allow_active_migration_gate=True
            )
            journal = _write_journal(
                journal_path, journal, "verified", verified=verified
            )
        else:
            verified = journal["verified"]

        result = {
            "kind": "fingerprint_payload_migration_result",
            "status": "succeeded",
            "migration_id": journal["migration_id"],
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "target_version": TARGET_VERSION,
            "state_db": str(state_db),
            "schema_before": plan["current_schema"],
            "schema_after": decision_store.SCHEMA_VERSION,
            "database_bytes_before": plan["database_bytes"],
            "database_bytes_after": state_db.stat().st_size,
            "fingerprint_count": verified["fingerprint_count"],
            "fingerprint_metadata_sha256": verified[
                "fingerprint_metadata_sha256"
            ],
            "legacy_anchor_rows_before": plan["legacy_anchor_rows"],
            "legacy_anchor_bytes_before": plan["legacy_anchor_bytes"],
            **verified,
            "backup_path": journal["backup_path"],
            "backup_sha256": journal["backup_sha256"],
            "legacy_anchor_evidence": journal["legacy_anchor_evidence"],
            "source_provenance": plan["source_provenance"],
            "journal_path": str(journal_path),
            "report_path": journal["report_path"],
        }
        report_path = Path(journal["report_path"])
        if not report_path.is_file():
            _migration_failpoint("before_report")
            _atomic_write_json(report_path, result)
            _migration_failpoint("report_replaced")
        else:
            result, _existing_sha256 = _load_validated_report(journal, state_db)
        report_sha256 = _sha256(report_path)
        _mark_migration_reported(state_db, journal, report_sha256)
        _write_journal(
            journal_path,
            journal,
            "reported",
            report_path=str(report_path),
            report_sha256=report_sha256,
        )
        return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="plan, apply, or resume file_check 1.5.2 payload migration"
    )
    parser.add_argument("--state-db", default=str(STATE_DB))
    parser.add_argument("--house", default=str(HOUSE_DIR))
    parser.add_argument("--temp", default=str(TEMP_DIR))
    parser.add_argument(
        "--legacy-anchor-backup",
        help="required for schema v18: preserved schema-v17 backup",
    )
    parser.add_argument("--run", action="store_true")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="allow an emergency dirty build and record its full git diff checksum",
    )
    parser.add_argument(
        "--reapply",
        action="store_true",
        help="archive a stale reported journal after an explicitly confirmed rollback",
    )
    args = parser.parse_args(argv)
    evidence = (
        Path(args.legacy_anchor_backup)
        if args.legacy_anchor_backup
        else None
    )
    if args.run:
        result = apply_migration(
            Path(args.state_db),
            legacy_anchor_backup=evidence,
            house_dir=Path(args.house),
            temp_dir=Path(args.temp),
            allow_dirty=args.allow_dirty,
            reapply=args.reapply,
        )
    else:
        result = build_plan(
            Path(args.state_db),
            legacy_anchor_backup=evidence,
            allow_dirty=args.allow_dirty,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
