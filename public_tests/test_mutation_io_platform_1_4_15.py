from pathlib import Path

import pytest

import mutation_io


def test_linux_tmp_and_var_paths_are_not_rewritten(monkeypatch):
    monkeypatch.setattr(mutation_io.sys, "platform", "linux")

    assert mutation_io.canonical_absolute_path("/tmp/example") == Path(
        "/tmp/example"
    )
    assert mutation_io.canonical_absolute_path("/var/example") == Path(
        "/var/example"
    )


def test_macos_stable_tmp_and_var_aliases_are_folded(monkeypatch):
    monkeypatch.setattr(mutation_io.sys, "platform", "darwin")

    assert mutation_io.canonical_absolute_path("/tmp/example") == Path(
        "/private/tmp/example"
    )
    assert mutation_io.canonical_absolute_path("/var/example") == Path(
        "/private/var/example"
    )


def test_json_evidence_read_enforces_the_byte_limit(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"payload":"too large"}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="exceeds the size limit"):
        mutation_io.read_json_with_evidence(manifest, max_bytes=8)
