from io import StringIO
from pathlib import Path

import decision_store
from bare_volume_context import (
    context_name,
    infer_bare_volume_overrides,
    parse_bare_volume_candidate,
)
from folderling import move_to_house
from scanner import get_file_entries
from volume_review import apply_auto_ready_volume_groups, list_volume_cases


def _record(key, name, *, assignment_state="unassigned", current_core_title=None):
    clean_name = context_name(name)
    analysis = decision_store.build_file_analysis(clean_name)
    return {
        "key": key,
        "name": clean_name,
        "analysis": analysis,
        "coordinates": decision_store.coordinate_fields_from_name(clean_name),
        "assignment_state": assignment_state,
        "current_core_title": current_core_title or analysis["core_title"],
        "title_override": False,
    }


def _add(conn, path, source):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(path.name, encoding="utf-8")
    with decision_store.transaction(conn):
        return decision_store.reconcile_file_metadata(conn, path, source=source)


def _approve(state_db, house, temp):
    conn = decision_store.connect_state_db(state_db)
    try:
        backup = decision_store.backup_state_db(
            conn, state_db.parent / "backups" / "before-bare-volume.sqlite3"
        )
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()
    return decision_store.prepare_actual_run(state_db, house, temp)[0]


def test_two_distinct_bare_numbers_promote_but_singleton_does_not():
    first = _record("first", "무극신갑 1[공금].txt")
    second = _record("second", "무극신갑 2[공금].txt")
    singleton = _record("singleton", "고유 작품 7.txt")

    overrides = infer_bare_volume_overrides([first, second, singleton])

    assert set(overrides) == {"first", "second"}
    assert overrides["first"].core_title == overrides["second"].core_title == "무극신갑"
    assert overrides["first"].volume_number == 1
    assert overrides["second"].volume_number == 2
    assert parse_bare_volume_candidate(singleton["name"], analysis=singleton["analysis"])


def test_explicit_volume_or_managed_work_is_enough_context_for_one_bare_file():
    explicit = _record("explicit", "별빛 연대기 1권.txt")
    bare = _record("bare", "별빛 연대기 2.epub")
    managed = _record(
        "managed",
        "관리 작품 4 [한작가].txt",
        assignment_state="managed",
        current_core_title="관리작품",
    )

    overrides = infer_bare_volume_overrides([explicit, bare, managed])

    assert set(overrides) == {"bare", "managed"}
    assert overrides["bare"].core_title == "별빛연대기"
    assert overrides["managed"].core_title == "관리작품"


def test_explicit_author_conflict_and_non_volume_numbers_fail_closed():
    records = [
        _record("left", "충돌 작품 1 [왼작가].epub"),
        _record("right", "충돌 작품 2 [오른작가].epub"),
        _record("range", "범위 작품 1-20.txt"),
        _record("large", "완결 웹소설 146.txt"),
        _record("date", "연감 2026 07.txt"),
    ]

    assert infer_bare_volume_overrides(records) == {}
    assert parse_bare_volume_candidate(
        "티어문 제국 이야기 10 소책자 한정판 (모치츠키 노조무).epub"
    ).qualifier == "소책자 한정판"


def test_decimal_special_edition_and_author_copy_suffix_are_closed_shapes():
    decimal = parse_bare_volume_candidate(
        "옆집 천사님 사연 11.5 (특별판) (사에키상).epub"
    )
    collision = parse_bare_volume_candidate(
        "비블리아 고서당 사건수첩 1 (미카미 엔) -2.epub"
    )

    assert decimal is not None
    assert decimal.volume_number == "11.5"
    assert decimal.coordinate_fields()["volume_num"] == 23
    assert decimal.coordinate_fields()["volume_den"] == 2
    assert collision is not None
    assert collision.volume_number == 1
    assert parse_bare_volume_candidate("86 에이티식스 Alter.2.epub").volume_number == 2
    assert parse_bare_volume_candidate("정상 제목 3.11 (작가).epub") is None


def test_parenthesized_total_count_is_one_through_total_not_one_episode():
    analysis = decision_store.build_file_analysis(
        "7번째 환생(총243화) [묘재].txt"
    )

    assert analysis["start_number"] == 1
    assert analysis["end_number"] == 243
    assert analysis["effective_max"] == 243


