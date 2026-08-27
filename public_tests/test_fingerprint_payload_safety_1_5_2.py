import hashlib
import inspect
import json
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest

import decision_store
import library_server
import migrate_fingerprint_payloads as migration
import run_folderling_one_button
import run_platform_catalog
import state_repository


def _v17_fixture(tmp_path):
    state_db = tmp_path / "state" / "dedup.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    path = house / "legacy.txt"
    path.write_text("legacy payload fixture", encoding="utf-8")
    info = path.stat()

    conn = decision_store.initialize_state_db(state_db)
    try:
        conn.executescript(
            """
            DROP TRIGGER fingerprints_insert_storage_guard;
            DROP TRIGGER fingerprint_anchor_refs_expected_hash;
            PRAGMA user_version = 17;
            """
        )
        file_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO files(file_id, canonical_path, source, size, mtime_ns) "
            "VALUES (?, ?, 'house', ?, ?)",
            (file_id, str(path), info.st_size, info.st_mtime_ns),
        )
        fingerprint_id = conn.execute(
            """
            INSERT INTO fingerprints(
                file_id, canonical_path, size, mtime_ns,
                normalizer_version, fingerprint_version, status,
                front_anchor, tail_anchor
            ) VALUES (?, ?, ?, ?, 'legacy', 'legacy', 'ok', 'front', 'tail')
            """,
            (file_id, str(path), info.st_size, info.st_mtime_ns),
        ).lastrowid
        conn.execute(
            "UPDATE files SET current_fingerprint_id = ? WHERE file_id = ?",
            (fingerprint_id, file_id),
        )
        conn.commit()
    finally:
        conn.close()
    return state_db, house, temp


def _set_active_gate(state_db):
    conn = decision_store.connect_state_db(state_db)
    try:
        conn.execute(
            "INSERT INTO settings(key, value) "
            "VALUES ('fingerprint_payload_migration_gate', 'active')"
        )
        conn.commit()
    finally:
        conn.close()


def _server_app(tmp_path, state_db, house, temp):
    return library_server.create_app(
        state_db=state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=tmp_path / "file_index.json",
        runtime_dir=tmp_path / "runtime",
        frontend_dist=tmp_path / "dist",
        project_root=Path(__file__).resolve().parents[1],
    )


def test_payload_migration_is_not_exposed_through_initialize_facade(tmp_path):
    parameters = inspect.signature(decision_store.initialize_state_db).parameters
    assert "anchor_payload_migration" not in parameters
    assert "legacy_anchor_expectations" not in parameters
    assert "require_legacy_expectations" not in parameters
    assert not hasattr(decision_store, "migrate_anchor_payload_schema_connection")

    state_db, _house, _temp = _v17_fixture(tmp_path)
    conn = decision_store.connect_state_db(state_db)
    try:
        conn.execute("PRAGMA user_version = 18")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError, match="schema-v17 anchor backup"):
            state_repository._migrate_anchor_payload_schema_connection(conn, 18)
        conn.rollback()
    finally:
        conn.close()
    with pytest.raises(RuntimeError, match="accepted only"):
        decision_store.initialize_state_db(state_db, migrate=True)


def test_precommit_resume_rechecks_epoch_and_rebuilds_rollback_backup(
    tmp_path, monkeypatch
):
    state_db, house, temp = _v17_fixture(tmp_path)

    def crash_after_backup(name):
        if name == "backup_fsynced":
            raise RuntimeError("injected crash")

    monkeypatch.setattr(migration, "_migration_failpoint", crash_after_backup)
    with pytest.raises(RuntimeError, match="injected crash"):
        migration.apply_migration(
            state_db,
            house_dir=house,
            temp_dir=temp,
            allow_dirty=True,
        )

    conn = decision_store.connect_state_db(state_db)
    try:
        conn.execute("INSERT INTO settings(key, value) VALUES ('between_epochs', 'kept')")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(migration, "_migration_failpoint", lambda _name: None)
    result = migration.apply_migration(
        state_db,
        house_dir=house,
        temp_dir=temp,
        allow_dirty=True,
    )
    backup = sqlite3.connect(result["backup_path"])
    try:
        assert backup.execute(
            "SELECT value FROM settings WHERE key = 'between_epochs'"
        ).fetchone()[0] == "kept"
    finally:
        backup.close()
    journal = json.loads(migration._journal_path(state_db).read_text())
    assert journal["writer_epoch"] == 2


