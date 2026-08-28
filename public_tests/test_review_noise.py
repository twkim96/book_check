import json
import zipfile
from pathlib import Path

import decision_store
import duplicate_auditor
import library_catalog
import review_noise


def _write_epub(path: Path, body: bytes):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("OEBPS/chapter.xhtml", body)


def _audit_fixture(tmp_path, names=None):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    names = names or ["분권 작품 05.epub", "분권 작품 09.epub"]
    for name, body in zip(names, (b"volume-five", b"volume-nine")):
        _write_epub(house / name, body)
    index = tmp_path / "file_index.json"
    index.write_text(json.dumps({
        "version": 2,
        "normalizer_version": "fixture",
        "entries": [
            {
                "type": "file",
                "name": name,
                "rel_path": name,
                "size": (house / name).stat().st_size,
            }
            for name in names
        ],
    }, ensure_ascii=False), encoding="utf-8")
    state_db = tmp_path / "state.sqlite3"
    args = duplicate_auditor.build_parser().parse_args([
        "--index", str(index),
        "--house", str(house),
        "--temp", str(temp),
        "--house-only",
        "--state-db", str(state_db),
    ])
    return house, temp, state_db, args


def test_distinct_terminal_epub_volume_parser_is_conservative():
    assert review_noise.distinct_terminal_epub_volumes(
        "재벌집 막내아들 05.epub", "재벌집 막내아들 09.epub"
    )
    assert review_noise.distinct_terminal_epub_volumes(
        "[1228] 피폐물 조연은 도망치고 싶다 2.epub",
        "[1228] 피폐물 조연은 도망치고 싶다 3.epub",
    )
    assert not review_noise.distinct_terminal_epub_volumes(
        "재벌집 막내아들 09.epub", "재벌집 막내아들 09.epub"
    )
    assert not review_noise.distinct_terminal_epub_volumes(
        "재벌집 막내아들 05.txt", "재벌집 막내아들 09.txt"
    )
    assert not review_noise.distinct_terminal_epub_volumes(
        "서로 다른 작품 05.epub", "다른 작품 09.epub"
    )
    assert review_noise.different_core_titles(
        "노게임노라이프", "노게임노라이프프랙티컬워게임"
    )
    assert not review_noise.different_core_titles(
        "노게임노라이프", "노게임노라이프"
    )
    assert review_noise.side_story_vs_numbered_epub_volume(
        "마녀의 여행 외전.epub", "마녀의 여행 14권.epub"
    )
    assert review_noise.side_story_vs_numbered_epub_volume(
        "마녀의 여행 02권.epub", "마녀의 여행 외전.epub"
    )
    assert not review_noise.side_story_vs_numbered_epub_volume(
        "마녀의 여행 외전.epub", "마녀의 여행 특별편.epub"
    )
    assert not review_noise.side_story_vs_numbered_epub_volume(
        "마녀의 여행 외전.txt", "마녀의 여행 14권.txt"
    )
    assert review_noise.structurally_distinct_epub_books(
        "역사 속 무기상이 되었다 001-050화.epub",
        "역사 속 무기상이 되었다 051-100화.epub",
    )
    assert not review_noise.structurally_distinct_epub_books(
        "역사 속 무기상이 되었다.epub",
        "역사 속 무기상이 되었다 051-100화.epub",
    )


def test_auditor_keeps_distinct_epub_volumes_out_of_human_review(tmp_path):
    _house, _temp, state_db, args = _audit_fixture(tmp_path)

    report = duplicate_auditor.run_audit(args)

    # 1.4.11 inventory context promotes the bare 05/09 suffixes to explicit
    # sibling volume coordinates before candidate generation. They no longer
    # need a metadata-only noise pair merely to suppress human review.
    assert report.results == []
    assert report.stats["candidate_pairs"] == 0
    assert report.stats["distinct_volume_reviews_suppressed"] == 0
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM review_items").fetchone()[0] == 0
    finally:
        conn.close()


def test_auditor_keeps_side_story_vs_numbered_volume_out_of_human_review(tmp_path):
    _house, _temp, state_db, args = _audit_fixture(
        tmp_path,
        names=["마녀의 여행 외전.epub", "마녀의 여행 14권.epub"],
    )

    report = duplicate_auditor.run_audit(args)

    assert report.results[0]["classification"] == "metadata_only"
    assert report.stats["side_story_volume_reviews_suppressed"] == 1
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM review_items").fetchone()[0] == 0
    finally:
        conn.close()


