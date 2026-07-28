import json
import os
from pathlib import Path

import decision_store
import duplicate_auditor
import library_catalog
from dedup_episode_relation import classify_dedup_coordinate_relation
from deduplicator import _ordered_body_direction, clean_duplicates
from scanner import generate_file_list
from text_preview import (
    NormalizationDeferred,
    NormalizedLineSequence,
    ReadBudget,
    analyze_text_file,
    ordered_body_coverage,
)


def _lines(count=5_000, changed=None):
    changed = set(changed or ())
    return "".join(
        (
            f"{number:05d} 수정된 고유 합성 문장과 사건 전개입니다.\n"
            if number in changed
            else f"{number:05d} 원본의 고유 합성 문장과 사건 전개입니다.\n"
        )
        for number in range(count)
    )


def _prepare_managed_reference(tmp_path, name, body, *, extra_house_files=None):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    reference = house / name
    reference.write_text(body, encoding="utf-8")
    for extra_name, extra_body in extra_house_files or ():
        (house / extra_name).write_text(extra_body, encoding="utf-8")
    state_db = tmp_path / "state.sqlite3"
    index = tmp_path / "file_index.json"
    generate_file_list(
        [str(house)], str(tmp_path / "file_list.json"), str(index),
        state_db_path=str(state_db),
    )
    conn = decision_store.connect_state_db(state_db)
    row = conn.execute(
        "SELECT * FROM files WHERE canonical_path = ?", (str(reference),)
    ).fetchone()
    analysis = analyze_text_file(
        reference, budget=ReadBudget(max_bytes=10_000_000)
    )
    with decision_store.transaction(conn):
        fingerprint_id = conn.execute(
            """
            INSERT INTO fingerprints(
                file_id, canonical_path, size, mtime_ns, normalizer_version,
                fingerprint_version, raw_sha256, normalized_sha256,
                normalized_length, encoding, status, front_anchor, tail_anchor,
                anchors_json
            ) VALUES (?, ?, ?, ?, 'public-test', '1', ?, ?, ?, ?, ?, ?, ?, '{}')
            """,
            (
                row["file_id"], str(reference), analysis.size, analysis.mtime_ns,
                analysis.raw_sha256, analysis.normalized_sha256,
                analysis.normalized_length, analysis.encoding, analysis.status,
                analysis.front_anchor, analysis.tail_anchor,
            ),
        ).lastrowid
        work_id = conn.execute(
            "INSERT INTO works(display_title) VALUES ('합성 작품')"
        ).lastrowid
        variant_id = conn.execute(
            "INSERT INTO variants(work_bucket_id, variant_kind) VALUES (?, 'base')",
            (work_id,),
        ).lastrowid
        conn.execute(
            """
            UPDATE files SET current_fingerprint_id = ?, variant_id = ?,
                assignment_state = 'managed', assignment_origin = 'human_decision',
                protected = 1 WHERE file_id = ?
            """,
            (fingerprint_id, variant_id, row["file_id"]),
        )
        conn.execute(
            "INSERT INTO representatives(variant_id, file_id) VALUES (?, ?)",
            (variant_id, row["file_id"]),
        )
    backup = tmp_path / "before.sqlite3"
    decision_store.backup_state_db(conn, backup)
    decision_store.issue_actual_run_token(
        conn, str(backup), house_dir=house, temp_dir=temp
    )
    conn.close()
    generate_file_list(
        [str(house)], str(tmp_path / "file_list.json"), str(index),
        state_db_path=str(state_db),
    )
    return house, temp, state_db, index, reference


def _run(house, temp, state_db, index):
    return clean_duplicates(
        house_dir=str(house), temp_dir=str(temp), dry_run=False,
        index_path=str(index), rescan=True, move_suspects=True,
        delete_exact=True, include_temp=True, audit_suspects=True,
        update_index_after_run=False, state_db_path=str(state_db),
        require_state_db=True,
    )


