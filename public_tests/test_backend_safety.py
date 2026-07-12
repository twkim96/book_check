import deduplicator
from normalizer import analyze_name, has_pass_marker


def test_synthetic_title_rules_keep_markers_and_ranges_separate():
    info = analyze_name("합성연재물 1-250화 완〔P〕.txt")
    assert info["core_title"] == "합성연재물"
    assert info["effective_max"] == 250
    assert info["complete"] is True
    assert has_pass_marker("합성연재물 1-250화 완〔P〕.txt") is True


def test_dry_run_never_removes_exact_duplicate_fixture(tmp_path):
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    first = house / "합성작품 1-10화.txt"
    second = house / "합성작품 1-10화 사본.txt"
    first.write_text("합성 본문 " * 100, encoding="utf-8")
    second.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")

    summary = deduplicator.clean_duplicates(
        house_dir=str(house),
        temp_dir=str(temp),
        dry_run=True,
        index_path=str(tmp_path / "file_index.json"),
        rescan=True,
    )

    assert summary["exact_count"] == 1
    assert first.is_file()
    assert second.is_file()
    assert not (temp / "trash_bin").exists()
    assert list((temp / "dedup_logs").glob("dedup_*.txt"))
