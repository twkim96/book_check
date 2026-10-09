"""Metadata-only manifest changes must preserve authorization and tamper checks."""

import json
import os
from pathlib import Path

import pytest

import decision_store
import mutation_io


@pytest.fixture
def active_manifest(tmp_path):
    db = tmp_path / "state.sqlite3"
    house, temp = tmp_path / "house", tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    source = temp / "sample.txt"
    source.write_bytes(b"approved sample")
    conn = decision_store.initialize_state_db(db)
    with decision_store.transaction(conn):
        decision_store.reconcile_file_metadata(conn, source, source="temp")
    backup = decision_store.backup_state_db(conn, tmp_path / "backup.sqlite3")
    decision_store.issue_actual_run_token(conn, str(backup), house_dir=house, temp_dir=temp)
    run_id, manifest = decision_store.prepare_actual_run(db, house, temp)
    run = conn.execute("SELECT * FROM actual_runs WHERE run_id = ?", (run_id,)).fetchone()
    yield conn, run, Path(manifest), source
    conn.close()


def metadata_change(path):
    before = path.stat()
    path.chmod(0o700)
    path.chmod(0o600)
    assert path.stat().st_ctime_ns != before.st_ctime_ns


def test_ctime_only_change_is_verified_once_and_keeps_source_authorization(
    active_manifest, monkeypatch
):
    conn, original_run, manifest, source = active_manifest
    raw = manifest.read_bytes()
    metadata_change(manifest)
    calls = []
    real_inspect = mutation_io.inspect_open_regular_file_fd

    def traced_inspect(fd):
        calls.append(fd)
        return real_inspect(fd)

    monkeypatch.setattr(mutation_io, "inspect_open_regular_file_fd", traced_inspect)
    refreshed = decision_store.assert_active_actual_run(conn, original_run["run_id"])
    decision_store.assert_active_actual_run(conn, original_run["run_id"])
    assert len(calls) == 1
    assert refreshed["state"] == "active"
    assert refreshed["manifest_ctime_ns"] == manifest.stat().st_ctime_ns
    assert refreshed["manifest_sha256"] == original_run["manifest_sha256"]
    assert manifest.read_bytes() == raw
    receipt = json.loads(conn.execute(
        "SELECT value FROM settings WHERE key = ?",
        (f"actual_run_manifest_ctime_refresh:{original_run['run_id']}",),
    ).fetchone()[0])
    assert receipt["previous_ctime_ns"] == original_run["manifest_ctime_ns"]
    assert receipt["verified_ctime_ns"] == refreshed["manifest_ctime_ns"]
    # A caller's previously returned snapshot must still authorize unchanged
    # sources after the verified metadata refresh.
    decision_store.assert_manifest_source(
        original_run, source, "temp_root", mutation_io.inspect_regular_file(source)
    )