def _assert_ordered_quarantine(summary, temp):
    assert summary["ordered_body_quarantine_count"] == 1
    assert summary["warning_count"] == 0
    report = json.loads(Path(summary["report_path"]).read_text(encoding="utf-8"))
    [record] = [
        item for item in report["suspect_move_records"]
        if item["status"] == "ordered_duplicate"
    ]
    evidence = record["ordered_body_evidence"]
    assert evidence["coverage_ppm"] >= 950_000
    quarantine_root = temp / "trash_bin" / "ordered_body_duplicates"
    assert os.path.commonpath((record["dest_path"], str(quarantine_root))) == str(
        quarantine_root
    )
    return record


def test_side_story_total_is_equal_without_relabeling_main_episodes():
    relation = classify_dedup_coordinate_relation(
        "판타지소설 1-150.txt",
        "판타지소설 1-130 외전 1-20.txt",
    )

    assert relation.mode == "side_aggregate_equivalent"
    assert relation.preferred_side is None
    assert relation.left.total_count == relation.right.total_count == 150
    assert relation.right.primary_end == 130
    assert relation.right.side_count == 20


def test_episode_and_volume_are_candidates_without_numeric_conversion():
    relation = classify_dedup_coordinate_relation(
        "판타지소설 1-150화.txt",
        "판타지소설 1-9권.txt",
    )

    assert relation.mode == "cross_unit_edition"
    assert relation.preferred_side is None
    assert relation.left.unit == "화" and relation.right.unit == "권"
    assert relation.left.total_count == 150
    assert relation.right.total_count == 9


def test_author_bracket_after_range_does_not_hide_dedup_coordinates():
    relation = classify_dedup_coordinate_relation(
        "판타지소설 1-150화 [작가A].txt",
        "판타지소설 1-150화 완결.txt",
    )

    assert relation.mode == "same_coordinates"
    assert relation.left.primary_end == relation.right.primary_end == 150


def test_ordered_match_graph_is_bounded_before_node_allocation():
    tokens = tuple(
        f"반복군-{group}" for group in range(1_000) for _ in range(23)
    )
    sequence = NormalizedLineSequence(
        path="synthetic.txt", size=1, mtime_ns=1, dev=1, ino=1, ctime_ns=1,
        lines=tokens, weights=tuple(1 for _ in tokens),
        total_chars=len(tokens), read_bytes=1,
    )

    try:
        ordered_body_coverage(sequence, sequence)
    except NormalizationDeferred as exc:
        assert "safe node budget" in str(exc)
    else:
        raise AssertionError("oversized ordered-match graph was not rejected")


def test_ordered_quarantine_has_a_readonly_catalog_category(tmp_path):
    temp = tmp_path / "temp"
    quarantined = temp / "trash_bin" / "ordered_body_duplicates" / "격리본.txt"
    quarantined.parent.mkdir(parents=True)
    quarantined.write_text("복구 가능", encoding="utf-8")

    listing = library_catalog.review_queue_listing(
        tmp_path / "unused.sqlite3",
        temp,
        category="ordered_body_duplicates",
    )

    assert listing["total_visible"] == 1
    assert listing["items"][0]["path"] == str(quarantined.resolve())
    assert listing["items"][0]["physical_state"] == "quarantined"


def test_same_coordinate_96_percent_body_uses_choose_keep_and_final_quarantine(tmp_path):
    base = _lines()
    changed = _lines(changed=range(0, 5_000, 25))
    house, temp, state_db, index, old = _prepare_managed_reference(
        tmp_path, "합성동일 1-150화.txt", base
    )
    incoming = temp / "합성동일 1-150화 완결.txt"
    incoming.write_text(changed, encoding="utf-8")

    summary = _run(house, temp, state_db, index)

    record = _assert_ordered_quarantine(summary, temp)
    assert not old.exists() and not incoming.exists()
    assert (house / incoming.name).exists()
    assert record["coordinate_mode"] == "same_coordinates"


def test_exact_95_percent_boundary_is_inclusive(tmp_path):
    base = _lines()
    changed = _lines(changed=range(0, 5_000, 20))
    house, temp, state_db, index, old = _prepare_managed_reference(
        tmp_path, "합성경계포함 1-150화.txt", base
    )
    incoming = temp / "합성경계포함 1-150화 완결.txt"
    incoming.write_text(changed, encoding="utf-8")

    summary = _run(house, temp, state_db, index)

    record = _assert_ordered_quarantine(summary, temp)
    assert not old.exists() and (house / incoming.name).exists()
    assert record["ordered_body_evidence"]["coverage_ppm"] == 950_000


