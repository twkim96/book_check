from pathlib import Path

import pytest

import decision_store
from dedup_mutations import _ensure_intake_fingerprint, _file_state
import library_organize
from library_organize import (
    apply_folder_quarantine,
    apply_file_relocate,
    apply_managed_folder_create,
    apply_managed_folder_adopt,
    apply_managed_folder_relocate,
    file_relocate_preview,
    folder_quarantine_preview,
    managed_folder_preview,
    managed_folder_adopt_preview,
    managed_folder_relocate_preview,
)


def _fixture(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    source_dir = house / "ㄱ" / "기존 작품"
    target_dir = house / "ㄱ" / "정리 폴더"
    source_dir.mkdir(parents=True)
    target_dir.mkdir(parents=True)
    temp.mkdir()
    source = source_dir / "기존 작품 01권 noPic ver.epub"
    source.write_bytes(b"synthetic epub bytes")
    conn = decision_store.initialize_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            row = decision_store.reconcile_file_metadata(conn, source, source="house")
        _ensure_intake_fingerprint(conn, _file_state(conn, row["file_id"]))
        with decision_store.transaction(conn):
            decision_store.upsert_file_analysis(conn, row["file_id"], source)
    finally:
        conn.close()
    return state_db, house, temp, tmp_path / "file_index.json", source, target_dir, row["file_id"]


def test_same_projection_rename_and_move_preserve_file_relationship(tmp_path):
    state_db, house, temp, index, source, target, file_id = _fixture(tmp_path)
    plan = file_relocate_preview(
        state_db,
        house_dir=house,
        file_id=file_id,
        target_directory=str(target),
        new_name="기존 작품 01권.epub",
    )
    assert plan["apply_available"] is True
    assert plan["projection_same"] is True
    assert plan["rename"] is True and plan["move"] is True

    result = apply_file_relocate(
        state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=index,
        file_id=file_id,
        target_directory=str(target),
        new_name="기존 작품 01권.epub",
        confirm_count=1,
        confirm_plan_sha256=plan["plan_sha256"],
    )

    destination = target / "기존 작품 01권.epub"
    assert destination.read_bytes() == b"synthetic epub bytes"
    assert not source.exists()
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        row = conn.execute(
            "SELECT canonical_path, active, source FROM files WHERE file_id = ?",
            (file_id,),
        ).fetchone()
        assert tuple(row) == (str(destination), 1, "house")
        operation = conn.execute(
            "SELECT action, state FROM operations WHERE operation_id = ?",
            (result["operation_id"],),
        ).fetchone()
        assert tuple(operation) == ("library_file_relocate", "committed")
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_analysis_changing_rename_routes_to_title_correction(tmp_path):
    state_db, house, _, _, _, target, file_id = _fixture(tmp_path)
    plan = file_relocate_preview(
        state_db,
        house_dir=house,
        file_id=file_id,
        target_directory=str(target),
        new_name="완전히 다른 작품 01권.epub",
    )
    assert plan["apply_available"] is False
    assert plan["route"] == "title_correction"
    assert "analysis_change_requires_title_correction" in plan["blocked_reasons"]


def test_file_relocate_refuses_destination_collision(tmp_path):
    state_db, house, _, _, _, target, file_id = _fixture(tmp_path)
    occupied = target / "기존 작품 01권.epub"
    occupied.write_bytes(b"occupied")
    plan = file_relocate_preview(
        state_db,
        house_dir=house,
        file_id=file_id,
        target_directory=str(target),
        new_name=occupied.name,
    )
    assert plan["apply_available"] is False
    assert "destination_occupied" in plan["blocked_reasons"]


def test_file_relocate_refuses_outside_house(tmp_path):
    state_db, house, _, _, _, _, file_id = _fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="house 내부"):
        file_relocate_preview(
            state_db,
            house_dir=house,
            file_id=file_id,
            target_directory=str(outside),
        )


