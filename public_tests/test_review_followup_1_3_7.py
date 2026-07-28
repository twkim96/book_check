from pathlib import Path

import pytest

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


def _managed_non_house_member_fixture(tmp_path, member_source):
    state_db = tmp_path / ".dedup_state" / "dedup.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    representative = house / "ㄱ" / "격리할 폴더" / "작품 1권.epub"
    member = temp / member_source / "작품 후보.epub"
    representative.parent.mkdir(parents=True)
    member.parent.mkdir(parents=True)
    representative.write_bytes(b"managed representative")
    member.write_bytes(b"managed non-house member")
    conn = decision_store.initialize_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            representative_row = decision_store.reconcile_file_metadata(
                conn, representative, source="house"
            )
            member_row = decision_store.reconcile_file_metadata(
                conn, member, source=member_source
            )
        _ensure_intake_fingerprint(
            conn, _file_state(conn, representative_row["file_id"])
        )
        _ensure_intake_fingerprint(conn, _file_state(conn, member_row["file_id"]))
        with decision_store.transaction(conn):
            work_id = conn.execute(
                "INSERT INTO works(display_title) VALUES ('비주택 멤버 작품')"
            ).lastrowid
            variant_id = conn.execute(
                "INSERT INTO variants(work_bucket_id, variant_kind) VALUES (?, 'base')",
                (work_id,),
            ).lastrowid
            conn.execute(
                "UPDATE files SET variant_id = ?, assignment_state = 'managed', "
                "assignment_origin = 'human_decision', protected = 1 WHERE file_id = ?",
                (variant_id, representative_row["file_id"]),
            )
            conn.execute(
                "UPDATE files SET variant_id = ?, assignment_state = 'managed', "
                "assignment_origin = 'strong_match', protected = 0 WHERE file_id = ?",
                (variant_id, member_row["file_id"]),
            )
            conn.execute(
                "INSERT INTO representatives(variant_id, file_id) VALUES (?, ?)",
                (variant_id, representative_row["file_id"]),
            )
    finally:
        conn.close()
    return (
        state_db,
        house,
        temp,
        representative,
        member,
        representative_row["file_id"],
        member_row["file_id"],
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


@pytest.mark.parametrize("member_source", ["temp", "queue"])
def test_folder_quarantine_blocks_managed_non_house_member_without_replacement(
    tmp_path, member_source
):
    (
        state_db,
        house,
        temp,
        representative,
        member,
        representative_id,
        member_id,
        variant_id,
    ) = _managed_non_house_member_fixture(tmp_path, member_source)
    plan = folder_quarantine_preview(
        state_db,
        house_dir=house,
        temp_dir=temp,
        folder_path=str(representative.parent),
    )
    blocker = f"variant_has_no_managed_house_replacement:{variant_id}"
    assert plan["apply_available"] is False
    assert blocker in plan["blocked_reasons"]
    transition = plan["representative_transitions"][0]
    assert transition["representative_file_id"] == representative_id
    assert transition["replacement_file_id"] is None
    assert [
        (row["file_id"], row["source"])
        for row in transition["remaining_active_managed_files"]
    ] == [(member_id, member_source)]

    with pytest.raises(RuntimeError, match="folder quarantine is blocked"):
        apply_folder_quarantine(
            state_db,
            house_dir=house,
            temp_dir=temp,
            index_path=tmp_path / "file_index.json",
            folder_path=str(representative.parent),
            confirm_count=plan["item_count"],
            confirm_plan_sha256=plan["plan_sha256"],
        )

    assert representative.is_file()
    assert member.is_file()
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        current = conn.execute(
            "SELECT file_id FROM representatives WHERE variant_id = ?",
            (variant_id,),
        ).fetchone()
        assert current["file_id"] == representative_id
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_folder_quarantine_promotes_remaining_managed_house_member(tmp_path):
    (
        state_db,
        house,
        temp,
        representative,
        _,
        _,
        member_id,
        variant_id,
    ) = _representative_fixture(tmp_path)
    conn = decision_store.connect_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE files SET assignment_state = 'managed', "
                "assignment_origin = 'strong_match' WHERE file_id = ?",
                (member_id,),
            )
    finally:
        conn.close()

    plan = folder_quarantine_preview(
        state_db,
        house_dir=house,
        temp_dir=temp,
        folder_path=str(representative.parent),
    )
    assert plan["apply_available"] is True
    assert plan["representative_transitions"][0]["replacement_file_id"] == member_id
    result = apply_folder_quarantine(
        state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=tmp_path / "file_index.json",
        folder_path=str(representative.parent),
        confirm_count=plan["item_count"],
        confirm_plan_sha256=plan["plan_sha256"],
    )
    assert Path(result["destination_path"]).is_dir()
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        current = conn.execute(
            "SELECT file_id FROM representatives WHERE variant_id = ?",
            (variant_id,),
        ).fetchone()
        assert current["file_id"] == member_id
        protected = conn.execute(
            "SELECT protected FROM files WHERE file_id = ?", (member_id,)
        ).fetchone()[0]
        assert protected == 1
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_doctor_reports_active_managed_variant_without_representative(tmp_path):
    (
        state_db,
        _,
        _,
        _,
        _,
        representative_id,
        _,
        variant_id,
    ) = _managed_non_house_member_fixture(tmp_path, "queue")
    conn = decision_store.connect_state_db(state_db)
    try:
        work_id = conn.execute(
            "SELECT work_bucket_id FROM variants WHERE variant_id = ?",
            (variant_id,),
        ).fetchone()[0]
        with decision_store.transaction(conn):
            conn.execute(
                "DELETE FROM representatives WHERE variant_id = ?", (variant_id,)
            )
        missing = [
            issue
            for issue in decision_store.doctor_issues(conn)
            if issue["kind"] == "active_managed_variant_missing_representative"
        ]
        assert missing == [{
            "kind": "active_managed_variant_missing_representative",
            "variant_id": variant_id,
            "work_bucket_id": work_id,
            "managed_file_count": 2,
        }]

        with decision_store.transaction(conn):
            conn.execute(
                "INSERT INTO representatives(variant_id, file_id) VALUES (?, ?)",
                (variant_id, representative_id),
            )
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
