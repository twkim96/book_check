import json
import os
import zipfile

import decision_store
import duplicate_auditor
import mutation_io
import pytest
from text_preview import ReadBudget


def _write_epub(path, body, *, compression, timestamp):
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        info = zipfile.ZipInfo("OEBPS/chapter.xhtml", date_time=timestamp)
        info.compress_type = compression
        archive.writestr(info, body)


def _write_metadata_repacked_epub(path, *, title, bookmark=False, reverse_spine=False):
    opf = f"""<?xml version='1.0' encoding='utf-8'?>
<package version='2.0' unique-identifier='BookId'
 xmlns='http://www.idpf.org/2007/opf'
 xmlns:dc='http://purl.org/dc/elements/1.1/'>
  <metadata><dc:identifier id='BookId'>urn:uuid:test</dc:identifier>
    <dc:title>{title}</dc:title><dc:date>2026-07-28</dc:date></metadata>
  <manifest><item id='one' href='one.xhtml' media-type='application/xhtml+xml'/>
    <item id='two' href='two.xhtml' media-type='application/xhtml+xml'/></manifest>
  <spine>{"<itemref idref='two'/><itemref idref='one'/>" if reverse_spine else "<itemref idref='one'/><itemref idref='two'/>"}</spine>
</package>""".encode("utf-8")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/one.xhtml", b"same first chapter")
        archive.writestr("OEBPS/two.xhtml", b"same second chapter")
        if bookmark:
            archive.writestr("META-INF/calibre_bookmarks.txt", b"reading position")


