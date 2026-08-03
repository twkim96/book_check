import json
import zipfile

import decision_store
import duplicate_auditor
from mutation_io import inspect_epub_spine_text
from normalizer import analyze_name


def _write_epub(path, *, title, identifier, publisher, date, body, asset):
    container = b"""<?xml version='1.0'?>
<container xmlns='urn:oasis:names:tc:opendocument:xmlns:container'>
  <rootfiles><rootfile full-path='OEBPS/content.opf'
    media-type='application/oebps-package+xml'/></rootfiles>
</container>"""
    opf = f"""<?xml version='1.0' encoding='utf-8'?>
<package version='2.0' unique-identifier='BookId'
 xmlns='http://www.idpf.org/2007/opf'
 xmlns:dc='http://purl.org/dc/elements/1.1/'>
 <metadata><dc:identifier id='BookId'>{identifier}</dc:identifier>
  <dc:title>{title}</dc:title><dc:creator>테스트 작가</dc:creator>
  <dc:publisher>{publisher}</dc:publisher><dc:date>{date}</dc:date></metadata>
 <manifest><item id='chapter' href='chapter.xhtml' media-type='application/xhtml+xml'/>
  <item id='asset' href='asset.bin' media-type='application/octet-stream'/></manifest>
 <spine><itemref idref='chapter'/></spine>
</package>""".encode("utf-8")
    chapter = (
        "<html><head><style>hidden</style></head><body>"
        f"{body}</body></html>"
    ).encode("utf-8")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/chapter.xhtml", chapter)
        archive.writestr("OEBPS/asset.bin", asset)


def _write_index(path, house, names):
    path.write_text(json.dumps({
        "version": 2,
        "normalizer_version": duplicate_auditor.NORMALIZER_VERSION,
        "entries": [
            {
                "type": "file",
                "name": name,
                "rel_path": name,
                "size": (house / name).stat().st_size,
                **analyze_name(name),
            }
            for name in names
        ],
    }, ensure_ascii=False), encoding="utf-8")


def _args(index, house, temp, state_db=None):
    raw = [
        "--index", str(index), "--house", str(house), "--temp", str(temp),
        "--house-only",
    ]
    if state_db is not None:
        raw.extend(("--state-db", str(state_db)))
    return duplicate_auditor.build_parser().parse_args(raw)


def test_spine_text_requires_same_stable_package_identity(tmp_path):
    body = "같은 실제 본문입니다." * 6_000
    first = tmp_path / "first.epub"
    second = tmp_path / "second.epub"
    _write_epub(
        first, title="작품 1", identifier="urn:uuid:same", publisher="출판사",
        date="2026-01-01", body=body, asset=b"small",
    )
    _write_epub(
        second, title="작품 1", identifier="urn:uuid:same", publisher="출판사",
        date="2026-01-01", body=body, asset=b"different richer asset",
    )

    left = inspect_epub_spine_text(first)
    right = inspect_epub_spine_text(second)

    assert left.text_chars > duplicate_auditor.EPUB_SPINE_TEXT_MIN_CHARS
    assert left.text_sha256 == right.text_sha256
    assert set(left.identifiers) & set(right.identifiers) == {"urn:uuid:same"}


def test_auditor_uses_spine_text_only_for_same_coordinate_and_identity(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    names = ["척추 증명 작품 1권.epub", "척추 증명 작품 01권.epub"]
    body = "본문 문장과 구두점." * 6_000
    for index, name in enumerate(names):
        _write_epub(
            house / name, title="척추 증명 작품 1", identifier="urn:uuid:shared",
            publisher="출판사", date="2026-01-01", body=body,
            asset=(b"a" if index == 0 else b"a much larger illustration payload"),
        )
    index_path = tmp_path / "file_index.json"
    _write_index(index_path, house, names)

    report = duplicate_auditor.run_audit(_args(index_path, house, temp))

    assert report.completed is True
    assert report.results[0]["classification"] == "epub_equivalent"
    assert report.results[0]["evidence"]["epub_equivalence_mode"] == "spine_text"


def test_disjoint_opf_edition_proof_suppresses_metadata_review(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    names = [
        "시원찮은 그녀를 위한 육성방법 FD (마루토 후미아키).epub",
        "시원찮은 그녀를 위한 육성방법 FD 2 (마루토 후미아키).epub",
    ]
    _write_epub(
        house / names[0], title="시원찮은 그녀를 위한 육성방법 FD",
        identifier="urn:uuid:first", publisher="디앤씨미디어", date="2018-09-07",
        body="첫 판본 본문" * 7_000, asset=b"one",
    )
    _write_epub(
        house / names[1], title="시원찮은 그녀를 위한 육성방법 FD 2",
        identifier="urn:uuid:second", publisher="L노벨", date="2019-09-25",
        body="두 번째 판본 본문" * 7_000, asset=b"two",
    )
    index_path = tmp_path / "file_index.json"
    state_db = tmp_path / "state.sqlite3"
    _write_index(index_path, house, names)

    report = duplicate_auditor.run_audit(
        _args(index_path, house, temp, state_db)
    )

    assert report.completed is True
    assert report.results[0]["classification"] == "different"
    assert report.results[0]["evidence"]["epub_distinct_edition"] is True
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM review_items WHERE state IN ('pending','deferred')"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0