def test_cleanup_supersedes_existing_side_story_volume_noise(tmp_path, monkeypatch):
    house, temp, state_db, args = _audit_fixture(
        tmp_path,
        names=["마녀의 여행 외전.epub", "마녀의 여행 14권.epub"],
    )
    monkeypatch.setattr(
        duplicate_auditor,
        "side_story_vs_numbered_epub_volume",
        lambda _left, _right: False,
    )
    monkeypatch.setattr(
        duplicate_auditor,
        "structurally_distinct_epub_books",
        lambda _left, _right: False,
    )
    monkeypatch.setattr(
        duplicate_auditor,
        "plan_open_review_reconciliation",
        lambda _conn: {"noise": [], "stale": [], "redundant": []},
    )
    duplicate_auditor.run_audit(args)

    preview = review_noise.cleanup_review_noise(
        state_db, house_dir=house, temp_dir=temp, apply=False
    )

    assert preview["planned_noise_superseded"] == 1
    [item] = preview["items"]
    assert item["suppression_reason"] == review_noise.SIDE_STORY_SUPPRESSION_REASON


def test_completed_audit_reconciles_historical_unqueued_diagnostic_row(
    tmp_path, monkeypatch,
):
    house, _temp, state_db, args = _audit_fixture(
        tmp_path,
        names=["마녀의 여행 외전.epub", "마녀의 여행 14권.epub"],
    )
    with monkeypatch.context() as patcher:
        patcher.setattr(
            duplicate_auditor,
            "side_story_vs_numbered_epub_volume",
            lambda _left, _right: False,
        )
        patcher.setattr(
            duplicate_auditor,
            "structurally_distinct_epub_books",
            lambda _left, _right: False,
        )
        patcher.setattr(
            duplicate_auditor,
            "plan_open_review_reconciliation",
            lambda _conn: {"noise": [], "stale": [], "redundant": []},
        )
        duplicate_auditor.run_audit(args)

    report = duplicate_auditor.run_audit(args)

    assert report.stats["diagnostic_reviews_superseded"] == 1
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        row = conn.execute(
            "SELECT state, evidence_json FROM review_items"
        ).fetchone()
        assert row["state"] == "superseded"
        assert json.loads(row["evidence_json"])["automatic_suppression"][
            "reason"
        ] == review_noise.SIDE_STORY_SUPPRESSION_REASON
    finally:
        conn.close()