def test_side_story_aggregate_96_percent_is_auto_deduplicated(tmp_path):
    base = _lines()
    changed = _lines(changed=range(0, 5_000, 25))
    house, temp, state_db, index, existing = _prepare_managed_reference(
        tmp_path, "합성판타지 1-150화.txt", base
    )
    incoming = temp / "합성판타지 1-130화 외전 1-20화.txt"
    incoming.write_text(changed, encoding="utf-8")

    summary = _run(house, temp, state_db, index)

    record = _assert_ordered_quarantine(summary, temp)
    assert existing.exists() and not incoming.exists()
    assert record["coordinate_mode"] == "side_aggregate_equivalent"


def test_nested_96_percent_body_keeps_wider_declared_coverage(tmp_path):
    shorter = _lines()
    longer = _lines(count=7_500, changed=range(0, 5_000, 25))
    house, temp, state_db, index, old = _prepare_managed_reference(
        tmp_path, "합성중첩 1-100화.txt", shorter
    )
    incoming = temp / "합성중첩 1-150화 완결.txt"
    incoming.write_text(longer, encoding="utf-8")

    summary = _run(house, temp, state_db, index)

    record = _assert_ordered_quarantine(summary, temp)
    assert not old.exists() and not incoming.exists()
    assert (house / incoming.name).exists()
    assert record["coordinate_mode"] == "contained_coordinates"


def test_reverse_nested_96_percent_body_quarantines_shorter_incoming(tmp_path):
    longer = _lines(count=7_500)
    shorter = _lines(changed=range(0, 5_000, 25))
    house, temp, state_db, index, existing = _prepare_managed_reference(
        tmp_path, "합성역중첩 1-150화 완결.txt", longer
    )
    incoming = temp / "합성역중첩 1-100화.txt"
    incoming.write_text(shorter, encoding="utf-8")

    summary = _run(house, temp, state_db, index)

    record = _assert_ordered_quarantine(summary, temp)
    assert existing.exists() and not incoming.exists()
    assert record["coordinate_mode"] == "contained_coordinates"


def test_full_house_run_quarantines_existing_nested_duplicate(tmp_path):
    shorter = _lines()
    longer = _lines(count=7_500, changed=range(0, 5_000, 25))
    longer_name = "합성기존전수 1-150화 완결.txt"
    house, temp, state_db, index, old = _prepare_managed_reference(
        tmp_path,
        "합성기존전수 1-100화.txt",
        shorter,
        extra_house_files=[(longer_name, longer)],
    )

    summary = _run(house, temp, state_db, index)

    record = _assert_ordered_quarantine(summary, temp)
    assert not old.exists() and (house / longer_name).exists()
    assert record["coordinate_mode"] == "contained_coordinates"


def test_episode_to_volume_96_percent_uses_choose_keep(tmp_path):
    base = _lines()
    changed = _lines(changed=range(0, 5_000, 25))
    house, temp, state_db, index, old = _prepare_managed_reference(
        tmp_path, "합성단행 1-150화.txt", base
    )
    incoming = temp / "합성단행 1-9권 완결.txt"
    incoming.write_text(changed, encoding="utf-8")

    summary = _run(house, temp, state_db, index)

    record = _assert_ordered_quarantine(summary, temp)
    assert not old.exists() and not incoming.exists()
    assert (house / incoming.name).exists()
    assert record["coordinate_mode"] == "cross_unit_edition"


def test_distributed_match_below_95_percent_is_not_auto_quarantined(tmp_path):
    base = _lines()
    changed = _lines(changed=range(0, 5_000, 16))
    house, temp, state_db, index, existing = _prepare_managed_reference(
        tmp_path, "합성경계 1-150화.txt", base
    )
    incoming = temp / "합성경계 1-150화 완결.txt"
    incoming.write_text(changed, encoding="utf-8")

    summary = _run(house, temp, state_db, index)

    assert existing.exists() and not incoming.exists()
    assert (temp / "trash_bin" / "warning" / incoming.name).exists()
    assert summary["ordered_body_quarantine_count"] == 0
    assert summary["warning_count"] == 1


