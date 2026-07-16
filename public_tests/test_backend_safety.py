import os

import decision_store
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


def test_doctor_ignores_unassigned_ctime_only_but_rejects_inode_replacement(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    source = tmp_path / "house" / "합성작품.txt"
    source.parent.mkdir()
    source.write_text("합성 본문", encoding="utf-8")
    conn = decision_store.initialize_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            decision_store.reconcile_file_metadata(conn, source, source="house")
        file_id = conn.execute(
            "SELECT file_id FROM files WHERE canonical_path = ?", (str(source.resolve()),)
        ).fetchone()[0]
        before = source.stat()
        source.chmod(before.st_mode ^ 0o100)
        source.chmod(before.st_mode)
        after_metadata = source.stat()
        assert after_metadata.st_ino == before.st_ino
        assert after_metadata.st_mtime_ns == before.st_mtime_ns
        assert not [
            issue for issue in decision_store.doctor_issues(conn)
            if issue["kind"] in {"stale_identity", "stale_snapshot"}
        ]

        replacement = source.with_suffix(".replacement")
        replacement.write_bytes(source.read_bytes())
        replacement.replace(source)
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        issues = decision_store.doctor_issues(conn)
        assert any(
            issue["kind"] == "stale_identity" and issue["file_id"] == file_id
            for issue in issues
        )
    finally:
        conn.close()
