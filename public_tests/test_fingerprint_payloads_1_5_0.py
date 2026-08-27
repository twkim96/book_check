import sqlite3
import uuid

import pytest

import decision_store
import fingerprint_payloads


def _insert_fingerprint(conn, root, index, front=None, tail=None):
    file_id = str(uuid.uuid4())
    path = root / f"book-{index}.txt"
    conn.execute(
        """
        INSERT INTO files(file_id, canonical_path, source, size, mtime_ns)
        VALUES (?, ?, 'house', ?, ?)
        """,
        (file_id, str(path), index + 1, index + 10),
    )
    if conn.execute("PRAGMA user_version").fetchone()[0] >= 19:
        state, payload_hash = decision_store.prepare_fingerprint_anchor_payload(
            conn, front, tail
        )
        fingerprint_id = conn.execute(
            """
            INSERT INTO fingerprints(
                file_id, canonical_path, size, mtime_ns,
                normalizer_version, fingerprint_version, status,
                anchor_payload_state, anchor_payload_hash
            ) VALUES (?, ?, ?, ?, 'test', ?, 'ok', ?, ?)
            """,
            (
                file_id, str(path), index + 1, index + 10,
                f"test-{index}", state, payload_hash,
            ),
        ).lastrowid
        decision_store.store_fingerprint_anchor_payload(
            conn, fingerprint_id, front, tail
        )
    else:
        fingerprint_id = conn.execute(
            """
            INSERT INTO fingerprints(
                file_id, canonical_path, size, mtime_ns,
                normalizer_version, fingerprint_version, status,
                front_anchor, tail_anchor
            ) VALUES (?, ?, ?, ?, 'test', ?, 'ok', ?, ?)
            """,
            (
                file_id, str(path), index + 1, index + 10,
                f"test-{index}", front, tail,
            ),
        ).lastrowid
    conn.execute(
        "UPDATE files SET current_fingerprint_id = ? WHERE file_id = ?",
        (fingerprint_id, file_id),
    )
    return fingerprint_id


def test_canonical_frame_preserves_front_tail_boundary():
    assert fingerprint_payloads.canonical_anchor_payload("ab", "c") != (
        fingerprint_payloads.canonical_anchor_payload("a", "bc")
    )


def test_new_payload_refs_deduplicate_and_keep_legacy_columns_empty(tmp_path):
    conn = decision_store.initialize_state_db(tmp_path / "state.sqlite3")
    try:
        with decision_store.transaction(conn):
            first = _insert_fingerprint(
                conn, tmp_path, 1, "앞부분" * 1000, "뒷부분" * 1000
            )
            _insert_fingerprint(
                conn, tmp_path, 2, "앞부분" * 1000, "뒷부분" * 1000
            )

        assert conn.execute(
            "SELECT COUNT(*) FROM anchor_payload_objects"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM fingerprint_anchor_refs"
        ).fetchone()[0] == 2
        assert conn.execute(
            """
            SELECT COUNT(*) FROM fingerprints
            WHERE front_anchor IS NOT NULL OR tail_anchor IS NOT NULL
            """
        ).fetchone()[0] == 0
        assert decision_store.load_fingerprint_anchor_payload(conn, first) == (
            "앞부분" * 1000,
            "뒷부분" * 1000,
        )
        decision_store.validate_schema(conn)
    finally:
        conn.close()


def test_schema_v19_migrates_v17_anchors_without_changing_fingerprint_ids(tmp_path):
    state_db = tmp_path / "state.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        conn.executescript(
            """
            DROP TRIGGER fingerprints_insert_storage_guard;
            DROP TRIGGER fingerprint_anchor_refs_expected_hash;
            PRAGMA user_version = 17;
            """
        )
        with decision_store.transaction(conn):
            first = _insert_fingerprint(
                conn, tmp_path, 1, "공유 앞" * 1000, "공유 뒤" * 1000
            )
            second = _insert_fingerprint(
                conn, tmp_path, 2, "공유 앞" * 1000, "공유 뒤" * 1000
            )
            empty = _insert_fingerprint(conn, tmp_path, 3, "", "")
    finally:
        conn.close()

    migrated = decision_store.initialize_state_db(
        state_db,
        migrate=True,
        compact_migrations=True,
        anchor_payload_migration=True,
    )
    try:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 19
        assert [
            row[0] for row in migrated.execute(
                "SELECT fingerprint_id FROM fingerprints ORDER BY fingerprint_id"
            )
        ] == [first, second, empty]
        assert migrated.execute(
            "SELECT COUNT(*) FROM anchor_payload_objects"
        ).fetchone()[0] == 1
        assert migrated.execute(
            "SELECT COUNT(*) FROM fingerprint_anchor_refs"
        ).fetchone()[0] == 2
        assert migrated.execute(
            """
            SELECT COUNT(*) FROM fingerprints
            WHERE front_anchor IS NOT NULL OR tail_anchor IS NOT NULL
            """
        ).fetchone()[0] == 0
        assert decision_store.load_fingerprint_anchor_payload(migrated, first) == (
            "공유 앞" * 1000,
            "공유 뒤" * 1000,
        )
        assert decision_store.load_fingerprint_anchor_payload(migrated, empty) == (
            "",
            "",
        )
        stats = decision_store.anchor_payload_storage_stats(migrated)
        assert stats["reference_count"] == 2
        assert stats["object_count"] == 1
        assert stats["compressed_bytes"] < stats["raw_bytes"]
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
        decision_store.validate_schema(migrated)
    finally:
        migrated.close()


