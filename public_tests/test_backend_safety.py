import json
import os

import decision_store
import deduplicator
import duplicate_auditor
from normalizer import (
    analyze_name,
    extract_catalog_query_title,
    extract_readable_title,
    extract_structure_hint_tokens,
    has_pass_marker,
    materialize_title_markup,
    materialize_title_literals,
    structure_hint_syntax_error,
    title_literal_syntax_error,
)


def test_synthetic_title_rules_keep_markers_and_ranges_separate():
    info = analyze_name("합성연재물 1-250화 완〔P〕.txt")
    assert info["core_title"] == "합성연재물"
    assert info["effective_max"] == 250
    assert info["complete"] is True
    assert has_pass_marker("합성연재물 1-250화 완〔P〕.txt") is True


def test_space_separated_completed_range_does_not_leak_into_core_title():
    info = analyze_name("최강 헌터의 자화상  1 125 완.txt")
    assert info["core_title"] == "최강헌터의자화상"
    assert info["start_number"] == 1
    assert info["end_number"] == 125
    assert info["effective_max"] == 125
    assert info["unit"] == "화"


def test_post_status_prefix_keeps_meaningful_attached_parenthetical():
    name = "[19禁완) 야설(근친) 작가로 살아가는 법 1-155 완 [ txt + epub ].txt"
    info = analyze_name(name)
    assert info["core_title"] == "야설근친작가로살아가는법"
    assert extract_readable_title(name) == "야설(근친) 작가로 살아가는 법"
    assert extract_catalog_query_title(name) == "야설(근친) 작가로 살아가는 법"
    assert info["start_number"] == 1
    assert info["end_number"] == 155


def test_new_completion_prefix_is_removed_without_touching_real_numeric_title():
    tagged = "신작완결) 출근 중 사건 발생 보고서 1-209 완결.txt"
    assert extract_readable_title(tagged) == "출근 중 사건 발생 보고서"
    assert analyze_name(tagged)["core_title"] == "출근중사건발생보고서"
    assert analyze_name("19호실의 비밀 1-50화 완결.txt")["core_title"] == "19호실의비밀"
    assert analyze_name("작품명(작가명) 1-100 완.txt")["core_title"] == "작품명"


def test_single_character_noise_tag_does_not_hide_real_trailing_author():
    name = (
        "노 게임·노 라이프 (게이머 남매는 한 턴 쉬겠다는데요) "
        "── 9권 (카미야 유우).epub"
    )
    assert analyze_name(name)["author"] == "카미야 유우"
    assert analyze_name("노 게임 노 라이프 1권 (카미야 유우).epub")["author"] == "카미야 유우"


def test_user_title_literal_preserves_real_noise_word_without_becoming_metadata():
    name = "[[19금]] 떡타지의 주인공 친구가 되었다 [스투피르] 0-631 완.txt"
    info = analyze_name(name)
    assert info["core_title"] == "19금떡타지의주인공친구가되었다"
    assert info["author"] == "스투피르"
    assert info["effective_max"] == 631
    assert info["complete"] is True
    assert info["title_literal_tokens"] == ("19금",)
    assert extract_catalog_query_title(name) == "19금 떡타지의 주인공 친구가 되었다"
    assert materialize_title_literals(name) == (
        "19금 떡타지의 주인공 친구가 되었다 [스투피르] 0-631 완.txt"
    )
    assert title_literal_syntax_error(name) is None
    assert title_literal_syntax_error("[[19금] 제목.txt") is not None
    assert title_literal_syntax_error("[[   ]] 제목.txt") is not None


def test_structure_hint_is_excluded_from_title_and_materialized_for_final_name():
    name = "구조 작품 {{R 307}}.epub"
    info = analyze_name(name)

    assert info["core_title"] == "구조작품"
    assert info["effective_max"] == 307
    assert info["structure_hint_tokens"] == ("R 307",)
    assert extract_structure_hint_tokens(name) == ("R 307",)
    assert materialize_title_markup(name) == "구조 작품 R 307.epub"
    assert structure_hint_syntax_error(name) is None
    assert structure_hint_syntax_error("구조 작품 {{R 307}.epub") is not None
    override = json.loads(decision_store.build_file_analysis(name)["title_override_json"])
    assert override == {"title_literals": [], "structure_hints": ["R 307"]}


