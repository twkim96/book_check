"""Folderling 1.4.17 EPUB analysis hold regressions."""

import json
import warnings
import zipfile

import decision_store
import deduplicator
import duplicate_auditor
import mutation_io
import pytest
from dedup_mutations import _ensure_intake_fingerprint, _file_state
from scanner import generate_file_list


CONTAINER = b"""<?xml version='1.0'?>
<container version='1.0' xmlns='urn:oasis:names:tc:opendocument:xmlns:container'>
  <rootfiles><rootfile full-path='OEBPS/content.opf'
    media-type='application/oebps-package+xml'/></rootfiles>
</container>"""
OPF = b"""<?xml version='1.0' encoding='utf-8'?>
<package version='2.0' unique-identifier='BookId'
 xmlns='http://www.idpf.org/2007/opf'
 xmlns:dc='http://purl.org/dc/elements/1.1/'>
 <metadata><dc:identifier id='BookId'>urn:uuid:duplicate-member</dc:identifier>
  <dc:title>duplicate member</dc:title></metadata>
 <manifest><item id='chapter' href='chapter.xhtml'
  media-type='application/xhtml+xml'/></manifest>
 <spine><itemref idref='chapter'/></spine>
</package>"""
CHAPTER = ("<html><body>안전한 본문 " + "가나다라마바사 " * 100 + "</body></html>").encode()


def _write_epub(path, *, duplicate_opf=None):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("mimetype", b"application/epub+zip")
            archive.writestr("META-INF/container.xml", CONTAINER)
            archive.writestr("OEBPS/content.opf", OPF)
            if duplicate_opf is not None:
                archive.writestr("OEBPS/content.opf", duplicate_opf)
            archive.writestr("OEBPS/chapter.xhtml", CHAPTER)


def _empty_index(path):
    path.write_text(
        json.dumps({"version": 2, "entries": []}, ensure_ascii=False),
        encoding="utf-8",
    )


def _audit_args(index, house, temp, state_db):
    args = duplicate_auditor.build_parser().parse_args([
        "--index", str(index),
        "--house", str(house),
        "--temp", str(temp),
        "--state-db", str(state_db),
    ])
    args.cache_write = True
    return args


