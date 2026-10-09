"""Copied destinations tolerate verified ctime drift, never changed bytes."""

import os

import pytest

import mutation_io


@pytest.mark.parametrize("after_unlink", [False, True])
def test_copied_destination_metadata_change_preserves_bytes_and_returns_current_evidence(
    tmp_path, monkeypatch, after_unlink
):
    source, destination = tmp_path / "source.txt", tmp_path / "destination.txt"
    source.write_bytes(b"verified copy")
    copied = mutation_io.copy_no_clobber(source, destination)

    def touch_metadata():
        destination.chmod(0o700)
        destination.chmod(0o600)

    if after_unlink:
        original_unlink = mutation_io.unlink_owned

        def unlink_then_metadata(path, **kwargs):
            original_unlink(path, **kwargs)
            touch_metadata()

        monkeypatch.setattr(mutation_io, "unlink_owned", unlink_then_metadata)
    else:
        touch_metadata()
    evidence = mutation_io.consume_copied_source(copied)
    assert not source.exists()
    assert destination.read_bytes() == b"verified copy"
    assert evidence.sha256 == copied.destination_evidence.sha256
    assert evidence.ctime_ns == destination.stat().st_ctime_ns
    assert evidence.ctime_ns != copied.destination_evidence.ctime_ns


@pytest.mark.parametrize("change", ["content", "replacement", "hardlink", "symlink"])
def test_destination_changes_keep_source_intact(tmp_path, change):
    source, destination = tmp_path / "source.txt", tmp_path / "destination.txt"
    source.write_bytes(b"verified copy")
    copied = mutation_io.copy_no_clobber(source, destination)
    if change == "content":
        before = destination.stat()
        destination.write_bytes(b"modified copy")
        os.utime(destination, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif change == "replacement":
        replacement = tmp_path / "replacement.txt"
        replacement.write_bytes(source.read_bytes())
        replacement.replace(destination)
    elif change == "hardlink":
        os.link(destination, tmp_path / "linked.txt")
    else:
        destination.unlink()
        destination.symlink_to(source)
    with pytest.raises((RuntimeError, OSError)):
        mutation_io.consume_copied_source(copied)
    assert source.read_bytes() == b"verified copy"
