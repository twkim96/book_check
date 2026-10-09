"""Verified ctime receipts authorize original and queued sources, never new bytes."""

import hashlib
import os
from pathlib import Path

import pytest

import decision_store
import dedup_mutations
from mutation_io import mutation_lock


def _add_file(conn, tmp_path, name, content, *, source, managed_variant=None, protected=False):
    root = tmp_path / source
    root.mkdir(exist_ok=True)
    path = root / name
    path.write_bytes(content)
    row = decision_store.reconcile_file_metadata(conn, path, source=source)
    file_id = row["file_id"]
    info = path.stat()
    raw_hash = hashlib.sha256(content).hexdigest()
    fingerprint_id = conn.execute(
        """
        INSERT INTO fingerprints(
            file_id, canonical_path, size, mtime_ns, normalizer_version,
            fingerprint_version, raw_sha256, normalized_sha256,
            normalized_length, status
        ) VALUES (?, ?, ?, ?, '1.2.1', '1', ?, ?, ?, 'ok')
        """,
        (file_id, str(path), info.st_size, info.st_mtime_ns, raw_hash, raw_hash, len(content)),
    ).lastrowid
    conn.execute(
        "UPDATE files SET current_fingerprint_id = ? WHERE file_id = ?",
        (fingerprint_id, file_id),
    )
    if managed_variant is not None:
        conn.execute(
            """
            UPDATE files SET variant_id = ?, assignment_state = 'managed',
                assignment_origin = 'human_decision', protected = ?
            WHERE file_id = ?
            """,
            (managed_variant, 1 if protected else 0, file_id),
        )
    return file_id, path


def _managed_reference(conn, tmp_path, content=b"same", name="대표.txt"):
    work_id = conn.execute("INSERT INTO works(display_title) VALUES ('작품')").lastrowid
    variant_id = conn.execute(
        "INSERT INTO variants(work_bucket_id, variant_kind) VALUES (?, 'base')", (work_id,)
    ).lastrowid
    file_id, path = _add_file(
        conn, tmp_path, name, content, source="house",
        managed_variant=variant_id, protected=True,
    )
    conn.execute(
        "INSERT INTO representatives(variant_id, file_id) VALUES (?, ?)",
        (variant_id, file_id),
    )
    return file_id, variant_id, path


def _review(conn, candidate_id, reference_id, classification):
    candidate_fp = conn.execute(
        "SELECT current_fingerprint_id FROM files WHERE file_id = ?", (candidate_id,)
    ).fetchone()[0]
    reference_fp = conn.execute(
        "SELECT current_fingerprint_id FROM files WHERE file_id = ?", (reference_id,)
    ).fetchone()[0]
    return conn.execute(
        """
        INSERT INTO review_items(
            candidate_file_id, reference_file_id, left_fingerprint_id,
            right_fingerprint_id, classification, state
        ) VALUES (?, ?, ?, ?, ?, 'pending')
        """,
        (candidate_id, reference_id, candidate_fp, reference_fp, classification),
    ).lastrowid


def _active_run(conn, tmp_path):
    existing = conn.execute(
        "SELECT run_id FROM actual_runs WHERE state = 'active' ORDER BY activated_at DESC LIMIT 1"
    ).fetchone()
    if existing:
        return existing[0]
    house, temp = tmp_path / "house", tmp_path / "temp"
    house.mkdir(exist_ok=True)
    temp.mkdir(exist_ok=True)
    backup = tmp_path / "actual-run-backup.sqlite3"
    decision_store.backup_state_db(conn, backup)
    decision_store.issue_actual_run_token(conn, str(backup), house_dir=house, temp_dir=temp)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    run_id, _ = decision_store.prepare_actual_run(db_path, house, temp)
    return run_id


