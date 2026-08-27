import errno
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

import decision_store
import fingerprint_payloads
import migrate_fingerprint_payloads as migration


def _v17_fixture(tmp_path):
    state_db = tmp_path / "state.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
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
        path = house / "legacy.txt"
        path.write_bytes(b"legacy-data")
        info = path.stat()
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
    return state_db, house, temp, fingerprint_id


def _convert_fixture_to_v18(state_db, evidence_path):
    source = decision_store.connect_state_db(state_db)
    destination = sqlite3.connect(evidence_path)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()
    conn = decision_store.connect_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            fingerprint_payloads.migrate_legacy_anchor_payloads(conn)
            conn.execute("DROP TRIGGER fingerprints_no_update")
            conn.execute("DROP TRIGGER fingerprints_no_delete")
            conn.execute(
                "UPDATE fingerprints SET front_anchor = NULL, tail_anchor = NULL"
            )
            conn.execute(
                """
                CREATE TRIGGER fingerprints_no_update
                BEFORE UPDATE ON fingerprints
                BEGIN SELECT RAISE(ABORT, 'fingerprints are immutable'); END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER fingerprints_no_delete
                BEFORE DELETE ON fingerprints
                BEGIN SELECT RAISE(ABORT, 'fingerprints are immutable'); END
                """
            )
            conn.execute("PRAGMA user_version = 18")
    finally:
        conn.close()


def test_dedicated_migration_preflight_and_report_are_idempotent(tmp_path):
    state_db, house, temp, fingerprint_id = _v17_fixture(tmp_path)
    plan = migration.build_plan(state_db, allow_dirty=True)
    assert plan["apply_available"] is True
    assert plan["source_extent_bytes"] >= max(
        plan["main_bytes"] + plan["wal_bytes"] + plan["journal_bytes"],
        plan["logical_page_bytes"],
    )
    assert plan["required_free_bytes"] == (
        plan["backup_reserve_bytes"]
        + plan["migration_wal_reserve_bytes"]
        + plan["vacuum_reserve_bytes"]
        + plan["safety_margin_bytes"]
    )

    result = migration.apply_migration(
        state_db, house_dir=house, temp_dir=temp, allow_dirty=True
    )
    assert result["status"] == "succeeded"
    assert result["schema_after"] == 19
    assert Path(result["backup_path"]).is_file()
    assert Path(result["report_path"]).is_file()
    journal = json.loads(
        migration._journal_path(state_db).read_text()
    )
    assert journal["phase"] == "reported"

    conn = decision_store.connect_state_db(state_db)
    try:
        assert decision_store.load_fingerprint_anchor_payload(
            conn, fingerprint_id
        ) == ("front", "tail")
        assert str(Path(result["backup_path"]).resolve()) in (
            decision_store.protected_state_backup_paths(conn)
        )
    finally:
        conn.close()
    assert migration.apply_migration(
        state_db, house_dir=house, temp_dir=temp, allow_dirty=True
    )["report_path"] == result["report_path"]


def test_migration_resumes_after_logical_commit_without_new_backup(
    tmp_path, monkeypatch
):
    state_db, house, temp, _fingerprint_id = _v17_fixture(tmp_path)

    def fail_after_commit(name):
        if name == "logical_committed":
            raise RuntimeError("injected crash after logical commit")

    monkeypatch.setattr(migration, "_migration_failpoint", fail_after_commit)
    with pytest.raises(RuntimeError, match="injected crash"):
        migration.apply_migration(
            state_db, house_dir=house, temp_dir=temp, allow_dirty=True
        )

    probe = decision_store.connect_state_db(state_db)
    try:
        assert probe.execute("PRAGMA user_version").fetchone()[0] == 19
        assert probe.execute(
            "SELECT value FROM settings "
            "WHERE key = 'fingerprint_payload_migration_gate'"
        ).fetchone()[0] == "active"
    finally:
        probe.close()
    assert len(list((state_db.parent / "backups").glob("*.sqlite3"))) == 1

    monkeypatch.setattr(migration, "_migration_failpoint", lambda _name: None)
    result = migration.apply_migration(
        state_db, house_dir=house, temp_dir=temp, allow_dirty=True
    )
    assert result["status"] == "succeeded"
    assert len(list((state_db.parent / "backups").glob("*.sqlite3"))) == 1
    probe = decision_store.connect_state_db(state_db)
    try:
        assert probe.execute(
            "SELECT value FROM settings "
            "WHERE key = 'fingerprint_payload_migration_gate'"
        ).fetchone() is None
    finally:
        probe.close()


