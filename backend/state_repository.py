"""SQLite connection, schema migration, and transaction primitives.

The repository owns storage mechanics only. Actual-run authorization, mutation
journals, recovery decisions, and Doctor policy remain together in
``decision_store`` so their fail-closed state machine is not fragmented.
"""

from __future__ import annotations

import os
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

from state_schema import (
    CATALOG_SCHEMA_SQL,
    FILE_ANALYSIS_SCHEMA_SQL,
    REQUIRED_TABLES,
    REQUIRED_VIEWS,
    SCHEMA_SQL,
    SCHEMA_VERSION,
)


DEFAULT_BUSY_TIMEOUT_MS = 5_000


def _connection_main_path(conn):
    rows = conn.execute("PRAGMA database_list").fetchall()
    main_path = next(row[2] for row in rows if row[1] == "main")
    return str(Path(main_path).expanduser().resolve())



def connect_state_db(path: os.PathLike | str, *, create: bool = False) -> sqlite3.Connection:
    db_path = Path(path)
    if create:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    elif not db_path.is_file():
        raise FileNotFoundError(db_path)

    conn = sqlite3.connect(str(db_path), timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn



def connect_state_db_readonly(
    path: os.PathLike | str, *, attempts: int = 3
) -> sqlite3.Connection:
    db_path = Path(path).resolve()
    attempts = max(1, int(attempts))
    conn = None
    for attempt in range(attempts):
        if not db_path.is_file():
            if attempt + 1 == attempts:
                raise FileNotFoundError(db_path)
        else:
            try:
                conn = sqlite3.connect(
                    f"file:{db_path.as_posix()}?mode=ro",
                    uri=True,
                    timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000,
                )
                break
            except sqlite3.OperationalError:
                if attempt + 1 == attempts:
                    raise
        time.sleep((0.1, 0.25)[min(attempt, 1)])
    if conn is None:  # pragma: no cover - loop exits by connect or exception
        raise sqlite3.OperationalError(f"unable to open read-only database: {db_path}")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
    return conn



def initialize_state_db(
    path: os.PathLike | str,
    *,
    migrate: bool = False,
    check_integrity: bool = True,
    _validate_schema=None,
) -> sqlite3.Connection:
    """Open/create the state DB; upgrade an existing DB only with explicit consent.

    Callers that pass ``migrate=True`` must first create a verified SQLite backup.
    This keeps Scanner/auditor/read workflows from silently invalidating an active
    one-button authorization as a side effect of merely opening an older DB.
    """
    schema_validator = _validate_schema or validate_schema
    conn = connect_state_db(path, create=True)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, SCHEMA_VERSION) and not migrate:
        conn.close()
        raise RuntimeError(
            "state DB schema migration required: "
            f"current={version}, expected={SCHEMA_VERSION}; "
            "use a backup-owning migration entry point"
        )
    if version == 0:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    elif version == 1:
        conn.executescript(
            """
            CREATE TABLE actual_runs (
                run_id TEXT PRIMARY KEY,
                state TEXT NOT NULL CHECK (state IN ('approved', 'active', 'finished', 'failed', 'cancelled')),
                house_root TEXT NOT NULL,
                temp_root TEXT NOT NULL,
                backup_path TEXT NOT NULL,
                backup_sha256 TEXT NOT NULL,
                manifest_path TEXT,
                manifest_sha256 TEXT,
                approved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                activated_at TEXT,
                finished_at TEXT,
                error TEXT
            );
            DELETE FROM settings WHERE key IN ('approved_run_id', 'approved_backup');
            UPDATE settings SET value = '0', updated_at = CURRENT_TIMESTAMP
            WHERE key = 'actual_mutation_enabled';
            PRAGMA user_version = 2;
            """
        )
        conn.commit()
        version = 2
    if version == 2:
        actual_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(actual_runs)")
        }
        for name, declaration in (
            ("backup_dev", "INTEGER"),
            ("backup_ino", "INTEGER"),
            ("backup_size", "INTEGER"),
            ("backup_mtime_ns", "INTEGER"),
            ("manifest_dev", "INTEGER"),
            ("manifest_ino", "INTEGER"),
            ("manifest_size", "INTEGER"),
            ("manifest_mtime_ns", "INTEGER"),
            ("activation_claim", "TEXT"),
        ):
            if name not in actual_columns:
                conn.execute(f"ALTER TABLE actual_runs ADD COLUMN {name} {declaration}")
        operation_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(operations)")
        }
        if "parent_operation_id" not in operation_columns:
            conn.execute(
                "ALTER TABLE operations ADD COLUMN parent_operation_id INTEGER "
                "REFERENCES operations(operation_id) ON DELETE RESTRICT"
            )
        conn.execute(
            """
            UPDATE actual_runs
            SET state = 'failed', finished_at = CURRENT_TIMESTAMP,
                error = 'schema v3 migration invalidated unfinished authorization'
            WHERE state IN ('approved', 'active')
            """
        )
        conn.execute("DELETE FROM settings WHERE key IN ('approved_run_id', 'approved_backup')")
        conn.execute(
            "UPDATE settings SET value = '0', updated_at = CURRENT_TIMESTAMP "
            "WHERE key = 'actual_mutation_enabled'"
        )
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        version = 3
    if version == 3:
        operation_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(operations)")
        }
        for name, declaration in (
            ("source_dev", "INTEGER"),
            ("source_ino", "INTEGER"),
            ("source_ctime_ns", "INTEGER"),
            ("source_sha256", "TEXT"),
            ("destination_dev", "INTEGER"),
            ("destination_ino", "INTEGER"),
            ("destination_ctime_ns", "INTEGER"),
            ("destination_size", "INTEGER"),
            ("destination_mtime_ns", "INTEGER"),
            ("destination_sha256", "TEXT"),
        ):
            if name not in operation_columns:
                conn.execute(f"ALTER TABLE operations ADD COLUMN {name} {declaration}")
        conn.execute(
            """
            UPDATE actual_runs
            SET state = 'failed', finished_at = CURRENT_TIMESTAMP,
                error = 'schema v4 migration invalidated unfinished authorization'
            WHERE state IN ('approved', 'active')
            """
        )
        conn.execute("DELETE FROM settings WHERE key IN ('approved_run_id', 'approved_backup')")
        conn.execute(
            "UPDATE settings SET value = '0', updated_at = CURRENT_TIMESTAMP "
            "WHERE key = 'actual_mutation_enabled'"
        )
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
        version = 4
    if version == 4:
        fingerprint_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(fingerprints)")
        }
        if "analysis_policy_hash" not in fingerprint_columns:
            conn.execute("ALTER TABLE fingerprints ADD COLUMN analysis_policy_hash TEXT")
        conn.execute(
            """
            UPDATE actual_runs
            SET state = 'failed', finished_at = CURRENT_TIMESTAMP,
                error = 'schema v5 migration invalidated unfinished authorization'
            WHERE state IN ('approved', 'active')
            """
        )
        conn.execute("DELETE FROM settings WHERE key IN ('approved_run_id', 'approved_backup')")
        conn.execute(
            "UPDATE settings SET value = '0', updated_at = CURRENT_TIMESTAMP "
            "WHERE key = 'actual_mutation_enabled'"
        )
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
        version = 5
    if version == 5:
        for table in ("files", "fingerprints"):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name in ("dev", "ino", "ctime_ns"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} INTEGER")
        conn.execute(
            """
            UPDATE actual_runs
            SET state = 'failed', finished_at = CURRENT_TIMESTAMP,
                error = 'schema v6 migration invalidated unfinished authorization'
            WHERE state IN ('approved', 'active')
            """
        )
        conn.execute("DELETE FROM settings WHERE key IN ('approved_run_id', 'approved_backup')")
        conn.execute(
            "UPDATE settings SET value = '0', updated_at = CURRENT_TIMESTAMP "
            "WHERE key = 'actual_mutation_enabled'"
        )
        conn.execute("PRAGMA user_version = 6")
        conn.commit()
        version = 6
    if version == 6:
        actual_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(actual_runs)")
        }
        for name in ("backup_ctime_ns", "manifest_ctime_ns"):
            if name not in actual_columns:
                conn.execute(f"ALTER TABLE actual_runs ADD COLUMN {name} INTEGER")
        conn.execute(
            """
            UPDATE actual_runs
            SET state = 'failed', finished_at = CURRENT_TIMESTAMP,
                error = 'schema v7 migration invalidated unfinished authorization'
            WHERE state IN ('approved', 'active')
            """
        )
        conn.execute("DELETE FROM settings WHERE key IN ('approved_run_id', 'approved_backup')")
        conn.execute(
            "UPDATE settings SET value = '0', updated_at = CURRENT_TIMESTAMP "
            "WHERE key = 'actual_mutation_enabled'"
        )
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
        version = 7
    if version == 7:
        conn.executescript(CATALOG_SCHEMA_SQL)
        conn.execute(
            """
            UPDATE actual_runs
            SET state = 'failed', finished_at = CURRENT_TIMESTAMP,
                error = 'schema v8 migration invalidated unfinished authorization'
            WHERE state IN ('approved', 'active')
            """
        )
        conn.execute("DELETE FROM settings WHERE key IN ('approved_run_id', 'approved_backup')")
        conn.execute(
            "UPDATE settings SET value = '0', updated_at = CURRENT_TIMESTAMP "
            "WHERE key = 'actual_mutation_enabled'"
        )
        conn.execute("PRAGMA user_version = 8")
        conn.commit()
        version = 8
    if version == 8:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(catalog_platform_stats)")
        }
        if "download_count" not in columns:
            conn.execute(
                "ALTER TABLE catalog_platform_stats ADD COLUMN download_count INTEGER "
                "CHECK (download_count IS NULL OR download_count >= 0)"
            )
        conn.execute(
            "UPDATE catalog_platform_stats SET download_count = interest_count "
            "WHERE download_count IS NULL AND interest_count IS NOT NULL"
        )
        conn.executescript(CATALOG_SCHEMA_SQL)
        conn.execute(
            """
            UPDATE actual_runs
            SET state = 'failed', finished_at = CURRENT_TIMESTAMP,
                error = 'schema v9 migration invalidated unfinished authorization'
            WHERE state IN ('approved', 'active')
            """
        )
        conn.execute("DELETE FROM settings WHERE key IN ('approved_run_id', 'approved_backup')")
        conn.execute(
            "UPDATE settings SET value = '0', updated_at = CURRENT_TIMESTAMP "
            "WHERE key = 'actual_mutation_enabled'"
        )
        conn.execute("PRAGMA user_version = 9")
        conn.commit()
        version = 9
    if version == 9:
        conn.executescript(FILE_ANALYSIS_SCHEMA_SQL)
        conn.execute(
            """
            UPDATE actual_runs
            SET state = 'failed', finished_at = CURRENT_TIMESTAMP,
                error = 'schema v10 migration invalidated unfinished authorization'
            WHERE state IN ('approved', 'active')
            """
        )
        conn.execute("DELETE FROM settings WHERE key IN ('approved_run_id', 'approved_backup')")
        conn.execute(
            "UPDATE settings SET value = '0', updated_at = CURRENT_TIMESTAMP "
            "WHERE key = 'actual_mutation_enabled'"
        )
        conn.execute("PRAGMA user_version = 10")
        conn.commit()
        version = 10
    if version == 10:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(file_analysis)")}
        if "title_override_json" not in columns:
            conn.execute("ALTER TABLE file_analysis ADD COLUMN title_override_json TEXT")
        conn.execute(
            """
            UPDATE actual_runs
            SET state = 'failed', finished_at = CURRENT_TIMESTAMP,
                error = 'schema v11 migration invalidated unfinished authorization'
            WHERE state IN ('approved', 'active')
            """
        )
        conn.execute("DELETE FROM settings WHERE key IN ('approved_run_id', 'approved_backup')")
        conn.execute(
            "UPDATE settings SET value = '0', updated_at = CURRENT_TIMESTAMP "
            "WHERE key = 'actual_mutation_enabled'"
        )
        conn.execute("PRAGMA user_version = 11")
        conn.commit()
        version = 11
    if version == 11:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS operation_groups (
                group_id INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES actual_runs(run_id) ON DELETE RESTRICT,
                action TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('planned', 'fs_done', 'db_done', 'committed', 'rolled_back', 'stale', 'failed')
                ),
                source_path TEXT,
                dest_path TEXT,
                item_count INTEGER NOT NULL DEFAULT 0 CHECK (item_count >= 0),
                plan_sha256 TEXT NOT NULL,
                manifest_path TEXT,
                source_manifest_json TEXT,
                source_dev INTEGER,
                source_ino INTEGER,
                source_ctime_ns INTEGER,
                destination_dev INTEGER,
                destination_ino INTEGER,
                destination_ctime_ns INTEGER,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS operation_groups_run_state
            ON operation_groups(run_id, state);
            CREATE TABLE IF NOT EXISTS work_folders (
                folder_id INTEGER PRIMARY KEY,
                work_bucket_id INTEGER NOT NULL REFERENCES works(work_bucket_id) ON DELETE RESTRICT,
                canonical_path TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL CHECK (role IN ('primary', 'edition', 'auxiliary')),
                state TEXT NOT NULL CHECK (state IN ('planned', 'active', 'retired', 'failed')),
                operation_group_id INTEGER REFERENCES operation_groups(group_id) ON DELETE RESTRICT,
                dev INTEGER,
                ino INTEGER,
                ctime_ns INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS work_folders_work_state
            ON work_folders(work_bucket_id, state, role);
            CREATE UNIQUE INDEX IF NOT EXISTS work_folders_one_primary
            ON work_folders(work_bucket_id)
            WHERE state = 'active' AND role = 'primary';
            UPDATE actual_runs
            SET state = 'failed', finished_at = CURRENT_TIMESTAMP,
                error = 'schema v12 migration invalidated unfinished authorization'
            WHERE state IN ('approved', 'active');
            DELETE FROM settings WHERE key IN ('approved_run_id', 'approved_backup');
            UPDATE settings SET value = '0', updated_at = CURRENT_TIMESTAMP
            WHERE key = 'actual_mutation_enabled';
            PRAGMA user_version = 12;
            """
        )
        conn.commit()
        version = 12
    if version == 12:
        work_columns = {row[1] for row in conn.execute("PRAGMA table_info(works)")}
        if "status" not in work_columns:
            conn.execute(
                "ALTER TABLE works ADD COLUMN status TEXT NOT NULL DEFAULT 'active' "
                "CHECK (status IN ('active', 'retired'))"
            )
        variant_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(variants)")
        }
        if "status" not in variant_columns:
            conn.execute(
                "ALTER TABLE variants ADD COLUMN status TEXT NOT NULL DEFAULT 'active' "
                "CHECK (status IN ('active', 'retired'))"
            )
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS work_aliases (
                alias_id INTEGER PRIMARY KEY,
                alias_kind TEXT NOT NULL CHECK (
                    alias_kind IN ('core_title', 'readable_title', 'folder_name')
                ),
                alias_key TEXT NOT NULL,
                alias_display TEXT NOT NULL,
                work_bucket_id INTEGER NOT NULL
                    REFERENCES works(work_bucket_id) ON DELETE RESTRICT,
                preferred_folder_id INTEGER
                    REFERENCES work_folders(folder_id) ON DELETE RESTRICT,
                origin TEXT NOT NULL DEFAULT 'human_decision'
                    CHECK (origin IN ('human_decision', 'strong_match')),
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                supersedes_alias_id INTEGER
                    REFERENCES work_aliases(alias_id) ON DELETE RESTRICT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS work_aliases_one_active_key
            ON work_aliases(alias_kind, alias_key) WHERE active = 1;
            CREATE INDEX IF NOT EXISTS work_aliases_work_active
            ON work_aliases(work_bucket_id, active, alias_kind);
            CREATE TABLE IF NOT EXISTS work_management_events (
                event_id INTEGER PRIMARY KEY,
                action TEXT NOT NULL,
                source_work_id INTEGER
                    REFERENCES works(work_bucket_id) ON DELETE RESTRICT,
                target_work_id INTEGER
                    REFERENCES works(work_bucket_id) ON DELETE RESTRICT,
                plan_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT 'local_user',
                supersedes_event_id INTEGER
                    REFERENCES work_management_events(event_id) ON DELETE RESTRICT,
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS work_management_events_work
            ON work_management_events(source_work_id, target_work_id, created_at);
            UPDATE actual_runs
            SET state = 'failed', finished_at = CURRENT_TIMESTAMP,
                error = 'schema v13 migration invalidated unfinished authorization'
            WHERE state IN ('approved', 'active');
            DELETE FROM settings WHERE key IN ('approved_run_id', 'approved_backup');
            UPDATE settings SET value = '0', updated_at = CURRENT_TIMESTAMP
            WHERE key = 'actual_mutation_enabled';
            PRAGMA user_version = 13;
            """
        )
        conn.commit()
        version = 13
    if version == 13:
        operation_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(operations)")
        }
        if "operation_group_id" not in operation_columns:
            conn.execute(
                "ALTER TABLE operations ADD COLUMN operation_group_id INTEGER "
                "REFERENCES operation_groups(group_id) ON DELETE RESTRICT"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS operations_group_state "
            "ON operations(operation_group_id, state)"
        )
        conn.execute(
            """
            UPDATE actual_runs
            SET state = 'failed', finished_at = CURRENT_TIMESTAMP,
                error = 'schema v14 migration invalidated unfinished authorization'
            WHERE state IN ('approved', 'active')
            """
        )
        conn.execute("DELETE FROM settings WHERE key IN ('approved_run_id', 'approved_backup')")
        conn.execute(
            "UPDATE settings SET value = '0', updated_at = CURRENT_TIMESTAMP "
            "WHERE key = 'actual_mutation_enabled'"
        )
        conn.execute("PRAGMA user_version = 14")
        conn.commit()
        version = 14
    if version == 14:
        # v1.2.7 title cleanup deliberately created a fresh intake identity,
        # but early runs left the inactive historical row on its former real
        # house path.  ``files.canonical_path`` is globally unique, so a later
        # intake materializing to that path could move the file successfully
        # and then fail its DB commit.  Modern title requeue code already uses
        # ``retired_canonical_path``; migrate every proven legacy tombstone once.
        retire_legacy_title_requeue_path_owners(conn)
        conn.execute(
            """
            UPDATE actual_runs
            SET state = 'failed', finished_at = CURRENT_TIMESTAMP,
                error = 'schema v15 migration invalidated unfinished authorization'
            WHERE state IN ('approved', 'active')
            """
        )
        conn.execute("DELETE FROM settings WHERE key IN ('approved_run_id', 'approved_backup')")
        conn.execute(
            "UPDATE settings SET value = '0', updated_at = CURRENT_TIMESTAMP "
            "WHERE key = 'actual_mutation_enabled'"
        )
        conn.execute("PRAGMA user_version = 15")
        conn.commit()
        version = 15
    schema_validator(conn, check_integrity=check_integrity)
    return conn



