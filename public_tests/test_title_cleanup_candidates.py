import hashlib
from pathlib import Path

import decision_store
import pytest
from normalizer import analyze_name, extract_catalog_query_title
from title_cleanup_candidates import audit_candidates
from title_cleanup_rules import apply_title_cleanup_rules


def _query_after(name):
    proposal = apply_title_cleanup_rules(name)
    return proposal, extract_catalog_query_title(proposal.candidate_name)


def test_closed_cleanup_rules_cover_confirmed_127_shapes():
    cases = {
        "0살 아기부터 슈퍼부자ⓒ김우진1101 0-150 完.txt": "0살 아기부터 슈퍼부자",
        "19) 아포칼립스인데 나만 너무 쉽다 1-537.txt": "아포칼립스인데 나만 너무 쉽다",
        "sss급 마왕이지만 알고보니 엑스트라 ep001-148 (완) noPic ver.epub": "sss급 마왕이지만 알고보니 엑스트라",
        "무협) 당문전생 1-206화 완결.txt": "당문전생",
        "S급들이 내게 집착한다 RS (티모원딜).epub": "S급들이 내게 집착한다",
        "검황 이계정벌하다 1 10 [완] - 한가.txt": "검황 이계정벌하다",
        "메인 히로인들이 나를 죽이려 한다 1 456 @Seira21.epub": "메인 히로인들이 나를 죽이려 한다",
        "멸망한 세계의 전승자 총206화 완.epub": "멸망한 세계의 전승자",
        "서랍 속 청개구리完⓳ [디키탈리스].txt": "서랍 속 청개구리",
        "내 가족 정령들%2540탁목조 -339%2528완%2529.txt": "내 가족 정령들",
        "마법 아카데미의 육체파 천재 ＠이동열 001-197 완.txt": "마법 아카데미의 육체파 천재",
        "날 좀 데려가 줘 $공금$직.txt": "날 좀 데려가 줘",
        "NWN [누루파파] 뇌령검제(雷影劍帝)(完) noPic ver.epub": "뇌령검제",
        "환생밀정 찰나회귀ⓖ 1-264 完.txt": "환생밀정 찰나회귀",
        "노가다의 신完1~252.txt": "노가다의 신",
        "Re 제로부터 시작하는 이세계 생활 10 (나가츠키 탓페이)_dup_1.epub": "Re 제로부터 시작하는 이세계 생활",
    }
    for name, expected_query in cases.items():
        proposal, query = _query_after(name)
        assert proposal.rule_ids, name
        assert query == expected_query, name


def test_cleanup_filename_preserves_real_author_tags_but_drops_uploader_noise():
    copyright = apply_title_cleanup_rules(
        "0살 아기부터 슈퍼부자ⓒ김우진1101 0-150 完.txt"
    )
    assert copyright.candidate_name == (
        "0살 아기부터 슈퍼부자 [ⓒ김우진1101] 0-150 完.txt"
    )
    assert analyze_name(copyright.candidate_name)["author"] == "김우진1101"

    with_uploader_bracket = apply_title_cleanup_rules(
        "가족이 많을수록 강해져 ⓒ김원두 1-247完 [뽀].txt"
    )
    assert analyze_name(with_uploader_bracket.candidate_name)["author"] == "김원두"

    trailing_author = apply_title_cleanup_rules(
        "검황 이계정벌하다 1 10 [완] - 한가.txt"
    )
    assert trailing_author.candidate_name == "검황 이계정벌하다 1-10 완 [한가].txt"
    assert analyze_name(trailing_author.candidate_name)["author"] == "한가"

    assert apply_title_cleanup_rules(
        "메인 히로인들이 나를 죽이려 한다 1 456 @Seira21.epub"
    ).candidate_name == "메인 히로인들이 나를 죽이려 한다 1-456.epub"
    assert apply_title_cleanup_rules(
        "S급들이 내게 집착한다 RS (티모원딜).epub"
    ).candidate_name == "S급들이 내게 집착한다.epub"


def test_bare_volume_before_author_keeps_volume_coordinate():
    proposal, query = _query_after(
        "전생한 대성녀는 성녀임을 숨긴다 6 (토야).epub"
    )
    info = analyze_name(proposal.candidate_name)
    assert query == "전생한 대성녀는 성녀임을 숨긴다"
    assert info["volume_number"] == (None, 6)
    assert info["unit"] == "권"
    assert info["start_number"] == 6
    assert info["end_number"] == 6


def test_total_episode_marker_becomes_full_range_not_single_episode():
    proposal, query = _query_after("멸망한 세계의 전승자 총206화 완.epub")
    info = analyze_name(proposal.candidate_name)
    assert query == "멸망한 세계의 전승자"
    assert info["start_number"] == 1
    assert info["end_number"] == 206
    assert info["effective_max"] == 206
    assert info["unit"] == "화"


def test_cleanup_boundaries_preserve_ambiguous_or_real_title_tokens():
    names = [
        "회귀 1988.txt",
        "서울 1988 (작가).txt",
        "19호실의 비밀 1-50화 완결.txt",
        "NWN연대기 1-100 완.txt",
        "작품 속 RS (부제).txt",
        "작품 noPic version.epub",
        "녹슨 열차 개정판4외완⓳.txt",
        "시장통 재벌처가 씹어먹다 256＋24 完.txt",
        "무인도 표류일지 R 307(완).txt",
        "악역을 구하고 떠나려 합니다 rd19 외포완.txt",
    ]
    for name in names:
        proposal = apply_title_cleanup_rules(name)
        assert proposal.candidate_name == name, name
        assert not proposal.rule_ids, name


