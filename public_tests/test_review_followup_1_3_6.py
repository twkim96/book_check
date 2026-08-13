import json

import pytest

import deduplicator
import folderling
from mutation_io import inspect_normalized_text


def _entry(path, **overrides):
    entry = {
        "name": path.name,
        "path": str(path),
        "size": path.stat().st_size,
        "source": "house",
        "ext": path.suffix,
        "protected": False,
        "representative": False,
        "complete": False,
        "unit": "미상",
        "effective_max": 0,
    }
    entry.update(overrides)
    return entry


def test_mutation_text_hash_matches_utf8_and_utf16_bom(tmp_path):
    text = "인코딩 규칙을 통일하는 본문\n둘째 줄"
    utf8 = tmp_path / "본문 utf8.txt"
    utf16 = tmp_path / "본문 utf16.txt"
    utf8.write_text(text, encoding="utf-8")
    utf16.write_text(text, encoding="utf-16")

    _, utf8_hash = inspect_normalized_text(utf8)
    _, utf16_hash = inspect_normalized_text(utf16)

    assert utf16_hash == utf8_hash


def test_mutation_text_hash_revalidates_lightly_damaged_bytes(tmp_path):
    first = tmp_path / "legacy-a.txt"
    second = tmp_path / "legacy-b.txt"
    raw = ("재검증 가능한 CP949 본문 " * 300).encode("cp949") + b"\x81"
    first.write_bytes(raw)
    second.write_bytes(raw)

    first_evidence, first_hash = inspect_normalized_text(first)
    second_evidence, second_hash = inspect_normalized_text(second)

    assert first_hash == second_hash
    assert first_evidence.sha256 == second_evidence.sha256


def test_exact_keep_prefers_representative_over_protected_nonrepresentative(tmp_path):
    protected = tmp_path / "보호 비대표.epub"
    representative = tmp_path / "대표.epub"
    protected.write_bytes(b"same")
    representative.write_bytes(b"same")

    keep = deduplicator.choose_keep_exact([
        _entry(protected, protected=True),
        _entry(representative, representative=True),
    ])

    assert keep["path"] == str(representative)


def test_zero_byte_files_participate_in_exact_duplicate_detection(tmp_path):
    first = tmp_path / "빈 파일 A.epub"
    second = tmp_path / "빈 파일 B.epub"
    first.write_bytes(b"")
    second.write_bytes(b"")

    groups = deduplicator.find_exact_duplicates([_entry(first), _entry(second)])

    assert len(groups) == 1
    assert len(groups[0]["duplicates"]) == 1


def test_index_generation_failure_is_not_loaded_as_empty(tmp_path, monkeypatch):
    house = tmp_path / "house"
    house.mkdir()
    index = tmp_path / "file_index.json"
    monkeypatch.setattr(deduplicator, "generate_file_list", lambda *args, **kwargs: False)

    with pytest.raises(RuntimeError, match="index generation failed"):
        deduplicator.load_index_entries(str(house), str(index), rescan=True)


def test_index_publish_rejects_symlink_without_touching_target(tmp_path):
    source = tmp_path / "source" / "file_index.json"
    house = tmp_path / "house"
    victim = tmp_path / "victim.json"
    source.parent.mkdir()
    house.mkdir()
    source.write_text(json.dumps({"version": 2, "entries": []}), encoding="utf-8")
    victim.write_text("preserve me", encoding="utf-8")
    (house / "file_index.json").symlink_to(victim)

    with pytest.raises(RuntimeError, match="symlink"):
        folderling.sync_house_index(str(source), str(house))

    assert victim.read_text(encoding="utf-8") == "preserve me"
    assert (house / "file_index.json").is_symlink()