@pytest.mark.parametrize("change", ["content", "replacement", "permissions", "hardlink", "symlink"])
def test_manifest_changes_that_are_not_safe_metadata_fail_closed(active_manifest, change):
    conn, run, manifest, _source = active_manifest
    before = manifest.stat()
    raw = manifest.read_bytes()
    if change == "content":
        manifest.write_bytes(raw.replace(b'"run_id"', b'"run_ix"', 1))
        os.utime(manifest, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif change == "replacement":
        replacement = manifest.with_suffix(".replacement")
        replacement.write_bytes(raw)
        replacement.chmod(0o600)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(manifest)
    elif change == "permissions":
        manifest.chmod(0o644)
    elif change == "hardlink":
        os.link(manifest, manifest.with_suffix(".link"))
    else:
        original = manifest.with_suffix(".original")
        manifest.rename(original)
        manifest.symlink_to(original)
    with pytest.raises(RuntimeError, match="manifest"):
        decision_store.assert_active_actual_run(conn, run["run_id"])
    failed = conn.execute("SELECT * FROM actual_runs WHERE run_id = ?", (run["run_id"],)).fetchone()
    assert failed["state"] == "failed"
    assert failed["manifest_ctime_ns"] == run["manifest_ctime_ns"]
    assert not conn.execute(
        "SELECT 1 FROM settings WHERE key = ?",
        (f"actual_run_manifest_ctime_refresh:{run['run_id']}",),
    ).fetchone()


@pytest.mark.parametrize("replace_parent", [False, True])
def test_manifest_replacement_during_ctime_verification_is_rejected(
    active_manifest, monkeypatch, replace_parent
):
    conn, run, manifest, _source = active_manifest
    metadata_change(manifest)
    real_inspect = mutation_io.inspect_open_regular_file_fd

    def replace_after_hash(fd):
        evidence = real_inspect(fd)
        if replace_parent:
            manifest.parent.rename(manifest.parent.with_name("detached-manifests"))
            manifest.parent.mkdir()
            manifest.write_bytes(b"unapproved replacement directory")
            manifest.chmod(0o600)
        else:
            replacement = manifest.with_suffix(".replacement")
            replacement.write_bytes(manifest.read_bytes())
            replacement.chmod(0o600)
            replacement.replace(manifest)
        return evidence

    monkeypatch.setattr(mutation_io, "inspect_open_regular_file_fd", replace_after_hash)
    with pytest.raises(RuntimeError, match="pathname changed"):
        decision_store.assert_active_actual_run(conn, run["run_id"])
    assert conn.execute("SELECT state FROM actual_runs WHERE run_id = ?", (run["run_id"],)).fetchone()[0] == "failed"


def test_ctime_change_can_be_verified_inside_existing_writer_transaction(active_manifest):
    conn, run, manifest, _source = active_manifest
    metadata_change(manifest)
    with decision_store.transaction(conn):
        refreshed = decision_store.assert_active_actual_run(conn, run["run_id"])
        assert refreshed["manifest_ctime_ns"] == manifest.stat().st_ctime_ns


def test_backup_ctime_is_verified_once_and_preserves_immutable_bytes(active_manifest, monkeypatch):
    conn, run, _manifest, _source = active_manifest
    backup = Path(run["backup_path"])
    original = backup.read_bytes()
    metadata_change(backup)
    inspect = mutation_io.inspect_open_regular_file_fd
    calls = []

    def counted(fd):
        calls.append(fd)
        return inspect(fd)

    monkeypatch.setattr(mutation_io, "inspect_open_regular_file_fd", counted)
    with decision_store.transaction(conn):
        refreshed = decision_store.assert_active_actual_run(conn, run["run_id"])
    decision_store.assert_active_actual_run(conn, run["run_id"])
    assert len(calls) == 1
    assert refreshed["backup_ctime_ns"] == backup.stat().st_ctime_ns
    assert refreshed["backup_sha256"] == run["backup_sha256"]
    assert backup.read_bytes() == original
    assert conn.execute("SELECT value FROM settings WHERE key = ?", (
        f"actual_run_backup_ctime_refresh:{run['run_id']}",
    )).fetchone()


@pytest.mark.parametrize("change", ["content", "replacement", "hardlink", "symlink"])
def test_backup_content_or_identity_changes_still_fail_closed(active_manifest, change):
    conn, run, _manifest, _source = active_manifest
    backup = Path(run["backup_path"])
    info = backup.stat()
    original = backup.read_bytes()
    if change == "content":
        backup.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        os.utime(backup, ns=(info.st_atime_ns, info.st_mtime_ns))
    elif change == "replacement":
        other = backup.with_suffix(".replacement")
        other.write_bytes(original)
        os.utime(other, ns=(info.st_atime_ns, info.st_mtime_ns))
        other.replace(backup)
    elif change == "hardlink":
        os.link(backup, backup.with_suffix(".linked"))
    else:
        target = backup.with_suffix(".original")
        backup.rename(target)
        backup.symlink_to(target)
    with pytest.raises(RuntimeError, match="backup"):
        decision_store.assert_active_actual_run(conn, run["run_id"])
    assert conn.execute("SELECT state FROM actual_runs WHERE run_id = ?", (run["run_id"],)).fetchone()[0] == "failed"
    assert not conn.execute("SELECT 1 FROM settings WHERE key = ?", (
        f"actual_run_backup_ctime_refresh:{run['run_id']}",
    )).fetchone()