def validate_schema(
    conn: sqlite3.Connection,
    *,
    check_integrity: bool = True,
) -> None:
    """Validate the state DB schema and, by default, its full integrity.

    Mutation and recovery callers keep the fail-closed full integrity check.
    Read-only UI previews may skip that expensive scan after validating the
    version, required objects, and columns; the real operation validates again
    with the default before it is allowed to mutate anything.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise RuntimeError(f"schema version mismatch: expected={SCHEMA_VERSION}, actual={version}")

    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    tables = {row[0] for row in rows}
    missing = REQUIRED_TABLES - tables
    if missing:
        raise RuntimeError(f"schema tables missing: {sorted(missing)}")
    required_columns = {
        "operation_groups": {
            "group_id", "run_id", "action", "state", "source_path",
            "dest_path", "item_count", "plan_sha256", "manifest_path",
            "source_manifest_json", "source_dev", "source_ino",
            "source_ctime_ns", "destination_dev", "destination_ino",
            "destination_ctime_ns", "error",
        },
        "operations": {
            "operation_id", "run_id", "action", "source_path", "dest_path",
            "file_id", "expected_fingerprint_id", "parent_operation_id",
            "operation_group_id", "state",
        },
        "work_folders": {
            "folder_id", "work_bucket_id", "canonical_path", "role", "state",
            "operation_group_id", "dev", "ino", "ctime_ns",
        },
        "work_aliases": {
            "alias_id", "alias_kind", "alias_key", "alias_display",
            "work_bucket_id", "preferred_folder_id", "origin", "active",
            "supersedes_alias_id",
        },
        "work_management_events": {
            "event_id", "action", "source_work_id", "target_work_id",
            "plan_sha256", "payload_json", "actor", "supersedes_event_id",
            "active",
        },
    }
    for table, expected in required_columns.items():
        actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        missing_columns = expected - actual
        if missing_columns:
            raise RuntimeError(
                f"schema columns missing from {table}: {sorted(missing_columns)}"
            )
    indexes = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    required_indexes = {
        "operation_groups_run_state",
        "work_folders_work_state",
        "work_folders_one_primary",
        "work_aliases_one_active_key",
        "work_aliases_work_active",
        "work_management_events_work",
        "operations_group_state",
    }
    missing_indexes = required_indexes - indexes
    if missing_indexes:
        raise RuntimeError(f"schema indexes missing: {sorted(missing_indexes)}")
    views = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'view'"
        )
    }
    missing_views = REQUIRED_VIEWS - views
    if missing_views:
        raise RuntimeError(f"schema views missing: {sorted(missing_views)}")

    analysis_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(file_analysis)")
    }
    if "title_override_json" not in analysis_columns:
        raise RuntimeError("file_analysis.title_override_json is missing")
    for table in ("works", "variants"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "status" not in columns:
            raise RuntimeError(f"{table}.status is missing")

    if check_integrity:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"integrity_check failed: {integrity}")



@contextmanager
def transaction(conn: sqlite3.Connection, *, immediate: bool = True):
    if conn.in_transaction:
        raise RuntimeError("nested decision_store transactions are not supported")
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        try:
            conn.commit()
        except Exception:
            # Deferred foreign keys are checked at commit time.  A failed
            # commit otherwise leaves the invalid transaction open.
            conn.rollback()
            raise



def canonicalize_path(path: os.PathLike | str) -> str:
    return unicodedata.normalize("NFC", os.path.abspath(os.fspath(path)))



def retired_canonical_path(
    conn: sqlite3.Connection,
    file_id: str,
    original_path: os.PathLike | str,
) -> str:
    """Return a stable virtual slot for one retired file identity.

    Title correction deliberately creates a fresh intake identity.  The old
    inactive row must therefore release its former real path, or a markup-only
    correction that materializes back to the same filename violates the UNIQUE
    ``files.canonical_path`` constraint.  Operations and immutable fingerprints
    retain the original path as provenance; this slot is never a real file.
    """
    db_row = conn.execute("PRAGMA database_list").fetchone()
    if db_row is None or not db_row[2]:
        raise RuntimeError("retired canonical path requires a file-backed state DB")
    state_dir = Path(db_row[2]).resolve().parent
    original_name = Path(os.fspath(original_path)).name or "retired-file"
    return canonicalize_path(
        state_dir / "retired_paths" / str(file_id) / original_name
    )



def retire_legacy_title_requeue_path_owners(
    conn: sqlite3.Connection,
    *,
    canonical_path: os.PathLike | str | None = None,
) -> list[dict]:
    """Release real paths held by proven inactive title-requeue tombstones.

    Early title cleanup operations consumed the house source and left its file
    row inactive, but did not move ``canonical_path`` to the virtual retired
    namespace.  Only a committed title-requeue operation whose recorded source
    is still the inactive row's path proves that the row intentionally released
    that path.  Unknown inactive owners remain fail-closed.

    The caller owns the transaction.  Operations and immutable fingerprints
    retain the original real path as provenance.
    """
    target = canonicalize_path(canonical_path) if canonical_path is not None else None
    params: list[object] = []
    target_clause = ""
    if target is not None:
        target_clause = "AND f.canonical_path = ?"
        params.append(target)
    rows = conn.execute(
        f"""
        SELECT DISTINCT f.file_id, f.canonical_path
        FROM files AS f
        WHERE f.active = 0
          AND f.source = 'house'
          {target_clause}
          AND EXISTS (
              SELECT 1
              FROM operations AS requeue
              WHERE requeue.file_id = f.file_id
                AND requeue.action IN ('title_cleanup_requeue', 'user_title_requeue')
                AND requeue.state = 'committed'
                AND requeue.source_path = f.canonical_path
          )
        ORDER BY f.file_id
        """,
        params,
    ).fetchall()
    retired = []
    for row in rows:
        retired_path = retired_canonical_path(
            conn, row["file_id"], row["canonical_path"]
        )
        updated = conn.execute(
            """
            UPDATE files
            SET canonical_path = ?, protected = 0,
                last_seen_at = CURRENT_TIMESTAMP
            WHERE file_id = ? AND canonical_path = ? AND active = 0
              AND source = 'house'
            """,
            (retired_path, row["file_id"], row["canonical_path"]),
        ).rowcount
        if updated:
            retired.append({
                "file_id": row["file_id"],
                "original_path": row["canonical_path"],
                "retired_path": retired_path,
            })
    return retired



def canonicalize_real_path(path: os.PathLike | str) -> str:
    return unicodedata.normalize(
        "NFC", os.path.realpath(os.path.abspath(os.fspath(path)))
    )