def test_cleanup_rules_are_idempotent():
    name = "NWN 광마록 ep001-159(完) noPic ver_dup_2.epub"
    first = apply_title_cleanup_rules(name)
    second = apply_title_cleanup_rules(first.candidate_name)
    assert first.rule_ids
    assert second.candidate_name == first.candidate_name
    assert not second.rule_ids


def _add_catalog_file(conn, house, name, statuses, *, legacy_title=None, legacy_core=None):
    path = house / name
    path.write_text("후보 감사 본문", encoding="utf-8")
    with decision_store.transaction(conn):
        row = decision_store.reconcile_file_metadata(conn, path, source="house")
        analysis = conn.execute(
            "SELECT * FROM file_analysis WHERE file_id = ?", (row["file_id"],)
        ).fetchone()
        title_key = legacy_core or analysis["core_title"]
        query_title = legacy_title or analysis["catalog_query_title"]
        if legacy_title is not None or legacy_core is not None:
            conn.execute(
                """
                UPDATE file_analysis
                SET normalizer_version = '1.2.3', core_title = ?,
                    readable_title = ?, catalog_query_title = ?
                WHERE file_id = ?
                """,
                (title_key, query_title, query_title, row["file_id"]),
            )
        conn.execute(
            """
            INSERT INTO catalog_titles(
                title_key, display_title, query_title, normalizer_version
            ) VALUES (?, ?, ?, ?)
            """,
            (
                title_key, query_title, query_title,
                "1.2.3" if legacy_title is not None or legacy_core is not None
                else analysis["normalizer_version"],
            ),
        )
        for platform, status in statuses.items():
            conn.execute(
                "INSERT INTO catalog_platform_stats(title_key, platform, status) "
                "VALUES (?, ?, ?)",
                (title_key, platform, status),
            )
    return title_key


def test_read_only_audit_reports_zero_protected_diff_and_keeps_inputs(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    house.mkdir()
    conn = decision_store.initialize_state_db(state_db)
    try:
        _add_catalog_file(conn, house, "작품명ⓒ작가 1-100 完.txt", {
            "series": "not_found", "kakao": "not_found", "novelpia": "not_found",
        }, legacy_title="작품명ⓒ작가", legacy_core="작품명작가")
        _add_catalog_file(conn, house, "보호 작품 1-100 完.txt", {
            "series": "ok", "kakao": "not_found", "novelpia": "not_found",
        })
    finally:
        conn.close()

    before = hashlib.sha256(state_db.read_bytes()).hexdigest()
    report = audit_candidates(state_db, index_path=None)
    after = hashlib.sha256(state_db.read_bytes()).hexdigest()

    assert report["read_only"] is True
    assert report["input_unchanged"] is True
    assert report["integrity_check"] == "ok"
    assert report["combined"]["changed_source_keys"] == 1
    assert report["protected_diff_count"] == 0
    assert before == after


def test_read_only_audit_exposes_protected_source_diff(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    house.mkdir()
    conn = decision_store.initialize_state_db(state_db)
    try:
        _add_catalog_file(conn, house, "NWN 보호 제목 1-100 完.txt", {
            "series": "ok", "kakao": "not_found", "novelpia": "not_found",
        }, legacy_title="NWN 보호 제목", legacy_core="nwn보호제목")
    finally:
        conn.close()

    report = audit_candidates(state_db, index_path=None)
    assert report["protected_diff_count"] == 1
    assert report["combined"]["protected_source_diffs"] == 1
    assert report["protected_diffs"][0]["before_query_title"] == "NWN 보호 제목"
    assert report["protected_diffs"][0]["after_query_title"] == "보호 제목"


def test_metadata_sync_blocks_existing_target_before_any_rekey_write(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    house.mkdir()
    conn = decision_store.initialize_state_db(state_db)
    try:
        clean_key = _add_catalog_file(conn, house, "충돌 작품 1-100 完.txt", {
            "series": "ok", "kakao": "not_found", "novelpia": "not_found",
        })
        old_key = _add_catalog_file(
            conn,
            house,
            "충돌 작품ⓒ작가 1-100 完.epub",
            {"series": "not_found", "kakao": "not_found", "novelpia": "not_found"},
            legacy_title="충돌 작품ⓒ작가",
            legacy_core="충돌작품작가",
        )
        assert clean_key == "충돌작품"

        with pytest.raises(RuntimeError, match="dedup-before-catalog migration"):
            decision_store.sync_active_file_analysis(conn)

        legacy = conn.execute(
            "SELECT core_title, normalizer_version FROM file_analysis "
            "WHERE core_title = ?", (old_key,)
        ).fetchone()
        assert tuple(legacy) == (old_key, "1.2.3")
        assert conn.execute(
            "SELECT COUNT(*) FROM catalog_titles WHERE title_key IN (?, ?)",
            (clean_key, old_key),
        ).fetchone()[0] == 2
    finally:
        conn.close()
