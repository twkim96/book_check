import json
import os
from pathlib import Path

import decision_store
from deduplicator import clean_duplicates
from mutation_io import inspect_contained_text
from scanner import generate_file_list
from text_preview import ReadBudget, analyze_text_file


def _prepare_managed_reference(tmp_path, name, body):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    reference = house / name
    reference.write_text(body, encoding="utf-8")
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
    return house, temp, state_db, index, reference, row["file_id"], variant_id


def _run(house, temp, state_db, index):
    return clean_duplicates(
        house_dir=str(house), temp_dir=str(temp), dry_run=False,
        index_path=str(index), rescan=True, move_suspects=True,
        delete_exact=True, include_temp=True, audit_suspects=True,
        update_index_after_run=False, state_db_path=str(state_db),
        require_state_db=True,
    )


def test_contained_text_proof_uses_complete_normalized_prefix(tmp_path):
    short = tmp_path / "short.txt"
    long = tmp_path / "long.txt"
    short.write_text("동일 본문\n" * 300, encoding="utf-8")
    long.write_text(("동일 본문 " * 300) + ("추가 회차 " * 50), encoding="utf-8")

    proof = inspect_contained_text(short, long)

    assert proof.short_normalized_length < proof.long_normalized_length
    assert proof.long_prefix_sha256 == proof.short_normalized_sha256
    assert proof.short_normalized_sha256 != proof.long_normalized_sha256


def test_managed_short_version_is_replaced_by_strictly_containing_intake(tmp_path):
    short_body = "동일 본문 " * 700
    long_body = short_body + ("추가 회차 " * 400)
    house, temp, state_db, index, old, old_file_id, variant_id = (
        _prepare_managed_reference(
            tmp_path, "합성판타지 1-100.txt", short_body
        )
    )
    incoming = temp / "합성판타지 1-150 외전포함.txt"
    incoming.write_text(long_body, encoding="utf-8")

    summary = _run(house, temp, state_db, index)

    replacement = house / incoming.name
    quarantine = temp / "trash_bin" / "superseded_versions" / old.name
    assert not old.exists() and not incoming.exists()
    assert replacement.read_text(encoding="utf-8") == long_body
    assert quarantine.read_text(encoding="utf-8") == short_body
    assert summary["contained_upgrade_count"] == 1
    assert summary["warning_count"] == 0
    recent = house / "_최근" / replacement.name
    assert recent.is_symlink()
    assert os.path.realpath(recent) == str(replacement)
    report = json.loads(Path(summary["report_path"]).read_text(encoding="utf-8"))
    [record] = [
        item for item in report["suspect_move_records"]
        if item["status"] == "superseded"
    ]
    assert record["classification"] == "contained_exact"
    assert record["operation_id"] is not None
    assert record["ingest_operation_id"] is not None
    assert record["containment_evidence"]["short_normalized_length"] < \
        record["containment_evidence"]["long_normalized_length"]

    conn = decision_store.connect_state_db(state_db)
    replacement_row = conn.execute(
        "SELECT file_id, variant_id, assignment_state, protected "
        "FROM files WHERE canonical_path = ?", (str(replacement),)
    ).fetchone()
    assert tuple(replacement_row)[1:] == (variant_id, "managed", 1)
    assert conn.execute(
        "SELECT file_id FROM representatives WHERE variant_id = ?", (variant_id,)
    ).fetchone()[0] == replacement_row["file_id"]
    assert tuple(conn.execute(
        "SELECT active, source, protected FROM files WHERE file_id = ?",
        (old_file_id,),
    ).fetchone()) == (0, "quarantine", 0)
    assert decision_store.doctor_issues(conn) == []
    conn.close()


def test_header_shifted_longer_edition_is_auto_replaced(tmp_path):
    short_body = "".join(
        f"{number:05d} 고유한 합성 본문 문장입니다.\n" for number in range(3_000)
    )
    shifted_long = ("별도 머리말 " * 80) + short_body + ("추가 회차 " * 400)
    house, temp, state_db, index, old, _, _ = _prepare_managed_reference(
        tmp_path, "합성판본 1-100화.txt", short_body
    )
    incoming = temp / "합성판본 1-150화.txt"
    incoming.write_text(shifted_long, encoding="utf-8")

    summary = _run(house, temp, state_db, index)

    assert not old.exists() and not incoming.exists()
    assert (house / incoming.name).exists()
    assert (temp / "trash_bin" / "superseded_versions" / old.name).exists()
    assert summary["contained_upgrade_count"] == 1
    assert summary["warning_count"] == 0


def test_shorter_intake_is_quarantined_when_only_house_has_author(tmp_path):
    short_body = "동일 본문 " * 700
    long_body = short_body + ("추가 회차 " * 400)
    house, temp, state_db, index, existing, _, variant_id = (
        _prepare_managed_reference(
            tmp_path, "합성역방향 1-150화 [작가A].txt", long_body
        )
    )
    incoming = temp / "합성역방향 1-100화.txt"
    incoming.write_text(short_body, encoding="utf-8")

    summary = _run(house, temp, state_db, index)

    quarantine = temp / "trash_bin" / "superseded_versions" / incoming.name
    assert existing.read_text(encoding="utf-8") == long_body
    assert not incoming.exists()
    assert quarantine.read_text(encoding="utf-8") == short_body
    assert summary["contained_upgrade_count"] == 1
    assert summary["warning_count"] == 0
    conn = decision_store.connect_state_db(state_db)
    assert conn.execute(
        "SELECT file_id FROM representatives WHERE variant_id = ?", (variant_id,)
    ).fetchone()[0] == conn.execute(
        "SELECT file_id FROM files WHERE canonical_path = ?", (str(existing),)
    ).fetchone()[0]
    assert decision_store.doctor_issues(conn) == []
    conn.close()


def test_contained_author_conflict_remains_warning_only(tmp_path):
    short_body = "동일 본문 " * 700
    long_body = short_body + ("추가 회차 " * 400)
    house, temp, state_db, index, old, _, _ = _prepare_managed_reference(
        tmp_path, "합성충돌 1-100화 [작가A].txt", short_body
    )
    incoming = temp / "합성충돌 1-150화 [작가B].txt"
    incoming.write_text(long_body, encoding="utf-8")

    summary = _run(house, temp, state_db, index)

    assert old.exists() and not incoming.exists()
    assert (temp / "trash_bin" / "warning" / incoming.name).exists()
    assert summary["contained_upgrade_count"] == 0
    assert summary["warning_count"] == 1