def test_large_contiguous_rewrite_stays_out_of_auto_quarantine(tmp_path):
    base = _lines()
    changed = _lines(changed=range(2_000, 2_200))
    house, temp, state_db, index, existing = _prepare_managed_reference(
        tmp_path, "합성개정 1-150화.txt", base
    )
    incoming = temp / "합성개정 1-150화 완결.txt"
    incoming.write_text(changed, encoding="utf-8")

    summary = _run(house, temp, state_db, index)

    assert existing.exists() and not incoming.exists()
    assert (temp / "trash_bin" / "warning" / incoming.name).exists()
    assert summary["ordered_body_quarantine_count"] == 0
    assert summary["warning_count"] == 1


def test_explicit_author_conflict_blocks_ordered_body_automation(tmp_path):
    base = _lines()
    changed = _lines(changed=range(0, 5_000, 25))
    house, temp, state_db, index, existing = _prepare_managed_reference(
        tmp_path, "합성작가 1-150화 [작가A].txt", base
    )
    incoming = temp / "합성작가 1-150화 완결 [작가B].txt"
    incoming.write_text(changed, encoding="utf-8")

    summary = _run(house, temp, state_db, index)

    assert existing.exists()
    assert summary["ordered_body_quarantine_count"] == 0


def test_missing_author_does_not_block_ordered_body_automation(tmp_path):
    base = _lines()
    changed = _lines(changed=range(0, 5_000, 25))
    house, temp, state_db, index, existing = _prepare_managed_reference(
        tmp_path, "합성작가누락 1-150화 [작가A].txt", base
    )
    incoming = temp / "합성작가누락 1-150화 완결.txt"
    incoming.write_text(changed, encoding="utf-8")

    summary = _run(house, temp, state_db, index)

    _assert_ordered_quarantine(summary, temp)
    assert not existing.exists() and (house / incoming.name).exists()


def test_managed_distinct_variants_veto_even_a_proven_body_match():
    shared = {
        "ext": ".txt", "core_title": "합성판본보존", "author": None,
        "source": "house", "mutation_eligible": True, "span_ambiguous": False,
        "assignment_state": "managed", "representative": True,
        "protected": True, "unit": "화", "effective_max": 150,
        "complete": True, "char_count": 500_000,
    }
    left = {
        **shared, "path": "/house/left.txt", "name": "합성판본보존 1-150화.txt",
        "variant_id": 1, "work_bucket_id": 10,
    }
    right = {
        **shared, "path": "/house/right.txt", "name": "합성판본보존 1-150화 완결.txt",
        "variant_id": 2, "work_bucket_id": 10,
    }

    assert _ordered_body_direction({
        "classification": "ordered_body_match", "left": left, "right": right,
    }) is None


def test_unchanged_ordered_pair_reuses_pair_cache_without_body_reads(tmp_path):
    base = _lines()
    changed = _lines(changed=range(0, 5_000, 25))
    house, temp, state_db, index, _ = _prepare_managed_reference(
        tmp_path, "합성캐시 1-150화.txt", base
    )
    (temp / "합성캐시 1-150화 완결.txt").write_text(changed, encoding="utf-8")
    argv = [
        "--index", str(index), "--house", str(house), "--temp", str(temp),
        "--state-db", str(state_db), "--max-read-bytes", "1GiB",
    ]

    first = duplicate_auditor.run_audit(
        duplicate_auditor.build_parser().parse_args(argv)
    )
    second = duplicate_auditor.run_audit(
        duplicate_auditor.build_parser().parse_args(argv)
    )

    assert first.results[0]["classification"] == "ordered_body_match"
    assert second.results[0]["classification"] == "ordered_body_match"
    assert second.stats["pair_cache_hits"] == 1
    assert second.stats["actual_read_bytes"] == 0
