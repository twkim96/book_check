"""Queue reference metadata drift preserves reviews and rejects changed bytes."""

import os
from pathlib import Path

import pytest

import decision_store
import dedup_mutations
import mutation_io
from public_tests.test_actual_file_ctime_refresh import (
    _active_run, _add_file, _managed_reference, _review,
)


def _fixture(tmp_path, *, ingested):
    conn = decision_store.initialize_state_db(tmp_path / "state.sqlite3")
    with decision_store.transaction(conn):
        if ingested:
            reference_id, reference = _add_file(conn, tmp_path, "기준.txt", b"keep", source="temp")
        else:
            reference_id, _, reference = _managed_reference(conn, tmp_path, content=b"keep")
        candidate_id, candidate = _add_file(conn, tmp_path, "검토.txt", b"maybe", source="temp")
        review_id = _review(conn, candidate_id, reference_id, "contained_version")
    run_id = _active_run(conn, tmp_path)
    if ingested:
        reference = tmp_path / "house" / reference.name
        dedup_mutations.ingest_to_house(
            conn, source_file_id=reference_id, destination=reference, run_id=run_id,
        )
    return conn, reference_id, reference, candidate_id, candidate, review_id, run_id


def _queue(tmp_path, fixture):
    conn, reference_id, _, candidate_id, _, review_id, run_id = fixture
    return dedup_mutations.queue_candidate(
        conn, candidate_file_id=candidate_id, reference_file_id=reference_id,
        classification="contained_version", queue_dir=tmp_path / "temp" / "warning",
        run_id=run_id, review_id=review_id, allow_unassigned_reference=True,
    )


@pytest.mark.parametrize("ingested", [False, True])
@pytest.mark.parametrize("during_copy", [False, True])
def test_queue_refreshes_metadata_and_keeps_review_fingerprint(tmp_path, monkeypatch, ingested, during_copy):
    fixture = _fixture(tmp_path, ingested=ingested)
    conn, reference_id, reference, _, candidate, _, run_id = fixture
    before = dict(conn.execute("SELECT * FROM files WHERE file_id = ?", (reference_id,)).fetchone())
    fingerprint = dict(conn.execute(
        "SELECT * FROM fingerprints WHERE fingerprint_id = ?", (before["current_fingerprint_id"],)
    ).fetchone())

    def metadata():
        reference.chmod(0o600)
        reference.chmod(0o700)

    if during_copy:
        copy = mutation_io.copy_no_clobber

        def copy_then_metadata(*args, **kwargs):
            copied = copy(*args, **kwargs)
            metadata()
            return copied

        monkeypatch.setattr(mutation_io, "copy_no_clobber", copy_then_metadata)
    else:
        metadata()
    result = _queue(tmp_path, fixture)
    assert not candidate.exists()
    assert Path(result["dest_path"]).read_bytes() == b"maybe"
    assert reference.read_bytes() == b"keep"
    after = dict(conn.execute("SELECT * FROM files WHERE file_id = ?", (reference_id,)).fetchone())
    assert after["ctime_ns"] == reference.stat().st_ctime_ns != before["ctime_ns"]
    assert after["current_fingerprint_id"] == before["current_fingerprint_id"]
    assert dict(conn.execute("SELECT * FROM fingerprints WHERE fingerprint_id = ?", (fingerprint["fingerprint_id"],)).fetchone()) == fingerprint
    assert conn.execute("SELECT state FROM operations WHERE operation_id = ?", (result["operation_id"],)).fetchone()[0] == "committed"
    assert conn.execute("SELECT value FROM settings WHERE key = ?", (
        f"actual_run_file_ctime_refresh:{run_id}:{reference_id}",
    )).fetchone()
    assert not decision_store.doctor_issues(conn, allowed_active_run_id=run_id)
    conn.close()


@pytest.mark.parametrize("change", ["content", "replacement", "hardlink", "symlink"])
@pytest.mark.parametrize("during_copy", [False, True])
def test_queue_reference_change_preserves_candidate_and_db(tmp_path, monkeypatch, change, during_copy):
    fixture = _fixture(tmp_path, ingested=True)
    conn, reference_id, reference, _, candidate, _, _ = fixture
    before = dict(conn.execute("SELECT * FROM files WHERE file_id = ?", (reference_id,)).fetchone())
    def modify():
        if change == "content":
            info = reference.stat()
            reference.write_bytes(b"evil")
            os.utime(reference, ns=(info.st_atime_ns, info.st_mtime_ns))
        elif change == "replacement":
            other = reference.with_suffix(".replacement")
            other.write_bytes(b"keep")
            other.replace(reference)
        elif change == "hardlink":
            os.link(reference, reference.with_suffix(".link"))
        else:
            reference.unlink()
            reference.symlink_to(candidate)
    if during_copy:
        copy = mutation_io.copy_no_clobber

        def copy_then_modify(*args, **kwargs):
            copied = copy(*args, **kwargs)
            modify()
            return copied

        monkeypatch.setattr(mutation_io, "copy_no_clobber", copy_then_modify)
    else:
        modify()
    with pytest.raises((RuntimeError, OSError)):
        _queue(tmp_path, fixture)
    assert candidate.read_bytes() == b"maybe"
    assert dict(conn.execute("SELECT * FROM files WHERE file_id = ?", (reference_id,)).fetchone()) == before
    assert not conn.execute("SELECT 1 FROM operations WHERE action = 'warning_move' AND state = 'committed'").fetchone()
    conn.close()
