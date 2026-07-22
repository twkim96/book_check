from pathlib import Path

import decision_store
from dedup_mutations import _ensure_intake_fingerprint, _file_state
from library_work_management import (
    alias_preview,
    alias_retire_preview,
    apply_alias,
    apply_alias_retire,
    apply_representative,
    apply_work_merge,
    apply_work_split,
    representative_preview,
    resolve_work_route,
    work_merge_preview,
    work_split_preview,
    work_search,
)


def _fixture(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    conn = decision_store.initialize_state_db(state_db)
    conn.close()
    return state_db, house, temp


def _work(conn, title):
    return int(conn.execute(
        "INSERT INTO works(display_title) VALUES (?)", (title,)
    ).lastrowid)


def _variant(conn, work_id, kind="base", label=None):
    return int(conn.execute(
        "INSERT INTO variants(work_bucket_id, variant_kind, label) VALUES (?, ?, ?)",
        (work_id, kind, label),
    ).lastrowid)


def _managed_file(conn, path: Path, variant_id: int, *, representative=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"fixture:{path.name}", encoding="utf-8")
    with decision_store.transaction(conn):
        row = decision_store.reconcile_file_metadata(conn, path, source="house")
    _ensure_intake_fingerprint(conn, _file_state(conn, row["file_id"]))
    with decision_store.transaction(conn):
        decision_store.upsert_file_analysis(conn, row["file_id"], path)
        conn.execute(
            "UPDATE files SET variant_id = ?, assignment_state = 'managed', "
            "assignment_origin = 'human_decision', protected = 1 WHERE file_id = ?",
            (variant_id, row["file_id"]),
        )
        if representative:
            conn.execute(
                "INSERT INTO representatives(variant_id, file_id) VALUES (?, ?)",
                (variant_id, row["file_id"]),
            )
    return row["file_id"]


def _folder(conn, path: Path, work_id: int, role="primary"):
    path.mkdir(parents=True, exist_ok=True)
    info = path.stat()
    with decision_store.transaction(conn):
        return int(conn.execute(
            """
            INSERT INTO work_folders(
                work_bucket_id, canonical_path, role, state, dev, ino, ctime_ns
            ) VALUES (?, ?, ?, 'active', ?, ?, ?)
            """,
            (work_id, str(path), role, info.st_dev, info.st_ino, info.st_ctime_ns),
        ).lastrowid)


def test_work_search_finds_title_id_and_alias(tmp_path):
    state_db, house, _ = _fixture(tmp_path)
    conn = decision_store.connect_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            work_id = _work(conn, "Re 제로부터 시작하는 이세계 생활")
            conn.execute(
                "INSERT INTO work_aliases(alias_kind, alias_key, alias_display, work_bucket_id, origin) "
                "VALUES ('folder_name', '리제로', '리제로', ?, 'human_decision')",
                (work_id,),
            )
        _folder(conn, house / "영어" / "Re 제로", work_id)
    finally:
        conn.close()
    by_title = work_search(state_db, search="제로부터", limit=10)
    by_alias = work_search(state_db, search="리제로", limit=10)
    by_id = work_search(state_db, search=str(work_id), limit=10)
    assert [item["work_bucket_id"] for item in by_title["items"]] == [work_id]
    assert [item["work_bucket_id"] for item in by_alias["items"]] == [work_id]
    assert by_id["items"][0]["work_bucket_id"] == work_id
    assert by_id["items"][0]["active_folder_count"] == 1


def test_alias_route_and_explicit_replacement(tmp_path):
    state_db, house, temp = _fixture(tmp_path)
    conn = decision_store.connect_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            first_work = _work(conn, "첫 작품")
            second_work = _work(conn, "둘째 작품")
        first_folder = _folder(conn, house / "ㄱ" / "첫 작품", first_work)
        second_folder = _folder(conn, house / "ㄷ" / "둘째 작품", second_work)
    finally:
        conn.close()

    plan = alias_preview(
        state_db,
        alias_kind="core_title",
        alias_value="Re:제로부터 시작하는 이세계 생활",
        work_bucket_id=first_work,
        preferred_folder_id=first_folder,
    )
    created = apply_alias(
        state_db,
        house_dir=house,
        temp_dir=temp,
        alias_kind="core_title",
        alias_value="Re:제로부터 시작하는 이세계 생활",
        work_bucket_id=first_work,
        preferred_folder_id=first_folder,
        replace_alias_id=None,
        confirm_count=1,
        confirm_plan_sha256=plan["plan_sha256"],
    )
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        route = resolve_work_route(
            conn, core_title="Re:제로부터 시작하는 이세계 생활"
        )
        assert route["status"] == "target"
        assert route["target_folder"] == str(house / "ㄱ" / "첫 작품")
    finally:
        conn.close()

    blocked = alias_preview(
        state_db,
        alias_kind="core_title",
        alias_value="Re:제로부터 시작하는 이세계 생활",
        work_bucket_id=second_work,
        preferred_folder_id=second_folder,
    )
    assert blocked["apply_available"] is False
    assert "alias_conflict_requires_explicit_replacement" in blocked["blocked_reasons"]
    replacement = alias_preview(
        state_db,
        alias_kind="core_title",
        alias_value="Re:제로부터 시작하는 이세계 생활",
        work_bucket_id=second_work,
        preferred_folder_id=second_folder,
        replace_alias_id=created["alias_id"],
    )
    replaced = apply_alias(
        state_db,
        house_dir=house,
        temp_dir=temp,
        alias_kind="core_title",
        alias_value="Re:제로부터 시작하는 이세계 생활",
        work_bucket_id=second_work,
        preferred_folder_id=second_folder,
        replace_alias_id=created["alias_id"],
        confirm_count=1,
        confirm_plan_sha256=replacement["plan_sha256"],
    )
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        rows = conn.execute(
            "SELECT alias_id, active, supersedes_alias_id FROM work_aliases ORDER BY alias_id"
        ).fetchall()
        assert rows[0]["active"] == 0
        assert rows[1]["alias_id"] == replaced["alias_id"]
        assert rows[1]["supersedes_alias_id"] == created["alias_id"]
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()

    retire_plan = alias_retire_preview(state_db, alias_id=replaced["alias_id"])
    retired = apply_alias_retire(
        state_db,
        house_dir=house,
        temp_dir=temp,
        alias_id=replaced["alias_id"],
        confirm_count=1,
        confirm_plan_sha256=retire_plan["plan_sha256"],
    )
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute(
            "SELECT active FROM work_aliases WHERE alias_id = ?",
            (retired["alias_id"],),
        ).fetchone()[0] == 0
        assert resolve_work_route(
            conn, core_title="Re:제로부터 시작하는 이세계 생활"
        )["status"] == "no_alias"
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_work_merge_moves_relations_and_demotes_second_primary(tmp_path):
    state_db, house, temp = _fixture(tmp_path)
    conn = decision_store.connect_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            source_work = _work(conn, "Re 제로")
            target_work = _work(conn, "Re:제로")
            source_variant = _variant(conn, source_work)
            target_variant = _variant(conn, target_work)
        source_folder = _folder(conn, house / "ㄹ" / "Re 제로", source_work)
        target_folder = _folder(conn, house / "ㄹ" / "Re:제로", target_work)
        _managed_file(conn, house / "ㄹ" / "Re 제로" / "Re 제로 1권.epub", source_variant)
        _managed_file(conn, house / "ㄹ" / "Re:제로" / "Re:제로 2권.epub", target_variant)
    finally:
        conn.close()
    alias = alias_preview(
        state_db,
        alias_kind="core_title",
        alias_value="Re 제로",
        work_bucket_id=source_work,
        preferred_folder_id=source_folder,
    )
    apply_alias(
        state_db,
        house_dir=house,
        temp_dir=temp,
        alias_kind="core_title",
        alias_value="Re 제로",
        work_bucket_id=source_work,
        preferred_folder_id=source_folder,
        replace_alias_id=None,
        confirm_count=1,
        confirm_plan_sha256=alias["plan_sha256"],
    )
    plan = work_merge_preview(
        state_db, source_work_id=source_work, target_work_id=target_work
    )
    assert plan["demoted_folder_ids"] == [source_folder]
    result = apply_work_merge(
        state_db,
        house_dir=house,
        temp_dir=temp,
        source_work_id=source_work,
        target_work_id=target_work,
        confirm_count=plan["item_count"],
        confirm_plan_sha256=plan["plan_sha256"],
    )
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute(
            "SELECT status FROM works WHERE work_bucket_id = ?", (source_work,)
        ).fetchone()[0] == "retired"
        assert conn.execute(
            "SELECT work_bucket_id FROM variants WHERE variant_id = ?", (source_variant,)
        ).fetchone()[0] == target_work
        assert conn.execute(
            "SELECT role, work_bucket_id FROM work_folders WHERE folder_id = ?",
            (source_folder,),
        ).fetchone()[:] == ("edition", target_work)
        assert conn.execute(
            "SELECT work_bucket_id FROM work_aliases WHERE active = 1"
        ).fetchone()[0] == target_work
        assert conn.execute(
            "SELECT action FROM work_management_events WHERE event_id = ?",
            (result["event_id"],),
        ).fetchone()[0] == "work_merge"
        assert decision_store.doctor_issues(conn) == []
        assert target_folder != source_folder
    finally:
        conn.close()


def test_work_split_moves_selected_variant_folder_and_alias(tmp_path):
    state_db, house, temp = _fixture(tmp_path)
    conn = decision_store.connect_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            source_work = _work(conn, "갈라진 작품")
            first_variant = _variant(conn, source_work, "base")
            second_variant = _variant(conn, source_work, "revision")
        first_folder = _folder(conn, house / "ㄱ" / "갈라진 작품", source_work)
        second_folder = _folder(
            conn, house / "ㄱ" / "갈라진 작품 개정판", source_work, role="edition"
        )
        _managed_file(conn, house / "ㄱ" / "갈라진 작품" / "갈라진 작품 1권.epub", first_variant)
        _managed_file(
            conn,
            house / "ㄱ" / "갈라진 작품 개정판" / "갈라진 작품 개정판 1권.epub",
            second_variant,
        )
    finally:
        conn.close()
    alias = alias_preview(
        state_db,
        alias_kind="core_title",
        alias_value="갈라진 작품 개정판",
        work_bucket_id=source_work,
        preferred_folder_id=second_folder,
    )
    alias_result = apply_alias(
        state_db,
        house_dir=house,
        temp_dir=temp,
        alias_kind="core_title",
        alias_value="갈라진 작품 개정판",
        work_bucket_id=source_work,
        preferred_folder_id=second_folder,
        replace_alias_id=None,
        confirm_count=1,
        confirm_plan_sha256=alias["plan_sha256"],
    )
    plan = work_split_preview(
        state_db,
        source_work_id=source_work,
        variant_ids=[second_variant],
        display_title="갈라진 작품 개정판",
        folder_ids=[second_folder],
        alias_ids=[alias_result["alias_id"]],
    )
    assert plan["apply_available"] is True
    result = apply_work_split(
        state_db,
        house_dir=house,
        temp_dir=temp,
        source_work_id=source_work,
        variant_ids=[second_variant],
        display_title="갈라진 작품 개정판",
        folder_ids=[second_folder],
        alias_ids=[alias_result["alias_id"]],
        confirm_count=plan["item_count"],
        confirm_plan_sha256=plan["plan_sha256"],
    )
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute(
            "SELECT work_bucket_id FROM variants WHERE variant_id = ?", (first_variant,)
        ).fetchone()[0] == source_work
        assert conn.execute(
            "SELECT work_bucket_id FROM variants WHERE variant_id = ?", (second_variant,)
        ).fetchone()[0] == result["new_work_id"]
        assert conn.execute(
            "SELECT work_bucket_id, role FROM work_folders WHERE folder_id = ?",
            (second_folder,),
        ).fetchone()[:] == (result["new_work_id"], "edition")
        assert conn.execute(
            "SELECT work_bucket_id, preferred_folder_id FROM work_aliases WHERE alias_id = ?",
            (alias_result["alias_id"],),
        ).fetchone()[:] == (result["new_work_id"], second_folder)
        assert decision_store.doctor_issues(conn) == []
        assert first_folder != second_folder
    finally:
        conn.close()


def test_work_split_blocks_variant_left_inside_source_managed_folder(tmp_path):
    state_db, house, _temp = _fixture(tmp_path)
    conn = decision_store.connect_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            source_work = _work(conn, "혼합 폴더 작품")
            first_variant = _variant(conn, source_work, "base")
            second_variant = _variant(conn, source_work, "revision")
        folder = _folder(conn, house / "ㅎ" / "혼합 폴더 작품", source_work)
        _managed_file(conn, house / "ㅎ" / "혼합 폴더 작품" / "1권.epub", first_variant)
        _managed_file(conn, house / "ㅎ" / "혼합 폴더 작품" / "2권.epub", second_variant)
    finally:
        conn.close()

    without_folder = work_split_preview(
        state_db,
        source_work_id=source_work,
        variant_ids=[first_variant],
        display_title="분리 작품",
    )
    assert without_folder["apply_available"] is False
    assert f"selected_variants_require_folder:{folder}" in without_folder["blocked_reasons"]
    assert f"folder_contains_unselected_variants:{folder}" in without_folder["blocked_reasons"]

    with_mixed_folder = work_split_preview(
        state_db,
        source_work_id=source_work,
        variant_ids=[first_variant],
        display_title="분리 작품",
        folder_ids=[folder],
    )
    assert with_mixed_folder["apply_available"] is False
    assert f"selected_variants_require_folder:{folder}" not in with_mixed_folder["blocked_reasons"]
    assert f"folder_contains_unselected_variants:{folder}" in with_mixed_folder["blocked_reasons"]


def test_representative_replacement_keeps_variant_relationship(tmp_path):
    state_db, house, temp = _fixture(tmp_path)
    conn = decision_store.connect_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            work_id = _work(conn, "대표 교체")
            variant_id = _variant(conn, work_id)
        first = _managed_file(conn, house / "ㄷ" / "대표 교체 1권.epub", variant_id)
        second = _managed_file(
            conn,
            house / "ㄷ" / "대표 교체 2권.epub",
            variant_id,
            representative=False,
        )
    finally:
        conn.close()
    plan = representative_preview(state_db, variant_id=variant_id, file_id=second)
    result = apply_representative(
        state_db,
        house_dir=house,
        temp_dir=temp,
        variant_id=variant_id,
        file_id=second,
        confirm_count=1,
        confirm_plan_sha256=plan["plan_sha256"],
    )
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute(
            "SELECT file_id FROM representatives WHERE variant_id = ?", (variant_id,)
        ).fetchone()[0] == second
        assert result["file_id"] != first
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_resolve_work_route_blocks_aliases_pointing_to_different_works(tmp_path):
    state_db, house, temp = _fixture(tmp_path)
    conn = decision_store.connect_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            core_work = _work(conn, "코어 작품")
            readable_work = _work(conn, "표시 작품")
        core_folder = _folder(conn, house / "ㅋ" / "코어 작품", core_work)
        readable_folder = _folder(conn, house / "ㅍ" / "표시 작품", readable_work)
    finally:
        conn.close()

    core_plan = alias_preview(
        state_db,
        alias_kind="core_title",
        alias_value="충돌작품",
        work_bucket_id=core_work,
        preferred_folder_id=core_folder,
    )
    apply_alias(
        state_db,
        house_dir=house,
        temp_dir=temp,
        alias_kind="core_title",
        alias_value="충돌작품",
        work_bucket_id=core_work,
        preferred_folder_id=core_folder,
        replace_alias_id=None,
        confirm_count=1,
        confirm_plan_sha256=core_plan["plan_sha256"],
    )
    readable_plan = alias_preview(
        state_db,
        alias_kind="readable_title",
        alias_value="충돌 표시 제목",
        work_bucket_id=readable_work,
        preferred_folder_id=readable_folder,
    )
    apply_alias(
        state_db,
        house_dir=house,
        temp_dir=temp,
        alias_kind="readable_title",
        alias_value="충돌 표시 제목",
        work_bucket_id=readable_work,
        preferred_folder_id=readable_folder,
        replace_alias_id=None,
        confirm_count=1,
        confirm_plan_sha256=readable_plan["plan_sha256"],
    )

    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        route = resolve_work_route(
            conn,
            core_title="충돌작품",
            readable_title="충돌 표시 제목",
        )
    finally:
        conn.close()
    assert route["status"] == "route_conflict"
    assert route["matched"] is False
    assert route["work_bucket_ids"] == sorted([core_work, readable_work])