def _fixture(tmp_path, origin):
    conn = decision_store.initialize_state_db(tmp_path / "state.sqlite3")
    with decision_store.transaction(conn):
        keep_id, _, keep = _managed_reference(conn, tmp_path, content=b"same")
        source_id, source = _add_file(conn, tmp_path, "사본.txt", b"same", source="temp")
        review_id = _review(conn, source_id, keep_id, "contained_version")
    run_id = _active_run(conn, tmp_path)
    origin_operation = None
    if origin != "manifest":
        result = dedup_mutations.queue_candidate(
            conn, candidate_file_id=source_id, reference_file_id=keep_id,
            classification="contained_version", queue_dir=tmp_path / "temp" / "warning",
            run_id=run_id, review_id=review_id,
        )
        source = Path(result["dest_path"])
        origin_operation = dict(conn.execute("SELECT * FROM operations WHERE operation_id = ?", (result["operation_id"],)).fetchone())
        if origin == "prior_run_queue":
            decision_store.finish_actual_run(conn, run_id, success=True)
            source.chmod(0o700)
            backup = tmp_path / "second-run-backup.sqlite3"
            decision_store.backup_state_db(conn, backup)
            decision_store.issue_actual_run_token(
                conn, str(backup), house_dir=tmp_path / "house", temp_dir=tmp_path / "temp",
            )
            run_id, _ = decision_store.prepare_actual_run(
                tmp_path / "state.sqlite3", tmp_path / "house", tmp_path / "temp",
            )
    return conn, run_id, source_id, source, keep_id, keep, origin_operation


@pytest.mark.parametrize("origin", ["manifest", "same_run_queue", "prior_run_queue"])
def test_exact_cleanup_corrects_source_ctime_and_preserves_original_evidence(tmp_path, origin):
    conn, run_id, source_id, source, keep_id, keep, origin_operation = _fixture(tmp_path, origin)
    source.chmod(0o600)
    source.chmod(0o700)
    result = dedup_mutations.exact_quarantine(
        conn, source_file_id=source_id, keep_file_id=keep_id,
        quarantine_dir=tmp_path / "temp" / "quarantine", run_id=run_id,
    )
    assert not source.exists()
    assert keep.read_bytes() == Path(result["dest_path"]).read_bytes() == b"same"
    assert conn.execute("SELECT state FROM operations WHERE operation_id = ?", (result["operation_id"],)).fetchone()[0] == "committed"
    if origin_operation is not None:
        assert dict(conn.execute("SELECT * FROM operations WHERE operation_id = ?", (origin_operation["operation_id"],)).fetchone()) == origin_operation
    assert not decision_store.doctor_issues(conn, allowed_active_run_id=run_id)
    conn.close()


def test_repeated_verified_ctime_refresh_keeps_activation_authorization(tmp_path):
    conn, run_id, source_id, source, keep_id, _, _ = _fixture(tmp_path, "manifest")
    for mode in (0o700, 0o600):
        source.chmod(mode)
        with mutation_lock(conn, "test-ctime-chain", run_id=run_id):
            row = dedup_mutations._file_state(conn, source_id, run_id=run_id)
            assert row["ctime_ns"] == source.stat().st_ctime_ns
    result = dedup_mutations.exact_quarantine(
        conn, source_file_id=source_id, keep_file_id=keep_id,
        quarantine_dir=tmp_path / "temp" / "quarantine", run_id=run_id,
    )
    assert Path(result["dest_path"]).read_bytes() == b"same"
    conn.close()


@pytest.mark.parametrize("change", ["content", "replacement"])
def test_source_refresh_rejects_changed_content_or_inode(tmp_path, change):
    conn, run_id, source_id, source, keep_id, keep, _ = _fixture(tmp_path, "manifest")
    if change == "content":
        info = source.stat()
        source.write_bytes(b"evil")
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
    else:
        other = source.with_suffix(".replacement")
        other.write_bytes(b"same")
        other.replace(source)
    with pytest.raises(RuntimeError):
        dedup_mutations.exact_quarantine(
            conn, source_file_id=source_id, keep_file_id=keep_id,
            quarantine_dir=tmp_path / "temp" / "quarantine", run_id=run_id,
        )
    assert source.exists()
    assert keep.read_bytes() == b"same"
    assert not conn.execute("SELECT 1 FROM operations WHERE action = 'exact_quarantine'").fetchone()
    conn.close()