def test_auditor_keeps_cross_core_metadata_only_out_of_human_review(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    names = [
        "노 게임 노 라이프 8권 (카미야 유우).epub",
        "노 게임·노 라이프 프랙티컬 워게임 (카미야 유우).epub",
    ]
    for name, body in zip(names, (b"main-volume", b"practical-war-game")):
        _write_epub(house / name, body)
    index = tmp_path / "file_index.json"
    index.write_text(json.dumps({
        "version": 2,
        "normalizer_version": "fixture",
        "entries": [
            {
                "type": "file",
                "name": name,
                "rel_path": name,
                "size": (house / name).stat().st_size,
            }
            for name in names
        ],
    }, ensure_ascii=False), encoding="utf-8")
    state_db = tmp_path / "state.sqlite3"
    args = duplicate_auditor.build_parser().parse_args([
        "--index", str(index), "--house", str(house),
        "--temp", str(temp), "--house-only", "--state-db", str(state_db),
    ])

    report = duplicate_auditor.run_audit(args)

    assert report.results[0]["classification"] == "metadata_only"
    assert report.results[0]["candidate_reasons"] == ["metadata_leak"]
    assert report.stats["cross_core_reviews_suppressed"] == 1
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM review_items").fetchone()[0] == 0
    finally:
        conn.close()


def test_auditor_keeps_cross_core_decode_lossy_as_diagnostic_only(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    names = [
        "다시 사는 예고천재 1-100화.txt",
        "다시 사는 연기천재 1-100화.txt",
    ]
    (house / names[0]).write_bytes(b"\xff" * 3_001)
    (house / names[1]).write_bytes(b"\xfe" * 5_003)
    index = tmp_path / "file_index.json"
    index.write_text(json.dumps({
        "version": 2,
        "normalizer_version": "fixture",
        "entries": [
            {
                "type": "file", "name": name, "rel_path": name,
                "size": (house / name).stat().st_size,
            }
            for name in names
        ],
    }, ensure_ascii=False), encoding="utf-8")
    state_db = tmp_path / "state.sqlite3"
    args = duplicate_auditor.build_parser().parse_args([
        "--index", str(index), "--house", str(house),
        "--temp", str(temp), "--house-only", "--state-db", str(state_db),
    ])

    report = duplicate_auditor.run_audit(args)

    assert report.results[0]["classification"] == "decode_lossy"
    assert report.stats["cross_core_decode_reviews_suppressed"] == 1
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM review_items").fetchone()[0] == 0
    finally:
        conn.close()


def test_reconcile_keeps_strongest_current_classification_per_unordered_pair(
    tmp_path,
):
    house, _temp, state_db, args = _audit_fixture(
        tmp_path,
        names=["검토 통합 작품 5권.epub", "검토 통합 작품 05권.epub"],
    )
    duplicate_auditor.run_audit(args)
    conn = decision_store.connect_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE review_items SET state = 'superseded' "
                "WHERE state IN ('pending', 'deferred')"
            )
        rows = conn.execute(
            "SELECT file_id FROM files ORDER BY canonical_path"
        ).fetchall()
        first, second = (row["file_id"] for row in rows)
        weaker = decision_store.add_review_item(
            conn,
            candidate_file_id=first,
            reference_file_id=second,
            classification="longer_unresolved",
        )
        stronger = decision_store.add_review_item(
            conn,
            candidate_file_id=second,
            reference_file_id=first,
            classification="ordered_body_review",
        )
        with decision_store.transaction(conn):
            result = review_noise.reconcile_open_reviews(conn)

        assert result["duplicate_superseded"] == 1
        states = {
            row["review_id"]: row
            for row in conn.execute(
                "SELECT review_id, classification, state, evidence_json "
                "FROM review_items WHERE review_id IN (?, ?)",
                (weaker, stronger),
            )
        }
        assert states[weaker]["state"] == "superseded"
        assert states[stronger]["state"] == "pending"
        marker = json.loads(states[weaker]["evidence_json"])[
            "automatic_suppression"
        ]
        assert marker["reason"] == review_noise.WEAKER_RELATION_SUPPRESSION_REASON
        assert marker["keep_review_id"] == stronger
    finally:
        conn.close()


def test_cleanup_supersedes_existing_noise_after_backup_without_moving_files(
    tmp_path, monkeypatch
):
    house, temp, state_db, args = _audit_fixture(tmp_path)
    monkeypatch.setattr(
        duplicate_auditor,
        "distinct_terminal_epub_volumes",
        lambda _left, _right: False,
    )
    monkeypatch.setattr(
        duplicate_auditor,
        "different_core_titles",
        lambda _left, _right: False,
    )
    monkeypatch.setattr(
        duplicate_auditor,
        "_different_explicit_volumes",
        lambda _left, _right: False,
    )
    monkeypatch.setattr(
        duplicate_auditor,
        "plan_open_review_reconciliation",
        lambda _conn: {"noise": [], "stale": [], "redundant": []},
    )
    duplicate_auditor.run_audit(args)
    before = {
        path.name: path.read_bytes()
        for path in house.iterdir()
        if path.is_file()
    }

    preview = review_noise.cleanup_review_noise(
        state_db, house_dir=house, temp_dir=temp, apply=False
    )
    result = review_noise.cleanup_review_noise(
        state_db, house_dir=house, temp_dir=temp, apply=True
    )

    assert preview["planned_superseded"] == 1
    assert result["superseded"] == 1
    assert Path(result["backup_path"]).is_file()
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        row = conn.execute(
            "SELECT state, evidence_json FROM review_items"
        ).fetchone()
        assert row["state"] == "superseded"
        evidence = json.loads(row["evidence_json"])
        assert evidence["automatic_suppression"]["reason"] == (
            review_noise.SUPPRESSION_REASON
        )
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
    after = {
        path.name: path.read_bytes()
        for path in house.iterdir()
        if path.is_file()
    }
    assert after == before


def test_review_queue_merges_database_relation_with_its_physical_queue_file(
    tmp_path, monkeypatch
):
    house, temp, state_db, args = _audit_fixture(tmp_path)
    monkeypatch.setattr(
        duplicate_auditor,
        "distinct_terminal_epub_volumes",
        lambda _left, _right: False,
    )
    monkeypatch.setattr(
        duplicate_auditor,
        "different_core_titles",
        lambda _left, _right: False,
    )
    monkeypatch.setattr(
        duplicate_auditor,
        "_different_explicit_volumes",
        lambda _left, _right: False,
    )
    original_plan = duplicate_auditor.plan_open_review_reconciliation
    monkeypatch.setattr(
        duplicate_auditor,
        "plan_open_review_reconciliation",
        lambda _conn: {"noise": [], "stale": [], "redundant": []},
    )
    duplicate_auditor.run_audit(args)
    monkeypatch.setattr(
        duplicate_auditor, "plan_open_review_reconciliation", original_plan
    )
    warning = temp / "trash_bin" / "warning"
    warning.mkdir(parents=True)
    queued = warning / "분권 작품 09.epub"
    queued.write_bytes(b"queued fixture")
    conn = decision_store.connect_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE review_items SET queue_path = ?",
                (str(queued.resolve()),),
            )
    finally:
        conn.close()

    duplicate_auditor.run_audit(args)
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert conn.execute(
            "SELECT state FROM review_items"
        ).fetchone()["state"] == "pending"
    finally:
        conn.close()

    listing = library_catalog.review_queue_listing(
        state_db,
        temp,
        physical="quarantined",
    )

    assert listing["total_visible"] == 1
    assert listing["summary"]["quarantined"] == 1
    [item] = listing["items"]
    assert item["kind"] == "database"
    assert item["physical_state"] == "quarantined"
    assert item["path"] == str(queued.resolve())
