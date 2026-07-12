"""SQLite source of truth for dedup decisions and immutable fingerprints.

The store is deliberately fail-closed.  Creating a valid schema does not enable
library mutations; the integration phases must finish and explicitly open that
gate after doctor/recovery checks exist.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import stat
import unicodedata
import uuid
from contextlib import contextmanager, nullcontext
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Optional, Tuple


SCHEMA_VERSION = 7
DEFAULT_BUSY_TIMEOUT_MS = 5_000

ASSIGNMENT_STATES = (
    "unassigned",
    "managed",
    "legacy_unresolved",
    "decision_required",
)
FINAL_VERDICTS = (
    "same_content",
    "same_work_distinct_variant",
    "distinct_work",
)
REVIEW_STATES = ("pending", "deferred", "decided", "superseded")
OPERATION_STATES = (
    "planned",
    "fs_done",
    "db_done",
    "committed",
    "rolled_back",
    "stale",
    "failed",
)
ACTUAL_RUN_RECOVERY_STATES = frozenset({"cancelled", "failed", "finished"})
_ACTUAL_RUN_TRANSITIONS = {
    "approved": {"active", "failed", "cancelled"},
    "active": {"finished", "failed", "cancelled"},
    "finished": set(),
    "failed": set(),
    "cancelled": set(),
}

REQUIRED_TABLES = frozenset({
    "settings",
    "works",
    "variants",
    "collision_groups",
    "collision_members",
    "files",
    "representatives",
    "decisions",
    "fingerprints",
    "review_items",
    "pair_cache",
    "actual_runs",
    "operations",
})

_ALLOWED_OPERATION_TRANSITIONS = {
    "planned": {"fs_done", "rolled_back", "stale", "failed"},
    "fs_done": {"db_done", "rolled_back", "failed"},
    "db_done": {"committed", "rolled_back", "stale", "failed"},
    "committed": set(),
    "rolled_back": set(),
    "stale": set(),
    "failed": set(),
}

_SYMBOL_COORDINATES = {
    "상": ("upper", 10),
    "상권": ("upper", 10),
    "upper": ("upper", 10),
    "중": ("middle", 20),
    "중권": ("middle", 20),
    "middle": ("middle", 20),
    "하": ("lower", 30),
    "하권": ("lower", 30),
    "lower": ("lower", 30),
    "본편": ("main", 100),
    "main": ("main", 100),
    "외전": ("side_story", 200),
    "외": ("side_story", 200),
    "side_story": ("side_story", 200),
    "특별편": ("special", 300),
    "special": ("special", 300),
}


SCHEMA_SQL = f"""
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE works (
    work_bucket_id INTEGER PRIMARY KEY,
    display_title TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE variants (
    variant_id INTEGER PRIMARY KEY,
    work_bucket_id INTEGER NOT NULL REFERENCES works(work_bucket_id) ON DELETE RESTRICT,
    variant_kind TEXT NOT NULL DEFAULT 'base'
        CHECK (variant_kind IN ('base', 'revision', 'adult', 'translation', 'other')),
    label TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE collision_groups (
    group_id INTEGER PRIMARY KEY,
    core_key TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE files (
    file_id TEXT PRIMARY KEY,
    canonical_path TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size >= 0),
    mtime_ns INTEGER NOT NULL CHECK (mtime_ns >= 0),
    dev INTEGER,
    ino INTEGER,
    ctime_ns INTEGER,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    variant_id INTEGER REFERENCES variants(variant_id) ON DELETE RESTRICT,
    current_fingerprint_id INTEGER,
    assignment_state TEXT NOT NULL DEFAULT 'unassigned'
        CHECK (assignment_state IN {ASSIGNMENT_STATES}),
    assignment_origin TEXT
        CHECK (assignment_origin IS NULL OR assignment_origin IN ('human_decision', 'strong_match')),
    protected INTEGER NOT NULL DEFAULT 0 CHECK (protected IN (0, 1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    coordinate_kind TEXT,
    part_num INTEGER,
    part_den INTEGER CHECK (part_den IS NULL OR part_den > 0),
    volume_num INTEGER,
    volume_den INTEGER CHECK (volume_den IS NULL OR volume_den > 0),
    coordinate_symbol TEXT,
    coordinate_sort_key INTEGER,
    episode_start INTEGER,
    episode_end INTEGER,
    coordinate_raw TEXT,
    span_ambiguous INTEGER NOT NULL DEFAULT 0 CHECK (span_ambiguous IN (0, 1)),
    CHECK (assignment_state != 'managed' OR variant_id IS NOT NULL),
    CHECK (assignment_state != 'managed' OR assignment_origin IS NOT NULL),
    CHECK (assignment_state = 'managed' OR assignment_origin IS NULL),
    UNIQUE (file_id, variant_id),
    UNIQUE (file_id, current_fingerprint_id),
    FOREIGN KEY (current_fingerprint_id, file_id)
        REFERENCES fingerprints(fingerprint_id, file_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE fingerprints (
    fingerprint_id INTEGER PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE RESTRICT,
    canonical_path TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size >= 0),
    mtime_ns INTEGER NOT NULL CHECK (mtime_ns >= 0),
    dev INTEGER,
    ino INTEGER,
    ctime_ns INTEGER,
    normalizer_version TEXT NOT NULL,
    fingerprint_version TEXT NOT NULL,
    analysis_policy_hash TEXT,
    raw_sha256 TEXT,
    normalized_sha256 TEXT,
    normalized_length INTEGER CHECK (normalized_length IS NULL OR normalized_length >= 0),
    encoding TEXT,
    status TEXT NOT NULL,
    front_anchor TEXT,
    tail_anchor TEXT,
    anchors_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (fingerprint_id, file_id),
    UNIQUE (file_id, canonical_path, size, mtime_ns, normalizer_version, fingerprint_version)
);

CREATE TRIGGER fingerprints_no_update
BEFORE UPDATE ON fingerprints
BEGIN
    SELECT RAISE(ABORT, 'fingerprints are immutable');
END;

CREATE TRIGGER fingerprints_no_delete
BEFORE DELETE ON fingerprints
BEGIN
    SELECT RAISE(ABORT, 'fingerprints are immutable');
END;

CREATE TABLE collision_members (
    group_id INTEGER NOT NULL REFERENCES collision_groups(group_id) ON DELETE CASCADE,
    variant_id INTEGER NOT NULL REFERENCES variants(variant_id) ON DELETE RESTRICT,
    display_disambig INTEGER NOT NULL CHECK (display_disambig > 0),
    PRIMARY KEY (group_id, variant_id),
    UNIQUE (group_id, display_disambig)
);

CREATE TABLE representatives (
    variant_id INTEGER PRIMARY KEY REFERENCES variants(variant_id) ON DELETE RESTRICT,
    file_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (file_id, variant_id)
        REFERENCES files(file_id, variant_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE decisions (
    decision_id INTEGER PRIMARY KEY,
    left_work_id INTEGER NOT NULL REFERENCES works(work_bucket_id) ON DELETE RESTRICT,
    left_variant_id INTEGER NOT NULL REFERENCES variants(variant_id) ON DELETE RESTRICT,
    right_work_id INTEGER NOT NULL REFERENCES works(work_bucket_id) ON DELETE RESTRICT,
    right_variant_id INTEGER NOT NULL REFERENCES variants(variant_id) ON DELETE RESTRICT,
    left_file_id TEXT NOT NULL,
    right_file_id TEXT NOT NULL,
    left_fingerprint_id INTEGER NOT NULL,
    right_fingerprint_id INTEGER NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN {FINAL_VERDICTS}),
    decided_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    evidence_json TEXT,
    note TEXT,
    supersedes_decision_id INTEGER REFERENCES decisions(decision_id) ON DELETE RESTRICT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    CHECK (left_file_id < right_file_id),
    FOREIGN KEY (left_fingerprint_id, left_file_id)
        REFERENCES fingerprints(fingerprint_id, file_id) ON DELETE RESTRICT,
    FOREIGN KEY (right_fingerprint_id, right_file_id)
        REFERENCES fingerprints(fingerprint_id, file_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX decisions_one_active_pair
ON decisions(left_file_id, right_file_id) WHERE active = 1;

CREATE TABLE review_items (
    review_id INTEGER PRIMARY KEY,
    candidate_file_id TEXT NOT NULL,
    reference_file_id TEXT NOT NULL,
    left_fingerprint_id INTEGER NOT NULL,
    right_fingerprint_id INTEGER NOT NULL,
    classification TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN {REVIEW_STATES}),
    decision_id INTEGER REFERENCES decisions(decision_id) ON DELETE RESTRICT,
    queue_path TEXT,
    evidence_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (candidate_file_id != reference_file_id),
    CHECK (state != 'decided' OR decision_id IS NOT NULL),
    FOREIGN KEY (left_fingerprint_id, candidate_file_id)
        REFERENCES fingerprints(fingerprint_id, file_id) ON DELETE RESTRICT,
    FOREIGN KEY (right_fingerprint_id, reference_file_id)
        REFERENCES fingerprints(fingerprint_id, file_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX review_one_open_pair
ON review_items(candidate_file_id, reference_file_id, left_fingerprint_id, right_fingerprint_id)
WHERE state IN ('pending', 'deferred');

CREATE TABLE pair_cache (
    left_fingerprint_id INTEGER NOT NULL REFERENCES fingerprints(fingerprint_id) ON DELETE RESTRICT,
    right_fingerprint_id INTEGER NOT NULL REFERENCES fingerprints(fingerprint_id) ON DELETE RESTRICT,
    auditor_version TEXT NOT NULL,
    configuration_hash TEXT NOT NULL,
    classification TEXT NOT NULL,
    evidence_json TEXT,
    completed INTEGER NOT NULL CHECK (completed IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (left_fingerprint_id < right_fingerprint_id),
    PRIMARY KEY (
        left_fingerprint_id, right_fingerprint_id, auditor_version, configuration_hash
    )
);

CREATE TABLE actual_runs (
    run_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('approved', 'active', 'finished', 'failed', 'cancelled')),
    house_root TEXT NOT NULL,
    temp_root TEXT NOT NULL,
    backup_path TEXT NOT NULL,
    backup_sha256 TEXT NOT NULL,
    backup_dev INTEGER,
    backup_ino INTEGER,
    backup_ctime_ns INTEGER,
    backup_size INTEGER,
    backup_mtime_ns INTEGER,
    manifest_path TEXT,
    manifest_sha256 TEXT,
    manifest_dev INTEGER,
    manifest_ino INTEGER,
    manifest_ctime_ns INTEGER,
    manifest_size INTEGER,
    manifest_mtime_ns INTEGER,
    activation_claim TEXT,
    approved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    activated_at TEXT,
    finished_at TEXT,
    error TEXT
);

CREATE TABLE operations (
    operation_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    action TEXT NOT NULL,
    source_path TEXT NOT NULL,
    dest_path TEXT,
    quarantine_path TEXT,
    file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE RESTRICT,
    keep_file_id TEXT REFERENCES files(file_id) ON DELETE RESTRICT,
    expected_size INTEGER NOT NULL CHECK (expected_size >= 0),
    expected_mtime_ns INTEGER NOT NULL CHECK (expected_mtime_ns >= 0),
    expected_fingerprint_id INTEGER NOT NULL REFERENCES fingerprints(fingerprint_id) ON DELETE RESTRICT,
    expected_keep_fingerprint_id INTEGER REFERENCES fingerprints(fingerprint_id) ON DELETE RESTRICT,
    parent_operation_id INTEGER REFERENCES operations(operation_id) ON DELETE RESTRICT,
    source_dev INTEGER,
    source_ino INTEGER,
    source_ctime_ns INTEGER,
    source_sha256 TEXT,
    destination_dev INTEGER,
    destination_ino INTEGER,
    destination_ctime_ns INTEGER,
    destination_size INTEGER,
    destination_mtime_ns INTEGER,
    destination_sha256 TEXT,
    state TEXT NOT NULL DEFAULT 'planned' CHECK (state IN {OPERATION_STATES}),
    error TEXT,
    purged_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO settings(key, value) VALUES ('actual_mutation_enabled', '0');
PRAGMA user_version = {SCHEMA_VERSION};
"""


def _enum_sql(values: Iterable[str]) -> str:
    return "(" + ", ".join(repr(value) for value in values) + ")"


# Tuples interpolate as Python tuple literals, which are valid for 2+ values.
assert _enum_sql(ASSIGNMENT_STATES)


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


def connect_state_db_readonly(path: os.PathLike | str) -> sqlite3.Connection:
    db_path = Path(path).resolve()
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(
        f"file:{db_path.as_posix()}?mode=ro", uri=True,
        timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
    return conn


def initialize_state_db(path: os.PathLike | str) -> sqlite3.Connection:
    conn = connect_state_db(path, create=True)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
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
    validate_schema(conn)
    return conn


def validate_schema(conn: sqlite3.Connection) -> None:
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

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"integrity_check failed: {integrity}")


def verify_state_db_ready(path: os.PathLike | str) -> Tuple[bool, str]:
    """Read-only readiness check used by the Phase 0 mutation gate."""
    db_path = Path(path)
    if not db_path.is_file():
        return False, "state DB does not exist"

    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
            validate_schema(conn)
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


def _verify_backup_evidence(backup_path: str, expected_sha256: str) -> None:
    path = Path(backup_path)
    if not path.is_file():
        raise RuntimeError(f"approved backup does not exist: {backup_path}")
    try:
        actual_sha256 = sha256_file(path)
    except OSError as exc:
        raise RuntimeError(f"approved backup cannot be read: {backup_path}: {exc}") from exc
    if actual_sha256 != expected_sha256:
        raise RuntimeError(f"approved backup SHA-256 mismatch: {backup_path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
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
    backup_path = canonicalize_path(backup_path)
    backup_sha256 = sha256_file(backup_path)
    _verify_backup_evidence(backup_path, backup_sha256)
    run_id = f"actual-{uuid.uuid4()}"
    with transaction(conn):
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


def prepare_actual_run(path, house_dir, temp_dir):
    """Consume one approval before any filesystem mutation and record its manifest."""
    ready, reason = verify_state_db_ready(path)
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
        for label, root_value in (("house", house_dir), ("temp", temp_dir)):
            root = Path(root_value).resolve()
            if not root.exists():
                continue
            for item in sorted(root.rglob("*")):
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
    return target


def canonical_rational(value) -> Optional[Tuple[int, int]]:
    if value is None or value == "":
        return None
    try:
        decimal_value = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid numeric coordinate: {value!r}") from exc
    if not decimal_value.is_finite() or decimal_value < 0:
        raise ValueError(f"invalid numeric coordinate: {value!r}")
    fraction = Fraction(decimal_value)
    return fraction.numerator, fraction.denominator


def canonical_symbol(value: str) -> Tuple[str, int]:
    key = str(value).strip().lower()
    try:
        return _SYMBOL_COORDINATES[key]
    except KeyError as exc:
        raise ValueError(f"unknown symbolic coordinate: {value!r}") from exc


def coordinate_sort_token(kind: str, value) -> tuple:
    if kind == "numeric":
        rational = canonical_rational(value)
        if rational is None:
            raise ValueError("numeric coordinate requires a value")
        return 0, Fraction(*rational)
    symbol, sort_key = canonical_symbol(value)
    return 1, sort_key, symbol


def coordinate_fields_from_name(name: str) -> dict:
    """Return the canonical coordinate columns derived from the current parser."""
    from normalizer import analyze_name

    filename = Path(name).name
    stem = Path(filename).stem
    info = analyze_name(filename)
    part = None
    if info["volume_number"] is not None:
        part, _ = info["volume_number"]
    part_rational = canonical_rational(part)

    masked = re.sub(
        r"\d+(?:\.\d+)?\s*(?:권\s*)?[-~]\s*\d+(?:\.\d+)?\s*권",
        " ",
        stem,
    )
    numeric_matches = re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*권", masked)
    volume_rational = canonical_rational(numeric_matches[0]) if len(numeric_matches) == 1 else None
    symbol_match = re.search(r"(상권|중권|하권)\s*$", stem)
    if symbol_match is None:
        symbol_match = re.search(r"(?:^|\s)(상|중|하)\s*$", stem)
    if symbol_match is None:
        symbol_match = re.search(r"(본편|특별편)\s*$", stem)
    symbol = sort_key = None
    coordinate_raw = None
    if symbol_match:
        symbol, sort_key = canonical_symbol(symbol_match.group(1))
        coordinate_raw = symbol_match.group(1)
    elif volume_rational is not None:
        coordinate_raw = numeric_matches[0]
    elif info["is_side_story"]:
        symbol, sort_key = canonical_symbol("외전")
        coordinate_raw = "외전"

    if symbol is not None:
        coordinate_kind = "symbol"
    elif volume_rational is not None:
        coordinate_kind = "volume"
    elif part_rational is not None:
        coordinate_kind = "part"
    elif info["start_number"] is not None and info["end_number"] is not None:
        coordinate_kind = "episode"
    else:
        coordinate_kind = None
    has_book_coordinate = symbol is not None or volume_rational is not None
    return {
        "coordinate_kind": coordinate_kind,
        "part_num": part_rational[0] if part_rational else None,
        "part_den": part_rational[1] if part_rational else None,
        "volume_num": volume_rational[0] if volume_rational else None,
        "volume_den": volume_rational[1] if volume_rational else None,
        "coordinate_symbol": symbol,
        "coordinate_sort_key": sort_key,
        "episode_start": None if has_book_coordinate else info["start_number"],
        "episode_end": None if has_book_coordinate else info["end_number"],
        "coordinate_raw": coordinate_raw or stem,
        "span_ambiguous": 1 if info["span_ambiguous"] else 0,
    }


def coordinates_compatible(left, right) -> bool:
    """Use one fail-closed coordinate contract for every mutation path."""
    if left["span_ambiguous"] or right["span_ambiguous"]:
        return False
    if left["coordinate_kind"] is not None and right["coordinate_kind"] is not None:
        if left["coordinate_kind"] != right["coordinate_kind"]:
            return False
    for numerator, denominator in (("part_num", "part_den"), ("volume_num", "volume_den")):
        if left[numerator] is not None and right[numerator] is not None:
            left_value = Fraction(left[numerator], left[denominator] or 1)
            right_value = Fraction(right[numerator], right[denominator] or 1)
            if left_value != right_value:
                return False
    if left["coordinate_symbol"] != right["coordinate_symbol"]:
        if left["coordinate_symbol"] is not None or right["coordinate_symbol"] is not None:
            return False
    if None not in (
        left["episode_start"], left["episode_end"],
        right["episode_start"], right["episode_end"],
    ):
        if left["episode_end"] < right["episode_start"] or right["episode_end"] < left["episode_start"]:
            return False
    return True


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
            source_dev, source_ino, source_ctime_ns, source_sha256, state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned')
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
            source_dev,
            source_ino,
            source_ctime_ns,
            source_sha256,
        ),
    )
    return cursor.lastrowid


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
    elif row["action"] == "house_review_move":
        assert_actual_run_path(actual_run, source, "house_root")
        assert_actual_run_path(actual_run, destination, "temp_root")
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
            source_bucket = {
                "queue_restore": "queue", "user_queue_restore": "queue",
                "user_queue_accept": "queue",
                "house_review_move": "house",
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
            }.get(row["action"], "temp")
            _finalize_existing_source_rollback(
                conn, row, source, source_bucket
            )
            return "rolled_back"
        with transaction(conn):
            transition_operation(conn, operation_id, "failed", error="both queue paths missing")
        return "failed"
    file_row = conn.execute(
        "SELECT canonical_path, source FROM files WHERE file_id = ?", (row["file_id"],)
    ).fetchone()
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
    }:
        return _recover_interrupted_queue_operation(conn, operation_id)
    if row["action"] == "quarantine_purge":
        return _recover_interrupted_purge_operation(conn, operation_id)
    raise ValueError(f"unsupported recovery action: {row['action']}")


def doctor_issues(conn: sqlite3.Connection, *, allowed_active_run_id=None):
    issues = []
    try:
        validate_schema(conn)
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
        if row["dev"] is not None and (
            row["dev"] != stat.st_dev
            or row["ino"] != stat.st_ino
            or row["ctime_ns"] != stat.st_ctime_ns
        ):
            issues.append({"kind": "stale_identity", "file_id": row["file_id"], "path": str(path)})
        if row["current_fingerprint_id"] is None and row["assignment_state"] == "managed":
            issues.append({"kind": "missing_fingerprint", "file_id": row["file_id"]})
    for row in conn.execute(
        """
        SELECT r.variant_id, r.file_id, f.protected, f.active, f.assignment_state
        FROM representatives AS r JOIN files AS f ON f.file_id = r.file_id
        """
    ):
        if not row["protected"] or not row["active"] or row["assignment_state"] != "managed":
            issues.append({
                "kind": "invalid_representative",
                "variant_id": row["variant_id"],
                "file_id": row["file_id"],
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


def canonicalize_path(path: os.PathLike | str) -> str:
    return unicodedata.normalize("NFC", os.path.abspath(os.fspath(path)))


def canonicalize_real_path(path: os.PathLike | str) -> str:
    return unicodedata.normalize(
        "NFC", os.path.realpath(os.path.abspath(os.fspath(path)))
    )


def _symlink_component(path: os.PathLike | str):
    current = Path(os.path.abspath(os.fspath(path)))
    for component in (current, *current.parents):
        if component.is_symlink():
            return component
    return None


def reconcile_file_metadata(
    conn: sqlite3.Connection,
    path: os.PathLike | str,
    *,
    source: str,
    legacy_marker: bool = False,
):
    """Create/update one stable file identity without inferring any decision.

    Existing managed content changes are downgraded to decision_required and
    their old immutable fingerprint remains as provenance.  A marker on an
    already managed file is treated as display-only; markers only initialize
    previously unseen/unassigned files as legacy_unresolved.
    """
    canonical_path = canonicalize_path(path)
    stat = os.stat(canonical_path, follow_symlinks=False)
    coordinates = coordinate_fields_from_name(Path(canonical_path).name)
    row = conn.execute(
        "SELECT * FROM files WHERE canonical_path = ?", (canonical_path,)
    ).fetchone()
    if row is None:
        file_id = str(uuid.uuid4())
        assignment_state = "legacy_unresolved" if legacy_marker else "unassigned"
        conn.execute(
            """
            INSERT INTO files(
                file_id, canonical_path, source, size, mtime_ns, dev, ino, ctime_ns,
                assignment_state, active, coordinate_kind,
                part_num, part_den, volume_num, volume_den,
                coordinate_symbol, coordinate_sort_key, episode_start, episode_end,
                coordinate_raw, span_ambiguous
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                canonical_path,
                source,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_dev,
                stat.st_ino,
                stat.st_ctime_ns,
                assignment_state,
                coordinates["coordinate_kind"],
                coordinates["part_num"],
                coordinates["part_den"],
                coordinates["volume_num"],
                coordinates["volume_den"],
                coordinates["coordinate_symbol"],
                coordinates["coordinate_sort_key"],
                coordinates["episode_start"],
                coordinates["episode_end"],
                coordinates["coordinate_raw"],
                coordinates["span_ambiguous"],
            ),
        )
    else:
        file_id = row["file_id"]
        changed = (
            row["size"] != stat.st_size
            or row["mtime_ns"] != stat.st_mtime_ns
            or (row["dev"] is not None and row["dev"] != stat.st_dev)
            or (row["ino"] is not None and row["ino"] != stat.st_ino)
            or (row["ctime_ns"] is not None and row["ctime_ns"] != stat.st_ctime_ns)
        )
        assignment_state = row["assignment_state"]
        assignment_origin = row["assignment_origin"]
        current_fingerprint_id = row["current_fingerprint_id"]
        if changed:
            current_fingerprint_id = None
            if assignment_state == "managed":
                assignment_state = "decision_required"
                assignment_origin = None
        elif assignment_state == "unassigned" and legacy_marker:
            assignment_state = "legacy_unresolved"
        conn.execute(
            """
            UPDATE files
            SET source = ?, size = ?, mtime_ns = ?, dev = ?, ino = ?, ctime_ns = ?,
                last_seen_at = CURRENT_TIMESTAMP,
                assignment_state = ?, assignment_origin = ?, current_fingerprint_id = ?, active = 1
                , coordinate_kind = ?, part_num = ?, part_den = ?, volume_num = ?, volume_den = ?
                , coordinate_symbol = ?, coordinate_sort_key = ?, episode_start = ?, episode_end = ?
                , coordinate_raw = ?, span_ambiguous = ?
            WHERE file_id = ?
            """,
            (
                source,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_dev,
                stat.st_ino,
                stat.st_ctime_ns,
                assignment_state,
                assignment_origin,
                current_fingerprint_id,
                coordinates["coordinate_kind"],
                coordinates["part_num"],
                coordinates["part_den"],
                coordinates["volume_num"],
                coordinates["volume_den"],
                coordinates["coordinate_symbol"],
                coordinates["coordinate_sort_key"],
                coordinates["episode_start"],
                coordinates["episode_end"],
                coordinates["coordinate_raw"],
                coordinates["span_ambiguous"],
                file_id,
            ),
        )

    return conn.execute(
        """
        SELECT
            f.canonical_path, f.file_id, f.variant_id, f.assignment_state,
            f.protected, v.work_bucket_id,
            CASE WHEN r.file_id IS NULL THEN 0 ELSE 1 END AS representative
        FROM files AS f
        LEFT JOIN variants AS v ON v.variant_id = f.variant_id
        LEFT JOIN representatives AS r ON r.file_id = f.file_id
        WHERE f.file_id = ?
        """,
        (file_id,),
    ).fetchone()


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


def _assert_no_active_actual_run(conn):
    row = conn.execute(
        "SELECT run_id FROM actual_runs WHERE state = 'active' LIMIT 1"
    ).fetchone()
    if row is not None:
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
) -> int:
    _assert_no_active_actual_run(conn)
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
