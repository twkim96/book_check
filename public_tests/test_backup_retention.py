import os
from pathlib import Path

import decision_store


def _automatic_backup(directory: Path, index: int) -> Path:
    path = directory / f"before_test_{index:02d}.sqlite3"
    path.write_bytes(str(index).encode("ascii"))
    os.utime(path, ns=(index, index))
    return path


def test_state_backup_retention_is_global_across_backup_names(tmp_path):
    state_db = tmp_path / "state" / "decisions.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    backup_dir = state_db.parent / "backups"
    backup_dir.mkdir()
    try:
        automatic = [_automatic_backup(backup_dir, index) for index in range(12)]
        differently_named = backup_dir / "pre_requeue_bundle_keep.sqlite3"
        differently_named.write_bytes(b"manual")
        os.utime(differently_named, ns=(6, 6))

        removed = decision_store.prune_state_backups(
            conn, backup_dir, keep_latest=3
        )

        assert len(removed) == 10
        assert [path.exists() for path in automatic] == [False] * 9 + [True] * 3
        assert not differently_named.exists()
    finally:
        conn.close()


def test_state_backup_retention_preserves_unfinished_run_backup(tmp_path):
    state_db = tmp_path / "state" / "decisions.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    backup_dir = state_db.parent / "backups"
    backup_dir.mkdir()
    try:
        automatic = [_automatic_backup(backup_dir, index) for index in range(5)]
        protected = automatic[0].resolve()
        conn.execute(
            """
            INSERT INTO actual_runs(
                run_id, state, house_root, temp_root, backup_path, backup_sha256
            ) VALUES (?, 'approved', ?, ?, ?, ?)
            """,
            ("protected-run", str(tmp_path / "house"), str(tmp_path / "temp"),
             str(protected), "test-only"),
        )
        conn.commit()

        decision_store.prune_state_backups(conn, backup_dir, keep_latest=2)

        assert protected.is_file()
        assert automatic[-2].is_file()
        assert automatic[-1].is_file()
        assert sum(path.exists() for path in automatic) == 3
    finally:
        conn.close()


def test_backup_creation_automatically_applies_retention(tmp_path):
    state_db = tmp_path / "state" / "decisions.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    backup_dir = state_db.parent / "backups"
    try:
        for index in range(decision_store.STATE_BACKUP_RETENTION + 2):
            decision_store.backup_state_db(
                conn, backup_dir / f"before_test_{index:02d}.sqlite3"
            )

        remaining = sorted(backup_dir.glob("*.sqlite3"))
        assert len(remaining) == decision_store.STATE_BACKUP_RETENTION
        assert not (backup_dir / "before_test_00.sqlite3").exists()
        assert not (backup_dir / "before_test_01.sqlite3").exists()
    finally:
        conn.close()
