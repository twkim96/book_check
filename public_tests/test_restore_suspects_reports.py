import json

from restore_suspects import find_latest_log, parse_log_moves


def test_restore_preview_reads_json_only_dedup_report(tmp_path):
    root = tmp_path / "dedup_logs"
    root.mkdir()
    report = root / "dedup_20260721_160000_123456.json"
    report.write_text(
        json.dumps({
            "kind": "folderling_dedup",
            "suspect_move_records": [
                {
                    "status": "warning",
                    "entry": {
                        "name": "검토 작품.txt",
                        "source": "house",
                        "rel_path": "ㄱ/검토 작품.txt",
                    },
                },
                {
                    "status": "unchanged",
                    "entry": {
                        "name": "이동 안 함.txt",
                        "source": "house",
                        "rel_path": "ㅇ/이동 안 함.txt",
                    },
                },
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    assert find_latest_log(tmp_path) == str(report)
    assert parse_log_moves(report) == {
        "검토 작품.txt": [("house", "ㄱ/검토 작품.txt")]
    }