def test_interrupted_file_relocate_rolls_back_to_original_path(tmp_path, monkeypatch):
    state_db, house, temp, index, source, target, file_id = _fixture(tmp_path)
    destination = target / "기존 작품 01권.epub"
    plan = file_relocate_preview(
        state_db,
        house_dir=house,
        file_id=file_id,
        target_directory=str(target),
        new_name=destination.name,
    )
    original_upsert = decision_store.upsert_file_analysis

    def fail_after_consume(*args, **kwargs):
        raise RuntimeError("injected relocation DB failure")

    monkeypatch.setattr(decision_store, "upsert_file_analysis", fail_after_consume)
    with pytest.raises(RuntimeError, match="injected relocation DB failure"):
        apply_file_relocate(
            state_db,
            house_dir=house,
            temp_dir=temp,
            index_path=index,
            file_id=file_id,
            target_directory=str(target),
            new_name=destination.name,
            confirm_count=1,
            confirm_plan_sha256=plan["plan_sha256"],
        )
    monkeypatch.setattr(decision_store, "upsert_file_analysis", original_upsert)

    assert destination.is_file()
    assert not source.exists()
    conn = decision_store.connect_state_db(state_db)
    try:
        operation = conn.execute(
            "SELECT operation_id, state FROM operations "
            "WHERE action = 'library_file_relocate'"
        ).fetchone()
        assert operation["state"] == "fs_done"
        assert decision_store.recover_interrupted_operation(
            conn, operation["operation_id"]
        ) == "rolled_back"
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
    assert source.is_file()
    assert not destination.exists()


def _work(state_db, title="관리 작품"):
    conn = decision_store.connect_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            return int(conn.execute(
                "INSERT INTO works(display_title) VALUES (?)", (title,)
            ).lastrowid)
    finally:
        conn.close()


def test_managed_folder_create_records_work_role_and_group(tmp_path):
    state_db, house, temp, _, _, _, _ = _fixture(tmp_path)
    work_id = _work(state_db)
    parent = house / "ㄱ"
    plan = managed_folder_preview(
        state_db,
        house_dir=house,
        work_bucket_id=work_id,
        parent_directory=str(parent),
        folder_name="관리 작품",
        role="primary",
    )
    assert plan["apply_available"] is True
    result = apply_managed_folder_create(
        state_db,
        house_dir=house,
        temp_dir=temp,
        work_bucket_id=work_id,
        parent_directory=str(parent),
        folder_name="관리 작품",
        role="primary",
        confirm_count=1,
        confirm_plan_sha256=plan["plan_sha256"],
    )
    destination = parent / "관리 작품"
    assert destination.is_dir()
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        folder = conn.execute(
            "SELECT work_bucket_id, canonical_path, role, state "
            "FROM work_folders WHERE folder_id = ?",
            (result["folder_id"],),
        ).fetchone()
        assert tuple(folder) == (work_id, str(destination), "primary", "active")
        group = conn.execute(
            "SELECT action, state FROM operation_groups WHERE group_id = ?",
            (result["group_id"],),
        ).fetchone()
        assert tuple(group) == ("managed_folder_create", "committed")
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()

    duplicate = managed_folder_preview(
        state_db,
        house_dir=house,
        work_bucket_id=work_id,
        parent_directory=str(parent),
        folder_name="다른 대표 폴더",
        role="primary",
    )
    assert duplicate["apply_available"] is False
    assert "work_primary_folder_exists" in duplicate["blocked_reasons"]