def test_auditor_bridge_accepts_path_objects_at_argparse_boundary(tmp_path, monkeypatch):
    index = tmp_path / "file_index.json"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    state_db = tmp_path / "state.sqlite3"
    index.write_text('{"entries": []}', encoding="utf-8")
    house.mkdir()
    temp.mkdir()
    captured = {}
    progress_events = []

    def fake_run(args):
        captured.update(vars(args))
        args.progress_callback({
            "audit_phase": "text_analysis",
            "completed": 100,
            "total": 200,
            "read_bytes": 1024,
        })
        return args

    monkeypatch.setattr(duplicate_auditor, "run_audit", fake_run)
    deduplicator.run_auditor_queue_report(
        index, house, temp, state_db_path=state_db, cache_write=False,
        progress_callback=progress_events.append,
    )

    assert captured["index"] == str(index)
    assert captured["house"] == str(house)
    assert captured["temp"] == str(temp)
    assert captured["state_db"] == str(state_db)
    assert progress_events == [{
        "audit_phase": "text_analysis",
        "completed": 100,
        "total": 200,
        "read_bytes": 1024,
    }]


def test_epub_representative_is_not_missing_from_txt_only_full_scan(tmp_path):
    house = tmp_path / "house"
    house.mkdir()
    representative = house / "합성 분권 1권.epub"
    representative.write_bytes(b"synthetic epub representative")
    state_db = tmp_path / "state.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            row = decision_store.reconcile_file_metadata(
                conn, representative, source="house"
            )
            work_id = conn.execute(
                "INSERT INTO works(display_title) VALUES ('합성 분권')"
            ).lastrowid
            variant_id = conn.execute(
                "INSERT INTO variants(work_bucket_id, variant_kind) VALUES (?, 'base')",
                (work_id,),
            ).lastrowid
            conn.execute(
                """
                UPDATE files SET variant_id = ?, assignment_state = 'managed',
                    assignment_origin = 'strong_match', protected = 1
                WHERE file_id = ?
                """,
                (variant_id, row["file_id"]),
            )
            conn.execute(
                "INSERT INTO representatives(variant_id, file_id) VALUES (?, ?)",
                (variant_id, row["file_id"]),
            )
    finally:
        conn.close()

    candidates, missing = duplicate_auditor.generate_managed_representative_candidates(
        [], str(state_db)
    )
    assert candidates == []
    assert missing == []


def test_dry_run_never_removes_exact_duplicate_fixture(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    first = house / "합성작품 1-10화.txt"
    second = house / "합성작품 1-10화 사본.txt"
    first.write_text("합성 본문 " * 100, encoding="utf-8")
    second.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")

    summary = deduplicator.clean_duplicates(
        house_dir=str(house),
        temp_dir=str(temp),
        dry_run=True,
        index_path=str(tmp_path / "file_index.json"),
        rescan=True,
    )

    assert summary["exact_count"] == 1
    assert first.is_file()
    assert second.is_file()
    assert not (temp / "trash_bin").exists()
    assert list((temp / "dedup_logs").glob("dedup_*.txt")) == []
    [structured_report] = list((temp / "dedup_logs").glob("dedup_*.json"))
    assert structured_report.is_file()
    payload = json.loads(structured_report.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["summary"]["exact_count"] == 1
    assert summary["report_path"] == str(structured_report)
    assert summary["structured_report_path"] == str(structured_report)


def test_doctor_ignores_unassigned_ctime_only_but_rejects_inode_replacement(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    source = tmp_path / "house" / "합성작품.txt"
    source.parent.mkdir()
    source.write_text("합성 본문", encoding="utf-8")
    conn = decision_store.initialize_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            decision_store.reconcile_file_metadata(conn, source, source="house")
        file_id = conn.execute(
            "SELECT file_id FROM files WHERE canonical_path = ?", (str(source.resolve()),)
        ).fetchone()[0]
        before = source.stat()
        source.chmod(before.st_mode ^ 0o100)
        source.chmod(before.st_mode)
        after_metadata = source.stat()
        assert after_metadata.st_ino == before.st_ino
        assert after_metadata.st_mtime_ns == before.st_mtime_ns
        assert not [
            issue for issue in decision_store.doctor_issues(conn)
            if issue["kind"] in {"stale_identity", "stale_snapshot"}
        ]

        replacement = source.with_suffix(".replacement")
        replacement.write_bytes(source.read_bytes())
        replacement.replace(source)
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        issues = decision_store.doctor_issues(conn)
        assert any(
            issue["kind"] == "stale_identity" and issue["file_id"] == file_id
            for issue in issues
        )
    finally:
        conn.close()
