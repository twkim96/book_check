from pathlib import Path

import decision_store
from dedup_mutations import _ensure_intake_fingerprint, _file_state
from library_management import quarantine_preview
from library_organize import apply_folder_quarantine, folder_quarantine_preview
from mutation_io import atomic_publish_regular_file


def _representative_fixture(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    source = house / "ㄱ" / "격리할 폴더" / "작품 1권.epub"
    unmanaged = house / "ㄱ" / "남길 폴더" / "작품 후보.epub"
    source.parent.mkdir(parents=True)
    unmanaged.parent.mkdir(parents=True)
    temp.mkdir()
    source.write_bytes(b"managed representative")
    unmanaged.write_bytes(b"unmanaged candidate")
    conn = decision_store.initialize_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            source_row = decision_store.reconcile_file_metadata(
                conn, source, source="house"
            )
            unmanaged_row = decision_store.reconcile_file_metadata(
                conn, unmanaged, source="house"
            )
        _ensure_intake_fingerprint(conn, _file_state(conn, source_row["file_id"]))
        _ensure_intake_fingerprint(conn, _file_state(conn, unmanaged_row["file_id"]))
        with decision_store.transaction(conn):
            work_id = conn.execute(
                "INSERT INTO works(display_title) VALUES ('대표 검증 작품')"
            ).lastrowid
            variant_id = conn.execute(
                "INSERT INTO variants(work_bucket_id, variant_kind) VALUES (?, 'base')",
                (work_id,),
            ).lastrowid
            conn.execute(
                "UPDATE files SET variant_id = ?, assignment_state = 'managed', "
                "assignment_origin = 'human_decision', protected = 1 WHERE file_id = ?",
                (variant_id, source_row["file_id"]),
            )
            conn.execute(
                "UPDATE files SET variant_id = ?, assignment_state = 'decision_required', "
                "protected = 0 WHERE file_id = ?",
                (variant_id, unmanaged_row["file_id"]),
            )
            conn.execute(
                "INSERT INTO representatives(variant_id, file_id) VALUES (?, ?)",
                (variant_id, source_row["file_id"]),
            )
    finally:
        conn.close()
    return (
        state_db,
        house,
        temp,
        source,
        unmanaged,
        source_row["file_id"],
        unmanaged_row["file_id"],
        int(variant_id),
    )


def test_atomic_publisher_refuses_symlink_destination(tmp_path):
    source = tmp_path / "new.json"
    victim = tmp_path / "victim.json"
    destination = tmp_path / "file_index.json"
    source.write_text("new", encoding="utf-8")
    victim.write_text("keep", encoding="utf-8")
    destination.symlink_to(victim)

    try:
        atomic_publish_regular_file(source, destination)
    except RuntimeError as exc:
        assert "symlink" in str(exc)
    else:
        raise AssertionError("symlink destination must be rejected")
    assert victim.read_text(encoding="utf-8") == "keep"


def test_individual_representative_quarantine_blocks_unmanaged_replacement(tmp_path):
    state_db, _, temp, _, _, source_id, _, _ = _representative_fixture(tmp_path)
    plan = quarantine_preview(
        state_db, temp_dir=temp, source_file_id=source_id
    )
    assert plan["remaining_variant_files"] == 1
    assert plan["replacement_representative"] is None
    assert plan["apply_available"] is False
    assert "variant_has_no_managed_replacement" in plan["blocked_reasons"]


def test_folder_quarantine_does_not_promote_unmanaged_replacement(tmp_path):
    (
        state_db,
        house,
        temp,
        source,
        _,
        _,
        unmanaged_id,
        variant_id,
    ) = _representative_fixture(tmp_path)
    index = tmp_path / "file_index.json"
    plan = folder_quarantine_preview(
        state_db,
        house_dir=house,
        temp_dir=temp,
        folder_path=str(source.parent),
    )
    result = apply_folder_quarantine(
        state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=index,
        folder_path=str(source.parent),
        confirm_count=plan["item_count"],
        confirm_plan_sha256=plan["plan_sha256"],
    )
    assert Path(result["destination_path"]).is_dir()
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute(
            "SELECT file_id FROM representatives WHERE variant_id = ?",
            (variant_id,),
        ).fetchone() is None
        unmanaged = conn.execute(
            "SELECT assignment_state, protected FROM files WHERE file_id = ?",
            (unmanaged_id,),
        ).fetchone()
        assert tuple(unmanaged) == ("decision_required", 0)
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
