#!/usr/bin/env python3
"""Plan or apply the guarded schema-v18 fingerprint payload migration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import decision_store
from mutation_io import mutation_lock_for_roots
from project_paths import HOUSE_DIR, STATE_DB, TEMP_DIR


def _tables(conn) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _metadata_digest(conn) -> str:
    columns = [
        row[1]
        for row in conn.execute("PRAGMA table_info(fingerprints)")
        if row[1] not in {"front_anchor", "tail_anchor"}
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


def build_plan(state_db: Path) -> dict:
    state_db = Path(state_db).expanduser().resolve()
    if not state_db.is_file():
        raise FileNotFoundError(state_db)
    conn = sqlite3.connect(
        f"file:{state_db.as_posix()}?mode=ro", uri=True, timeout=5
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > decision_store.SCHEMA_VERSION:
            raise RuntimeError(
                f"state DB schema is newer than this program: {version}"
            )
        tables = _tables(conn)
        fingerprint_count = conn.execute(
            "SELECT COUNT(*) FROM fingerprints"
        ).fetchone()[0]
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
        database_bytes = state_db.stat().st_size
        free_bytes = shutil.disk_usage(state_db.parent).free
        required_free_bytes = database_bytes * 3 + 256 * 1024 * 1024
        current_stats = None
        if {
            "anchor_payload_objects", "fingerprint_anchor_refs"
        } <= tables:
            current_stats = decision_store.anchor_payload_storage_stats(conn)
        return {
            "kind": "fingerprint_payload_migration_plan",
            "target_version": "1.5.0",
            "target_schema": decision_store.SCHEMA_VERSION,
            "state_db": str(state_db),
            "current_schema": version,
            "database_bytes": database_bytes,
            "free_bytes": free_bytes,
            "required_free_bytes": required_free_bytes,
            "fingerprint_count": fingerprint_count,
            "fingerprint_metadata_sha256": _metadata_digest(conn),
            "legacy_anchor_rows": int(legacy["rows"]),
            "legacy_anchor_bytes": int(legacy["bytes"]),
            "current_payload_stats": current_stats,
            "blockers": blockers,
            "apply_available": (
                version < decision_store.SCHEMA_VERSION
                and version == 17
                and not blockers
                and free_bytes >= required_free_bytes
            ),
        }
    finally:
        conn.close()


def _write_report(state_db: Path, payload: dict) -> Path:
    report_dir = state_db.parent / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = report_dir / f"fingerprint_payload_migration_1_5_0_{stamp}.json"
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=report_dir
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return target


def apply_migration(
    state_db: Path,
    *,
    house_dir: Path = HOUSE_DIR,
    temp_dir: Path = TEMP_DIR,
) -> dict:
    state_db = Path(state_db).expanduser().resolve()
    with mutation_lock_for_roots(
        house_dir, temp_dir, "fingerprint-payload-schema-v18"
    ):
        plan = build_plan(state_db)
        if plan["current_schema"] == decision_store.SCHEMA_VERSION:
            conn = decision_store.connect_state_db(state_db)
            try:
                decision_store.validate_schema(conn)
                stats = decision_store.anchor_payload_storage_stats(conn)
            finally:
                conn.close()
            return {
                "kind": "fingerprint_payload_migration_result",
                "status": "already_current",
                "state_db": str(state_db),
                "schema": decision_store.SCHEMA_VERSION,
                "payload_stats": stats,
            }
        if not plan["apply_available"]:
            raise RuntimeError(
                "fingerprint payload migration is blocked: "
                + (", ".join(plan["blockers"]) or "disk/schema preflight failed")
            )
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_path = state_db.parent / "backups" / (
            f"before_fingerprint_payload_v18_{stamp}_{uuid.uuid4().hex[:8]}.sqlite3"
        )
        source = decision_store.connect_state_db(state_db)
        try:
            backup = decision_store.backup_state_db(source, backup_path)
        finally:
            source.close()
        backup_sha256 = _sha256(backup)

        migrated = decision_store.initialize_state_db(
            state_db,
            migrate=True,
            compact_migrations=True,
            check_integrity=True,
        )
        try:
            decision_store.validate_schema(migrated)
            stats = decision_store.anchor_payload_storage_stats(migrated)
            legacy_rows = migrated.execute(
                """
                SELECT COUNT(*) FROM fingerprints
                WHERE front_anchor IS NOT NULL OR tail_anchor IS NOT NULL
                """
            ).fetchone()[0]
            fingerprint_count = migrated.execute(
                "SELECT COUNT(*) FROM fingerprints"
            ).fetchone()[0]
            fingerprint_metadata_sha256 = _metadata_digest(migrated)
            foreign_key_issues = len(
                migrated.execute("PRAGMA foreign_key_check").fetchall()
            )
        finally:
            migrated.close()
        if fingerprint_count != plan["fingerprint_count"]:
            raise RuntimeError("fingerprint count changed during payload migration")
        if fingerprint_metadata_sha256 != plan["fingerprint_metadata_sha256"]:
            raise RuntimeError("fingerprint metadata changed during payload migration")
        if stats["reference_count"] != plan["legacy_anchor_rows"]:
            raise RuntimeError("anchor reference count does not match legacy evidence")
        if legacy_rows or foreign_key_issues:
            raise RuntimeError("payload migration left legacy anchors or FK issues")
        result = {
            "kind": "fingerprint_payload_migration_result",
            "status": "succeeded",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "state_db": str(state_db),
            "schema_before": plan["current_schema"],
            "schema_after": decision_store.SCHEMA_VERSION,
            "database_bytes_before": plan["database_bytes"],
            "database_bytes_after": state_db.stat().st_size,
            "fingerprint_count": fingerprint_count,
            "fingerprint_metadata_sha256": fingerprint_metadata_sha256,
            "legacy_anchor_rows_before": plan["legacy_anchor_rows"],
            "legacy_anchor_bytes_before": plan["legacy_anchor_bytes"],
            "legacy_anchor_rows_after": legacy_rows,
            "payload_stats": stats,
            "foreign_key_issues": foreign_key_issues,
            "backup_path": str(backup),
            "backup_sha256": backup_sha256,
        }
        report = _write_report(state_db, result)
        result["report_path"] = str(report)
        return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="plan or apply file_check 1.5.0 fingerprint payload migration"
    )
    parser.add_argument("--state-db", default=str(STATE_DB))
    parser.add_argument("--house", default=str(HOUSE_DIR))
    parser.add_argument("--temp", default=str(TEMP_DIR))
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    if args.run:
        result = apply_migration(
            Path(args.state_db),
            house_dir=Path(args.house),
            temp_dir=Path(args.temp),
        )
    else:
        result = build_plan(Path(args.state_db))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