def test_precommit_resume_rechecks_new_dynamic_blockers(tmp_path, monkeypatch):
    state_db, house, temp = _v17_fixture(tmp_path)

    def crash_after_backup(name):
        if name == "backup_fsynced":
            raise RuntimeError("injected crash")

    monkeypatch.setattr(migration, "_migration_failpoint", crash_after_backup)
    with pytest.raises(RuntimeError, match="injected crash"):
        migration.apply_migration(
            state_db,
            house_dir=house,
            temp_dir=temp,
            allow_dirty=True,
        )
    conn = decision_store.connect_state_db(state_db)
    try:
        conn.execute(
            """
            INSERT INTO actual_runs(
                run_id, state, house_root, temp_root, backup_path, backup_sha256
            ) VALUES ('new-blocker', 'approved', ?, ?, ?, ?)
            """,
            (str(house), str(temp), str(tmp_path / "run.sqlite3"), "test"),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(migration, "_migration_failpoint", lambda _name: None)
    with pytest.raises(RuntimeError, match="actual_run:new-blocker:approved"):
        migration.apply_migration(
            state_db,
            house_dir=house,
            temp_dir=temp,
            allow_dirty=True,
        )
    probe = sqlite3.connect(state_db)
    try:
        assert probe.execute("PRAGMA user_version").fetchone()[0] == 17
    finally:
        probe.close()


def test_active_gate_blocks_schema_transactions_and_new_server(tmp_path):
    state_db = tmp_path / "state.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    conn = decision_store.initialize_state_db(state_db)
    conn.close()
    _set_active_gate(state_db)

    conn = decision_store.connect_state_db(state_db)
    try:
        with pytest.raises(RuntimeError, match="migration is incomplete"):
            decision_store.validate_schema(conn, check_integrity=False)
        with pytest.raises(RuntimeError, match="migration is incomplete"):
            with decision_store.transaction(conn):
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES ('forbidden', 'write')"
                )
        assert conn.execute(
            "SELECT COUNT(*) FROM settings WHERE key = 'forbidden'"
        ).fetchone()[0] == 0
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="migration is incomplete"):
        _server_app(tmp_path, state_db, house, temp)
    assert not (tmp_path / "runtime").exists()


def test_active_gate_blocks_folderling_and_platform_before_side_effects(
    tmp_path, monkeypatch
):
    state_db = tmp_path / "state" / "dedup.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    conn = decision_store.initialize_state_db(state_db)
    conn.close()
    _set_active_gate(state_db)

    with pytest.raises(RuntimeError, match="migration is incomplete"):
        run_folderling_one_button.run(temp, house, state_db)
    assert not (state_db.parent / "backups").exists()

    monkeypatch.setattr(run_platform_catalog, "HOUSE_DIR", house)
    monkeypatch.setattr(run_platform_catalog, "TEMP_DIR", temp)
    with pytest.raises(RuntimeError, match="migration is incomplete"):
        run_platform_catalog.ensure_catalog_schema(str(state_db))
    probe = sqlite3.connect(state_db)
    try:
        assert probe.execute("SELECT COUNT(*) FROM actual_runs").fetchone()[0] == 0
        assert probe.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
    finally:
        probe.close()


def test_health_reports_active_gate_for_already_running_server(tmp_path):
    state_db = tmp_path / "state.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    conn = decision_store.initialize_state_db(state_db)
    conn.close()
    app = _server_app(tmp_path, state_db, house, temp)
    try:
        _set_active_gate(state_db)
        response = app.test_client().get("/health")
        assert response.status_code == 503
        assert response.get_json()["migration_state"] == "active"
        assert response.get_json()["database"] == "maintenance"
    finally:
        keeper = app.extensions.get("library_state_db_readonly_keeper")
        if keeper is not None:
            keeper.close()


