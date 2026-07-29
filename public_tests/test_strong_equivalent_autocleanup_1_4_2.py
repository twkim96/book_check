import zipfile
from pathlib import Path

import decision_store
from deduplicator import clean_duplicates
from scanner import generate_file_list


def _write_epub(path: Path, body: bytes, *, compression, year: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        info = zipfile.ZipInfo("OEBPS/chapter.xhtml", (year, 1, 1, 0, 0, 0))
        info.compress_type = compression
        archive.writestr(info, body)


def _write_semantic_repack(path: Path, *, title: str, bookmark: bool):
    path.parent.mkdir(parents=True, exist_ok=True)
    opf = f"""<package version='2.0' xmlns='http://www.idpf.org/2007/opf'
 xmlns:dc='http://purl.org/dc/elements/1.1/'>
 <metadata><dc:title>{title}</dc:title></metadata>
 <manifest><item id='chapter' href='chapter.xhtml' media-type='application/xhtml+xml'/></manifest>
 <spine><itemref idref='chapter'/></spine></package>"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/chapter.xhtml", "동일한 읽기 본문" * 500)
        if bookmark:
            archive.writestr("META-INF/calibre_bookmarks.txt", "position=42")


def _prepare(tmp_path, house_names, temp_names=()):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    body = ("동일한 EPUB 본문입니다. " * 500).encode("utf-8")
    for index, name in enumerate(house_names):
        _write_epub(
            house / name,
            body,
            compression=(zipfile.ZIP_STORED if index % 2 == 0 else zipfile.ZIP_DEFLATED),
            year=2020 + index,
        )
    for index, name in enumerate(temp_names):
        _write_epub(
            temp / name,
            body,
            compression=zipfile.ZIP_DEFLATED,
            year=2025 + index,
        )

    state_db = tmp_path / ".state" / "dedup.sqlite3"
    index_path = tmp_path / "file_index.json"
    file_list = tmp_path / "file_list.json"
    assert generate_file_list(
        [str(house)], str(file_list), str(index_path),
        state_db_path=str(state_db), temp_root=str(temp),
    )
    conn = decision_store.connect_state_db(state_db)
    try:
        backup = decision_store.backup_state_db(
            conn, state_db.parent / "before-strong-equivalent.sqlite3"
        )
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()
    return house, temp, state_db, index_path


def _run(house, temp, state_db, index_path):
    return clean_duplicates(
        house_dir=str(house),
        temp_dir=str(temp),
        dry_run=False,
        index_path=str(index_path),
        rescan=True,
        move_suspects=True,
        delete_exact=True,
        include_temp=True,
        audit_suspects=True,
        update_index_after_run=False,
        state_db_path=str(state_db),
        require_state_db=True,
    )


def test_repacked_incoming_epub_is_automatically_quarantined(tmp_path):
    house, temp, state_db, index_path = _prepare(
        tmp_path,
        ["신검의 계약자들 001-151 (완).epub"],
        ["신검의 계약자들 ep001-151 (완) noPic ver.epub"],
    )

    summary = _run(house, temp, state_db, index_path)

    assert summary["exact_mutation_count"] == 0
    assert summary["suspect_move_count"] == 1
    assert summary["warning_count"] == 0
    assert (house / "신검의 계약자들 001-151 (완).epub").is_file()
    assert not (temp / "신검의 계약자들 ep001-151 (완) noPic ver.epub").exists()
    assert len(list((temp / "trash_bin" / "suspected_duplicates").glob("*.epub"))) == 1

    conn = decision_store.connect_state_db(state_db)
    try:
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_repacked_house_epub_pair_is_automatically_reduced_to_one(tmp_path):
    names = [
        "차원의 조각 001-102 (완).epub",
        "차원의 조각 ep001-102 (완) noPic ver.epub",
    ]
    house, temp, state_db, index_path = _prepare(tmp_path, names)

    summary = _run(house, temp, state_db, index_path)

    assert summary["suspect_move_count"] == 1
    assert summary["warning_count"] == 0
    assert sum((house / name).is_file() for name in names) == 1
    assert len(list((temp / "trash_bin" / "suspected_duplicates").glob("*.epub"))) == 1

    conn = decision_store.connect_state_db(state_db)
    try:
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_metadata_only_epub_repack_is_revalidated_and_quarantined(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    existing = house / "마도구사 달리아는 고개 숙이지 않아 3권.epub"
    incoming = temp / "마도구사 달리아는 고개 숙이지 않아 03권.epub"
    _write_semantic_repack(existing, title="마도구사 달리아 3", bookmark=False)
    _write_semantic_repack(incoming, title="마도구사 달리아", bookmark=True)

    state_db = tmp_path / ".state" / "dedup.sqlite3"
    index_path = tmp_path / "file_index.json"
    assert generate_file_list(
        [str(house)], str(tmp_path / "file_list.json"), str(index_path),
        state_db_path=str(state_db), temp_root=str(temp),
    )
    conn = decision_store.connect_state_db(state_db)
    try:
        backup = decision_store.backup_state_db(
            conn, state_db.parent / "before-semantic-repack.sqlite3"
        )
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()

    summary = _run(house, temp, state_db, index_path)

    assert summary["suspect_move_count"] == 1
    assert summary["warning_count"] == 0
    assert existing.is_file()
    assert not incoming.exists()
    assert len(list((temp / "trash_bin" / "suspected_duplicates").glob("*.epub"))) == 1
    conn = decision_store.connect_state_db(state_db)
    try:
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_metadata_only_house_epub_repack_is_revalidated_and_reduced(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    first = house / "메타 작품 03권.epub"
    second = house / "메타 작품 3권.epub"
    _write_semantic_repack(first, title="메타 작품", bookmark=True)
    _write_semantic_repack(second, title="메타 작품 3", bookmark=False)

    state_db = tmp_path / ".state" / "dedup.sqlite3"
    index_path = tmp_path / "file_index.json"
    assert generate_file_list(
        [str(house)], str(tmp_path / "file_list.json"), str(index_path),
        state_db_path=str(state_db), temp_root=str(temp),
    )
    conn = decision_store.connect_state_db(state_db)
    try:
        backup = decision_store.backup_state_db(
            conn, state_db.parent / "before-house-semantic-repack.sqlite3"
        )
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()

    summary = _run(house, temp, state_db, index_path)

    assert summary["suspect_move_count"] == 1
    assert summary["warning_count"] == 0
    assert sum(path.is_file() for path in (first, second)) == 1
    assert len(list((temp / "trash_bin" / "suspected_duplicates").glob("*.epub"))) == 1
    conn = decision_store.connect_state_db(state_db)
    try:
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()