def test_temp_source_suffix_and_special_edition_share_contextual_core(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        names = [
            "티어문 제국 이야기 2 (모치츠키 노조무) "
            "(z-library.sk, 1lib.sk, z-lib.sk).epub",
            "티어문 제국 이야기 10 소책자 한정판 (모치츠키 노조무) "
            "(z-library.sk, 1lib.sk, z-lib.sk).epub",
        ]
        for name in names:
            _add(conn, temp / name, "temp")
        with decision_store.transaction(conn):
            result = decision_store.sync_contextual_bare_volume_metadata(
                conn,
                target_sources=("temp",),
                evidence_sources=("temp",),
            )
        rows = conn.execute(
            """
            SELECT f.volume_num, f.coordinate_kind, a.core_title, a.author, a.unit
            FROM files AS f JOIN file_analysis AS a ON a.file_id = f.file_id
            ORDER BY f.volume_num
            """
        ).fetchall()
    finally:
        conn.close()

    assert result["candidate_count"] == 1
    assert result["promoted_count"] == 1
    assert [row["volume_num"] for row in rows] == [2, 10]
    assert {row["coordinate_kind"] for row in rows} == {"volume"}
    assert {row["core_title"] for row in rows} == {"티어문제국이야기"}
    assert {row["author"] for row in rows} == {"모치츠키 노조무"}
    assert {row["unit"] for row in rows} == {"권"}


def test_house_and_temp_bare_singletons_can_prove_each_other(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        house_row = _add(conn, house / "교차 작품 1[공금].txt", "house")
        temp_row = _add(conn, temp / "교차 작품 2[공금].txt", "temp")
        with decision_store.transaction(conn):
            result = decision_store.sync_contextual_bare_volume_metadata(
                conn,
                target_sources=("temp",),
                evidence_sources=("house", "temp"),
            )
        projected_temp = conn.execute(
            """
            SELECT f.coordinate_kind, f.volume_num, a.core_title
            FROM files AS f JOIN file_analysis AS a ON a.file_id = f.file_id
            WHERE f.file_id = ?
            """,
            (temp_row["file_id"],),
        ).fetchone()
        untouched_house = conn.execute(
            "SELECT coordinate_kind FROM files WHERE file_id = ?",
            (house_row["file_id"],),
        ).fetchone()
    finally:
        conn.close()

    assert result["promoted_count"] == 1
    assert tuple(projected_temp) == ("volume", 2, "교차작품")
    assert untouched_house["coordinate_kind"] != "volume"


def test_full_scanner_context_projection_is_idempotent(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    for name in ("반복 작품 1.txt", "반복 작품 2.txt"):
        (house / name).write_text(name, encoding="utf-8")

    first = get_file_entries([str(house)], state_db_path=state_db)
    second = get_file_entries([str(house)], state_db_path=state_db)

    for entries in (first, second):
        files = [entry for entry in entries if entry["type"] == "file"]
        assert {entry["core_title"] for entry in files} == {"반복작품"}
        assert {tuple(entry["volume_number"]) for entry in files} == {
            (None, 1),
            (None, 2),
        }


def test_scanner_keeps_managed_singleton_context_without_special_folder(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    path = house / "관리 작품 4.txt"
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        row = _add(conn, path, "house")
        with decision_store.transaction(conn):
            work_id = conn.execute(
                "INSERT INTO works(display_title) VALUES ('관리 작품')"
            ).lastrowid
            variant_id = conn.execute(
                "INSERT INTO variants(work_bucket_id) VALUES (?)", (work_id,)
            ).lastrowid
            conn.execute(
                """
                UPDATE files
                SET variant_id = ?, assignment_state = 'managed',
                    assignment_origin = 'human_decision'
                WHERE file_id = ?
                """,
                (variant_id, row["file_id"]),
            )
            conn.execute(
                """
                UPDATE file_analysis
                SET core_title = '관리작품', readable_title = '관리 작품',
                    catalog_query_title = '관리 작품'
                WHERE file_id = ?
                """,
                (row["file_id"],),
            )
    finally:
        conn.close()

    entries = get_file_entries([str(house)], state_db_path=state_db)
    item = next(entry for entry in entries if entry["type"] == "file")
    assert item["core_title"] == "관리작품"
    assert tuple(item["volume_number"]) == (None, 4)


def test_side_story_is_not_used_as_bare_main_volume_evidence(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        _add(conn, house / "외전 관계 작품 외전 1.txt", "house")
        temp_row = _add(conn, temp / "외전 관계 작품 2.txt", "temp")
        with decision_store.transaction(conn):
            result = decision_store.sync_contextual_bare_volume_metadata(
                conn,
                target_sources=("temp",),
                evidence_sources=("house", "temp"),
            )
        projected = conn.execute(
            "SELECT coordinate_kind FROM files WHERE file_id = ?",
            (temp_row["file_id"],),
        ).fetchone()
    finally:
        conn.close()

    assert result["promoted_count"] == 0
    assert projected["coordinate_kind"] != "volume"


def test_warm_context_projection_does_not_restat_unchanged_house(tmp_path, monkeypatch):
    house = tmp_path / "house"
    house.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        for name in ("따뜻한 반복 1.txt", "따뜻한 반복 2.txt"):
            _add(conn, house / name, "house")
        _add(conn, house / "따뜻한 반복 합본 1-100화.txt", "house")
        with decision_store.transaction(conn):
            first = decision_store.sync_contextual_bare_volume_metadata(
                conn,
                target_sources=("house",),
                evidence_sources=("house",),
            )
        assert first["analysis_changed"] == 2

        def unexpected_stat(*_args, **_kwargs):
            raise AssertionError("warm contextual projection must reuse reconciled identity")

        monkeypatch.setattr(decision_store.os, "stat", unexpected_stat)
        original_build = decision_store.build_file_analysis
        reanalyzed_names = []

        def track_bare_only(name):
            reanalyzed_names.append(name)
            return original_build(name)

        monkeypatch.setattr(decision_store, "build_file_analysis", track_bare_only)
        with decision_store.transaction(conn):
            second = decision_store.sync_contextual_bare_volume_metadata(
                conn,
                target_sources=("house",),
                evidence_sources=("house",),
            )
    finally:
        conn.close()

    assert second["analysis_changed"] == 0
    assert second["coordinate_changed"] == 0
    assert set(reanalyzed_names) == {"따뜻한 반복 1.txt", "따뜻한 반복 2.txt"}


def test_platform_metadata_sync_allows_context_proven_core_convergence(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        for name in ("플랫폼 문맥 1.txt", "플랫폼 문맥 2.txt"):
            _add(conn, house / name, "house")
        with decision_store.transaction(conn):
            result = decision_store.sync_active_file_analysis(conn)
        cores = {
            row["core_title"]
            for row in conn.execute("SELECT core_title FROM file_analysis")
        }
    finally:
        conn.close()

    assert result["contextual_bare_volumes"]["promoted_count"] == 2
    assert cores == {"플랫폼문맥"}


def test_context_convergence_requeries_ambiguous_per_volume_catalog_success(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        old_cores = []
        for name in ("재조회 문맥 1.txt", "재조회 문맥 2.txt"):
            row = _add(conn, house / name, "house")
            old_core = conn.execute(
                "SELECT core_title FROM file_analysis WHERE file_id = ?",
                (row["file_id"],),
            ).fetchone()[0]
            old_cores.append(old_core)
            with decision_store.transaction(conn):
                conn.execute(
                    """
                    INSERT INTO catalog_titles(
                        title_key, display_title, query_title, normalizer_version
                    ) VALUES (?, ?, ?, 'old')
                    """,
                    (old_core, old_core, old_core),
                )
                conn.execute(
                    """
                    INSERT INTO catalog_platform_stats(title_key, platform, status)
                    VALUES (?, 'series', 'ok')
                    """,
                    (old_core,),
                )
        with decision_store.transaction(conn):
            result = decision_store.sync_active_file_analysis(conn)
        target_status = conn.execute(
            "SELECT status FROM catalog_platform_stats "
            "WHERE title_key = '재조회문맥' AND platform = 'series'"
        ).fetchone()
    finally:
        conn.close()

    assert set(old_cores) == {"재조회문맥1", "재조회문맥2"}
    assert result["title_rekeys"]["ambiguous_success_rows_discarded"] == 2
    assert target_status is None


def test_bare_txt_cohort_survives_ingest_and_becomes_auto_ready(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        names = ["문맥 연대기 1[공금].txt", "문맥 연대기 2[공금].txt"]
        for name in names:
            _add(conn, temp / name, "temp")
        with decision_store.transaction(conn):
            projected = decision_store.sync_contextual_bare_volume_metadata(
                conn,
                target_sources=("temp",),
                evidence_sources=("temp",),
            )
    finally:
        conn.close()
    assert projected["promoted_count"] == 2

    run_id = _approve(state_db, house, temp)
    for name in names:
        move_to_house(
            str(temp / name),
            str(house),
            str(house / "_최근"),
            name,
            StringIO(),
            "",
            state_db_path=str(state_db),
            run_id=run_id,
        )

    conn = decision_store.connect_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            after_ingest = decision_store.sync_contextual_bare_volume_metadata(
                conn,
                target_sources=("house",),
                evidence_sources=("house",),
            )
            decision_store.migrate_catalog_title_keys(conn, after_ingest["rekeys"])
    finally:
        conn.close()

    listing = list_volume_cases(state_db, house_dir=house, limit=20)
    case = next(item for item in listing["items"] if item["core_title"] == "문맥연대기")
    assert case["classification"] == "auto_ready"
    assert case["coordinate_range"] == ["1권", "2권"]

    applied = apply_auto_ready_volume_groups(
        state_db,
        house_dir=house,
        temp_dir=temp,
        run_id=run_id,
    )
    assert applied["applied_count"] == 1
    target = house / "ㅁ" / "문맥 연대기"
    assert {path.name for path in target.iterdir()} == set(names)

    conn = decision_store.connect_state_db(state_db)
    try:
        decision_store.finish_actual_run(conn, run_id, success=True)
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