def _write_index(path, house, names):
    path.write_text(json.dumps({
        "version": 2,
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


def _args(index, house, temp, *extra):
    return duplicate_auditor.build_parser().parse_args([
        "--index", str(index), "--house", str(house), "--temp", str(temp),
        "--house-only", "--same-coordinate-only", *extra,
    ])


def _general_args(index, house, temp, *extra):
    return duplicate_auditor.build_parser().parse_args([
        "--index", str(index), "--house", str(house), "--temp", str(temp),
        *extra,
    ])


def test_repacked_epub_is_compared_by_internal_content(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    names = ["합성작품 1-10화 [작가A].epub", "합성작품 01-010화 [작가B].epub"]
    _write_epub(
        house / names[0], b"same chapter",
        compression=zipfile.ZIP_STORED, timestamp=(2020, 1, 1, 0, 0, 0),
    )
    _write_epub(
        house / names[1], b"same chapter",
        compression=zipfile.ZIP_DEFLATED, timestamp=(2025, 1, 1, 0, 0, 0),
    )
    index = tmp_path / "file_index.json"
    _write_index(index, house, names)

    args = _args(index, house, temp)
    progress_events = []
    args.progress_callback = progress_events.append
    report = duplicate_auditor.run_audit(args)

    assert report.completed is True
    assert report.stats["unique_candidate_files"] == 2
    assert report.results[0]["classification"] == "epub_equivalent"
    assert report.results[0]["evidence"]["left_raw_sha256"] != \
        report.results[0]["evidence"]["right_raw_sha256"]
    assert progress_events[0] == {
        "audit_phase": "epub_analysis",
        "completed": 0,
        "total": 2,
        "read_bytes": 0,
    }
    assert any(
        event["audit_phase"] == "epub_analysis"
        and event["completed"] == event["total"] == 2
        for event in progress_events
    )
    assert any(
        event["audit_phase"] == "pair_classification"
        and event["completed"] == event["total"] == 1
        for event in progress_events
    )


def test_epub_reading_payload_ignores_only_metadata_and_bookmark_state(tmp_path):
    first = tmp_path / "first.epub"
    second = tmp_path / "second.epub"
    reordered = tmp_path / "reordered.epub"
    _write_metadata_repacked_epub(first, title="표시 제목 A")
    _write_metadata_repacked_epub(second, title="표시 제목 B", bookmark=True)
    _write_metadata_repacked_epub(
        reordered, title="표시 제목 B", bookmark=True, reverse_spine=True
    )

    strict_first = mutation_io.inspect_epub_content(first)
    strict_second = mutation_io.inspect_epub_content(second)
    payload_first = mutation_io.inspect_epub_reading_payload(first)
    payload_second = mutation_io.inspect_epub_reading_payload(second)
    payload_reordered = mutation_io.inspect_epub_reading_payload(reordered)

    assert strict_first.content_sha256 != strict_second.content_sha256
    assert payload_first.content_sha256 == payload_second.content_sha256
    assert payload_first.content_sha256 != payload_reordered.content_sha256


def test_auditor_classifies_metadata_only_epub_repack_as_equivalent(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    names = ["메타 재포장 작품 1권.epub", "메타 재포장 작품 01권.epub"]
    _write_metadata_repacked_epub(house / names[0], title="작품")
    _write_metadata_repacked_epub(
        house / names[1], title="작품 1", bookmark=True
    )
    index = tmp_path / "file_index.json"
    _write_index(index, house, names)

    report = duplicate_auditor.run_audit(_args(index, house, temp))

    assert report.completed is True
    assert report.results[0]["classification"] == "epub_equivalent"
    assert report.results[0]["evidence"]["epub_equivalence_mode"] == "reading_payload"


def test_open_review_is_refreshed_when_same_fingerprints_gain_stronger_proof(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    names = ["검토 갱신 작품 3권.epub", "검토 갱신 작품 03권.epub"]
    _write_metadata_repacked_epub(house / names[0], title="검토 갱신 작품")
    _write_metadata_repacked_epub(
        house / names[1], title="검토 갱신 작품 3", bookmark=True
    )
    index = tmp_path / "file_index.json"
    _write_index(index, house, names)
    state_db = tmp_path / "state.sqlite3"
    def audit_args():
        return _general_args(
            index, house, temp,
            "--house-only", "--same-coordinate-only", "--state-db", str(state_db),
        )

    initial = duplicate_auditor.run_audit(audit_args())
    assert initial.results[0]["classification"] == "epub_equivalent"
    conn = decision_store.connect_state_db(state_db)
    try:
        row = conn.execute(
            "SELECT review_id FROM review_items "
            "WHERE state IN ('pending', 'deferred')"
        ).fetchone()
        assert row is not None
        conn.execute(
            """
            UPDATE review_items
            SET classification = 'metadata_only',
                evidence_json = '{"epub_reading_payload_error":"body_budget_exhausted"}'
            WHERE review_id = ?
            """,
            (row["review_id"],),
        )
        conn.commit()
    finally:
        conn.close()

    refreshed = duplicate_auditor.run_audit(audit_args())

    assert refreshed.results[0]["classification"] == "epub_equivalent"
    assert refreshed.stats["pair_cache_hits"] == 1
    assert refreshed.stats["review_items_refreshed"] == 1
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        rows = conn.execute(
            """
            SELECT classification, evidence_json FROM review_items
            WHERE state IN ('pending', 'deferred')
            """
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["classification"] == "epub_equivalent"
        assert json.loads(rows[0]["evidence_json"])["epub_equivalence_mode"] == \
            "reading_payload"
    finally:
        conn.close()


def test_epub_payload_budget_deferral_is_not_cached_as_metadata_only(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    names = ["예산 재포장 작품 1권.epub", "예산 재포장 작품 01권.epub"]
    _write_metadata_repacked_epub(house / names[0], title="작품")
    _write_metadata_repacked_epub(
        house / names[1], title="작품 1", bookmark=True
    )
    index = tmp_path / "file_index.json"
    _write_index(index, house, names)
    state_db = tmp_path / "state.sqlite3"

    limited = duplicate_auditor.run_audit(_general_args(
        index, house, temp,
        "--house-only", "--same-coordinate-only",
        "--state-db", str(state_db), "--max-read-bytes", "4KiB",
    ))

    assert limited.completed is False
    assert "body_budget_exhausted" in limited.stop_reasons
    assert limited.results[0]["classification"] == "body_budget_exhausted"

    retried = duplicate_auditor.run_audit(_general_args(
        index, house, temp,
        "--house-only", "--same-coordinate-only", "--state-db", str(state_db),
    ))

    assert retried.completed is True
    assert retried.results[0]["classification"] == "epub_equivalent"
    assert retried.results[0]["evidence"]["epub_equivalence_mode"] == "reading_payload"


def test_candidate_file_limit_fails_closed_before_unbounded_read(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    names = []
    for number in range(3):
        pair = [
            f"제한작품{number} 1-10화 [작가A].epub",
            f"제한작품{number} 01-010화 [작가B].epub",
        ]
        for name in pair:
            _write_epub(
                house / name, f"chapter {number}".encode(),
                compression=zipfile.ZIP_DEFLATED,
                timestamp=(2024, 1, 1, 0, 0, 0),
            )
        names.extend(pair)
    index = tmp_path / "file_index.json"
    _write_index(index, house, names)

    report = duplicate_auditor.run_audit(_args(
        index, house, temp, "--max-candidate-files", "4"
    ))

    assert report.completed is False
    assert "candidate_file_limit" in report.stop_reasons
    assert report.stats["unique_candidate_files"] <= 4
    assert report.stats["coverage_counts"]["candidate_file_limit_deferred_pairs"] == 1


def test_epub_audit_counts_raw_and_uncompressed_reads(tmp_path):
    path = tmp_path / "읽기 집계.epub"
    body = b"budgeted chapter"
    _write_epub(
        path,
        body,
        compression=zipfile.ZIP_STORED,
        timestamp=(2024, 1, 1, 0, 0, 0),
    )
    budget = ReadBudget(max_bytes=1024 * 1024)

    evidence = mutation_io.inspect_epub_content(
        path, max_file_bytes=1024 * 1024, budget=budget
    )

    assert budget.read_bytes == path.stat().st_size + evidence.uncompressed_size


def test_epub_file_limit_is_checked_before_raw_hash(tmp_path, monkeypatch):
    path = tmp_path / "크기 제한.epub"
    _write_epub(
        path,
        b"chapter",
        compression=zipfile.ZIP_STORED,
        timestamp=(2024, 1, 1, 0, 0, 0),
    )

    def unexpected_hash(_fd):
        raise AssertionError("raw hash must not run above max_file_bytes")

    monkeypatch.setattr(mutation_io, "_hash_fd", unexpected_hash)
    with pytest.raises(RuntimeError, match="EPUB file limit exceeded"):
        mutation_io.inspect_epub_content(
            path, max_file_bytes=path.stat().st_size - 1
        )


def test_corrupt_epub_candidate_makes_audit_incomplete(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    names = ["손상 작품 [작가A].epub", "손상 작품 [작가B].epub"]
    for name in names:
        (house / name).write_bytes(b"not a zip archive")
    index = tmp_path / "file_index.json"
    _write_index(index, house, names)

    report = duplicate_auditor.run_audit(_args(index, house, temp))

    assert report.completed is False
    assert "epub_analysis_error" in report.stop_reasons
    assert report.stats["classification_counts"] == {"metadata_only": 1}


def test_epub_limit_semantics_use_new_cache_generation():
    assert duplicate_auditor.FINGERPRINT_VERSION == "5"
    assert duplicate_auditor.FINGERPRINT_POLICY_VERSION == "1.4.2"
    assert duplicate_auditor.FINGERPRINT_NORMALIZER_COMPAT_VERSION == "1.3.0"
    assert duplicate_auditor.PAIR_POLICY_VERSION == "1.4.2"
    assert duplicate_auditor.PAIR_NORMALIZER_COMPAT_VERSION == "1.3.0"
    assert duplicate_auditor.AUDITOR_VERSION == "1.4.6"


def _txt_cache_fixture(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    names = ["정책 캐시 1-100.txt", "정책 캐시 1-100 완.txt"]
    body = "fingerprint policy 본문입니다. " * 500
    for name in names:
        (house / name).write_text(body, encoding="utf-8")
    index = tmp_path / "file_index.json"
    _write_index(index, house, names)
    state_db = tmp_path / "state.sqlite3"
    return house, temp, index, state_db


def test_release_fingerprint_and_pair_cache_versions_are_independent(
    tmp_path, monkeypatch,
):
    house, temp, index, state_db = _txt_cache_fixture(tmp_path)
    args = _general_args(
        index, house, temp, "--house-only", "--state-db", str(state_db)
    )
    analysis_hash = duplicate_auditor._analysis_policy_hash(args)
    pair_hash = duplicate_auditor._pair_configuration_hash(args)
    duplicate_auditor.run_audit(args)

    monkeypatch.setattr(duplicate_auditor, "AUDITOR_VERSION", "1.4.6")
    warm = duplicate_auditor.run_audit(args)

    assert duplicate_auditor._analysis_policy_hash(args) == analysis_hash
    assert duplicate_auditor._pair_configuration_hash(args) == pair_hash
    assert warm.stats["fingerprint_cache_hits"] == 2
    assert warm.stats["fingerprint_cache_misses"] == 0
    assert warm.stats["pair_cache_hits"] == 1
    assert warm.stats["actual_read_bytes"] == 0

    monkeypatch.setattr(duplicate_auditor, "NORMALIZER_VERSION", "1.3.2")
    filename_parser_changed = duplicate_auditor.run_audit(args)

    assert duplicate_auditor._analysis_policy_hash(args) == analysis_hash
    assert duplicate_auditor._pair_configuration_hash(args) == pair_hash
    assert filename_parser_changed.stats["fingerprint_cache_hits"] == 2
    assert filename_parser_changed.stats["fingerprint_cache_misses"] == 0
    assert filename_parser_changed.stats["pair_cache_hits"] == 1
    assert filename_parser_changed.stats["actual_read_bytes"] == 0

    monkeypatch.setattr(duplicate_auditor, "PAIR_POLICY_VERSION", "1.4.3")
    pair_changed = duplicate_auditor.run_audit(args)

    assert duplicate_auditor._analysis_policy_hash(args) == analysis_hash
    assert duplicate_auditor._pair_configuration_hash(args) != pair_hash
    assert pair_changed.stats["fingerprint_cache_hits"] == 2
    assert pair_changed.stats["fingerprint_cache_misses"] == 0
    assert pair_changed.stats["pair_cache_hits"] == 0
    assert pair_changed.stats["actual_read_bytes"] == 0


def test_warm_audit_skips_unchanged_house_metadata_reconcile(
    tmp_path, monkeypatch,
):
    house, temp, index, state_db = _txt_cache_fixture(tmp_path)
    args = _general_args(
        index, house, temp, "--house-only", "--state-db", str(state_db)
    )
    cold = duplicate_auditor.run_audit(args)
    calls = []
    original = decision_store.reconcile_file_metadata

    def tracked_reconcile(*call_args, **call_kwargs):
        calls.append(call_args[1])
        return original(*call_args, **call_kwargs)

    monkeypatch.setattr(
        decision_store, "reconcile_file_metadata", tracked_reconcile
    )
    warm = duplicate_auditor.run_audit(args)

    assert cold.stats["metadata_reconcile_writes"] == 2
    assert warm.stats["metadata_reconcile_skips"] == 2
    assert warm.stats["metadata_reconcile_writes"] == 0
    assert calls == []


def test_warm_audit_reconciles_only_the_changed_house_entry(tmp_path):
    house, temp, index, state_db = _txt_cache_fixture(tmp_path)
    args = _general_args(
        index, house, temp, "--house-only", "--state-db", str(state_db)
    )
    duplicate_auditor.run_audit(args)
    names = sorted(path.name for path in house.iterdir())
    changed = house / names[-1]
    changed.write_text(
        changed.read_text(encoding="utf-8") + "변경 본문",
        encoding="utf-8",
    )
    _write_index(index, house, names)

    refreshed = duplicate_auditor.run_audit(args)

    assert refreshed.stats["metadata_reconcile_skips"] == 1
    assert refreshed.stats["metadata_reconcile_writes"] == 1
    assert refreshed.stats["fingerprint_cache_hits"] == 1
    assert refreshed.stats["fingerprint_cache_misses"] == 1


def test_full_sweep_backfills_cross_core_txt_and_warm_run_reuses_cache(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    names = ["그리스의 사는법 1-180완.txt", "그리스의 용병 1-180완.txt"]
    body = "전역 fingerprint 동일 본문" * 400
    (house / names[0]).write_text(body, encoding="utf-8")
    (house / names[1]).write_text(" \n".join(body), encoding="utf-8")
    index = tmp_path / "file_index.json"
    _write_index(index, house, names)
    state_db = tmp_path / "state.sqlite3"

    cold = duplicate_auditor.run_audit(_general_args(
        index, house, temp,
        "--house-only", "--state-db", str(state_db),
        "--full-fingerprint-sweep",
    ))

    assert cold.completed is True
    assert cold.stats["full_fingerprint_sweep_eligible_files"] == 2
    assert cold.stats["full_fingerprint_sweep_analyzed_files"] == 2
    assert cold.stats["full_fingerprint_sweep_failed_files"] == 0
    assert cold.stats["global_fingerprint_pairs"] == 1
    assert cold.results[0]["classification"] == "text_equivalent"
    assert "global_normalized_sha256" in cold.results[0]["candidate_reasons"]

    warm = duplicate_auditor.run_audit(_general_args(
        index, house, temp, "--house-only", "--state-db", str(state_db)
    ))

    assert warm.completed is True
    assert warm.stats["full_fingerprint_sweep_requested"] is False
    assert warm.stats["fingerprint_cache_hits"] >= 2
    assert warm.stats["actual_read_bytes"] == 0
    assert warm.results[0]["classification"] == "text_equivalent"
    assert "global_normalized_sha256" in warm.results[0]["candidate_reasons"]


def test_default_run_fingerprints_new_temp_txt_against_backfilled_house(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    house_name = "퍼펙트 메이드 01권-06권완.txt"
    body = "신규 temp exact join 본문" * 400
    (house / house_name).write_text(body, encoding="utf-8")
    index = tmp_path / "file_index.json"
    _write_index(index, house, [house_name])
    state_db = tmp_path / "state.sqlite3"
    duplicate_auditor.run_audit(_general_args(
        index, house, temp,
        "--house-only", "--state-db", str(state_db),
        "--full-fingerprint-sweep",
    ))

    incoming = temp / "최강의하녀.txt"
    incoming.write_text(" \n".join(body), encoding="utf-8")
    report = duplicate_auditor.run_audit(_general_args(
        index, house, temp, "--state-db", str(state_db)
    ))

    pair = next(
        result for result in report.results
        if {result["left"]["name"], result["right"]["name"]}
        == {house_name, incoming.name}
    )
    assert pair["classification"] == "text_equivalent"
    assert "global_normalized_sha256" in pair["candidate_reasons"]
    assert report.stats["temp_fingerprint_eligible_files"] == 1
    assert report.stats["temp_fingerprint_analyzed_files"] == 1
    assert report.stats["temp_fingerprint_failed_files"] == 0


def test_read_only_default_run_uses_backfilled_house_without_db_writes(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    house_name = "서로 다른 보관 제목.txt"
    incoming_name = "완전히 별개인 신규 제목.txt"
    body = "read only global fingerprint join" * 400
    (house / house_name).write_text(body, encoding="utf-8")
    index = tmp_path / "file_index.json"
    _write_index(index, house, [house_name])
    state_db = tmp_path / "state.sqlite3"
    duplicate_auditor.run_audit(_general_args(
        index, house, temp,
        "--house-only", "--state-db", str(state_db),
        "--full-fingerprint-sweep",
    ))
    (temp / incoming_name).write_text(" \n".join(body), encoding="utf-8")
    before = state_db.read_bytes()
    args = _general_args(index, house, temp, "--state-db", str(state_db))
    args.cache_write = False

    report = duplicate_auditor.run_audit(args)

    pair = next(
        result for result in report.results
        if {result["left"]["name"], result["right"]["name"]}
        == {house_name, incoming_name}
    )
    assert report.completed is True
    assert pair["classification"] == "text_equivalent"
    assert "global_normalized_sha256" in pair["candidate_reasons"]
    assert report.stats["fingerprint_cache_hits"] >= 1
    assert state_db.read_bytes() == before


def test_default_run_fingerprints_new_temp_epub_against_backfilled_house(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    house_name = "잃고 나서야 깨달았다 1-184완.epub"
    incoming_name = "로판 잃고 나서야 깨달았다 1-184완.epub"
    body = b"same global epub content"
    _write_epub(
        house / house_name, body,
        compression=zipfile.ZIP_STORED, timestamp=(2020, 1, 1, 0, 0, 0),
    )
    index = tmp_path / "file_index.json"
    _write_index(index, house, [house_name])
    state_db = tmp_path / "state.sqlite3"
    duplicate_auditor.run_audit(_general_args(
        index, house, temp,
        "--house-only", "--state-db", str(state_db),
        "--full-fingerprint-sweep",
    ))
    _write_epub(
        temp / incoming_name, body,
        compression=zipfile.ZIP_DEFLATED, timestamp=(2025, 1, 1, 0, 0, 0),
    )

    report = duplicate_auditor.run_audit(_general_args(
        index, house, temp, "--state-db", str(state_db)
    ))

    pair = next(
        result for result in report.results
        if {result["left"]["name"], result["right"]["name"]}
        == {house_name, incoming_name}
    )
    assert pair["classification"] == "epub_equivalent"
    assert "global_epub_content_sha256" in pair["candidate_reasons"]
    assert report.stats["temp_fingerprint_analyzed_files"] == 1


def test_front_mismatch_continues_to_bounded_internal_tail_review(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    names = ["훈수 두는 천마님 1-202완 [A].txt", "훈수 두는 천마님 1-202완 [B].txt"]
    middle = "".join(f"{number:06d}고유중간문장" for number in range(1200))
    tail = "".join(f"고유한결말{number:05d}" for number in range(800))
    (house / names[0]).write_text(("첫업로드헤더" * 500) + middle + tail, encoding="utf-8")
    (house / names[1]).write_text(("둘업로드헤더" * 500) + middle + tail, encoding="utf-8")
    index = tmp_path / "file_index.json"
    _write_index(index, house, names)

    report = duplicate_auditor.run_audit(_general_args(
        index, house, temp, "--house-only"
    ))

    result = report.results[0]
    assert result["classification"] in {"near_identical", "contained_version"}
    assert result["evidence"]["front_anchor_equal"] is False
    assert result["evidence"]["tail_anchor_equal"] is True
    assert result["classification"] not in {
        "text_equivalent", "epub_equivalent", "marker_recheck"
    }
    assert report.stats["actual_read_bytes"] <= report.stats["estimated_max_read_bytes"]


def test_three_character_core_uses_adaptive_bigram_candidate_only(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    names = ["가나다 1-10화.txt", "가나마 1-10화.txt"]
    for name in names:
        (house / name).write_text("서로 다른 본문" * 300, encoding="utf-8")
    index = tmp_path / "file_index.json"
    _write_index(index, house, names)

    report = duplicate_auditor.run_audit(_general_args(
        index, house, temp, "--house-only", "--metadata-only"
    ))

    assert report.stats["candidate_pairs"] == 1
    assert report.results[0]["classification"] == "metadata_only"
    assert "near_core_adaptive" in report.results[0]["candidate_reasons"]
    assert report.stats["coverage_counts"]["adaptive_short_gram_entries"] == 2


def test_global_hash_group_cap_fails_closed(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    names = [f"서로다른제목{number}.txt" for number in range(3)]
    for name in names:
        (house / name).write_text("동일 전역 본문" * 300, encoding="utf-8")
    index = tmp_path / "file_index.json"
    _write_index(index, house, names)
    state_db = tmp_path / "state.sqlite3"

    report = duplicate_auditor.run_audit(_general_args(
        index, house, temp,
        "--house-only", "--state-db", str(state_db),
        "--full-fingerprint-sweep", "--max-global-hash-group-pairs", "2",
    ))

    assert report.completed is False
    assert "global_hash_group_overflow" in report.stop_reasons
    assert report.stats["coverage_counts"]["global_hash_group_unprocessed_pairs"] >= 3


def test_full_sweep_requires_versioned_state_db(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    index = tmp_path / "file_index.json"
    _write_index(index, house, [])

    with pytest.raises(ValueError, match="requires --state-db"):
        duplicate_auditor.run_audit(_general_args(
            index, house, temp, "--house-only", "--full-fingerprint-sweep"
        ))


def test_temp_preparation_and_candidate_analysis_share_read_budget(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    names = ["예산작품 [A].txt", "예산작품 [B].txt"]
    (house / names[0]).write_text("a" * 600, encoding="utf-8")
    (house / names[1]).write_text("b" * 600, encoding="utf-8")
    (temp / "무관한신규파일.txt").write_text("x" * 900, encoding="utf-8")
    index = tmp_path / "file_index.json"
    _write_index(index, house, names)

    report = duplicate_auditor.run_audit(_general_args(
        index, house, temp, "--max-read-bytes", "1KiB"
    ))

    assert report.completed is False
    assert "body_budget_exhausted" in report.stop_reasons
    assert report.stats["temp_fingerprint_read_bytes"] == 900
    assert report.stats["actual_read_bytes"] <= 1024


def test_preloaded_analysis_replacement_is_stale_and_drops_equivalence(
    tmp_path, monkeypatch,
):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    names = ["서로무관한캐시A.txt", "별개의캐시B.txt"]
    body = "A" * 4096
    for name in names:
        (house / name).write_text(body, encoding="utf-8")
    index = tmp_path / "file_index.json"
    _write_index(index, house, names)
    state_db = tmp_path / "state.sqlite3"

    cold = duplicate_auditor.run_audit(_general_args(
        index,
        house,
        temp,
        "--house-only",
        "--state-db",
        str(state_db),
        "--full-fingerprint-sweep",
    ))
    assert cold.completed is True
    assert any(
        result["classification"] == "text_equivalent"
        for result in cold.results
    )

    target = house / names[1]
    original_generate = duplicate_auditor.generate_fingerprint_candidates
    replaced = False

    def replace_after_preload(entries, analyses, config):
        nonlocal replaced
        if not replaced:
            before = target.stat()
            replacement = target.with_suffix(".replacement")
            replacement.write_bytes(b"Z" * before.st_size)
            replacement.replace(target)
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
            replaced = True
        return original_generate(entries, analyses, config)

    monkeypatch.setattr(
        duplicate_auditor,
        "generate_fingerprint_candidates",
        replace_after_preload,
    )
    warm = duplicate_auditor.run_audit(_general_args(
        index, house, temp, "--house-only", "--state-db", str(state_db)
    ))

    assert replaced is True
    assert warm.completed is False
    assert {"stale", "stale_input"} & set(warm.stop_reasons)
    assert all(
        result["classification"] != "text_equivalent"
        for result in warm.results
    )
    assert any(
        change["path"] == str(target)
        for change in warm.stats["input_changes"]
    )


def test_managed_representative_scan_uses_shared_read_budget(
    tmp_path, monkeypatch,
):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    representative = house / "예산관리대표.txt"
    incoming = temp / "무관한신규후보.txt"
    body = "R" * 900
    representative.write_text(body, encoding="utf-8")
    incoming.write_text(body, encoding="utf-8")
    index = tmp_path / "file_index.json"
    _write_index(index, house, [representative.name])
    state_db = tmp_path / "state.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            row = decision_store.reconcile_file_metadata(
                conn, representative, source="house"
            )
            work_id = conn.execute(
                "INSERT INTO works(display_title) VALUES ('예산 관리 대표')"
            ).lastrowid
            variant_id = conn.execute(
                "INSERT INTO variants(work_bucket_id, variant_kind) "
                "VALUES (?, 'base')",
                (work_id,),
            ).lastrowid
            conn.execute(
                """
                UPDATE files SET variant_id = ?, assignment_state = 'managed',
                    assignment_origin = 'human_decision', protected = 1
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

    def unbudgeted_read_forbidden(_path):
        raise AssertionError("managed representative bypassed ReadBudget")

    monkeypatch.setattr(
        mutation_io, "inspect_normalized_text", unbudgeted_read_forbidden
    )
    report = duplicate_auditor.run_audit(_general_args(
        index,
        house,
        temp,
        "--state-db",
        str(state_db),
        "--max-read-bytes",
        "1KiB",
    ))

    assert report.completed is False
    assert "body_budget_exhausted" in report.stop_reasons
    assert report.stats["actual_read_bytes"] == 900
    assert report.stats["actual_read_bytes"] <= 1024
    assert report.stats["managed_representative_fingerprint_read_bytes"] == 900
    assert all(
        result["classification"] != "text_equivalent"
        for result in report.results
    )
