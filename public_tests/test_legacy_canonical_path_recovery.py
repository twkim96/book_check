import os
from pathlib import Path

import decision_store
import pytest
from dedup_mutations import (
    _ensure_intake_fingerprint,
    _file_state,
    ingest_to_house,
)
from folderling import create_recent_link, ensure_recent_link_slot
from mutation_io import inspect_regular_file


def _legacy_collision_fixture(tmp_path, *, with_requeue_provenance=True):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    destination = house / "ㄱ" / "과거 제목 1-100.txt"
    destination.parent.mkdir(parents=True)
    temp.mkdir()
    destination.write_text("과거 본문", encoding="utf-8")

    conn = decision_store.initialize_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            old_row = decision_store.reconcile_file_metadata(
                conn, destination, source="house"
            )
        old_state = _ensure_intake_fingerprint(
            conn, _file_state(conn, old_row["file_id"])
        )
        old_evidence = inspect_regular_file(destination)
        destination.unlink()

        incoming = temp / "과거_제목_1-100.txt"
        incoming.write_text("새 입고 본문", encoding="utf-8")
        with decision_store.transaction(conn):
            incoming_row = decision_store.reconcile_file_metadata(
                conn, incoming, source="temp"
            )
            conn.execute(
                "UPDATE files SET active = 0 WHERE file_id = ?",
                (old_row["file_id"],),
            )
        incoming_state = _ensure_intake_fingerprint(
            conn, _file_state(conn, incoming_row["file_id"])
        )
        backup = decision_store.backup_state_db(
            conn, state_db.parent / "backups" / "before-legacy-path.sqlite3"
        )
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()

    run_id, _ = decision_store.prepare_actual_run(state_db, house, temp)
    conn = decision_store.connect_state_db(state_db)
    if with_requeue_provenance:
        with decision_store.transaction(conn):
            legacy_operation_id = decision_store.create_operation(
                conn,
                run_id=run_id,
                action="title_cleanup_requeue",
                source_path=str(destination.resolve()),
                dest_path=str((temp / "과거 제목 1-100.txt").resolve()),
                file_id=old_row["file_id"],
                expected_size=old_state["size"],
                expected_mtime_ns=old_state["mtime_ns"],
                expected_fingerprint_id=old_state["current_fingerprint_id"],
                source_dev=old_evidence.dev,
                source_ino=old_evidence.ino,
                source_ctime_ns=old_evidence.ctime_ns,
                source_sha256=old_evidence.sha256,
            )
            conn.execute(
                "UPDATE operations SET state = 'committed' WHERE operation_id = ?",
                (legacy_operation_id,),
            )
    return {
        "conn": conn,
        "state_db": state_db,
        "house": house,
        "temp": temp,
        "destination": destination,
        "incoming": incoming,
        "incoming_state": incoming_state,
        "old_file_id": old_row["file_id"],
        "incoming_file_id": incoming_row["file_id"],
        "run_id": run_id,
    }


def test_house_ingest_releases_proven_legacy_title_path_owner(tmp_path):
    fixture = _legacy_collision_fixture(tmp_path)
    conn = fixture["conn"]
    try:
        result = ingest_to_house(
            conn,
            source_file_id=fixture["incoming_file_id"],
            destination=fixture["destination"],
            run_id=fixture["run_id"],
        )
        decision_store.finish_actual_run(conn, fixture["run_id"], success=True)
        assert result["dest_path"] == str(fixture["destination"].resolve())
        assert fixture["destination"].is_file()
        assert not fixture["incoming"].exists()
        old = conn.execute(
            "SELECT canonical_path, active FROM files WHERE file_id = ?",
            (fixture["old_file_id"],),
        ).fetchone()
        assert old["canonical_path"] == decision_store.retired_canonical_path(
            conn, fixture["old_file_id"], fixture["destination"]
        )
        assert old["active"] == 0
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_house_ingest_blocks_unknown_db_path_owner_before_file_move(tmp_path):
    fixture = _legacy_collision_fixture(
        tmp_path, with_requeue_provenance=False
    )
    conn = fixture["conn"]
    try:
        with pytest.raises(RuntimeError, match="reserved in state DB"):
            ingest_to_house(
                conn,
                source_file_id=fixture["incoming_file_id"],
                destination=fixture["destination"],
                run_id=fixture["run_id"],
            )
        assert fixture["incoming"].is_file()
        assert not fixture["destination"].exists()
        assert conn.execute(
            "SELECT COUNT(*) FROM operations WHERE action = 'house_ingest'"
        ).fetchone()[0] == 0
        decision_store.finish_actual_run(
            conn, fixture["run_id"], success=False, error="expected preflight block"
        )
    finally:
        conn.close()


