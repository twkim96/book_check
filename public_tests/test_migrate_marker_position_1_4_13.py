from pathlib import Path

import pytest

from tools.legacy import migrate_marker_position


def test_legacy_marker_migration_dry_run_is_read_only(tmp_path, capsys):
    house = tmp_path / "house"
    recent = house / "_최근"
    source = house / "ㄱ" / "〔P〕과거 작품.txt"
    source.parent.mkdir(parents=True)
    recent.mkdir()
    source.write_text("book", encoding="utf-8")
    recent_link = recent / source.name
    recent_link.symlink_to(source)

    assert migrate_marker_position.migrate(str(house), dry_run=True) == 1

    assert source.is_file()
    assert recent_link.is_symlink()
    assert recent_link.resolve() == source.resolve()
    assert "미리보기 완료" in capsys.readouterr().out


def test_legacy_marker_migration_actual_mode_hard_fails_before_walk(
    tmp_path, monkeypatch,
):
    house = tmp_path / "house"
    house.mkdir()
    walked = []
    monkeypatch.setattr(
        migrate_marker_position.os,
        "walk",
        lambda *_args, **_kwargs: walked.append(True),
    )

    with pytest.raises(RuntimeError, match="actual mode is disabled"):
        migrate_marker_position.migrate(str(house), dry_run=False)

    assert walked == []
    assert list(house.iterdir()) == []


def test_run_flag_remains_parseable_but_cannot_enable_mutation(tmp_path):
    options = migrate_marker_position.parse_args(
        ["--run", "--house", str(tmp_path)]
    )
    assert options == {"house_dir": str(tmp_path), "dry_run": False}
    with pytest.raises(RuntimeError, match="managed title/rename operation"):
        migrate_marker_position.migrate(**options)
