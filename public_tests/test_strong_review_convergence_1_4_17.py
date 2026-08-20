from pathlib import Path

import decision_store
import dedup_mutations
import deduplicator
from dedup_episode_relation import classify_loose_title_upgrade_relation
from mutation_io import mutation_lock_for_roots
from text_preview import ReadBudget, analyze_text_file


def _add_text(
    conn,
    path: Path,
    source: str,
    body: str,
    *,
    assignment_state="unassigned",
):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    with decision_store.transaction(conn):
        row = decision_store.reconcile_file_metadata(
            conn, path, source=source
        )
    analysis = analyze_text_file(
        path, budget=ReadBudget(max_bytes=max(10_000_000, path.stat().st_size * 2))
    )
    stat_result = path.stat()
    with decision_store.transaction(conn):
        fingerprint_id = conn.execute(
            """
            INSERT INTO fingerprints(
                file_id, canonical_path, size, mtime_ns, normalizer_version,
                fingerprint_version, dev, ino, ctime_ns,
                raw_sha256, normalized_sha256, normalized_length,
                encoding, status, front_anchor, tail_anchor, anchors_json
            ) VALUES (?, ?, ?, ?, 'test-1.4.17', 'test-strong-review',
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')
            """,
            (
                row["file_id"], str(path), stat_result.st_size,
                stat_result.st_mtime_ns, stat_result.st_dev,
                stat_result.st_ino, stat_result.st_ctime_ns,
                analysis.raw_sha256, analysis.normalized_sha256,
                analysis.normalized_length, analysis.encoding,
                analysis.status, analysis.front_anchor, analysis.tail_anchor,
            ),
        ).lastrowid
        conn.execute(
            """
            UPDATE files
            SET current_fingerprint_id = ?, assignment_state = ?,
                assignment_origin = NULL, variant_id = NULL, protected = 0
            WHERE file_id = ?
            """,
            (fingerprint_id, assignment_state, row["file_id"]),
        )
    return {
        "file_id": row["file_id"],
        "fingerprint_id": fingerprint_id,
        "path": str(path),
    }


def _add_review(conn, left, right, classification):
    with decision_store.transaction(conn):
        return conn.execute(
            """
            INSERT INTO review_items(
                candidate_file_id, reference_file_id,
                left_fingerprint_id, right_fingerprint_id,
                classification, evidence_json
            ) VALUES (?, ?, ?, ?, ?, '{}')
            """,
            (
                left["file_id"], right["file_id"],
                left["fingerprint_id"], right["fingerprint_id"],
                classification,
            ),
        ).lastrowid


def _approve(conn, state_db, house, temp):
    backup = decision_store.backup_state_db(
        conn, state_db.parent / "before-strong-review.sqlite3"
    )
    decision_store.issue_actual_run_token(
        conn, str(backup), house_dir=house, temp_dir=temp
    )
    return decision_store.prepare_actual_run(state_db, house, temp)[0]