def test_recovery_commits_owned_destination_after_legacy_path_collision(tmp_path):
    fixture = _legacy_collision_fixture(tmp_path)
    conn = fixture["conn"]
    try:
        source_evidence = inspect_regular_file(fixture["incoming"])
        with decision_store.transaction(conn):
            operation_id = decision_store.create_operation(
                conn,
                run_id=fixture["run_id"],
                action="house_ingest",
                source_path=str(fixture["incoming"].resolve()),
                dest_path=str(fixture["destination"].resolve()),
                file_id=fixture["incoming_file_id"],
                expected_size=fixture["incoming_state"]["size"],
                expected_mtime_ns=fixture["incoming_state"]["mtime_ns"],
                expected_fingerprint_id=fixture["incoming_state"][
                    "current_fingerprint_id"
                ],
                source_dev=source_evidence.dev,
                source_ino=source_evidence.ino,
                source_ctime_ns=source_evidence.ctime_ns,
                source_sha256=source_evidence.sha256,
            )
        decision_store.copy_record_consume_operation(
            conn,
            operation_id,
            fixture["incoming"],
            fixture["destination"],
            source_evidence,
        )
        with decision_store.transaction(conn):
            rescanned = decision_store.reconcile_file_metadata(
                conn, fixture["destination"], source="house"
            )
        assert rescanned["file_id"] == fixture["old_file_id"]
        decision_store.finish_actual_run(
            conn, fixture["run_id"], success=False, error="synthetic DB collision"
        )

        assert decision_store.recover_interrupted_operation(
            conn, operation_id
        ) == "committed"
        assert fixture["destination"].is_file()
        assert not fixture["incoming"].exists()
        incoming = conn.execute(
            "SELECT canonical_path, source, active FROM files WHERE file_id = ?",
            (fixture["incoming_file_id"],),
        ).fetchone()
        assert tuple(incoming) == (
            str(fixture["destination"].resolve()), "house", 1
        )
        old = conn.execute(
            "SELECT canonical_path, active FROM files WHERE file_id = ?",
            (fixture["old_file_id"],),
        ).fetchone()
        assert old["canonical_path"] == decision_store.retired_canonical_path(
            conn, fixture["old_file_id"], fixture["destination"]
        )
        assert old["active"] == 0
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_v15_migration_retires_legacy_title_real_path(tmp_path):
    fixture = _legacy_collision_fixture(tmp_path)
    conn = fixture["conn"]
    try:
        decision_store.finish_actual_run(conn, fixture["run_id"], success=True)
        conn.execute("PRAGMA user_version = 14")
        conn.commit()
    finally:
        conn.close()

    migrated = decision_store.initialize_state_db(
        fixture["state_db"], migrate=True
    )
    try:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 17
        old = migrated.execute(
            "SELECT canonical_path, active FROM files WHERE file_id = ?",
            (fixture["old_file_id"],),
        ).fetchone()
        assert old["canonical_path"] == decision_store.retired_canonical_path(
            migrated, fixture["old_file_id"], fixture["destination"]
        )
        assert old["active"] == 0
    finally:
        migrated.close()


def test_matching_broken_recent_link_is_reused_without_replacement(tmp_path):
    house = tmp_path / "house"
    recent = house / "_최근"
    destination = house / "ㄱ" / "재입고 작품 1-100.txt"
    recent.mkdir(parents=True)
    destination.parent.mkdir()
    link = recent / destination.name
    link.symlink_to(destination)
    original_target = os.readlink(link)
    assert link.is_symlink()
    assert not link.exists()

    ensure_recent_link_slot(destination.name, recent, destination)
    destination.write_text("복구 본문", encoding="utf-8")
    assert create_recent_link(destination, destination.name, recent) is False
    assert os.readlink(link) == original_target
    assert link.resolve() == destination.resolve()

    other_name = "다른 대상.txt"
    other_link = recent / other_name
    other_link.symlink_to(house / "ㄴ" / other_name)
    with pytest.raises(FileExistsError, match="입고를 중단"):
        ensure_recent_link_slot(other_name, recent, house / "ㄷ" / other_name)
    with pytest.raises(FileExistsError, match="보존합니다"):
        create_recent_link(house / "ㄷ" / other_name, other_name, recent)
