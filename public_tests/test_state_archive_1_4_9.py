import json
import os
from pathlib import Path

import pytest

import decision_store
import state_archive


def _state_with_backups(tmp_path, count=4):
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    backups = []
    try:
        for index in range(count):
            backup = decision_store.backup_state_db(
                conn,
                state_db.parent / "backups" / f"before-test-{index}.sqlite3",
            )
            timestamp = 1_000_000_000 + index
            os.utime(backup, ns=(timestamp, timestamp))
            backups.append(backup)
    finally:
        conn.close()
    return state_db, backups


def _reference_backup(state_db, backup, *, state="finished", run_id="run-ref"):
    conn = decision_store.connect_state_db(state_db)
    try:
        conn.execute(
            """
            INSERT INTO actual_runs(
                run_id, state, house_root, temp_root, backup_path, backup_sha256
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                state,
                str(state_db.parent / "house"),
                str(state_db.parent / "temp"),
                str(backup.resolve()),
                decision_store.sha256_file(backup),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_verified_archive_excludes_references_and_restores_without_consuming_cold_copy(
    tmp_path,
):
    state_db, backups = _state_with_backups(tmp_path)
    _reference_backup(state_db, backups[0])

    plan = state_archive.build_backup_archive_plan(
        state_db, keep_latest_unreferenced=1
    )

    assert plan["blockers"] == []
    assert plan["eligible_count"] == 2
    assert str(backups[0].resolve()) in plan["referenced_paths"]
    assert plan["retained_unreferenced_paths"] == [str(backups[3].resolve())]
    assert {Path(item["source_path"]) for item in plan["items"]} == {
        backups[1].resolve(),
        backups[2].resolve(),
    }

    report = state_archive.apply_backup_archive_plan(
        state_db,
        plan,
        confirm_count=plan["eligible_count"],
        confirm_plan_sha256=plan["plan_sha256"],
    )

    assert report["archived_count"] == 2
    assert Path(report["intent_path"]).is_file()
    assert Path(report["report_path"]).is_file()
    assert backups[0].is_file()
    assert backups[3].is_file()
    assert not backups[1].exists()
    assert not backups[2].exists()
    for archived in report["archived"]:
        assert Path(archived["archive_path"]).is_file()
        assert Path(archived["metadata_path"]).is_file()
        assert archived["source_bytes"] > archived["archive_bytes"]

    selected = report["archived"][0]
    restored = state_archive.restore_archived_backup(
        state_db,
        selected["metadata_path"],
        confirm_raw_sha256=selected["source_sha256"],
    )
    assert Path(restored["restored_path"]).is_file()
    assert Path(restored["archive_preserved"]).is_file()
    assert Path(restored["report_path"]).is_file()
    state_archive._sqlite_integrity(Path(restored["restored_path"]))

    conn = decision_store.connect_state_db(state_db)
    try:
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_archive_requires_exact_fresh_plan_confirmation(tmp_path):
    state_db, _backups = _state_with_backups(tmp_path, count=3)
    plan = state_archive.build_backup_archive_plan(
        state_db, keep_latest_unreferenced=1
    )

    with pytest.raises(RuntimeError, match="confirmation count mismatch"):
        state_archive.apply_backup_archive_plan(
            state_db,
            plan,
            confirm_count=plan["eligible_count"] + 1,
            confirm_plan_sha256=plan["plan_sha256"],
        )

    changed = Path(plan["items"][0]["source_path"])
    current = changed.stat()
    os.utime(changed, ns=(current.st_atime_ns, current.st_mtime_ns + 1))
    with pytest.raises(RuntimeError, match="plan is stale"):
        state_archive.apply_backup_archive_plan(
            state_db,
            plan,
            confirm_count=plan["eligible_count"],
            confirm_plan_sha256=plan["plan_sha256"],
        )
    assert all(backup.is_file() for backup in _backups)


def test_archive_fails_closed_for_open_run_and_unsafe_backup_entries(tmp_path):
    state_db, backups = _state_with_backups(tmp_path, count=3)
    _reference_backup(state_db, backups[0], state="approved")
    symlink = state_db.parent / "backups" / "unsafe-link.sqlite3"
    symlink.symlink_to(backups[1])
    hardlink = state_db.parent / "backups" / "unsafe-hardlink.sqlite3"
    os.link(backups[2], hardlink)

    plan = state_archive.build_backup_archive_plan(
        state_db, keep_latest_unreferenced=0
    )

    assert plan["blockers"] == ["open_actual_runs:1"]
    assert {Path(item["path"]).name for item in plan["unsafe_paths"]} == {
        backups[2].name,
        hardlink.name,
        symlink.name,
    }
    with pytest.raises(RuntimeError, match="maintenance is blocked"):
        state_archive.apply_backup_archive_plan(
            state_db,
            plan,
            confirm_count=plan["eligible_count"],
            confirm_plan_sha256=plan["plan_sha256"],
        )
    assert all(backup.is_file() for backup in backups)


def test_existing_archive_or_metadata_tampering_never_consumes_source(tmp_path):
    state_db, backups = _state_with_backups(tmp_path, count=2)
    plan = state_archive.build_backup_archive_plan(
        state_db, keep_latest_unreferenced=1
    )
    item = plan["items"][0]
    source = Path(item["source_path"])
    evidence = state_archive.inspect_regular_file(source)
    archive = Path(item["archive_path"])
    metadata = Path(item["metadata_path"])
    state_archive._compress_backup(source, archive, evidence)
    payload = state_archive._archive_metadata(state_db, source, archive, evidence)
    payload["source"]["sha256"] = "0" * 64
    state_archive._atomic_json_write(metadata, payload)

    with pytest.raises(RuntimeError, match="existing archive evidence mismatch"):
        state_archive.archive_backup_path(state_db, item)
    assert source.is_file()
    assert archive.is_file()


def test_verified_cold_object_from_interrupted_attempt_is_reused(tmp_path):
    state_db, _backups = _state_with_backups(tmp_path, count=2)
    plan = state_archive.build_backup_archive_plan(
        state_db, keep_latest_unreferenced=1
    )
    item = plan["items"][0]
    source = Path(item["source_path"])
    evidence = state_archive.inspect_regular_file(source)
    archive = Path(item["archive_path"])
    metadata = Path(item["metadata_path"])
    state_archive._compress_backup(source, archive, evidence)
    state_archive._atomic_json_write(
        metadata,
        state_archive._archive_metadata(state_db, source, archive, evidence),
    )

    result = state_archive.archive_backup_path(state_db, item)

    assert not source.exists()
    assert archive.is_file()
    assert metadata.is_file()
    assert result["source_sha256"] == evidence.sha256


def test_actual_run_approval_revalidates_backup_inside_writer_transaction(
    tmp_path, monkeypatch,
):
    state_db, backups = _state_with_backups(tmp_path, count=1)
    backup = backups[0]
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    original = decision_store._verify_backup_evidence
    calls = []

    def remove_after_first_verification(path, expected_sha256=None):
        result = original(path, expected_sha256)
        calls.append(expected_sha256)
        if len(calls) == 1:
            Path(path).unlink()
        return result

    monkeypatch.setattr(
        decision_store, "_verify_backup_evidence", remove_after_first_verification
    )
    conn = decision_store.connect_state_db(state_db)
    try:
        with pytest.raises(RuntimeError, match="does not exist"):
            decision_store.issue_actual_run_token(
                conn, str(backup), house_dir=house, temp_dir=temp
            )
        assert conn.execute("SELECT COUNT(*) FROM actual_runs").fetchone()[0] == 0
    finally:
        conn.close()


def test_restore_rejects_wrong_confirmation_and_tampered_cold_object(tmp_path):
    state_db, _backups = _state_with_backups(tmp_path, count=2)
    plan = state_archive.build_backup_archive_plan(
        state_db, keep_latest_unreferenced=1
    )
    report = state_archive.apply_backup_archive_plan(
        state_db,
        plan,
        confirm_count=plan["eligible_count"],
        confirm_plan_sha256=plan["plan_sha256"],
    )
    archived = report["archived"][0]

    with pytest.raises(RuntimeError, match="confirmation raw SHA-256 mismatch"):
        state_archive.restore_archived_backup(
            state_db,
            archived["metadata_path"],
            confirm_raw_sha256="0" * 64,
        )

    archive = Path(archived["archive_path"])
    with archive.open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(RuntimeError, match="cold archive SHA-256 mismatch"):
        state_archive.restore_archived_backup(
            state_db,
            archived["metadata_path"],
            confirm_raw_sha256=archived["source_sha256"],
        )


def test_archive_intent_is_self_describing(tmp_path):
    state_db, _backups = _state_with_backups(tmp_path, count=2)
    plan = state_archive.build_backup_archive_plan(
        state_db, keep_latest_unreferenced=1
    )
    report = state_archive.apply_backup_archive_plan(
        state_db,
        plan,
        confirm_count=plan["eligible_count"],
        confirm_plan_sha256=plan["plan_sha256"],
    )
    intent = json.loads(Path(report["intent_path"]).read_text(encoding="utf-8"))
    assert intent["kind"] == "state_backup_archive_intent"
    assert intent["plan_sha256"] == plan["plan_sha256"]
    assert intent["eligible_count"] == plan["eligible_count"]