def _initialize_empty_library(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    state_db = tmp_path / ".state" / "dedup.sqlite3"
    index = tmp_path / "file_index.json"
    file_list = tmp_path / "file_list.json"
    assert generate_file_list(
        [str(house)], str(file_list), str(index),
        state_db_path=str(state_db), temp_root=str(temp),
    )
    return house, temp, state_db, index


def test_identical_duplicate_epub_member_is_one_logical_member(tmp_path):
    single = tmp_path / "single.epub"
    duplicate = tmp_path / "duplicate.epub"
    _write_epub(single)
    _write_epub(duplicate, duplicate_opf=OPF)

    strict_single = mutation_io.inspect_epub_content(single)
    strict_duplicate = mutation_io.inspect_epub_content(duplicate)
    assert strict_duplicate.member_count == strict_single.member_count == 4
    assert strict_duplicate.uncompressed_size == strict_single.uncompressed_size
    assert strict_duplicate.content_sha256 == strict_single.content_sha256

    payload_single = mutation_io.inspect_epub_reading_payload(single)
    payload_duplicate = mutation_io.inspect_epub_reading_payload(duplicate)
    assert payload_duplicate.content_sha256 == payload_single.content_sha256

    spine_single = mutation_io.inspect_epub_spine_text(single)
    spine_duplicate = mutation_io.inspect_epub_spine_text(duplicate)
    assert spine_duplicate.text_sha256 == spine_single.text_sha256
    assert spine_duplicate.text_chars == spine_single.text_chars


@pytest.mark.parametrize(
    "inspector",
    (
        mutation_io.inspect_epub_content,
        mutation_io.inspect_epub_reading_payload,
        mutation_io.inspect_epub_spine_text,
    ),
)
def test_conflicting_duplicate_epub_member_remains_fail_closed(tmp_path, inspector):
    path = tmp_path / "conflicting.epub"
    _write_epub(path, duplicate_opf=OPF + b"different")

    with pytest.raises(
        RuntimeError,
        match="conflicting duplicate normalized member name: OEBPS/content.opf",
    ):
        inspector(path)


def test_incoming_epub_error_is_structured_warning_not_global_stop(tmp_path):
    house, temp, state_db, index = _initialize_empty_library(tmp_path)
    source = temp / "충돌 EPUB.epub"
    _write_epub(source, duplicate_opf=OPF + b"different")

    report = duplicate_auditor.run_audit(
        _audit_args(index, house, temp, state_db)
    )

    assert report.completed is True
    assert report.stop_reasons == []
    assert report.stats["temp_fingerprint_failed_files"] == 1
    assert report.stats["epub_analysis_errors"] == [{
        "source": "temp",
        "name": source.name,
        "rel_path": source.name,
        "error": (
            "EPUB contains conflicting duplicate normalized member name: "
            "OEBPS/content.opf"
        ),
    }]
    conn = decision_store.connect_state_db(state_db)
    try:
        row = conn.execute(
            "SELECT current_fingerprint_id FROM files WHERE canonical_path = ?",
            (str(source),),
        ).fetchone()
        assert row["current_fingerprint_id"] is None
    finally:
        conn.close()


def test_folderling_holds_incoming_epub_error_and_finishes_cleanly(tmp_path):
    house, temp, state_db, index = _initialize_empty_library(tmp_path)
    source = temp / "입고 보류 EPUB.epub"
    _write_epub(source, duplicate_opf=OPF + b"different")
    conn = decision_store.connect_state_db(state_db)
    try:
        backup = decision_store.backup_state_db(
            conn, state_db.parent / "before-epub-analysis-hold.sqlite3"
        )
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()

    summary = deduplicator.clean_duplicates(
        house_dir=str(house),
        temp_dir=str(temp),
        dry_run=False,
        index_path=str(index),
        rescan=True,
        move_suspects=True,
        delete_exact=True,
        include_temp=True,
        audit_suspects=True,
        update_index_after_run=False,
        state_db_path=str(state_db),
        require_state_db=True,
    )

    held = (
        temp / "trash_bin" / "warning" / "epub_analysis_errors" / source.name
    )
    assert not source.exists()
    assert held.is_file()
    assert summary["epub_analysis_hold_count"] == 1
    assert summary["warning_count"] == 1
    assert summary["review_queue_move_count"] == 1
    conn = decision_store.connect_state_db(state_db)
    try:
        row = conn.execute(
            "SELECT * FROM files WHERE canonical_path = ?", (str(held),)
        ).fetchone()
        assert row["source"] == "queue"
        assert row["active"] == 1
        assert row["assignment_state"] == "decision_required"
        operation = conn.execute(
            "SELECT * FROM operations WHERE file_id = ?", (row["file_id"],)
        ).fetchone()
        assert operation["action"] == "epub_analysis_hold"
        assert operation["state"] == "committed"
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_interrupted_epub_analysis_hold_restores_temp_source(tmp_path):
    house, temp, state_db, index = _initialize_empty_library(tmp_path)
    source = temp / "복구 EPUB.epub"
    _write_epub(source, duplicate_opf=OPF + b"different")
    report = duplicate_auditor.run_audit(
        _audit_args(index, house, temp, state_db)
    )
    assert report.completed is True
    conn = decision_store.connect_state_db(state_db)
    try:
        file_id = conn.execute(
            "SELECT file_id FROM files WHERE canonical_path = ?", (str(source),)
        ).fetchone()[0]
        source_row = _ensure_intake_fingerprint(
            conn, _file_state(conn, file_id)
        )
        backup = decision_store.backup_state_db(
            conn, state_db.parent / "before-interrupted-epub-hold.sqlite3"
        )
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()
    run_id, _ = decision_store.prepare_actual_run(state_db, house, temp)
    destination = (
        temp / "trash_bin" / "warning" / "epub_analysis_errors" / source.name
    )
    destination.parent.mkdir(parents=True)
    conn = decision_store.connect_state_db(state_db)
    try:
        evidence = mutation_io.inspect_regular_file(source)
        with decision_store.transaction(conn):
            operation_id = decision_store.create_operation(
                conn,
                run_id=run_id,
                action="epub_analysis_hold",
                source_path=str(source),
                dest_path=str(destination),
                file_id=file_id,
                expected_size=source_row["size"],
                expected_mtime_ns=source_row["mtime_ns"],
                expected_fingerprint_id=source_row["current_fingerprint_id"],
                source_dev=evidence.dev,
                source_ino=evidence.ino,
                source_ctime_ns=evidence.ctime_ns,
                source_sha256=evidence.sha256,
            )
        decision_store.copy_record_consume_operation(
            conn, operation_id, source, destination, evidence
        )
        decision_store.finish_actual_run(
            conn, run_id, success=False, error="synthetic interruption"
        )
        assert decision_store.recover_interrupted_operation(
            conn, operation_id
        ) == "rolled_back"
        assert source.is_file()
        assert not destination.exists()
    finally:
        conn.close()
