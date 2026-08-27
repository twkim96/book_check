#!/usr/bin/env python3
"""Plan, apply, and resume the guarded schema-v19 payload migration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import decision_store
from fingerprint_payloads import canonical_anchor_payload
from mutation_io import mutation_lock_for_roots
from project_paths import HOUSE_DIR, STATE_DB, TEMP_DIR


TARGET_VERSION = "1.5.1"
JOURNAL_NAME = "fingerprint_payload_migration_1_5_1_journal.json"
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
    return state_db.parent / "reports" / JOURNAL_NAME


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
) -> dict:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > decision_store.SCHEMA_VERSION:
        raise RuntimeError(f"state DB schema is newer than this program: {version}")
    tables = _tables(conn)
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
    blockers = []
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
        "blockers": blockers,
        "apply_available": version in (17, 18) and not blockers,
    }


def build_plan(
    state_db: Path,
    *,
    legacy_anchor_backup: Path | None = None,
) -> dict:
    state_db = Path(state_db).expanduser().resolve()
    if not state_db.is_file():
        raise FileNotFoundError(state_db)
    evidence = (
        Path(legacy_anchor_backup).expanduser().resolve()
        if legacy_anchor_backup is not None
        else None
    )
    conn = sqlite3.connect(
        f"file:{state_db.as_posix()}?mode=ro", uri=True, timeout=5
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    try:
        return _build_plan_from_connection(conn, state_db, evidence)
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
    if final_path.is_file():
        _validate_snapshot(final_path, plan)
        return final_path, _sha256(final_path)
    if partial_path.exists():
        try:
            _validate_snapshot(partial_path, plan)
        except Exception:
            partial_path.unlink()
        else:
            os.replace(partial_path, final_path)
            _fsync_directory(final_path.parent)
            return final_path, _sha256(final_path)

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


def _verify_current(state_db: Path, plan: dict) -> dict:
    conn = decision_store.connect_state_db(state_db)
    try:
        decision_store.validate_schema(conn)
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
        }
    finally:
        conn.close()


def _compact_current(state_db: Path) -> None:
    conn = decision_store.connect_state_db(state_db)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        _migration_failpoint("before_vacuum")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _clear_writer_gate(state_db: Path) -> None:
    conn = decision_store.connect_state_db(state_db)
    try:
        conn.execute("BEGIN IMMEDIATE")
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
    conn = sqlite3.connect(f"file:{state_db.as_posix()}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0])
    finally:
        conn.close()


def _current_metadata_digest(state_db: Path) -> str:
    conn = sqlite3.connect(f"file:{state_db.as_posix()}?mode=ro", uri=True)
    try:
        return _metadata_digest(conn)
    finally:
        conn.close()


def apply_migration(
    state_db: Path,
    *,
    legacy_anchor_backup: Path | None = None,
    house_dir: Path = HOUSE_DIR,
    temp_dir: Path = TEMP_DIR,
) -> dict:
    state_db = Path(state_db).expanduser().resolve()
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
            if journal.get("state_db") != str(state_db):
                raise RuntimeError("migration journal belongs to another state DB")
            if journal.get("target_schema") != decision_store.SCHEMA_VERSION:
                raise RuntimeError("migration journal targets another schema")
            if journal["phase"] == "reported":
                report_path = Path(journal["report_path"])
                report = _read_json(report_path)
                if report is None:
                    raise RuntimeError("reported migration result is missing")
                if (
                    journal.get("report_sha256")
                    and _sha256(report_path) != journal["report_sha256"]
                ):
                    raise RuntimeError("reported migration result checksum mismatch")
                return report

        version_probe = sqlite3.connect(
            f"file:{state_db.as_posix()}?mode=ro", uri=True
        )
        try:
            current_version = int(
                version_probe.execute("PRAGMA user_version").fetchone()[0]
            )
        finally:
            version_probe.close()
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
                if journal is None:
                    plan = _build_plan_from_connection(
                        lock_conn, state_db, evidence_arg
                    )
                    if not plan["apply_available"]:
                        raise RuntimeError(
                            "fingerprint payload migration is blocked: "
                            + (", ".join(plan["blockers"]) or "schema preflight failed")
                        )
                    migration_id = uuid.uuid4().hex
                    backup_path = state_db.parent / "backups" / (
                        f"before_fingerprint_payload_v19_{migration_id}.sqlite3"
                    )
                    report_path = state_db.parent / "reports" / (
                        f"fingerprint_payload_migration_1_5_1_{migration_id}.json"
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
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    journal = _write_journal(
                        journal_path, journal, "preparing"
                    )
                else:
                    plan = journal["plan"]
                    if (
                        int(lock_conn.execute(
                            "SELECT COUNT(*) FROM fingerprints"
                        ).fetchone()[0])
                        != plan["fingerprint_count"]
                        or _metadata_digest(lock_conn)
                        != plan["fingerprint_metadata_sha256"]
                    ):
                        raise RuntimeError(
                            "source DB changed since the prepared migration snapshot"
                        )

                backup, backup_sha256 = _create_verified_backup(
                    state_db,
                    Path(journal["backup_partial_path"]),
                    Path(journal["backup_path"]),
                    plan,
                )
                evidence_path = backup if current_version == 17 else evidence_arg
                if evidence_path is None:
                    stored_evidence = journal.get(
                        "legacy_anchor_evidence", {}
                    ).get("path")
                    evidence_path = Path(stored_evidence) if stored_evidence else None
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
                migration = decision_store.migrate_anchor_payload_schema_connection(
                    lock_conn,
                    current_version,
                    legacy_anchor_expectations=expectations,
                    require_legacy_expectations=(current_version == 18),
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
                lock_conn.execute(
                    """
                    INSERT INTO settings(key, value, updated_at)
                    VALUES ('fingerprint_payload_rollback_backup', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (str(backup),),
                )
                lock_conn.execute(
                    """
                    INSERT INTO settings(key, value, updated_at)
                    VALUES ('fingerprint_payload_rollback_sha256', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (backup_sha256,),
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

        plan = journal["plan"]
        if not _phase_at_least(journal, "compacted"):
            _compact_current(state_db)
            journal = _write_journal(journal_path, journal, "compacted")
        if not _phase_at_least(journal, "verified"):
            _migration_failpoint("before_post_validation")
            verified = _verify_current(state_db, plan)
            journal = _write_journal(
                journal_path, journal, "verified", verified=verified
            )
        else:
            verified = journal["verified"]

        result = {
            "kind": "fingerprint_payload_migration_result",
            "status": "succeeded",
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
            "journal_path": str(journal_path),
            "report_path": journal["report_path"],
        }
        report_path = Path(journal["report_path"])
        if not report_path.is_file():
            _migration_failpoint("before_report")
            _atomic_write_json(report_path, result)
            _migration_failpoint("report_replaced")
        else:
            existing_result = _read_json(report_path)
            if (
                existing_result is None
                or existing_result.get("kind")
                != "fingerprint_payload_migration_result"
                or existing_result.get("state_db") != str(state_db)
                or existing_result.get("schema_after")
                != decision_store.SCHEMA_VERSION
            ):
                raise RuntimeError("existing migration report is invalid")
            result = existing_result
        report_sha256 = _sha256(report_path)
        _clear_writer_gate(state_db)
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
        description="plan, apply, or resume file_check 1.5.1 payload migration"
    )
    parser.add_argument("--state-db", default=str(STATE_DB))
    parser.add_argument("--house", default=str(HOUSE_DIR))
    parser.add_argument("--temp", default=str(TEMP_DIR))
    parser.add_argument(
        "--legacy-anchor-backup",
        help="required for schema v18: preserved schema-v17 backup",
    )
    parser.add_argument("--run", action="store_true")
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
        )
    else:
        result = build_plan(
            Path(args.state_db), legacy_anchor_backup=evidence
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
