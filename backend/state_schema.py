"""Schema constants for the dedup decision database.

This module is declarative: importing it must never open or migrate SQLite.
"""

from __future__ import annotations


SCHEMA_VERSION = 15
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
    "operation_groups",
    "operations",
    "work_folders",
    "work_aliases",
    "work_management_events",
    "file_analysis",
    "catalog_titles",
    "catalog_platform_stats",
})
REQUIRED_VIEWS = frozenset({
    "catalog_title_metrics",
})


FILE_ANALYSIS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS file_analysis (
    file_id TEXT PRIMARY KEY REFERENCES files(file_id) ON DELETE CASCADE,
    normalizer_version TEXT NOT NULL,
    analyzed_name TEXT NOT NULL,
    core_title TEXT NOT NULL,
    readable_title TEXT NOT NULL,
    catalog_query_title TEXT NOT NULL,
    title_override_json TEXT,
    author TEXT,
    max_number INTEGER NOT NULL CHECK (max_number >= 0),
    effective_max INTEGER NOT NULL CHECK (effective_max >= 0),
    unit TEXT NOT NULL,
    complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
    disambig INTEGER NOT NULL CHECK (disambig > 0),
    analyzed_size INTEGER NOT NULL CHECK (analyzed_size >= 0),
    analyzed_mtime_ns INTEGER NOT NULL CHECK (analyzed_mtime_ns >= 0),
    analyzed_ctime_ns INTEGER,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS file_analysis_core_title
ON file_analysis(core_title);
"""


CATALOG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS catalog_titles (
    title_key TEXT PRIMARY KEY,
    display_title TEXT NOT NULL,
    query_title TEXT NOT NULL,
    normalizer_version TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalog_platform_stats (
    title_key TEXT NOT NULL REFERENCES catalog_titles(title_key) ON DELETE CASCADE,
    platform TEXT NOT NULL CHECK (platform IN ('series', 'kakao', 'novelpia')),
    status TEXT NOT NULL CHECK (status IN ('ok', 'not_found', 'error', 'skipped')),
    remote_id TEXT,
    remote_title TEXT,
    remote_url TEXT,
    download_count INTEGER CHECK (download_count IS NULL OR download_count >= 0),
    -- v8 compatibility column. New writes use download_count.
    interest_count INTEGER CHECK (interest_count IS NULL OR interest_count >= 0),
    view_count INTEGER CHECK (view_count IS NULL OR view_count >= 0),
    recommend_count INTEGER CHECK (recommend_count IS NULL OR recommend_count >= 0),
    rating REAL CHECK (rating IS NULL OR rating >= 0),
    rating_count INTEGER CHECK (rating_count IS NULL OR rating_count >= 0),
    last_attempt_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_success_at TEXT,
    retry_after TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (title_key, platform)
);

CREATE INDEX IF NOT EXISTS catalog_platform_stats_refresh
ON catalog_platform_stats(platform, status, retry_after);

DROP VIEW IF EXISTS catalog_title_metrics;
CREATE VIEW catalog_title_metrics AS
SELECT
    title.title_key,
    title.display_title,
    title.query_title,
    MAX(CASE WHEN stat.platform = 'series' THEN
        COALESCE(stat.download_count, stat.interest_count) END) AS series_download_count,
    MAX(CASE WHEN stat.platform = 'series' THEN stat.rating END) AS series_rating,
    MAX(CASE WHEN stat.platform = 'kakao' THEN stat.view_count END) AS kakao_view_count,
    MAX(CASE WHEN stat.platform = 'kakao' THEN stat.rating END) AS kakao_rating,
    MAX(CASE WHEN stat.platform = 'novelpia' THEN stat.view_count END) AS novelpia_view_count,
    MAX(CASE WHEN stat.platform = 'novelpia' THEN stat.recommend_count END) AS novelpia_recommend_count,
    MAX(CASE WHEN stat.platform = 'series' THEN stat.status END) AS series_status,
    MAX(CASE WHEN stat.platform = 'kakao' THEN stat.status END) AS kakao_status,
    MAX(CASE WHEN stat.platform = 'novelpia' THEN stat.status END) AS novelpia_status
FROM catalog_titles AS title
LEFT JOIN catalog_platform_stats AS stat ON stat.title_key = title.title_key
GROUP BY title.title_key, title.display_title, title.query_title;
"""


SCHEMA_SQL = f"""
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE works (
    work_bucket_id INTEGER PRIMARY KEY,
    display_title TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE variants (
    variant_id INTEGER PRIMARY KEY,
    work_bucket_id INTEGER NOT NULL REFERENCES works(work_bucket_id) ON DELETE RESTRICT,
    variant_kind TEXT NOT NULL DEFAULT 'base'
        CHECK (variant_kind IN ('base', 'revision', 'adult', 'translation', 'other')),
    label TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
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

CREATE TABLE operation_groups (
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

CREATE INDEX operation_groups_run_state
ON operation_groups(run_id, state);

CREATE TABLE work_folders (
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

CREATE INDEX work_folders_work_state
ON work_folders(work_bucket_id, state, role);

CREATE UNIQUE INDEX work_folders_one_primary
ON work_folders(work_bucket_id)
WHERE state = 'active' AND role = 'primary';

CREATE TABLE work_aliases (
    alias_id INTEGER PRIMARY KEY,
    alias_kind TEXT NOT NULL CHECK (
        alias_kind IN ('core_title', 'readable_title', 'folder_name')
    ),
    alias_key TEXT NOT NULL,
    alias_display TEXT NOT NULL,
    work_bucket_id INTEGER NOT NULL REFERENCES works(work_bucket_id) ON DELETE RESTRICT,
    preferred_folder_id INTEGER REFERENCES work_folders(folder_id) ON DELETE RESTRICT,
    origin TEXT NOT NULL DEFAULT 'human_decision'
        CHECK (origin IN ('human_decision', 'strong_match')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    supersedes_alias_id INTEGER REFERENCES work_aliases(alias_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX work_aliases_one_active_key
ON work_aliases(alias_kind, alias_key) WHERE active = 1;

CREATE INDEX work_aliases_work_active
ON work_aliases(work_bucket_id, active, alias_kind);

CREATE TABLE work_management_events (
    event_id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    source_work_id INTEGER REFERENCES works(work_bucket_id) ON DELETE RESTRICT,
    target_work_id INTEGER REFERENCES works(work_bucket_id) ON DELETE RESTRICT,
    plan_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'local_user',
    supersedes_event_id INTEGER
        REFERENCES work_management_events(event_id) ON DELETE RESTRICT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX work_management_events_work
ON work_management_events(source_work_id, target_work_id, created_at);

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
    operation_group_id INTEGER REFERENCES operation_groups(group_id) ON DELETE RESTRICT,
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

CREATE INDEX operations_group_state
ON operations(operation_group_id, state);

{FILE_ANALYSIS_SCHEMA_SQL}

{CATALOG_SCHEMA_SQL}

INSERT INTO settings(key, value) VALUES ('actual_mutation_enabled', '0');
PRAGMA user_version = {SCHEMA_VERSION};
"""
