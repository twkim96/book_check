from pathlib import Path
import unicodedata

import pytest

import decision_store
import dedup_mutations
import deduplicator


def _add(conn, path: Path, source: str, content=b"identical bytes"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    with decision_store.transaction(conn):
        row = decision_store.reconcile_file_metadata(conn, path, source=source)
    dedup_mutations.refresh_user_approved_snapshot(conn, row["file_id"])
    result = dict(dedup_mutations._file_state(conn, row["file_id"]))
    result.update({
        "name": path.name,
        "path": str(path),
        "ext": path.suffix.lower(),
        "mutation_eligible": True,
    })
    return result


def _approve(state_db: Path, house: Path, temp: Path):
    conn = decision_store.connect_state_db(state_db)
    try:
        backup = decision_store.backup_state_db(
            conn, state_db.parent / "backups" / "before-exact-cleanup.sqlite3"
        )
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()
    return decision_store.prepare_actual_run(state_db, house, temp)[0]


def _exact_group(keep, duplicate):
    return {
        "hash": keep["raw_sha256"],
        "keep": keep,
        "duplicates": [duplicate],
    }


def _run_exact_group(state_db, temp, run_id, keep, duplicate):
    return deduplicator._managed_exact_records(
        [_exact_group(keep, duplicate)],
        str(state_db),
        str(temp),
        False,
        actual_run_id=run_id,
    )[0]


def _multi_representative_exact_fixture(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        first = _add(conn, house / "첫 작품.txt", "house")
        second = _add(conn, house / "둘째 작품.txt", "house")
        incoming = _add(conn, temp / "검토 대상.txt", "temp")
        with decision_store.transaction(conn):
            for title, representative in (
                ("첫 작품", first),
                ("둘째 작품", second),
            ):
                work_id = conn.execute(
                    "INSERT INTO works(display_title) VALUES (?)", (title,)
                ).lastrowid
                variant_id = conn.execute(
                    "INSERT INTO variants(work_bucket_id, variant_kind) "
                    "VALUES (?, 'base')",
                    (work_id,),
                ).lastrowid
                conn.execute(
                    "UPDATE files SET variant_id = ?, assignment_state = 'managed', "
                    "assignment_origin = 'human_decision', protected = 1 "
                    "WHERE file_id = ?",
                    (variant_id, representative["file_id"]),
                )
                conn.execute(
                    "INSERT INTO representatives(variant_id, file_id) VALUES (?, ?)",
                    (variant_id, representative["file_id"]),
                )
    finally:
        conn.close()
    return state_db, house, temp, first, second, incoming


def test_unassigned_temp_exact_is_quarantined_against_unassigned_house_keep(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        keep = _add(conn, house / "ㄴ" / "남궁세가의 서자 1-175 완.txt", "house")
        duplicate = _add(conn, temp / "남궁세가의 서자 1-175 완.txt", "temp")
    finally:
        conn.close()

    run_id = _approve(state_db, house, temp)
    record = _run_exact_group(state_db, temp, run_id, keep, duplicate)

    conn = decision_store.connect_state_db(state_db)
    try:
        decision_store.finish_actual_run(conn, run_id, success=True)
        moved = conn.execute(
            "SELECT * FROM files WHERE file_id = ?", (duplicate["file_id"],)
        ).fetchone()
        assert moved["active"] == 0
        assert moved["source"] == "quarantine"
        assert moved["assignment_state"] == "unassigned"
        assert moved["variant_id"] is None
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()

    assert record["action"] == "exact_quarantine"
    assert Path(record["dest_path"]).is_file()
    assert Path(keep["canonical_path"]).is_file()
    assert not Path(duplicate["canonical_path"]).exists()


def test_state_enrichment_matches_macos_nfd_path_to_nfc_db_row(tmp_path):
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    physical = tmp_path / unicodedata.normalize("NFD", "한글 제목.txt")
    physical.write_bytes(b"same")
    conn = decision_store.initialize_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            row = decision_store.reconcile_file_metadata(conn, physical, source="temp")
    finally:
        conn.close()

    entry = {"path": str(physical)}
    deduplicator.enrich_entries_from_state_db([entry], str(state_db))

    assert entry["file_id"] == row["file_id"]
    assert entry["assignment_state"] == "unassigned"


def test_raw_exact_ignores_conflicting_filename_coordinates(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        keep = _add(conn, house / "작품 1권.epub", "house")
        duplicate = _add(conn, temp / "다른표기 9권.epub", "temp")
    finally:
        conn.close()

    run_id = _approve(state_db, house, temp)
    record = _run_exact_group(state_db, temp, run_id, keep, duplicate)
    conn = decision_store.connect_state_db(state_db)
    try:
        decision_store.finish_actual_run(conn, run_id, success=True)
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
    assert record["action"] == "exact_quarantine"
    assert Path(record["dest_path"]).is_file()


def test_raw_exact_can_finalize_a_queue_hold(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        keep = _add(conn, house / "보관본 1권.epub", "house")
        queued = _add(
            conn,
            temp / "trash_bin" / "warning" / "volume_coordinate_conflicts" /
            "보류본 1권.epub",
            "queue",
        )
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE files SET assignment_state='decision_required' WHERE file_id=?",
                (queued["file_id"],),
            )
    finally:
        conn.close()

    run_id = _approve(state_db, house, temp)
    conn = decision_store.connect_state_db(state_db)
    try:
        result = dedup_mutations.exact_quarantine(
            conn,
            source_file_id=queued["file_id"],
            keep_file_id=keep["file_id"],
            quarantine_dir=temp / "trash_bin" / "exact_quarantine",
            run_id=run_id,
        )
        decision_store.finish_actual_run(conn, run_id, success=True)
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
    assert Path(result["dest_path"]).is_file()
    assert not Path(queued["canonical_path"]).exists()


def test_unassigned_house_exact_duplicate_is_cleaned_by_same_pipeline(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        keep = _add(conn, house / "ㄴ" / "남궁세가의 서자 1-175 완.txt", "house")
        duplicate = _add(
            conn, house / "ㄴ" / "남궁세가의 서자 1-175 완_dup_1.txt", "house"
        )
        with decision_store.transaction(conn):
            review_id = conn.execute(
                """
                INSERT INTO review_items(
                    candidate_file_id, reference_file_id,
                    left_fingerprint_id, right_fingerprint_id,
                    classification, evidence_json
                ) VALUES (?, ?, ?, ?, 'text_equivalent', '{}')
                """,
                (
                    duplicate["file_id"], keep["file_id"],
                    duplicate["current_fingerprint_id"], keep["current_fingerprint_id"],
                ),
            ).lastrowid
    finally:
        conn.close()

    run_id = _approve(state_db, house, temp)
    record = _run_exact_group(state_db, temp, run_id, keep, duplicate)

    conn = decision_store.connect_state_db(state_db)
    try:
        decision_store.finish_actual_run(conn, run_id, success=True)
        moved = conn.execute(
            "SELECT * FROM files WHERE file_id = ?", (duplicate["file_id"],)
        ).fetchone()
        review = conn.execute(
            "SELECT state, evidence_json FROM review_items WHERE review_id = ?", (review_id,)
        ).fetchone()
        assert moved["active"] == 0
        assert moved["source"] == "quarantine"
        assert review["state"] == "superseded"
        assert (
            __import__("json").loads(review["evidence_json"])["automatic_suppression"]["reason"]
            == "exact_quarantine"
        )
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()

    assert record["action"] == "exact_quarantine"
    assert Path(record["dest_path"]).is_file()
    assert Path(keep["canonical_path"]).is_file()
    assert not Path(duplicate["canonical_path"]).exists()


def test_unassigned_house_cleanup_still_revalidates_raw_sha(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        keep = _add(conn, house / "원본.txt", "house", b"keep")
        duplicate = _add(conn, house / "원본_dup_1.txt", "house", b"different")
    finally:
        conn.close()

    run_id = _approve(state_db, house, temp)
    conn = decision_store.connect_state_db(state_db)
    try:
        try:
            dedup_mutations.exact_quarantine(
                conn,
                source_file_id=duplicate["file_id"],
                keep_file_id=keep["file_id"],
                quarantine_dir=temp / "trash_bin" / "exact_quarantine",
                run_id=run_id,
            )
        except RuntimeError as exc:
            assert "raw SHA" in str(exc)
        else:
            raise AssertionError("different bytes must not be quarantined as exact")
        decision_store.finish_actual_run(conn, run_id, success=False, error="expected")
    finally:
        conn.close()

    assert Path(keep["canonical_path"]).is_file()
    assert Path(duplicate["canonical_path"]).is_file()


def test_exact_mutation_api_blocks_multiple_managed_representative_identities(tmp_path):
    state_db, house, temp, first, second, incoming = (
        _multi_representative_exact_fixture(tmp_path)
    )

    run_id = _approve(state_db, house, temp)
    conn = decision_store.connect_state_db(state_db)
    try:
        with pytest.raises(
            RuntimeError,
            match="multiple managed representative identities",
        ):
            dedup_mutations.exact_quarantine(
                conn,
                source_file_id=incoming["file_id"],
                keep_file_id=first["file_id"],
                quarantine_dir=temp / "trash_bin" / "exact_quarantine",
                run_id=run_id,
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM operations WHERE run_id = ?", (run_id,)
        ).fetchone()[0] == 0
        decision_store.finish_actual_run(
            conn, run_id, success=False, error="expected conflict"
        )
    finally:
        conn.close()

    assert Path(first["canonical_path"]).is_file()
    assert Path(second["canonical_path"]).is_file()
    assert Path(incoming["canonical_path"]).is_file()


def test_exact_orchestrator_reports_blocked_multi_representative_candidate(tmp_path):
    state_db, house, temp, first, second, incoming = (
        _multi_representative_exact_fixture(tmp_path)
    )
    run_id = _approve(state_db, house, temp)
    record = deduplicator._managed_exact_records(
        [_exact_group(first, incoming)],
        str(state_db),
        str(temp),
        False,
        actual_run_id=run_id,
        blocked_candidate_paths={incoming["path"]},
    )[0]

    conn = decision_store.connect_state_db(state_db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM operations WHERE run_id = ?", (run_id,)
        ).fetchone()[0] == 0
        decision_store.finish_actual_run(conn, run_id, success=True)
    finally:
        conn.close()
    assert record["action"] == "managed_report_only"
    assert record["reason"] == "multi_representative_conflict"
    assert record["dest_path"] is None
    assert Path(first["canonical_path"]).is_file()
    assert Path(second["canonical_path"]).is_file()
    assert Path(incoming["canonical_path"]).is_file()


def test_exact_cleanup_accepts_decode_lossy_fingerprint_without_cached_raw_sha(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        keep = _add(conn, house / "손실 디코딩.txt", "house")
        duplicate = _add(conn, house / "손실 디코딩_dup_1.txt", "house")
        with decision_store.transaction(conn):
            lossy_ids = []
            for item in (keep, duplicate):
                lossy_ids.append(conn.execute(
                    """
                    INSERT INTO fingerprints(
                        file_id, canonical_path, size, mtime_ns, normalizer_version,
                        fingerprint_version, dev, ino, ctime_ns, status
                    ) VALUES (?, ?, ?, ?, '1.3.0', ?, ?, ?, ?, 'decode_lossy')
                    """,
                    (
                        item["file_id"], item["canonical_path"], item["size"],
                        item["mtime_ns"], f"test-lossy-{item['file_id']}",
                        item["dev"], item["ino"], item["ctime_ns"],
                    ),
                ).lastrowid)
            conn.execute(
                "UPDATE files SET current_fingerprint_id = ? WHERE file_id = ?",
                (lossy_ids[0], keep["file_id"]),
            )
            conn.execute(
                "UPDATE files SET current_fingerprint_id = ? WHERE file_id = ?",
                (lossy_ids[1], duplicate["file_id"]),
            )
    finally:
        conn.close()

    run_id = _approve(state_db, house, temp)
    record = _run_exact_group(state_db, temp, run_id, keep, duplicate)

    conn = decision_store.connect_state_db(state_db)
    try:
        decision_store.finish_actual_run(conn, run_id, success=True)
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
    assert record["action"] == "exact_quarantine"
    assert Path(record["dest_path"]).is_file()
    assert Path(keep["canonical_path"]).is_file()


def test_exact_cleanup_prepares_missing_legacy_fingerprints(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        keep = _add(conn, house / "레거시.txt", "house")
        duplicate = _add(conn, house / "레거시_dup_1.txt", "house")
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE files SET current_fingerprint_id = NULL WHERE file_id IN (?, ?)",
                (keep["file_id"], duplicate["file_id"]),
            )
    finally:
        conn.close()

    run_id = _approve(state_db, house, temp)
    record = _run_exact_group(state_db, temp, run_id, keep, duplicate)

    conn = decision_store.connect_state_db(state_db)
    try:
        current_keep = conn.execute(
            "SELECT current_fingerprint_id FROM files WHERE file_id = ?", (keep["file_id"],)
        ).fetchone()
        decision_store.finish_actual_run(conn, run_id, success=True)
        assert current_keep["current_fingerprint_id"] is not None
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
    assert record["action"] == "exact_quarantine"
    assert Path(record["dest_path"]).is_file()


def test_exact_cleanup_preserves_decision_required_keep(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        keep = _add(conn, house / "검토 대상.txt", "house")
        duplicate = _add(conn, house / "검토 대상_dup_1.txt", "house")
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE files SET assignment_state = 'decision_required' WHERE file_id = ?",
                (keep["file_id"],),
            )
        keep["assignment_state"] = "decision_required"
        keep["mutation_eligible"] = False
    finally:
        conn.close()

    run_id = _approve(state_db, house, temp)
    record = _run_exact_group(state_db, temp, run_id, keep, duplicate)

    conn = decision_store.connect_state_db(state_db)
    try:
        current_keep = conn.execute(
            "SELECT active, assignment_state FROM files WHERE file_id = ?", (keep["file_id"],)
        ).fetchone()
        decision_store.finish_actual_run(conn, run_id, success=True)
        assert current_keep["active"] == 1
        assert current_keep["assignment_state"] == "decision_required"
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
    assert record["action"] == "exact_quarantine"
    assert Path(keep["canonical_path"]).is_file()


def test_exact_cleanup_quarantines_unprotected_legacy_duplicate(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        keep = _add(conn, house / "레거시 중복.txt", "house")
        duplicate = _add(conn, house / "레거시 중복〔P〕.txt", "house")
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE files SET assignment_state = 'legacy_unresolved' WHERE file_id = ?",
                (duplicate["file_id"],),
            )
        duplicate["assignment_state"] = "legacy_unresolved"
        duplicate["mutation_eligible"] = False
    finally:
        conn.close()

    run_id = _approve(state_db, house, temp)
    record = _run_exact_group(state_db, temp, run_id, keep, duplicate)

    conn = decision_store.connect_state_db(state_db)
    try:
        decision_store.finish_actual_run(conn, run_id, success=True)
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
    assert record["action"] == "exact_quarantine"
    assert Path(record["dest_path"]).is_file()
    assert Path(keep["canonical_path"]).is_file()