def test_migration_holds_one_writer_epoch_through_backup(tmp_path, monkeypatch):
    state_db, house, temp, _fingerprint_id = _v17_fixture(tmp_path)
    original = migration._create_verified_backup
    observed = {"locked": False}

    def assert_locked(*args, **kwargs):
        outsider = sqlite3.connect(state_db, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                outsider.execute(
                    "INSERT INTO settings(key, value) VALUES ('outsider', 'write')"
                )
                outsider.commit()
            observed["locked"] = True
        finally:
            outsider.close()
        return original(*args, **kwargs)

    monkeypatch.setattr(migration, "_create_verified_backup", assert_locked)
    migration.apply_migration(
        state_db, house_dir=house, temp_dir=temp, allow_dirty=True
    )
    assert observed["locked"] is True


def test_backup_enospc_leaves_source_and_resumes_same_journal(
    tmp_path, monkeypatch
):
    state_db, house, temp, _fingerprint_id = _v17_fixture(tmp_path)
    original = migration._create_verified_backup

    def no_space(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "injected backup ENOSPC")

    monkeypatch.setattr(migration, "_create_verified_backup", no_space)
    with pytest.raises(OSError) as raised:
        migration.apply_migration(
            state_db, house_dir=house, temp_dir=temp, allow_dirty=True
        )
    assert raised.value.errno == errno.ENOSPC
    probe = sqlite3.connect(state_db)
    try:
        assert probe.execute("PRAGMA user_version").fetchone()[0] == 17
    finally:
        probe.close()
    journal_path = migration._journal_path(state_db)
    first_journal = json.loads(journal_path.read_text())
    assert first_journal["phase"] == "preparing"

    monkeypatch.setattr(migration, "_create_verified_backup", original)
    result = migration.apply_migration(
        state_db, house_dir=house, temp_dir=temp, allow_dirty=True
    )
    final_journal = json.loads(journal_path.read_text())
    assert result["status"] == "succeeded"
    assert final_journal["migration_id"] == first_journal["migration_id"]
    assert len(list((state_db.parent / "backups").glob("*.sqlite3"))) == 1


def test_v18_requires_legacy_evidence_and_checks_id_hash_mapping(tmp_path):
    state_db, house, temp, fingerprint_id = _v17_fixture(tmp_path)
    evidence = tmp_path / "schema-v17-evidence.sqlite3"
    _convert_fixture_to_v18(state_db, evidence)
    plan = migration.build_plan(state_db, allow_dirty=True)
    assert plan["apply_available"] is False
    assert "legacy_anchor_backup_required" in plan["blockers"]
    assert migration.build_plan(
        state_db, legacy_anchor_backup=evidence, allow_dirty=True
    )["apply_available"] is True

    conn = decision_store.connect_state_db(state_db)
    try:
        conn.execute("DROP TRIGGER fingerprint_anchor_refs_no_delete")
        conn.execute(
            "DELETE FROM fingerprint_anchor_refs WHERE fingerprint_id = ?",
            (fingerprint_id,),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RuntimeError, match="mapping mismatch"):
        migration.apply_migration(
            state_db,
            legacy_anchor_backup=evidence,
            house_dir=house,
            temp_dir=temp,
            allow_dirty=True,
        )
    probe = sqlite3.connect(state_db)
    try:
        assert probe.execute("PRAGMA user_version").fetchone()[0] == 18
    finally:
        probe.close()


def test_v18_success_records_and_protects_both_migration_artifacts(tmp_path):
    state_db, house, temp, _fingerprint_id = _v17_fixture(tmp_path)
    evidence = tmp_path / "schema-v17-evidence.sqlite3"
    _convert_fixture_to_v18(state_db, evidence)

    result = migration.apply_migration(
        state_db,
        legacy_anchor_backup=evidence,
        house_dir=house,
        temp_dir=temp,
        allow_dirty=True,
    )

    conn = decision_store.connect_state_db(state_db)
    try:
        settings = dict(
            conn.execute(
                "SELECT key, value FROM settings WHERE key IN ("
                "'fingerprint_payload_rollback_backup', "
                "'fingerprint_payload_legacy_evidence_backup'"
                ")"
            )
        )
        protected = decision_store.protected_state_backup_paths(conn)
    finally:
        conn.close()
    assert settings["fingerprint_payload_rollback_backup"] == result["backup_path"]
    assert settings["fingerprint_payload_legacy_evidence_backup"] == str(
        evidence.resolve()
    )
    assert str(Path(result["backup_path"]).resolve()) in protected
    assert str(evidence.resolve()) in protected