def test_compound_part_prefix_chain_selects_widest_declared_house_version(
    tmp_path,
):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        short_body = "동일한 1부와 2부 본문 " * 900
        long_body = short_body + ("추가 2부 회차 " * 300)
        shorter = _add_text(
            conn,
            house / "ㄱ" / "광룡이계전생 1-361 1부 완 2부1-105.txt",
            "house",
            short_body,
        )
        longer = _add_text(
            conn,
            house / "ㄱ" / "광룡이계전생 1-361 1부 완 2부 1-146 완.txt",
            "house",
            long_body,
        )
        review_id = _add_review(conn, shorter, longer, "contained_exact")
    finally:
        conn.close()

    with mutation_lock_for_roots(house, temp, "test-compound-contained"):
        conn = decision_store.connect_state_db(state_db)
        try:
            run_id = _approve(conn, state_db, house, temp)
        finally:
            conn.close()
        relation = {
            "classification": "contained_exact",
            "evidence": {
                "left_normalized_length": len("".join(short_body.split())),
                "right_normalized_length": len("".join(long_body.split())),
            },
            "left": {
                "file_id": shorter["file_id"],
                "path": shorter["path"],
                "name": Path(shorter["path"]).name,
                "source": "house",
                "core_title": "광룡이계전생",
                "author": None,
                "unit": "미상",
                "span_ambiguous": True,
                "assignment_state": "unassigned",
                "mutation_eligible": True,
                "protected": False,
                "representative": False,
                "variant_id": None,
            },
            "right": {
                "file_id": longer["file_id"],
                "path": longer["path"],
                "name": Path(longer["path"]).name,
                "source": "house",
                "core_title": "광룡이계전생",
                "author": None,
                "unit": "미상",
                "span_ambiguous": True,
                "assignment_state": "unassigned",
                "mutation_eligible": True,
                "protected": False,
                "representative": False,
                "variant_id": None,
            },
        }
        direction = deduplicator._contained_upgrade_direction(relation)
        assert direction is not None
        assert direction[0]["file_id"] == shorter["file_id"]
        assert direction[1]["file_id"] == longer["file_id"]

        from dedup_mutations import apply_contained_upgrade

        conn = decision_store.connect_state_db(state_db)
        try:
            result = apply_contained_upgrade(
                conn,
                review_id=review_id,
                shorter_file_id=shorter["file_id"],
                longer_file_id=longer["file_id"],
                quarantine_dir=temp / "trash_bin" / "superseded_versions",
                run_id=run_id,
                classification="contained_exact",
            )
            decision_store.finish_actual_run(conn, run_id, success=True)
            assert decision_store.doctor_issues(conn) == []
        finally:
            conn.close()

    assert Path(longer["path"]).is_file()
    assert not Path(shorter["path"]).exists()
    assert Path(result["quarantine_path"]).is_file()