def test_payload_objects_and_refs_are_immutable_and_corruption_fails_closed(tmp_path):
    conn = decision_store.initialize_state_db(tmp_path / "state.sqlite3")
    try:
        with decision_store.transaction(conn):
            fingerprint_id = _insert_fingerprint(
                conn, tmp_path, 1, "front", "tail"
            )
            payload_hash = conn.execute(
                "SELECT anchor_payload_hash FROM fingerprints WHERE fingerprint_id = ?",
                (fingerprint_id,),
            ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE anchor_payload_objects SET codec = codec WHERE payload_hash = ?",
                (payload_hash,),
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "DELETE FROM fingerprint_anchor_refs WHERE fingerprint_id = ?",
                (fingerprint_id,),
            )
        conn.rollback()

        conn.execute("DROP TRIGGER anchor_payload_objects_no_update")
        conn.execute(
            """
            UPDATE anchor_payload_objects
            SET compressed_payload = X'00010203'
            WHERE payload_hash = ?
            """,
            (payload_hash,),
        )
        conn.commit()
        with pytest.raises(
            fingerprint_payloads.AnchorPayloadCorruptionError,
            match="decompression failed",
        ):
            decision_store.load_fingerprint_anchor_payload(conn, fingerprint_id)
    finally:
        conn.close()


def test_missing_or_wrong_reference_fails_against_immutable_expectation(tmp_path):
    conn = decision_store.initialize_state_db(tmp_path / "state.sqlite3")
    try:
        with decision_store.transaction(conn):
            first = _insert_fingerprint(conn, tmp_path, 1, "shared", "payload")
            _insert_fingerprint(conn, tmp_path, 2, "shared", "payload")
            other = _insert_fingerprint(conn, tmp_path, 3, "other", "payload")
        conn.execute("DROP TRIGGER fingerprint_anchor_refs_no_delete")
        conn.execute(
            "DELETE FROM fingerprint_anchor_refs WHERE fingerprint_id = ?", (first,)
        )
        conn.commit()
        with pytest.raises(
            fingerprint_payloads.AnchorPayloadCorruptionError,
            match="expected anchor reference is missing",
        ):
            decision_store.validate_anchor_payload_storage(conn)
        with pytest.raises(
            fingerprint_payloads.AnchorPayloadCorruptionError,
            match="expected anchor payload reference is missing",
        ):
            decision_store.load_fingerprint_anchor_payload(conn, first)

        conn.execute("DROP TRIGGER fingerprint_anchor_refs_expected_hash")
        wrong_hash = conn.execute(
            "SELECT payload_hash FROM fingerprint_anchor_refs WHERE fingerprint_id = ?",
            (other,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO fingerprint_anchor_refs(fingerprint_id, payload_hash) VALUES (?, ?)",
            (first, wrong_hash),
        )
        conn.commit()
        with pytest.raises(
            fingerprint_payloads.AnchorPayloadCorruptionError,
            match="differs from expected hash",
        ):
            decision_store.validate_anchor_payload_storage(conn)
    finally:
        conn.close()


def test_schema_v19_rejects_legacy_and_pre_v19_writer_inserts(tmp_path):
    state_db = tmp_path / "state.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        file_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO files(file_id, canonical_path, source, size, mtime_ns) "
            "VALUES (?, ?, 'house', 1, 1)",
            (file_id, str(tmp_path / "legacy.txt")),
        )
        with pytest.raises(sqlite3.IntegrityError, match="legacy fingerprint"):
            conn.execute(
                """
                INSERT INTO fingerprints(
                    file_id, canonical_path, size, mtime_ns,
                    normalizer_version, fingerprint_version, status, front_anchor
                ) VALUES (?, ?, 1, 1, 'test', 'legacy', 'ok', 'forbidden')
                """,
                (file_id, str(tmp_path / "legacy.txt")),
            )
        conn.rollback()
    finally:
        conn.close()

    raw = sqlite3.connect(state_db)
    try:
        file_id = str(uuid.uuid4())
        raw.execute(
            "INSERT INTO files(file_id, canonical_path, source, size, mtime_ns) "
            "VALUES (?, ?, 'house', 1, 1)",
            (file_id, str(tmp_path / "old-writer.txt")),
        )
        with pytest.raises(sqlite3.OperationalError, match="no such function"):
            raw.execute(
                """
                INSERT INTO fingerprints(
                    file_id, canonical_path, size, mtime_ns,
                    normalizer_version, fingerprint_version, status
                ) VALUES (?, ?, 1, 1, 'old', 'old', 'ok')
                """,
                (file_id, str(tmp_path / "old-writer.txt")),
            )
    finally:
        raw.close()
