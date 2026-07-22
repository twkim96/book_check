import json
from pathlib import Path

import pytest

import decision_store
from dedup_mutations import _ensure_intake_fingerprint, _file_state
from library_management import (
    apply_purge,
    apply_quarantine,
    apply_relationship,
    apply_restore,
    cancel_relationship,
    purge_preview,
    quarantine_preview,
    relationship_preview,
    restore_preview,
)


def _fixture(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    folder = house / "ㅅ" / "사람 판단 작품"
    folder.mkdir(parents=True)
    temp.mkdir()
    paths = [folder / "사람 판단 작품 9권.txt", folder / "사람 판단 작품 9권 extra.txt"]
    paths[0].write_text("첫 번째 판본 본문", encoding="utf-8")
    paths[1].write_text("서로 다른 extra 본문", encoding="utf-8")
    conn = decision_store.initialize_state_db(state_db)
    try:
        ids = []
        for path in paths:
            with decision_store.transaction(conn):
                row = decision_store.reconcile_file_metadata(conn, path, source="house")
            ids.append(row["file_id"])
            _ensure_intake_fingerprint(conn, _file_state(conn, row["file_id"]))
    finally:
        conn.close()

    return state_db, house, temp, tmp_path / "file_index.json", paths, ids


def _apply_relationship(state_db, house, temp, ids, verdict="same_work_distinct_variant"):
    plan = relationship_preview(
        state_db, left_file_id=ids[0], right_file_id=ids[1],
        verdict=verdict, variant_kind="other", note="사람 확인",
    )
    assert plan["apply_available"] is True
    return apply_relationship(
        state_db, house_dir=house, temp_dir=temp,
        left_file_id=ids[0], right_file_id=ids[1], verdict=verdict,
        variant_kind="other", note="사람 확인", confirm_count=2,
        confirm_plan_sha256=plan["plan_sha256"],
    )


def test_relationship_preview_apply_and_cancel_preserve_history(tmp_path):
    state_db, house, temp, _, _, ids = _fixture(tmp_path)
    result = _apply_relationship(state_db, house, temp, ids)

    conn = decision_store.connect_state_db(state_db)
    try:
        active = conn.execute(
            "SELECT verdict, active FROM decisions WHERE decision_id = ?", (result["decision_id"],)
        ).fetchone()
        assert tuple(active) == ("same_work_distinct_variant", 1)
        files = conn.execute(
            "SELECT assignment_state, variant_id FROM files WHERE file_id IN (?, ?) ORDER BY file_id",
            ids,
        ).fetchall()
        assert all(row["assignment_state"] == "managed" for row in files)
        assert len({row["variant_id"] for row in files}) == 2
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()

    cancelled = cancel_relationship(
        state_db, house_dir=house, temp_dir=temp, decision_id=result["decision_id"]
    )
    assert cancelled["review_id"]
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute(
            "SELECT active FROM decisions WHERE decision_id = ?", (result["decision_id"],)
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_relationship_cancel_fails_closed_when_doctor_is_not_clean(tmp_path):
    state_db, house, temp, _, paths, ids = _fixture(tmp_path)
    result = _apply_relationship(state_db, house, temp, ids)
    paths[0].write_text("외부에서 바뀐 본문과 크기", encoding="utf-8")

    with pytest.raises(RuntimeError, match="doctor failed before decision cancel"):
        cancel_relationship(
            state_db, house_dir=house, temp_dir=temp,
            decision_id=result["decision_id"],
        )

    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute(
            "SELECT active FROM decisions WHERE decision_id = ?",
            (result["decision_id"],),
        ).fetchone()[0] == 1
    finally:
        conn.close()

def test_user_quarantine_retires_representative_and_restore_records_distinct_decision(tmp_path):
    state_db, house, temp, index, paths, ids = _fixture(tmp_path)
    _apply_relationship(state_db, house, temp, ids)
    plan = quarantine_preview(
        state_db, temp_dir=temp, source_file_id=ids[0], keep_file_id=ids[1],
    )
    assert plan["apply_available"] is True
    assert plan["retired_variant"] is True
    quarantined = apply_quarantine(
        state_db, house_dir=house, temp_dir=temp, index_path=index,
        source_file_id=ids[0], keep_file_id=ids[1],
        confirm_count=1, confirm_plan_sha256=plan["plan_sha256"],
    )
    quarantine_path = Path(quarantined["dest_path"])
    assert quarantine_path.is_file()
    assert not paths[0].exists()
    manifest = json.loads(Path(quarantined["manifest_path"]).read_text(encoding="utf-8"))
    assert len(manifest["files"]) == 2

    restore = restore_preview(
        state_db, house_dir=house, operation_id=quarantined["operation_id"],
        reference_file_id=ids[1], verdict="same_work_distinct_variant",
        note="extra와 별도 보존",
    )
    assert restore["apply_available"] is True
    restored = apply_restore(
        state_db, house_dir=house, temp_dir=temp, index_path=index,
        operation_id=quarantined["operation_id"], reference_file_id=ids[1],
        verdict="same_work_distinct_variant", note="extra와 별도 보존",
        confirm_count=1, confirm_plan_sha256=restore["plan_sha256"],
    )
    assert Path(restored["dest_path"]).is_file()
    assert not quarantine_path.exists()
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        row = conn.execute(
            "SELECT active, source, assignment_state FROM files WHERE file_id = ?", (ids[0],)
        ).fetchone()
        assert tuple(row) == (1, "house", "managed")
        assert conn.execute(
            "SELECT verdict FROM decisions WHERE decision_id = ?", (restored["decision_id"],)
        ).fetchone()[0] == "same_work_distinct_variant"
    finally:
        conn.close()


def test_user_quarantine_prepares_missing_fingerprint_after_backup(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    source = house / "ㄱ" / "지문 없는 신규 도서.txt"
    source.parent.mkdir(parents=True)
    temp.mkdir()
    source.write_text("아직 감사되지 않은 실제 도서 본문", encoding="utf-8")
    conn = decision_store.initialize_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            row = decision_store.reconcile_file_metadata(conn, source, source="house")
        file_id = row["file_id"]
        assert conn.execute(
            "SELECT current_fingerprint_id FROM files WHERE file_id = ?", (file_id,)
        ).fetchone()[0] is None
    finally:
        conn.close()

    plan = quarantine_preview(state_db, temp_dir=temp, source_file_id=file_id)
    assert plan["apply_available"] is True
    assert plan["fingerprint_preparation_count"] == 1
    assert plan["source"]["current_fingerprint_id"] is None
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute(
            "SELECT current_fingerprint_id FROM files WHERE file_id = ?", (file_id,)
        ).fetchone()[0] is None
    finally:
        conn.close()

    result = apply_quarantine(
        state_db, house_dir=house, temp_dir=temp, index_path=tmp_path / "file_index.json",
        source_file_id=file_id, keep_file_id=None, confirm_count=1,
        confirm_plan_sha256=plan["plan_sha256"],
    )
    assert not source.exists()
    assert Path(result["dest_path"]).is_file()
    assert Path(result["backup_path"]).is_file()
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        stored = conn.execute(
            """
            SELECT f.current_fingerprint_id, fp.status
            FROM files f JOIN fingerprints fp
              ON fp.fingerprint_id = f.current_fingerprint_id
            WHERE f.file_id = ?
            """,
            (file_id,),
        ).fetchone()
        assert stored["current_fingerprint_id"] is not None
        assert stored["status"] == "raw_only"
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_selected_quarantine_purge_confirms_plan_and_releases_only_selected_bytes(tmp_path):
    state_db, house, temp, index, paths, ids = _fixture(tmp_path)
    plan = quarantine_preview(
        state_db, temp_dir=temp, source_file_id=ids[0], keep_file_id=ids[1]
    )
    quarantined = apply_quarantine(
        state_db, house_dir=house, temp_dir=temp, index_path=index,
        source_file_id=ids[0], keep_file_id=ids[1],
        confirm_count=1, confirm_plan_sha256=plan["plan_sha256"],
    )
    purge = purge_preview(state_db, operation_ids=[quarantined["operation_id"]])
    assert purge["apply_available"] is True
    result = apply_purge(
        state_db, house_dir=house, temp_dir=temp,
        operation_ids=[quarantined["operation_id"]], confirm_count=1,
        confirm_plan_sha256=purge["plan_sha256"],
    )
    assert result["purged_count"] == 1
    assert result["released_bytes"] == len("첫 번째 판본 본문".encode("utf-8"))
    assert not Path(quarantined["dest_path"]).exists()
    assert paths[1].is_file()
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute(
            "SELECT purged_at IS NOT NULL FROM operations WHERE operation_id = ?",
            (quarantined["operation_id"],),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_interrupted_restore_rolls_bytes_back_to_quarantine(tmp_path, monkeypatch):
    state_db, house, temp, index, paths, ids = _fixture(tmp_path)
    quarantine = quarantine_preview(
        state_db, temp_dir=temp, source_file_id=ids[0], keep_file_id=ids[1]
    )
    quarantined = apply_quarantine(
        state_db, house_dir=house, temp_dir=temp, index_path=index,
        source_file_id=ids[0], keep_file_id=ids[1],
        confirm_count=1, confirm_plan_sha256=quarantine["plan_sha256"],
    )
    restore = restore_preview(
        state_db, house_dir=house, operation_id=quarantined["operation_id"],
        reference_file_id=ids[1], verdict="same_work_distinct_variant",
    )

    def fail_decision(*args, **kwargs):
        raise RuntimeError("injected decision failure")

    monkeypatch.setattr(decision_store, "apply_decision", fail_decision)
    with pytest.raises(RuntimeError, match="injected decision failure"):
        apply_restore(
            state_db, house_dir=house, temp_dir=temp, index_path=index,
            operation_id=quarantined["operation_id"], reference_file_id=ids[1],
            verdict="same_work_distinct_variant", note="", confirm_count=1,
            confirm_plan_sha256=restore["plan_sha256"],
        )

    conn = decision_store.connect_state_db(state_db)
    try:
        unfinished = conn.execute(
            "SELECT operation_id FROM operations WHERE action = 'user_quarantine_restore' AND state = 'fs_done'"
        ).fetchone()
        assert unfinished is not None
        assert decision_store.recover_interrupted_operation(conn, unfinished["operation_id"]) == "rolled_back"
        file_row = conn.execute(
            "SELECT canonical_path, source, active FROM files WHERE file_id = ?", (ids[0],)
        ).fetchone()
        assert tuple(file_row) == (quarantined["dest_path"], "quarantine", 0)
    finally:
        conn.close()
    assert Path(quarantined["dest_path"]).is_file()
    assert not paths[0].exists()
