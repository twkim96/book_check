import json
import zipfile

import duplicate_auditor
import mutation_io
import pytest
from text_preview import ReadBudget


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