def test_managed_folder_create_failure_removes_only_owned_empty_folder(tmp_path, monkeypatch):
    state_db, house, temp, _, _, _, _ = _fixture(tmp_path)
    work_id = _work(state_db)
    parent = house / "ㄱ"
    destination = parent / "실패 폴더"
    plan = managed_folder_preview(
        state_db,
        house_dir=house,
        work_bucket_id=work_id,
        parent_directory=str(parent),
        folder_name=destination.name,
        role="edition",
    )
    original_transition = decision_store.transition_operation_group

    def fail_fs_done(conn, group_id, new_state, **kwargs):
        if new_state == "fs_done":
            raise RuntimeError("injected folder journal failure")
        return original_transition(conn, group_id, new_state, **kwargs)

    monkeypatch.setattr(decision_store, "transition_operation_group", fail_fs_done)
    with pytest.raises(RuntimeError, match="injected folder journal failure"):
        apply_managed_folder_create(
            state_db,
            house_dir=house,
            temp_dir=temp,
            work_bucket_id=work_id,
            parent_directory=str(parent),
            folder_name=destination.name,
            role="edition",
            confirm_count=1,
            confirm_plan_sha256=plan["plan_sha256"],
        )
    assert not destination.exists()
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute(
            "SELECT state FROM operation_groups ORDER BY group_id DESC LIMIT 1"
        ).fetchone()[0] == "rolled_back"
        assert conn.execute(
            "SELECT state FROM work_folders ORDER BY folder_id DESC LIMIT 1"
        ).fetchone()[0] == "failed"
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def _managed_folder_with_book(tmp_path):
    state_db, house, temp, index, source, _, file_id = _fixture(tmp_path)
    work_id = _work(state_db, "폴더 이동 작품")
    parent = house / "ㄱ"
    create = managed_folder_preview(
        state_db,
        house_dir=house,
        work_bucket_id=work_id,
        parent_directory=str(parent),
        folder_name="폴더 이동 작품",
        role="primary",
    )
    created = apply_managed_folder_create(
        state_db,
        house_dir=house,
        temp_dir=temp,
        work_bucket_id=work_id,
        parent_directory=str(parent),
        folder_name="폴더 이동 작품",
        role="primary",
        confirm_count=1,
        confirm_plan_sha256=create["plan_sha256"],
    )
    folder = Path(created["destination_path"])
    move = file_relocate_preview(
        state_db,
        house_dir=house,
        file_id=file_id,
        target_directory=str(folder),
        new_name=source.name,
    )
    apply_file_relocate(
        state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=index,
        file_id=file_id,
        target_directory=str(folder),
        new_name=source.name,
        confirm_count=1,
        confirm_plan_sha256=move["plan_sha256"],
    )
    (folder / "cover.jpg").write_bytes(b"cover")
    (folder / "source.zip").write_bytes(b"zip")
    (folder / "empty extras").mkdir()
    target_parent = house / "ㄴ"
    target_parent.mkdir()
    return state_db, house, temp, index, folder, target_parent, file_id, created["folder_id"]


