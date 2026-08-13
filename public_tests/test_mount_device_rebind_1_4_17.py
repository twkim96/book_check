"""Folderling 1.4.17 mount-device rebind regressions."""

import os
from pathlib import Path

import pytest

import decision_store
import run_folderling_one_button
from mutation_io import MutationLockBusy, mutation_lock_for_roots


def _fixture(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    return house, temp, state_db, conn


def _record_house_file(conn, path):
    path.write_text("same bytes", encoding="utf-8")
    with decision_store.transaction(conn):
        row = decision_store.reconcile_file_metadata(conn, path, source="house")
    return row["file_id"]


def _backup(conn, state_db, name="before_rebind.sqlite3"):
    return decision_store.backup_state_db(conn, state_db.parent / name)


def test_mount_device_rebind_repairs_only_complete_device_renumber(tmp_path):
    house, temp, state_db, conn = _fixture(tmp_path)
    first = _record_house_file(conn, house / "first.txt")
    second = _record_house_file(conn, house / "second.txt")
    current_dev = os.stat(house).st_dev
    old_dev = current_dev + 1000
    with decision_store.transaction(conn):
        conn.execute("UPDATE files SET dev = ? WHERE active = 1", (old_dev,))
    backup = _backup(conn, state_db)

    with mutation_lock_for_roots(house, temp, "test-device-rebind"):
        result = decision_store.rebind_mount_device_identities(
            conn,
            backup_path=backup,
            house_dir=house,
            temp_dir=temp,
        )

    assert result["applied"] is True
    assert result["file_count"] == 2
    assert result["folder_count"] == 0
    assert result["device_mappings"] == [
        {
            "roots": sorted((str(house.resolve()), str(temp.resolve()))),
            "old_dev": old_dev,
            "new_dev": current_dev,
        }
    ]
    assert not decision_store.doctor_issues(conn)
    assert {
        row["file_id"]: row["dev"]
        for row in conn.execute("SELECT file_id, dev FROM files WHERE active = 1")
    } == {first: current_dev, second: current_dev}
    conn.close()


def test_mount_device_rebind_preserves_decisions_and_fingerprint_pointers(tmp_path):
    house, temp, state_db, conn = _fixture(tmp_path)
    file_id = _record_house_file(conn, house / "managed.txt")
    current_dev = os.stat(house).st_dev
    with decision_store.transaction(conn):
        fingerprint_id = conn.execute(
            """
            INSERT INTO fingerprints(
                file_id, canonical_path, size, mtime_ns, dev, ino, ctime_ns,
                normalizer_version, fingerprint_version, analysis_policy_hash,
                raw_sha256, normalized_sha256, normalized_length, encoding, status
            )
            SELECT file_id, canonical_path, size, mtime_ns, ?, ino, ctime_ns,
                   'fixture', 'fixture', 'fixture', 'raw', 'normalized', 10,
                   'utf-8', 'ok'
            FROM files WHERE file_id = ?
            """,
            (current_dev + 1000, file_id),
        ).lastrowid
        conn.execute(
            "UPDATE files SET current_fingerprint_id = ?, dev = ? WHERE file_id = ?",
            (fingerprint_id, current_dev + 1000, file_id),
        )
    backup = _backup(conn, state_db)

    with mutation_lock_for_roots(house, temp, "test-device-rebind"):
        decision_store.rebind_mount_device_identities(
            conn,
            backup_path=backup,
            house_dir=house,
            temp_dir=temp,
        )

    row = conn.execute(
        "SELECT assignment_state, current_fingerprint_id FROM files WHERE file_id = ?",
        (file_id,),
    ).fetchone()
    assert row["assignment_state"] == "unassigned"
    assert row["current_fingerprint_id"] == fingerprint_id
    fingerprint = conn.execute(
        "SELECT dev FROM fingerprints WHERE fingerprint_id = ?", (fingerprint_id,)
    ).fetchone()
    assert fingerprint["dev"] == current_dev + 1000
    conn.close()


def test_mount_device_rebind_rejects_partial_or_non_device_change(tmp_path):
    house, temp, state_db, conn = _fixture(tmp_path)
    first = house / "first.txt"
    second = house / "second.txt"
    _record_house_file(conn, first)
    _record_house_file(conn, second)
    current_dev = os.stat(house).st_dev
    with decision_store.transaction(conn):
        conn.execute(
            "UPDATE files SET dev = ? WHERE canonical_path = ?",
            (current_dev + 1000, str(first.resolve())),
        )
    backup = _backup(conn, state_db)

    with mutation_lock_for_roots(house, temp, "test-device-rebind"):
        with pytest.raises(RuntimeError, match="complete mount-wide"):
            decision_store.rebind_mount_device_identities(
                conn,
                backup_path=backup,
                house_dir=house,
                temp_dir=temp,
            )

    first.write_text("changed bytes", encoding="utf-8")
    with mutation_lock_for_roots(house, temp, "test-device-rebind"):
        with pytest.raises(RuntimeError, match="non-device Doctor issue"):
            decision_store.rebind_mount_device_identities(
                conn,
                backup_path=backup,
                house_dir=house,
                temp_dir=temp,
            )
    conn.close()


def test_mount_device_rebind_allows_only_explicitly_claimed_current_file(tmp_path):
    house, temp, state_db, conn = _fixture(tmp_path)
    claimed = _record_house_file(conn, house / "claimed.txt")
    stale = _record_house_file(conn, house / "stale.txt")
    current_dev = os.stat(house).st_dev
    old_dev = current_dev + 1000
    with decision_store.transaction(conn):
        conn.execute(
            "UPDATE files SET dev = ? WHERE file_id = ?", (old_dev, stale)
        )
    backup = _backup(conn, state_db)

    with mutation_lock_for_roots(house, temp, "test-device-rebind"):
        result = decision_store.rebind_mount_device_identities(
            conn,
            backup_path=backup,
            house_dir=house,
            temp_dir=temp,
            current_file_ids=(claimed,),
        )

    assert result["file_count"] == 1
    assert not decision_store.doctor_issues(conn)
    conn.close()


def test_mount_device_rebind_noop_does_not_run_full_doctor(
    tmp_path, monkeypatch
):
    house, temp, state_db, conn = _fixture(tmp_path)
    _record_house_file(conn, house / "current.txt")
    backup = _backup(conn, state_db)

    def unexpected_doctor(*args, **kwargs):
        raise AssertionError("full Doctor should remain the approval step")

    monkeypatch.setattr(decision_store, "doctor_issues", unexpected_doctor)
    with mutation_lock_for_roots(house, temp, "test-device-rebind"):
        result = decision_store.rebind_mount_device_identities(
            conn,
            backup_path=backup,
            house_dir=house,
            temp_dir=temp,
        )

    assert result["applied"] is False
    conn.close()


def test_mount_device_rebind_requires_the_exact_roots_lock(tmp_path):
    house, temp, state_db, conn = _fixture(tmp_path)
    _record_house_file(conn, house / "locked.txt")
    backup = _backup(conn, state_db)

    with pytest.raises(MutationLockBusy, match="not held"):
        decision_store.rebind_mount_device_identities(
            conn,
            backup_path=backup,
            house_dir=house,
            temp_dir=temp,
        )
    conn.close()


def test_mount_device_rebind_repairs_managed_folder_device_only(tmp_path):
    house, temp, state_db, conn = _fixture(tmp_path)
    folder = house / "managed"
    folder.mkdir()
    current = os.stat(folder, follow_symlinks=False)
    with decision_store.transaction(conn):
        work_id = conn.execute(
            "INSERT INTO works(display_title) VALUES ('managed')"
        ).lastrowid
        conn.execute(
            """
            INSERT INTO work_folders(
                work_bucket_id, canonical_path, role, state, dev, ino, ctime_ns
            ) VALUES (?, ?, 'primary', 'active', ?, ?, ?)
            """,
            (
                work_id,
                str(folder.resolve()),
                current.st_dev + 1000,
                current.st_ino,
                current.st_ctime_ns,
            ),
        )
    backup = _backup(conn, state_db)

    with mutation_lock_for_roots(house, temp, "test-device-rebind"):
        result = decision_store.rebind_mount_device_identities(
            conn,
            backup_path=backup,
            house_dir=house,
            temp_dir=temp,
        )

    assert result["folder_count"] == 1
    assert not decision_store.doctor_issues(conn)
    conn.close()


def test_mount_device_rebind_rejects_existing_actual_approval(tmp_path):
    house, temp, state_db, conn = _fixture(tmp_path)
    _record_house_file(conn, house / "approved.txt")
    backup = _backup(conn, state_db)
    run_id = decision_store.issue_actual_run_token(
        conn, str(backup), house_dir=house, temp_dir=temp
    )

    with mutation_lock_for_roots(house, temp, "test-device-rebind"):
        with pytest.raises(RuntimeError, match="no approved or active"):
            decision_store.rebind_mount_device_identities(
                conn,
                backup_path=backup,
                house_dir=house,
                temp_dir=temp,
            )

    decision_store.disable_actual_run(conn)
    assert conn.execute(
        "SELECT state FROM actual_runs WHERE run_id = ?", (run_id,)
    ).fetchone()["state"] == "cancelled"
    conn.close()


def test_one_button_records_noop_device_rebind_in_preflight_event(
    tmp_path, monkeypatch
):
    house, temp, state_db, conn = _fixture(tmp_path)
    conn.close()
    events = []

    def consume_run(src, dst, script_dir, state_db_path=None, *, event_callback=None):
        run_id, _ = decision_store.prepare_actual_run(state_db_path, dst, src)
        run_conn = decision_store.connect_state_db(state_db_path)
        decision_store.finish_actual_run(run_conn, run_id, success=True)
        run_conn.close()
        return {"failure_count": 0}

    monkeypatch.setattr(
        run_folderling_one_button.folderling,
        "_process_items_with_lock_held",
        consume_run,
    )
    run_folderling_one_button.run(
        temp, house, state_db, event_callback=events.append
    )

    preflight = next(event for event in events if event["phase"] == "preflight_result")
    assert preflight["device_identity_rebind"] == {
        "applied": False,
        "file_count": 0,
        "folder_count": 0,
        "device_mappings": [],
        "backup_sha256": preflight["device_identity_rebind"]["backup_sha256"],
    }


def test_one_button_rebinds_mount_device_before_approval(tmp_path, monkeypatch):
    house, temp, state_db, conn = _fixture(tmp_path)
    _record_house_file(conn, house / "rebind.txt")
    current_dev = os.stat(house).st_dev
    with decision_store.transaction(conn):
        conn.execute("UPDATE files SET dev = ?", (current_dev + 1000,))
    conn.close()
    events = []

    def consume_run(src, dst, script_dir, state_db_path=None, *, event_callback=None):
        run_id, _ = decision_store.prepare_actual_run(state_db_path, dst, src)
        run_conn = decision_store.connect_state_db(state_db_path)
        decision_store.finish_actual_run(run_conn, run_id, success=True)
        run_conn.close()
        return {"failure_count": 0}

    monkeypatch.setattr(
        run_folderling_one_button.folderling,
        "_process_items_with_lock_held",
        consume_run,
    )
    result = run_folderling_one_button.run(
        temp, house, state_db, event_callback=events.append
    )

    assert result["failure_count"] == 0
    preflight = next(event for event in events if event["phase"] == "preflight_result")
    assert preflight["device_identity_rebind"]["applied"] is True
    assert preflight["device_identity_rebind"]["file_count"] == 1
    check = decision_store.connect_state_db(state_db)
    assert not decision_store.doctor_issues(check)
    check.close()
