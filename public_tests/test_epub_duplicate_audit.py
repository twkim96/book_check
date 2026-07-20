import json
import zipfile

import duplicate_auditor


def _write_epub(path, body, *, compression, timestamp):
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        info = zipfile.ZipInfo("OEBPS/chapter.xhtml", date_time=timestamp)
        info.compress_type = compression
        archive.writestr(info, body)


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

    report = duplicate_auditor.run_audit(_args(index, house, temp))

    assert report.completed is True
    assert report.stats["unique_candidate_files"] == 2
    assert report.results[0]["classification"] == "epub_equivalent"
    assert report.results[0]["evidence"]["left_raw_sha256"] != \
        report.results[0]["evidence"]["right_raw_sha256"]


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