def test_managed_folder_relocate_preserves_registered_auxiliary_and_empty_dirs(tmp_path):
    state_db, house, temp, index, source, target_parent, file_id, folder_id = (
        _managed_folder_with_book(tmp_path)
    )
    plan = managed_folder_relocate_preview(
        state_db,
        house_dir=house,
        folder_id=folder_id,
        target_parent=str(target_parent),
        new_name="이동 완료 작품",
    )
    assert plan["apply_available"] is True
    assert plan["registered_count"] == 1
    assert plan["auxiliary_count"] == 2
    destination = target_parent / "이동 완료 작품"
    result = apply_managed_folder_relocate(
        state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=index,
        folder_id=folder_id,
        target_parent=str(target_parent),
        new_name=destination.name,
        confirm_count=plan["item_count"],
        confirm_plan_sha256=plan["plan_sha256"],
    )
    assert not source.exists()
    assert (destination / "cover.jpg").read_bytes() == b"cover"
    assert (destination / "source.zip").read_bytes() == b"zip"
    assert (destination / "empty extras").is_dir()
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute(
            "SELECT canonical_path FROM files WHERE file_id = ?", (file_id,)
        ).fetchone()[0].startswith(str(destination) + "/")
        assert conn.execute(
            "SELECT canonical_path FROM work_folders WHERE folder_id = ?", (folder_id,)
        ).fetchone()[0] == str(destination)
        assert conn.execute(
            "SELECT state FROM operation_groups WHERE group_id = ?", (result["group_id"],)
        ).fetchone()[0] == "committed"
        assert conn.execute(
            "SELECT state FROM operations WHERE operation_id IN ({})".format(
                ",".join("?" for _ in result["operation_ids"])
            ),
            result["operation_ids"],
        ).fetchone()[0] == "committed"
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_managed_folder_relocate_db_failure_rolls_entire_tree_back(tmp_path, monkeypatch):
    state_db, house, temp, index, source, target_parent, _, folder_id = (
        _managed_folder_with_book(tmp_path)
    )
    destination = target_parent / "실패 이동"
    plan = managed_folder_relocate_preview(
        state_db,
        house_dir=house,
        folder_id=folder_id,
        target_parent=str(target_parent),
        new_name=destination.name,
    )
    original_transition = decision_store.transition_operation_group

    def fail_db_done(conn, group_id, new_state, **kwargs):
        if new_state == "db_done":
            raise RuntimeError("injected folder DB failure")
        return original_transition(conn, group_id, new_state, **kwargs)

    monkeypatch.setattr(decision_store, "transition_operation_group", fail_db_done)
    with pytest.raises(RuntimeError, match="injected folder DB failure"):
        apply_managed_folder_relocate(
            state_db,
            house_dir=house,
            temp_dir=temp,
            index_path=index,
            folder_id=folder_id,
            target_parent=str(target_parent),
            new_name=destination.name,
            confirm_count=plan["item_count"],
            confirm_plan_sha256=plan["plan_sha256"],
        )
    assert source.is_dir()
    assert not destination.exists()
    assert (source / "cover.jpg").is_file()
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute(
            "SELECT state FROM operation_groups "
            "WHERE action = 'managed_folder_relocate' ORDER BY group_id DESC LIMIT 1"
        ).fetchone()[0] == "rolled_back"
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_managed_folder_relocate_final_commit_failure_recovers_as_success(
    tmp_path, monkeypatch
):
    state_db, house, temp, index, source, target_parent, _, folder_id = (
        _managed_folder_with_book(tmp_path)
    )
    destination = target_parent / "커밋 복구"
    plan = managed_folder_relocate_preview(
        state_db,
        house_dir=house,
        folder_id=folder_id,
        target_parent=str(target_parent),
        new_name=destination.name,
    )
    original_transition = decision_store.transition_operation_group
    failed_once = False

    def fail_first_commit(conn, group_id, new_state, **kwargs):
        nonlocal failed_once
        if new_state == "committed" and not failed_once:
            failed_once = True
            raise RuntimeError("injected final commit failure")
        return original_transition(conn, group_id, new_state, **kwargs)

    monkeypatch.setattr(decision_store, "transition_operation_group", fail_first_commit)
    result = apply_managed_folder_relocate(
        state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=index,
        folder_id=folder_id,
        target_parent=str(target_parent),
        new_name=destination.name,
        confirm_count=plan["item_count"],
        confirm_plan_sha256=plan["plan_sha256"],
    )
    assert "injected final commit failure" in result["recovered_after_error"]
    assert destination.is_dir()
    assert not source.exists()
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute(
            "SELECT state FROM operation_groups WHERE group_id = ?",
            (result["group_id"],),
        ).fetchone()[0] == "committed"
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_existing_folder_can_be_adopted_without_moving_files(tmp_path):
    state_db, house, temp, _, source, _, _ = _fixture(tmp_path)
    folder = source.parent
    (folder / "cover.jpg").write_bytes(b"cover")
    work_id = _work(state_db, "기존 폴더 작품")
    before = sorted(str(path.relative_to(folder)) for path in folder.rglob("*"))
    plan = managed_folder_adopt_preview(
        state_db,
        house_dir=house,
        folder_path=str(folder),
        work_bucket_id=work_id,
        role="primary",
    )
    assert plan["apply_available"] is True
    assert plan["registered_count"] == 1
    assert plan["auxiliary_count"] == 1
    result = apply_managed_folder_adopt(
        state_db,
        house_dir=house,
        temp_dir=temp,
        folder_path=str(folder),
        work_bucket_id=work_id,
        role="primary",
        confirm_count=plan["item_count"],
        confirm_plan_sha256=plan["plan_sha256"],
    )
    assert sorted(str(path.relative_to(folder)) for path in folder.rglob("*")) == before
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute(
            "SELECT canonical_path, role, state FROM work_folders WHERE folder_id = ?",
            (result["folder_id"],),
        ).fetchone()[:] == (str(folder), "primary", "active")
        assert conn.execute(
            "SELECT action, state FROM operation_groups WHERE group_id = ?",
            (result["group_id"],),
        ).fetchone()[:] == ("managed_folder_adopt", "committed")
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_folder_quarantine_moves_registered_and_auxiliary_as_one_group(tmp_path):
    state_db, house, temp, index, source, _, file_id = _fixture(tmp_path)
    folder = source.parent
    conn = decision_store.connect_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE files SET current_fingerprint_id = NULL WHERE file_id = ?",
                (file_id,),
            )
    finally:
        conn.close()
    (folder / "cover.jpg").write_bytes(b"cover")
    (folder / "extras").mkdir()
    (folder / "extras" / "map.zip").write_bytes(b"zip")
    plan = folder_quarantine_preview(
        state_db, house_dir=house, temp_dir=temp, folder_path=str(folder)
    )
    assert plan["apply_available"] is True
    assert plan["registered_count"] == 1
    assert plan["auxiliary_count"] == 2
    assert next(item for item in plan["items"] if item["registered"])["fingerprint_id"] is None
    result = apply_folder_quarantine(
        state_db, house_dir=house, temp_dir=temp, index_path=index,
        folder_path=str(folder), confirm_count=plan["item_count"],
        confirm_plan_sha256=plan["plan_sha256"],
    )
    destination = Path(result["destination_path"])
    assert not folder.exists()
    assert (destination / source.name).read_bytes() == b"synthetic epub bytes"
    assert (destination / "cover.jpg").read_bytes() == b"cover"
    assert (destination / "extras" / "map.zip").read_bytes() == b"zip"
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        file_row = conn.execute(
            "SELECT active, source, canonical_path FROM files WHERE file_id = ?", (file_id,)
        ).fetchone()
        assert file_row[0:2] == (0, "quarantine")
        assert file_row[2] == str(destination / source.name)
        assert conn.execute(
            "SELECT action, state FROM operation_groups WHERE group_id = ?",
            (result["group_id"],),
        ).fetchone()[:] == ("user_folder_quarantine", "committed")
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_folder_quarantine_db_failure_rolls_tree_back(tmp_path, monkeypatch):
    state_db, house, temp, index, source, _, _ = _fixture(tmp_path)
    folder = source.parent
    plan = folder_quarantine_preview(
        state_db, house_dir=house, temp_dir=temp, folder_path=str(folder)
    )
    original = decision_store.transition_operation_group

    def fail_db_done(conn, group_id, new_state, **kwargs):
        if new_state == "db_done":
            raise RuntimeError("injected quarantine db failure")
        return original(conn, group_id, new_state, **kwargs)

    monkeypatch.setattr(decision_store, "transition_operation_group", fail_db_done)
    with pytest.raises(RuntimeError, match="injected quarantine db failure"):
        apply_folder_quarantine(
            state_db, house_dir=house, temp_dir=temp, index_path=index,
            folder_path=str(folder), confirm_count=plan["item_count"],
            confirm_plan_sha256=plan["plan_sha256"],
        )
    assert folder.is_dir() and source.is_file()
    assert not Path(plan["destination_path"]).exists()
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute(
            "SELECT state FROM operation_groups WHERE action = 'user_folder_quarantine' "
            "ORDER BY group_id DESC LIMIT 1"
        ).fetchone()[0] == "rolled_back"
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_managed_folder_adopt_final_commit_failure_recovers_as_success(
    tmp_path, monkeypatch
):
    state_db, house, temp, _, source, _, _ = _fixture(tmp_path)
    folder = source.parent
    work_id = _work(state_db, "등록 커밋 복구")
    plan = managed_folder_adopt_preview(
        state_db,
        house_dir=house,
        folder_path=str(folder),
        work_bucket_id=work_id,
        role="primary",
    )
    original_transition = decision_store.transition_operation_group
    failed_once = False

    def fail_first_commit(conn, group_id, new_state, **kwargs):
        nonlocal failed_once
        if new_state == "committed" and not failed_once:
            failed_once = True
            raise RuntimeError("injected adopt commit failure")
        return original_transition(conn, group_id, new_state, **kwargs)

    monkeypatch.setattr(decision_store, "transition_operation_group", fail_first_commit)
    result = apply_managed_folder_adopt(
        state_db,
        house_dir=house,
        temp_dir=temp,
        folder_path=str(folder),
        work_bucket_id=work_id,
        role="primary",
        confirm_count=plan["item_count"],
        confirm_plan_sha256=plan["plan_sha256"],
    )
    assert "injected adopt commit failure" in result["recovered_after_error"]
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute(
            "SELECT state FROM operation_groups WHERE group_id = ?",
            (result["group_id"],),
        ).fetchone()[0] == "committed"
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
