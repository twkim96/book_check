from io import StringIO
from pathlib import Path

import decision_store
import volume_review
from folderling import move_to_house, retarget_owned_recent_link
from library_work_management import alias_preview, apply_alias
from volume_group_mutations import (
    classify_folderling_volume_target,
    ensure_volume_fingerprints,
    link_volume_relationships,
    suggest_folderling_volume_target,
)
from volume_review import apply_auto_ready_volume_groups, list_volume_cases


def _add(conn, path, source):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(path.name, encoding="utf-8")
    with decision_store.transaction(conn):
        return decision_store.reconcile_file_metadata(conn, path, source=source)


def _fixture(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        existing = [
            _add(conn, house / "ㅂ" / "별빛 연대기" / f"별빛 연대기 {number}권.txt", "house")
            for number in (1, 2)
        ]
        incoming = _add(conn, temp / "별빛 연대기 3권.txt", "temp")
    finally:
        conn.close()
    return state_db, house, temp, existing, incoming


def _approve(state_db, house, temp):
    conn = decision_store.connect_state_db(state_db)
    try:
        backup = decision_store.backup_state_db(
            conn, state_db.parent / "backups" / "before-folderling-volume.sqlite3"
        )
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()
    return decision_store.prepare_actual_run(state_db, house, temp)[0]


def test_folderling_auto_adds_non_overlapping_volume_to_existing_group(tmp_path):
    state_db, house, temp, existing, incoming = _fixture(tmp_path)
    run_id = _approve(state_db, house, temp)
    log = StringIO()
    destination = move_to_house(
        str(temp / "별빛 연대기 3권.txt"),
        str(house),
        str(house / "_최근"),
        "별빛 연대기 3권.txt",
        log,
        "",
        state_db_path=str(state_db),
        run_id=run_id,
    )
    conn = decision_store.connect_state_db(state_db)
    try:
        decision_store.finish_actual_run(conn, run_id, success=True)
        rows = conn.execute(
            """
            SELECT f.file_id, f.canonical_path, f.assignment_state,
                   f.assignment_origin, f.variant_id, v.work_bucket_id
            FROM files AS f LEFT JOIN variants AS v ON v.variant_id = f.variant_id
            WHERE f.active = 1 AND f.source = 'house'
            ORDER BY f.canonical_path
            """
        ).fetchall()
        assert len(rows) == 3
        assert len({row["work_bucket_id"] for row in rows}) == 1
        assert len({row["variant_id"] for row in rows}) == 3
        assert all(row["assignment_state"] == "managed" for row in rows)
        assert all(row["assignment_origin"] == "strong_match" for row in rows)
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
    assert Path(destination).parent == house / "ㅂ" / "별빛 연대기"
    assert "volume-auto" in log.getvalue()
    assert all(row["file_id"] for row in existing + [incoming])


def test_full_auto_pass_groups_loose_existing_and_same_run_intake(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        _add(conn, house / "ㅍ" / "판타지 소설 1권.txt", "house")
        _add(conn, temp / "판타지 소설 2권.epub", "temp")
    finally:
        conn.close()

    run_id = _approve(state_db, house, temp)
    loose_destination = move_to_house(
        str(temp / "판타지 소설 2권.epub"),
        str(house),
        str(house / "_최근"),
        "판타지 소설 2권.epub",
        StringIO(),
        "",
        state_db_path=str(state_db),
        run_id=run_id,
    )
    assert Path(loose_destination).parent == house / "ㅍ"

    result = apply_auto_ready_volume_groups(
        state_db,
        house_dir=house,
        temp_dir=temp,
        run_id=run_id,
    )
    conn = decision_store.connect_state_db(state_db)
    try:
        decision_store.finish_actual_run(conn, run_id, success=True)
        rows = conn.execute(
            "SELECT canonical_path, assignment_origin, variant_id "
            "FROM files WHERE active = 1 ORDER BY canonical_path"
        ).fetchall()
        assert {Path(row["canonical_path"]).parent for row in rows} == {
            house / "ㅍ" / "판타지 소설"
        }
        assert all(row["assignment_origin"] == "strong_match" for row in rows)
        assert all(row["variant_id"] is not None for row in rows)
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()

    assert result["candidate_count"] == result["applied_count"] == 1
    assert result["moved_count"] == 2
    assert result["remaining_summary"]["auto_ready"] == 0
    assert result["remaining_summary"]["already_grouped"] == 1


def test_episode_split_backlog_is_auto_grouped_but_side_only_is_not(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        for name in ("연속 작품 1-100화.txt", "연속 작품 105-200화.txt"):
            _add(conn, house / "ㅇ" / name, "house")
        for name in ("외전 작품 외전 1.txt", "외전 작품 외전 2.epub"):
            _add(conn, house / "ㅇ" / name, "house")
    finally:
        conn.close()

    before = list_volume_cases(state_db, house_dir=house, limit=20)
    assert before["summary"]["auto_ready"] == 1
    assert before["summary"]["review_required"] == 1
    run_id = _approve(state_db, house, temp)
    result = apply_auto_ready_volume_groups(
        state_db,
        house_dir=house,
        temp_dir=temp,
        run_id=run_id,
    )
    conn = decision_store.connect_state_db(state_db)
    try:
        decision_store.finish_actual_run(conn, run_id, success=True)
    finally:
        conn.close()

    assert (house / "ㅇ" / "연속 작품").is_dir()
    assert not (house / "ㅇ" / "외전 작품").exists()
    assert result["applied_count"] == 1
    assert result["remaining_summary"]["review_required"] == 1


def test_auto_volume_pass_with_no_candidates_keeps_warm_analysis(
    tmp_path, monkeypatch,
):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        _add(conn, house / "ㄷ" / "단독 작품 1권.txt", "house")
    finally:
        conn.close()
    run_id = _approve(state_db, house, temp)
    analyze_calls = []
    original_analyze = volume_review.analyze_volume_cases

    def tracked_analyze(*args, **kwargs):
        analyze_calls.append(1)
        return original_analyze(*args, **kwargs)

    monkeypatch.setattr(volume_review, "analyze_volume_cases", tracked_analyze)
    monkeypatch.setattr(
        volume_review,
        "invalidate_volume_case_cache",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("0-candidate pass must keep the case cache")
        ),
    )

    result = volume_review.apply_auto_ready_volume_groups(
        state_db,
        house_dir=house,
        temp_dir=temp,
        run_id=run_id,
    )

    assert result["candidate_count"] == 0
    assert result["applied_count"] == 0
    assert len(analyze_calls) == 1
    conn = decision_store.connect_state_db(state_db)
    try:
        decision_store.finish_actual_run(conn, run_id, success=True)
    finally:
        conn.close()


def test_recent_link_retarget_requires_exact_old_destination(tmp_path):
    recent = tmp_path / "recent"
    recent.mkdir()
    source = tmp_path / "house" / "작품 1권.txt"
    destination = tmp_path / "house" / "작품" / source.name
    foreign = tmp_path / "house" / "다른 곳" / source.name
    source.parent.mkdir()
    destination.parent.mkdir()
    foreign.parent.mkdir()
    source.write_text("source", encoding="utf-8")
    destination.write_text("destination", encoding="utf-8")
    foreign.write_text("foreign", encoding="utf-8")
    link = recent / source.name
    link.symlink_to(source)

    assert retarget_owned_recent_link(recent, source, destination) == "retargeted"
    assert link.resolve() == destination.resolve()
    assert retarget_owned_recent_link(recent, source, foreign) == "preserved"
    assert link.resolve() == destination.resolve()
    link.unlink()
    assert retarget_owned_recent_link(recent, source, destination) == "missing"


def test_human_alias_route_precedes_volume_and_links_incoming_file(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    target = house / "ㄹ" / "Re 제로 통합"
    target.mkdir(parents=True)
    incoming_path = temp / "Re 제로부터 시작하는 이세계 생활 11권.epub"
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        incoming = _add(conn, incoming_path, "temp")
        with decision_store.transaction(conn):
            decision_store.upsert_file_analysis(conn, incoming["file_id"], incoming_path)
            work_id = int(conn.execute(
                "INSERT INTO works(display_title) VALUES ('Re 제로 통합')"
            ).lastrowid)
            info = target.stat()
            folder_id = int(conn.execute(
                """
                INSERT INTO work_folders(
                    work_bucket_id, canonical_path, role, state, dev, ino, ctime_ns
                ) VALUES (?, ?, 'primary', 'active', ?, ?, ?)
                """,
                (work_id, str(target), info.st_dev, info.st_ino, info.st_ctime_ns),
            ).lastrowid)
    finally:
        conn.close()
    plan = alias_preview(
        state_db,
        alias_kind="core_title",
        alias_value="Re 제로부터 시작하는 이세계 생활",
        work_bucket_id=work_id,
        preferred_folder_id=folder_id,
    )
    apply_alias(
        state_db,
        house_dir=house,
        temp_dir=temp,
        alias_kind="core_title",
        alias_value="Re 제로부터 시작하는 이세계 생활",
        work_bucket_id=work_id,
        preferred_folder_id=folder_id,
        replace_alias_id=None,
        confirm_count=1,
        confirm_plan_sha256=plan["plan_sha256"],
    )
    run_id = _approve(state_db, house, temp)
    log = StringIO()
    destination = move_to_house(
        str(incoming_path),
        str(house),
        str(house / "_최근"),
        incoming_path.name,
        log,
        "",
        state_db_path=str(state_db),
        run_id=run_id,
    )
    conn = decision_store.connect_state_db(state_db)
    try:
        decision_store.finish_actual_run(conn, run_id, success=True)
        row = conn.execute(
            """
            SELECT f.assignment_state, f.assignment_origin, v.work_bucket_id
            FROM files AS f JOIN variants AS v ON v.variant_id = f.variant_id
            WHERE f.file_id = ?
            """,
            (incoming["file_id"],),
        ).fetchone()
        assert row[:] == ("managed", "human_decision", work_id)
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
    assert Path(destination).parent == target
    assert "work-route" in log.getvalue()


def test_folderling_auto_fills_gap_and_appends_latest_to_existing_group(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        existing = [
            _add(
                conn,
                house / "ㄹ" / "Re 제로부터 시작하는 이세계 생활" /
                f"Re 제로부터 시작하는 이세계 생활 {number}권.epub",
                "house",
            )
            for number in (7, 9)
        ]
        incoming = [
            _add(
                conn,
                temp / f"Re 제로부터 시작하는 이세계 생활 {number}권.epub",
                "temp",
            )
            for number in (8, 10)
        ]
    finally:
        conn.close()

    run_id = _approve(state_db, house, temp)
    destinations = []
    for number in (8, 10):
        destinations.append(
            move_to_house(
                str(temp / f"Re 제로부터 시작하는 이세계 생활 {number}권.epub"),
                str(house),
                str(house / "_최근"),
                f"Re 제로부터 시작하는 이세계 생활 {number}권.epub",
                StringIO(),
                "",
                state_db_path=str(state_db),
                run_id=run_id,
            )
        )

    target = house / "ㄹ" / "Re 제로부터 시작하는 이세계 생활"
    conn = decision_store.connect_state_db(state_db)
    try:
        decision_store.finish_actual_run(conn, run_id, success=True)
        rows = conn.execute(
            """
            SELECT f.variant_id, v.work_bucket_id
            FROM files AS f JOIN variants AS v ON v.variant_id = f.variant_id
            WHERE f.active = 1 AND f.source = 'house'
            ORDER BY f.canonical_path
            """
        ).fetchall()
        assert len(rows) == 4
        assert len({row["variant_id"] for row in rows}) == 4
        assert len({row["work_bucket_id"] for row in rows}) == 1
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()

    assert {Path(path).parent for path in destinations} == {target}
    assert all(row["file_id"] for row in existing + incoming)


def test_folderling_volume_target_rejects_duplicate_coordinate(tmp_path):
    state_db, house, temp, _, _ = _fixture(tmp_path)
    conn = decision_store.connect_state_db(state_db)
    try:
        duplicate = _add(conn, temp / "별빛 연대기 2권.epub", "temp")
        assert suggest_folderling_volume_target(
            conn,
            source_file_id=duplicate["file_id"],
            house_root=house,
        ) is None
    finally:
        conn.close()


def test_folderling_volume_target_allows_missing_author_for_authored_work(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        _add(
            conn,
            house / "ㅂ" / "별빛 연대기" / "별빛 연대기 1권 [한작가].epub",
            "house",
        )
        incoming = _add(conn, temp / "별빛 연대기 2권.epub", "temp")
        decision = classify_folderling_volume_target(
            conn,
            source_file_id=incoming["file_id"],
            house_root=house,
        )
    finally:
        conn.close()

    assert decision["status"] == "target"
    assert Path(decision["target_folder"]) == house / "ㅂ" / "별빛 연대기"


def test_folderling_volume_target_preserves_current_stored_author_conflict(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        existing = _add(
            conn,
            house / "ㅂ" / "별빛 연대기" / "별빛 연대기 1권.epub",
            "house",
        )
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE file_analysis SET author = ? WHERE file_id = ?",
                ("저장작가", existing["file_id"]),
            )
        incoming = _add(
            conn, temp / "별빛 연대기 2권 [다른작가].epub", "temp"
        )

        decision = classify_folderling_volume_target(
            conn,
            source_file_id=incoming["file_id"],
            house_root=house,
        )
    finally:
        conn.close()

    assert decision == {"status": "no_target", "reason": "author_conflict"}


def test_folderling_volume_target_reparses_only_stale_stored_author(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        existing = _add(
            conn,
            house / "ㅂ" / "별빛 연대기" / "별빛 연대기 1권.epub",
            "house",
        )
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE file_analysis SET author = ?, analyzed_mtime_ns = analyzed_mtime_ns - 1 "
                "WHERE file_id = ?",
                ("과거작가", existing["file_id"]),
            )
        incoming = _add(
            conn, temp / "별빛 연대기 2권 [새작가].epub", "temp"
        )

        decision = classify_folderling_volume_target(
            conn,
            source_file_id=incoming["file_id"],
            house_root=house,
        )
    finally:
        conn.close()

    assert decision["status"] == "target"
    assert Path(decision["target_folder"]) == house / "ㅂ" / "별빛 연대기"


def test_folderling_volume_target_rejects_two_explicit_authors(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        _add(
            conn,
            house / "ㅂ" / "별빛 연대기" / "별빛 연대기 1권 [한작가].epub",
            "house",
        )
        incoming = _add(
            conn, temp / "별빛 연대기 2권 [다른작가].epub", "temp"
        )
        decision = classify_folderling_volume_target(
            conn,
            source_file_id=incoming["file_id"],
            house_root=house,
        )
    finally:
        conn.close()

    assert decision == {"status": "no_target", "reason": "author_conflict"}


def test_folderling_volume_target_requires_existing_work_folder(tmp_path):
    state_db, house, temp, _, incoming = _fixture(tmp_path)
    conn = decision_store.connect_state_db(state_db)
    try:
        rows = conn.execute(
            "SELECT file_id, canonical_path FROM files WHERE source = 'house'"
        ).fetchall()
        with decision_store.transaction(conn):
            for row in rows:
                old = Path(row["canonical_path"])
                new = house / "ㅂ" / old.name
                old.replace(new)
                stat = new.stat()
                conn.execute(
                    "UPDATE files SET canonical_path = ?, dev = ?, ino = ?, ctime_ns = ?, "
                    "size = ?, mtime_ns = ? WHERE file_id = ?",
                    (
                        str(new), stat.st_dev, stat.st_ino, stat.st_ctime_ns,
                        stat.st_size, stat.st_mtime_ns, row["file_id"],
                    ),
                )
        assert suggest_folderling_volume_target(
            conn,
            source_file_id=incoming["file_id"],
            house_root=house,
        ) is None
    finally:
        conn.close()


def test_folderling_accepts_latest_volume_after_human_approved_coordinate_duplicates(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        existing = [
            _add(conn, house / "ㄷ" / "대장간 작품" / name, "house")
            for name in (
                "대장간 작품 1권.epub",
                "대장간 작품 1권_dup_1.epub",
                "대장간 작품 2권.epub",
            )
        ]
        incoming = _add(conn, temp / "대장간 작품 3권.epub", "temp")
        ensure_volume_fingerprints(conn, [row["file_id"] for row in existing])
        with decision_store.transaction(conn):
            link_volume_relationships(
                conn,
                file_ids=[row["file_id"] for row in existing],
                display_title="대장간 작품",
                origin="human_decision",
            )

        target = suggest_folderling_volume_target(
            conn,
            source_file_id=incoming["file_id"],
            house_root=house,
        )
    finally:
        conn.close()

    assert target is not None
    assert Path(target["target_folder"]) == house / "ㄷ" / "대장간 작품"


def test_folderling_accepts_latest_volume_when_group_contains_side_story(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        existing = [
            _add(conn, house / "ㄷ" / "다정한 작품" / name, "house")
            for name in (
                "다정한 작품 1권 [한작가].epub",
                "다정한 작품 외전 [한작가].epub",
            )
        ]
        incoming = _add(conn, temp / "다정한 작품 2권 [한작가].epub", "temp")
        ensure_volume_fingerprints(conn, [row["file_id"] for row in existing])
        with decision_store.transaction(conn):
            link_volume_relationships(
                conn,
                file_ids=[row["file_id"] for row in existing],
                display_title="다정한 작품",
                origin="human_decision",
            )

        target = suggest_folderling_volume_target(
            conn,
            source_file_id=incoming["file_id"],
            house_root=house,
        )
    finally:
        conn.close()

    assert target is not None
    assert Path(target["target_folder"]) == house / "ㄷ" / "다정한 작품"


def test_bare_ebook_number_before_author_uses_same_volume_coordinate(tmp_path):
    explicit = decision_store.coordinate_fields_from_name(
        "Re 제로부터 시작하는 이세계 생활 5권 (나가츠키 탓페이).epub"
    )
    inferred = decision_store.coordinate_fields_from_name(
        "Re 제로부터 시작하는 이세계 생활 5 (나가츠키 탓페이).epub"
    )
    txt_episode = decision_store.coordinate_fields_from_name(
        "Re 제로부터 시작하는 이세계 생활 5 (나가츠키 탓페이).txt"
    )
    large_compilation = decision_store.coordinate_fields_from_name(
        "완결 웹소설 146 (한작가).epub"
    )

    assert (explicit["coordinate_kind"], explicit["volume_num"]) == ("volume", 5)
    assert (inferred["coordinate_kind"], inferred["volume_num"]) == ("volume", 5)
    assert txt_episode["coordinate_kind"] != "volume"
    assert large_compilation["coordinate_kind"] != "volume"


def test_bare_and_explicit_same_volume_are_coordinate_conflict(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        _add(
            conn,
            house / "ㄹ" / "Re 제로부터 시작하는 이세계 생활" /
            "Re 제로부터 시작하는 이세계 생활 5권 (나가츠키 탓페이).epub",
            "house",
        )
        incoming = _add(
            conn,
            temp / "Re 제로부터 시작하는 이세계 생활 5 (나가츠키 탓페이).epub",
            "temp",
        )
        decision = classify_folderling_volume_target(
            conn,
            source_file_id=incoming["file_id"],
            house_root=house,
            new_group_parent=house / "ㄹ",
        )
    finally:
        conn.close()

    assert decision["status"] == "coordinate_conflict"
    assert decision["coordinate_kind"] == "volume"
    assert decision["coordinate_num"] == 5


def test_numbered_side_stories_have_distinct_canonical_coordinates():
    first = decision_store.coordinate_fields_from_name("블랙 라벨 외전 1.epub")
    second = decision_store.coordinate_fields_from_name("블랙 라벨 외전 2.epub")

    assert first["coordinate_symbol"] == second["coordinate_symbol"] == "side_story"
    assert first["coordinate_sort_key"] == 201
    assert second["coordinate_sort_key"] == 202
    assert decision_store.coordinates_compatible(first, second) is False


def test_new_contiguous_ebook_batch_creates_one_work_folder(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    names = [
        f"아라포 현자의 이세계 생활 일기 {number}권 (코토부키 야스키요).epub"
        for number in (1, 2, 3)
    ]
    conn = decision_store.initialize_state_db(state_db)
    try:
        incoming = [_add(conn, temp / name, "temp") for name in names]
    finally:
        conn.close()

    run_id = _approve(state_db, house, temp)
    destinations = []
    for name in names:
        destinations.append(Path(move_to_house(
            str(temp / name),
            str(house),
            str(house / "_최근"),
            name,
            StringIO(),
            "",
            state_db_path=str(state_db),
            run_id=run_id,
        )))

    target = house / "ㅇ" / "아라포 현자의 이세계 생활 일기"
    conn = decision_store.connect_state_db(state_db)
    try:
        decision_store.finish_actual_run(conn, run_id, success=True)
        rows = conn.execute(
            """
            SELECT f.coordinate_kind, f.volume_num, f.variant_id, v.work_bucket_id
            FROM files f JOIN variants v ON v.variant_id = f.variant_id
            WHERE f.file_id IN (?, ?, ?) ORDER BY f.volume_num
            """,
            tuple(row["file_id"] for row in incoming),
        ).fetchall()
        assert [row["volume_num"] for row in rows] == [1, 2, 3]
        assert all(row["coordinate_kind"] == "volume" for row in rows)
        assert len({row["variant_id"] for row in rows}) == 3
        assert len({row["work_bucket_id"] for row in rows}) == 1
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()

    assert {path.parent for path in destinations} == {target}


def test_new_anonymous_ebook_batch_creates_one_work_folder(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    names = [f"무명 연대기 {number}권.epub" for number in (1, 2, 3)]
    conn = decision_store.initialize_state_db(state_db)
    try:
        incoming = [_add(conn, temp / name, "temp") for name in names]
    finally:
        conn.close()

    run_id = _approve(state_db, house, temp)
    destinations = [
        Path(move_to_house(
            str(temp / name), str(house), str(house / "_최근"), name,
            StringIO(), "", state_db_path=str(state_db), run_id=run_id,
        ))
        for name in names
    ]

    conn = decision_store.connect_state_db(state_db)
    try:
        decision_store.finish_actual_run(conn, run_id, success=True)
        rows = conn.execute(
            "SELECT v.work_bucket_id FROM files f JOIN variants v "
            "ON v.variant_id=f.variant_id WHERE f.file_id IN (?, ?, ?)",
            tuple(row["file_id"] for row in incoming),
        ).fetchall()
        assert len({row["work_bucket_id"] for row in rows}) == 1
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
    assert {path.parent for path in destinations} == {house / "ㅁ" / "무명 연대기"}


def test_same_core_episode_compilation_does_not_block_new_volume_folder(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        _add(conn, house / "ㅍ" / "판타지 연대기 1-150 완결.epub", "house")
        incoming = [
            _add(conn, temp / f"판타지 연대기 {number}권.epub", "temp")
            for number in (1, 2)
        ]
        decision = classify_folderling_volume_target(
            conn,
            source_file_id=incoming[0]["file_id"],
            house_root=house,
            new_group_parent=house / "ㅍ",
        )
    finally:
        conn.close()

    assert decision["status"] == "target"
    assert Path(decision["target_folder"]) == house / "ㅍ" / "판타지 연대기"


def test_part_volume_coordinates_are_distinct_and_route_to_same_folder(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    target = house / "ㅊ" / "천마군림"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        _add(conn, target / "천마군림 1부 1권.epub", "house")
        incoming = _add(conn, temp / "천마군림 2부 1권.epub", "temp")
        decision = classify_folderling_volume_target(
            conn,
            source_file_id=incoming["file_id"],
            house_root=house,
        )
    finally:
        conn.close()

    assert decision["status"] == "target"
    assert Path(decision["target_folder"]) == target


def test_new_part_volume_batch_creates_one_work_folder(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    names = [
        f"천마군림 {part}부 {volume}권.epub"
        for part in (1, 2) for volume in (1, 2)
    ]
    conn = decision_store.initialize_state_db(state_db)
    try:
        incoming = [_add(conn, temp / name, "temp") for name in names]
    finally:
        conn.close()

    run_id = _approve(state_db, house, temp)
    destinations = [
        Path(move_to_house(
            str(temp / name), str(house), str(house / "_최근"), name,
            StringIO(), "", state_db_path=str(state_db), run_id=run_id,
        ))
        for name in names
    ]
    conn = decision_store.connect_state_db(state_db)
    try:
        decision_store.finish_actual_run(conn, run_id, success=True)
        rows = conn.execute(
            "SELECT f.part_num, f.volume_num, v.work_bucket_id FROM files f "
            "JOIN variants v ON v.variant_id=f.variant_id "
            "WHERE f.file_id IN (?, ?, ?, ?)",
            tuple(row["file_id"] for row in incoming),
        ).fetchall()
        assert {(row["part_num"], row["volume_num"]) for row in rows} == {
            (1, 1), (1, 2), (2, 1), (2, 2),
        }
        assert len({row["work_bucket_id"] for row in rows}) == 1
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
    assert {path.parent for path in destinations} == {house / "ㅊ" / "천마군림"}


def test_managed_volume_cohort_routes_despite_unmanaged_same_core_compilation(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    target = house / "영어" / "Re 제로부터 시작하는 이세계 생활"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        managed = [
            _add(
                conn,
                target / f"Re 제로부터 시작하는 이세계 생활 {number}권 "
                "(나가츠키 탓페이).epub",
                "house",
            )
            for number in (36, 37, 39)
        ]
        _add(
            conn,
            house / "영어" / "Re 제로부터 시작하는 이세계 생활 16-33권.txt",
            "house",
        )
        incoming = _add(
            conn,
            temp / "Re 제로부터 시작하는 이세계 생활 38 "
            "(나가츠키 탓페이).epub",
            "temp",
        )
        ensure_volume_fingerprints(conn, [row["file_id"] for row in managed])
        with decision_store.transaction(conn):
            link_volume_relationships(
                conn,
                file_ids=[row["file_id"] for row in managed],
                display_title="Re 제로부터 시작하는 이세계 생활",
                origin="human_decision",
            )
        decision = classify_folderling_volume_target(
            conn,
            source_file_id=incoming["file_id"],
            house_root=house,
            new_group_parent=house / "영어",
        )
    finally:
        conn.close()

    assert decision["status"] == "target"
    assert Path(decision["target_folder"]) == target