def test_reported_journal_requires_matching_current_db_and_explicit_reapply(
    tmp_path
):
    state_db, house, temp = _v17_fixture(tmp_path)
    first = migration.apply_migration(
        state_db,
        house_dir=house,
        temp_dir=temp,
        allow_dirty=True,
    )
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{state_db}{suffix}").unlink(missing_ok=True)
    state_db.unlink()
    shutil.copy2(first["backup_path"], state_db)

    with pytest.raises(RuntimeError, match="appears rolled back"):
        migration.apply_migration(
            state_db,
            house_dir=house,
            temp_dir=temp,
            allow_dirty=True,
        )
    second = migration.apply_migration(
        state_db,
        house_dir=house,
        temp_dir=temp,
        allow_dirty=True,
        reapply=True,
    )
    assert second["migration_id"] != first["migration_id"]
    assert list(migration._journal_path(state_db).parent.glob("*.stale-*.json"))


def test_source_fk_and_operational_doctor_block_before_backup(tmp_path):
    state_db, house, temp = _v17_fixture(tmp_path)
    conn = sqlite3.connect(state_db)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("UPDATE files SET current_fingerprint_id = 999999")
        conn.commit()
    finally:
        conn.close()

    plan = migration.build_plan(state_db, allow_dirty=True)
    assert plan["apply_available"] is False
    assert "foreign_key_check:1" in plan["blockers"]
    with pytest.raises(RuntimeError, match="foreign_key_check:1"):
        migration.apply_migration(
            state_db,
            house_dir=house,
            temp_dir=temp,
            allow_dirty=True,
        )
    assert not (state_db.parent / "backups").exists()


def test_dirty_migration_requires_explicit_override_and_records_provenance(
    tmp_path, monkeypatch
):
    state_db, house, temp = _v17_fixture(tmp_path)
    monkeypatch.setattr(
        migration,
        "resolve_build_info",
        lambda _root: {"build_commit": "a" * 40, "build_dirty": True},
    )
    monkeypatch.setattr(migration, "_git_status_porcelain", lambda _root: b"")
    monkeypatch.setattr(migration, "_git_diff_sha256", lambda _root: "d" * 64)

    blocked = migration.build_plan(state_db)
    assert blocked["apply_available"] is False
    assert any(item.startswith("build_provenance:") for item in blocked["blockers"])
    result = migration.apply_migration(
        state_db,
        house_dir=house,
        temp_dir=temp,
        allow_dirty=True,
    )
    provenance = result["source_provenance"]
    assert provenance["build_commit"] == "a" * 40
    assert provenance["build_dirty"] is True
    assert provenance["source_diff_sha256"] == "d" * 64
    assert len(provenance["migration_script_sha256"]) == 64
    assert len(provenance["schema_module_sha256"]) == 64

    conn = decision_store.connect_state_db(state_db)
    try:
        settings = dict(
            conn.execute(
                "SELECT key, value FROM settings "
                "WHERE key LIKE 'fingerprint_payload_migration_%'"
            )
        )
    finally:
        conn.close()
    assert settings["fingerprint_payload_migration_build_commit"] == "a" * 40
    assert settings["fingerprint_payload_migration_report_sha256"] == hashlib.sha256(
        Path(result["report_path"]).read_bytes()
    ).hexdigest()


def test_report_content_is_cross_checked_even_if_journal_checksum_is_updated(
    tmp_path
):
    state_db, house, temp = _v17_fixture(tmp_path)
    result = migration.apply_migration(
        state_db,
        house_dir=house,
        temp_dir=temp,
        allow_dirty=True,
    )
    report_path = Path(result["report_path"])
    report = json.loads(report_path.read_text())
    report["migration_id"] = "0" * 32
    migration._atomic_write_json(report_path, report)
    journal_path = migration._journal_path(state_db)
    journal = json.loads(journal_path.read_text())
    journal["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    migration._atomic_write_json(journal_path, journal)

    with pytest.raises(RuntimeError, match="differs from journal"):
        migration.apply_migration(
            state_db,
            house_dir=house,
            temp_dir=temp,
            allow_dirty=True,
        )


def test_journal_identity_and_checkpoint_busy_are_fail_closed(tmp_path):
    first = (tmp_path / "first.sqlite3").resolve()
    second = (tmp_path / "second.sqlite3").resolve()
    assert migration._journal_path(first) != migration._journal_path(second)

    class BusyConnection:
        def execute(self, _sql):
            return self

        def fetchone(self):
            return (1, 10, 5)

    with pytest.raises(RuntimeError, match="checkpoint is busy"):
        migration._checkpoint_truncate(BusyConnection())