def test_pending_queue_contained_exact_adopts_aggregate_suffix_version(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        short_body = "시장통 합성 본문 " * 1_000
        long_body = short_body + ("추가 외전 본문 " * 250)
        shorter = _add_text(
            conn,
            house / "ㅅ" / "시장통 작품 1-256 완.txt",
            "house",
            short_body,
        )
        longer = _add_text(
            conn,
            temp / "trash_bin" / "warning" / "시장통 작품 256＋24 完.txt",
            "queue",
            long_body,
            assignment_state="decision_required",
        )
        review_id = _add_review(conn, shorter, longer, "contained_exact")
    finally:
        conn.close()

    with mutation_lock_for_roots(house, temp, "test-queue-contained"):
        conn = decision_store.connect_state_db(state_db)
        try:
            run_id = _approve(conn, state_db, house, temp)
        finally:
            conn.close()
        records = deduplicator.cleanup_pending_queue_strong_reviews(
            str(state_db), str(house), str(temp), run_id
        )
        conn = decision_store.connect_state_db(state_db)
        try:
            review = conn.execute(
                "SELECT state FROM review_items WHERE review_id = ?",
                (review_id,),
            ).fetchone()
            adopted = conn.execute(
                "SELECT active, source, canonical_path FROM files WHERE file_id = ?",
                (longer["file_id"],),
            ).fetchone()
            retired = conn.execute(
                "SELECT active, source FROM files WHERE file_id = ?",
                (shorter["file_id"],),
            ).fetchone()
            decision_store.finish_actual_run(conn, run_id, success=True)
            assert decision_store.doctor_issues(conn) == []
        finally:
            conn.close()

    assert len(records) == 1
    assert records[0]["classification"] == "contained_exact"
    assert adopted["active"] == 1 and adopted["source"] == "house"
    assert Path(adopted["canonical_path"]).is_file()
    assert retired["active"] == 0 and retired["source"] == "quarantine"
    assert review["state"] == "superseded"


def test_pending_queue_ordered_match_discards_shorter_declared_copy(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        house_body = "".join(
            f"{index:05d} 귀촌 생활의 고유 본문 문장입니다.\n"
            for index in range(5_000)
        )
        queue_body = "".join(
            (
                f"{index:05d} 일부 교정된 귀촌 생활 문장입니다.\n"
                if index % 100 == 0
                else f"{index:05d} 귀촌 생활의 고유 본문 문장입니다.\n"
            )
            for index in range(5_000)
        )
        keep = _add_text(
            conn,
            house / "ㄷ" / "대마법사의 귀촌 생활 001~127 완결.txt",
            "house",
            house_body,
            assignment_state="decision_required",
        )
        discard = _add_text(
            conn,
            temp / "trash_bin" / "warning" / "대마법사의 귀촌 생활 1-5 완.txt",
            "queue",
            queue_body,
            assignment_state="decision_required",
        )
        review_id = _add_review(conn, discard, keep, "ordered_body_match")
    finally:
        conn.close()

    with mutation_lock_for_roots(house, temp, "test-queue-ordered"):
        conn = decision_store.connect_state_db(state_db)
        try:
            run_id = _approve(conn, state_db, house, temp)
        finally:
            conn.close()
        records = deduplicator.cleanup_pending_queue_strong_reviews(
            str(state_db), str(house), str(temp), run_id
        )
        conn = decision_store.connect_state_db(state_db)
        try:
            review = conn.execute(
                "SELECT state FROM review_items WHERE review_id = ?",
                (review_id,),
            ).fetchone()
            discarded = conn.execute(
                "SELECT active, source FROM files WHERE file_id = ?",
                (discard["file_id"],),
            ).fetchone()
            decision_store.finish_actual_run(conn, run_id, success=True)
            assert decision_store.doctor_issues(conn) == []
        finally:
            conn.close()

    assert len(records) == 1
    assert records[0]["classification"] == "ordered_body_match"
    assert records[0]["coverage_ppm"] >= 950_000
    assert discarded["active"] == 0 and discarded["source"] == "quarantine"
    assert Path(keep["path"]).is_file()
    assert review["state"] == "superseded"


def test_ordered_match_can_retire_only_the_loose_legacy_marked_side(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        clean_body = "".join(
            f"{index:05d} 현재 강한 중복 증거용 본문입니다.\n"
            for index in range(5_000)
        )
        marked_body = "".join(
            (
                f"{index:05d} 일부 교정된 중복 증거용 문장입니다.\n"
                if index % 200 == 0
                else f"{index:05d} 현재 강한 중복 증거용 본문입니다.\n"
            )
            for index in range(5_000)
        )
        marked = _add_text(
            conn,
            house / "ㅁ" / "마커 강한 작품 1-200 완〔P〕.txt",
            "house",
            marked_body,
            assignment_state="legacy_unresolved",
        )
        clean = _add_text(
            conn,
            house / "ㅁ" / "마커 강한 작품 1-200 완.txt",
            "house",
            clean_body,
        )
        review_id = _add_review(conn, marked, clean, "ordered_body_match")
    finally:
        conn.close()

    relation = {
        "classification": "ordered_body_match",
        "left": {
            "file_id": marked["file_id"], "path": marked["path"],
            "name": Path(marked["path"]).name, "source": "house",
            "ext": ".txt", "size": Path(marked["path"]).stat().st_size,
            "core_title": "마커강한작품", "author": None, "unit": "화",
            "complete": True, "span_ambiguous": False,
            "assignment_state": "legacy_unresolved",
            "mutation_eligible": False, "variant_id": None,
            "protected": False, "representative": False,
        },
        "right": {
            "file_id": clean["file_id"], "path": clean["path"],
            "name": Path(clean["path"]).name, "source": "house",
            "ext": ".txt", "size": Path(clean["path"]).stat().st_size,
            "core_title": "마커강한작품", "author": None, "unit": "화",
            "complete": True, "span_ambiguous": False,
            "assignment_state": "unassigned", "mutation_eligible": True,
            "variant_id": None, "protected": False, "representative": False,
        },
    }
    direction = deduplicator._ordered_body_direction(relation)
    assert direction is not None
    assert direction[0]["file_id"] == marked["file_id"]
    assert direction[1]["file_id"] == clean["file_id"]

    with mutation_lock_for_roots(house, temp, "test-marker-ordered"):
        conn = decision_store.connect_state_db(state_db)
        try:
            run_id = _approve(conn, state_db, house, temp)
        finally:
            conn.close()

        records = deduplicator.cleanup_pending_queue_strong_reviews(
            str(state_db), str(house), str(temp), run_id
        )
        conn = decision_store.connect_state_db(state_db)
        try:
            decision_store.finish_actual_run(conn, run_id, success=True)
            assert decision_store.doctor_issues(conn) == []
        finally:
            conn.close()

    assert len(records) == 1
    assert records[0]["review_id"] == review_id
    assert records[0]["coverage_ppm"] >= 950_000
    assert not Path(marked["path"]).exists()
    assert Path(clean["path"]).is_file()
    assert Path(records[0]["dest_path"]).is_file()


def test_ordered_match_never_uses_a_legacy_marker_as_keep(tmp_path):
    marked = {
        "name": "마커 보존 금지 1-300 완〔D2〕.txt",
        "path": str(tmp_path / "마커 보존 금지 1-300 완〔D2〕.txt"),
        "source": "house", "core_title": "마커보존금지", "author": None,
        "unit": "화", "complete": True, "span_ambiguous": False,
        "assignment_state": "legacy_unresolved", "mutation_eligible": False,
        "variant_id": None, "protected": False, "representative": False,
        "char_count": 300_000, "size": 300_000,
    }
    clean = {
        "name": "마커 보존 금지 1-200 완.txt",
        "path": str(tmp_path / "마커 보존 금지 1-200 완.txt"),
        "source": "house", "core_title": "마커보존금지", "author": None,
        "unit": "화", "complete": True, "span_ambiguous": False,
        "assignment_state": "unassigned", "mutation_eligible": True,
        "variant_id": None, "protected": False, "representative": False,
        "char_count": 200_000, "size": 200_000,
    }

    assert deduplicator._ordered_body_direction({
        "classification": "ordered_body_match",
        "left": marked,
        "right": clean,
        "evidence": {},
    }) is None


def test_failed_current_contained_proof_is_deferred_until_fingerprint_changes(
    tmp_path,
):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        shorter = _add_text(
            conn,
            house / "ㄱ" / "증명 부족 작품 1-100 완.txt",
            "house",
            "짧은 판본과 무관한 현재 본문\n" * 2_000,
        )
        longer = _add_text(
            conn,
            house / "ㄱ" / "증명 부족 작품 1-200 완.txt",
            "house",
            "긴 판본의 전혀 다른 현재 본문\n" * 4_000,
        )
        review_id = _add_review(conn, shorter, longer, "contained_version")
    finally:
        conn.close()

    assert deduplicator.count_actionable_pending_strong_reviews(state_db) == 1
    with mutation_lock_for_roots(house, temp, "test-contained-proof-deferred"):
        conn = decision_store.connect_state_db(state_db)
        try:
            run_id = _approve(conn, state_db, house, temp)
        finally:
            conn.close()
        records = deduplicator.cleanup_pending_queue_strong_reviews(
            str(state_db), str(house), str(temp), run_id
        )
        conn = decision_store.connect_state_db(state_db)
        try:
            row = conn.execute(
                "SELECT state, evidence_json FROM review_items WHERE review_id = ?",
                (review_id,),
            ).fetchone()
            decision_store.finish_actual_run(conn, run_id, success=True)
            assert decision_store.doctor_issues(conn) == []
        finally:
            conn.close()

    assert records == []
    assert row["state"] == "deferred"
    marker = __import__("json").loads(row["evidence_json"])[
        "current_proof_not_sufficient"
    ]
    assert marker["policy_version"] == deduplicator.STRONG_PROOF_POLICY_VERSION
    assert marker["error_type"] == "ContainedUpgradeNotProven"
    assert deduplicator.count_actionable_pending_strong_reviews(state_db) == 0


def test_near_identical_queue_copy_requires_bidirectional_95_percent(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        house_body = "".join(
            f"{index:05d} 양방향 강한 본문 고유 문장입니다.\n"
            for index in range(5_000)
        )
        queue_body = "".join(
            (
                f"{index:05d} 교정된 양방향 강한 본문 문장입니다.\n"
                if index % 200 == 0
                else f"{index:05d} 양방향 강한 본문 고유 문장입니다.\n"
            )
            for index in range(5_000)
        )
        keep = _add_text(
            conn,
            house / "ㅇ" / "양방향 강한 작품 1-200 완.txt",
            "house",
            house_body,
        )
        discard = _add_text(
            conn,
            temp / "trash_bin" / "warning" / "양방향 강한 작품 1-200 완.txt",
            "queue",
            queue_body,
        )
        review_id = _add_review(conn, discard, keep, "near_identical")
    finally:
        conn.close()

    assert deduplicator.count_actionable_pending_strong_reviews(state_db) == 1
    with mutation_lock_for_roots(house, temp, "test-near-bidirectional"):
        conn = decision_store.connect_state_db(state_db)
        try:
            run_id = _approve(conn, state_db, house, temp)
        finally:
            conn.close()
        records = deduplicator.cleanup_pending_queue_strong_reviews(
            str(state_db), str(house), str(temp), run_id
        )
        conn = decision_store.connect_state_db(state_db)
        try:
            row = conn.execute(
                "SELECT state FROM review_items WHERE review_id = ?",
                (review_id,),
            ).fetchone()
            decision_store.finish_actual_run(conn, run_id, success=True)
            assert decision_store.doctor_issues(conn) == []
        finally:
            conn.close()

    assert len(records) == 1
    assert records[0]["classification"] == "near_identical"
    assert records[0]["coverage_ppm"] >= 950_000
    assert not Path(discard["path"]).exists()
    assert Path(keep["path"]).is_file()
    assert Path(records[0]["dest_path"]).is_file()
    assert row["state"] == "superseded"


def test_same_run_longer_unresolved_queue_copy_converges_at_bidirectional_95(
    tmp_path,
):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    house_body = "".join(
        f"{index:05d} 분류명과 무관한 양방향 강한 본문입니다.\n"
        for index in range(5_000)
    )
    queue_body = "".join(
        (
            f"{index:05d} 교정된 양방향 강한 본문입니다.\n"
            if index % 250 == 0
            else f"{index:05d} 분류명과 무관한 양방향 강한 본문입니다.\n"
        )
        for index in range(5_000)
    )
    conn = decision_store.initialize_state_db(state_db)
    try:
        keep = _add_text(
            conn,
            house / "ㄹ" / "분류명 무관 작품.txt",
            "house",
            house_body,
        )
        discard = _add_text(
            conn,
            temp / "batch" / "분류명 무관 작품 1-200 완 [2026.03.11].txt",
            "temp",
            queue_body,
        )
        review_id = _add_review(conn, discard, keep, "longer_unresolved")
    finally:
        conn.close()

    with mutation_lock_for_roots(house, temp, "test-longer-bidirectional"):
        conn = decision_store.connect_state_db(state_db)
        try:
            run_id = _approve(conn, state_db, house, temp)
            queued = dedup_mutations.queue_candidate(
                conn,
                candidate_file_id=discard["file_id"],
                reference_file_id=keep["file_id"],
                classification="longer_unresolved",
                queue_dir=temp / "trash_bin" / "warning",
                run_id=run_id,
                review_id=review_id,
                allow_unassigned_reference=True,
            )
        finally:
            conn.close()
        assert deduplicator.count_actionable_pending_strong_reviews(state_db) == 1
        records = deduplicator.cleanup_pending_queue_strong_reviews(
            str(state_db), str(house), str(temp), run_id
        )
        conn = decision_store.connect_state_db(state_db)
        try:
            review = conn.execute(
                "SELECT state FROM review_items WHERE review_id = ?", (review_id,)
            ).fetchone()
            decision_store.finish_actual_run(conn, run_id, success=True)
            assert decision_store.doctor_issues(conn) == []
        finally:
            conn.close()

    assert queued["action"] == "warning_move"
    assert len(records) == 1
    assert records[0]["classification"] == "longer_unresolved"
    assert records[0]["coverage_ppm"] >= 950_000
    assert records[0]["reverse_coverage_ppm"] >= 950_000
    assert review["state"] == "superseded"
    assert Path(records[0]["dest_path"]).is_file()
    assert Path(keep["path"]).is_file()


def test_near_identical_same_coordinates_allow_contained_title_annotation(
    tmp_path,
):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    house_body = "".join(
        f"{index:05d} 제목 병기와 무관한 양방향 강한 본문입니다.\n"
        for index in range(5_000)
    )
    queue_body = "".join(
        (
            f"{index:05d} 교정된 제목 병기 양방향 본문입니다.\n"
            if index % 250 == 0
            else f"{index:05d} 제목 병기와 무관한 양방향 강한 본문입니다.\n"
        )
        for index in range(5_000)
    )
    conn = decision_store.initialize_state_db(state_db)
    try:
        keep = _add_text(
            conn,
            house / "ㄷ" / "대라선 조염 1-226 완.txt",
            "house",
            house_body,
        )
        discard = _add_text(
            conn,
            temp / "batch" / "대라선 조염大羅仙 趙炎 1-226 완.txt",
            "temp",
            queue_body,
        )
        review_id = _add_review(conn, discard, keep, "near_identical")
    finally:
        conn.close()

    with mutation_lock_for_roots(house, temp, "test-near-annotated-core"):
        conn = decision_store.connect_state_db(state_db)
        try:
            run_id = _approve(conn, state_db, house, temp)
            dedup_mutations.queue_candidate(
                conn,
                candidate_file_id=discard["file_id"],
                reference_file_id=keep["file_id"],
                classification="near_identical",
                queue_dir=temp / "trash_bin" / "warning",
                run_id=run_id,
                review_id=review_id,
                allow_unassigned_reference=True,
            )
        finally:
            conn.close()
        assert deduplicator.count_actionable_pending_strong_reviews(state_db) == 1
        records = deduplicator.cleanup_pending_queue_strong_reviews(
            str(state_db), str(house), str(temp), run_id
        )
        conn = decision_store.connect_state_db(state_db)
        try:
            decision_store.finish_actual_run(conn, run_id, success=True)
            assert decision_store.doctor_issues(conn) == []
        finally:
            conn.close()

    assert len(records) == 1
    assert records[0]["classification"] == "near_identical"
    assert records[0]["coverage_ppm"] >= 950_000
    assert records[0]["reverse_coverage_ppm"] >= 950_000
    assert Path(records[0]["dest_path"]).is_file()
    assert Path(keep["path"]).is_file()


def test_near_identical_current_subthreshold_evidence_is_not_actionable(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        keep = _add_text(
            conn,
            house / "ㅈ" / "절반만 같은 작품 1-200 완.txt",
            "house",
            "house 본문 문장\n" * 10_000,
        )
        discard = _add_text(
            conn,
            temp / "trash_bin" / "warning" / "절반만 같은 작품 1-200 완.txt",
            "queue",
            "queue 다른 문장\n" * 10_000,
        )
        review_id = _add_review(conn, discard, keep, "near_identical")
        with decision_store.transaction(conn):
            conn.execute(
                """
                UPDATE review_items SET evidence_json = ? WHERE review_id = ?
                """,
                (
                    __import__("json").dumps({
                        "ordered_body_checked": True,
                        "ordered_body_coverage": {
                            "source_chars": 100_000,
                            "target_chars": 100_000,
                            "source_lines": 10_000,
                            "target_lines": 10_000,
                            "matched_chars": 50_000,
                            "matched_lines": 5_000,
                            "coverage_ppm": 500_000,
                            "max_unmatched_chars": 1_000,
                            "repetitive_source_chars": 0,
                        },
                    }),
                    review_id,
                ),
            )
    finally:
        conn.close()

    assert deduplicator.count_actionable_pending_strong_reviews(state_db) == 0


def test_house_distribution_near_duplicate_requires_bidirectional_99_percent(
    tmp_path,
):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        clean_body = "".join(
            f"{index:05d} 특수지원소대의 고유 본문 문장입니다.\n"
            for index in range(5_000)
        )
        distributed_body = "".join(
            (
                f"{index:05d} 특수지원중대의 교정된 본문 문장입니다.\n"
                if index % 250 == 0
                else f"{index:05d} 특수지원소대의 고유 본문 문장입니다.\n"
            )
            for index in range(5_000)
        )
        keep = _add_text(
            conn,
            house / "ㅌ" / "특수지원소대 알베르트 소위 1-105 (완).txt",
            "house",
            clean_body,
        )
        discard = _add_text(
            conn,
            house / "ㅌ" / "특수지원중대 알베르트 소위 1-105 완@19-판190514.txt",
            "house",
            distributed_body,
            assignment_state="decision_required",
        )
        review_id = _add_review(conn, discard, keep, "near_identical")
    finally:
        conn.close()

    assert deduplicator.count_actionable_pending_strong_reviews(state_db) == 1
    with mutation_lock_for_roots(house, temp, "test-house-near-99"):
        conn = decision_store.connect_state_db(state_db)
        try:
            run_id = _approve(conn, state_db, house, temp)
        finally:
            conn.close()
        records = deduplicator.cleanup_pending_queue_strong_reviews(
            str(state_db), str(house), str(temp), run_id
        )
        conn = decision_store.connect_state_db(state_db)
        try:
            review = conn.execute(
                "SELECT state FROM review_items WHERE review_id = ?",
                (review_id,),
            ).fetchone()
            decision_store.finish_actual_run(conn, run_id, success=True)
            assert decision_store.doctor_issues(conn) == []
        finally:
            conn.close()

    assert len(records) == 1
    assert records[0]["coverage_ppm"] >= 990_000
    assert records[0]["reverse_coverage_ppm"] >= 990_000
    assert review["state"] == "superseded"
    assert Path(keep["path"]).is_file()
    assert not Path(discard["path"]).exists()
    assert Path(records[0]["dest_path"]).is_file()


def test_house_near_duplicate_without_distribution_suffix_stays_manual(tmp_path):
    common = {
        "source": "house",
        "ext": ".txt",
        "unit": "화",
        "complete": True,
        "span_ambiguous": False,
        "assignment_state": "unassigned",
        "mutation_eligible": True,
        "variant_id": None,
        "protected": False,
        "representative": False,
        "normalized_length": 400_000,
        "char_count": 400_000,
        "size": 1_200_000,
    }
    left = {
        **common,
        "name": "근접한 작품 표기 A 1-105 완.txt",
        "path": str(tmp_path / "근접한 작품 표기 A 1-105 완.txt"),
        "core_title": "근접한작품표기a",
        "author": None,
    }
    right = {
        **common,
        "name": "근접한 작품 표기 B 1-105 완.txt",
        "path": str(tmp_path / "근접한 작품 표기 B 1-105 완.txt"),
        "core_title": "근접한작품표기b",
        "author": None,
    }
    assert deduplicator._bidirectional_near_direction({
        "classification": "near_identical",
        "left": left,
        "right": right,
        "evidence": {},
    }) is None


def test_loose_title_upgrade_contract_is_closed_to_known_safe_forms():
    allowed = (
        (
            "테라리움 어드벤쳐 1-95화 공금.txt",
            "테라리움 어드벤처 1-1000 완.txt",
        ),
        (
            "내가 키운 S급 1-870 완.txt",
            "내가 키운 S급들 1-1165 본편 외전 후일담 (완).txt",
        ),
        (
            "천재 궁수의 스트리밍 1-801(시즌3 271화까지)@멍멍킴.txt",
            "천재 궁수의 스트리밍 1115 시즌4 完.txt",
        ),
        (
            "마늘소금 -이번-생은-아역부터-1-820-完- 삽화.txt",
            "이번 생은 아역부터 1-940 완결.txt",
        ),
    )
    for shorter, longer in allowed:
        relation = classify_loose_title_upgrade_relation(shorter, longer)
        assert relation is not None
        assert relation.preferred_side == "right"

    blocked = (
        (
            "동로마를 다시 위대하게 외전 1-10 완결.txt",
            "동로마를 다시 위대하게 001-213 완.txt",
        ),
        (
            "다운(DOWN)@작가(19N) 1-289 완.txt",
            "다운 1-314 완.txt",
        ),
        (
            "테라리움 어드벤처 개정판 1-95 완.txt",
            "테라리움 어드벤처 1-1000 완.txt",
        ),
        (
            "서로 비슷해 보이는 작품 A 1-95 완.txt",
            "서로 비슷해 보이는 작품 B 1-1000 완.txt",
        ),
    )
    for shorter, longer in blocked:
        assert classify_loose_title_upgrade_relation(shorter, longer) is None


def test_loose_title_ordered_upgrade_replays_pinned_body_before_quarantine(
    tmp_path,
):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        common = "".join(
            f"{index:05d} 테라리움의 고유 본문 문장입니다.\n"
            for index in range(5_000)
        )
        shorter = _add_text(
            conn,
            house / "ㅌ" / "테라리움 어드벤쳐 1-95화 공금.txt",
            "house",
            common,
            assignment_state="decision_required",
        )
        longer = _add_text(
            conn,
            house / "ㅌ" / "테라리움 어드벤처 1-1000 완.txt",
            "house",
            common + "".join(
                f"{index:05d} 확장판의 추가 회차 문장입니다.\n"
                for index in range(5_000, 6_000)
            ),
        )
        review_id = _add_review(
            conn, shorter, longer, "ordered_body_match"
        )
    finally:
        conn.close()

    assert deduplicator.count_actionable_pending_strong_reviews(state_db) == 1
    with mutation_lock_for_roots(house, temp, "test-loose-title-upgrade"):
        conn = decision_store.connect_state_db(state_db)
        try:
            run_id = _approve(conn, state_db, house, temp)
        finally:
            conn.close()
        records = deduplicator.cleanup_pending_queue_strong_reviews(
            str(state_db), str(house), str(temp), run_id
        )
        conn = decision_store.connect_state_db(state_db)
        try:
            review = conn.execute(
                "SELECT state FROM review_items WHERE review_id = ?",
                (review_id,),
            ).fetchone()
            decision_store.finish_actual_run(conn, run_id, success=True)
            assert decision_store.doctor_issues(conn) == []
        finally:
            conn.close()

    assert len(records) == 1
    assert records[0]["classification"] == "ordered_body_match"
    assert records[0]["coverage_ppm"] >= 950_000
    assert review["state"] == "superseded"
    assert Path(longer["path"]).is_file()
    assert not Path(shorter["path"]).exists()
    assert Path(records[0]["dest_path"]).is_file()
