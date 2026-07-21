import json

import pytest

from library_reports import (
    dedup_report_listing,
    dedup_report_path,
    read_dedup_report,
)


def _reports(tmp_path):
    temp = tmp_path / "temp"
    root = temp / "dedup_logs"
    root.mkdir(parents=True)
    old = root / "dedup_20260720_171801.txt"
    old.write_text(
        "[중복/검토 큐 정리 로그]\n"
        "모드: 실제 실행 | 정확 중복 quarantine 1개 | 검토 큐 그룹 2개\n",
        encoding="utf-8",
    )
    current = root / "dedup_20260721_141500_123456.txt"
    current.write_text(
        "[중복/검토 큐 정리 로그]\n"
        "모드: 실제 실행 | 정확 중복 quarantine 0개 | 검토 큐 그룹 0개\n",
        encoding="utf-8",
    )
    current.with_suffix(".json").write_text(
        json.dumps({
            "schema_version": 1,
            "kind": "folderling_dedup",
            "generated_at": "2026-07-21T14:15:00+09:00",
            "summary": {"exact_count": 0, "suspect_group_count": 0},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    strong = root / "strong_candidates_20260720_120535_063869.txt"
    strong.write_text(
        "강력 후보 감사: house 100개 / temp 0개 / 메타 후보 2쌍\n",
        encoding="utf-8",
    )
    (root / "terminal.code-workspace").write_text("ignored", encoding="utf-8")
    return temp, old, current, strong


def test_dedup_report_listing_groups_historical_text_and_new_structured_reports(tmp_path):
    temp, _old, current, _strong = _reports(tmp_path)

    listing = dedup_report_listing(temp)

    assert listing["readonly"] is True
    assert listing["total"] == 3
    assert listing["items"][0]["name"] == current.name
    assert listing["items"][0]["structured_available"] is True
    old = next(item for item in listing["items"] if item["name"].startswith("dedup_20260720"))
    assert old["structured_available"] is False
    assert "정확 중복 quarantine 1개" in old["summary"]


def test_dedup_report_detail_reads_structured_summary_and_safe_downloads(tmp_path):
    temp, _old, current, _strong = _reports(tmp_path)

    detail = read_dedup_report(temp, current.name)

    assert detail["structured_summary"] == {
        "exact_count": 0,
        "suspect_group_count": 0,
    }
    assert dedup_report_path(temp, current.name) == current.resolve()
    assert dedup_report_path(temp, current.name, structured=True) == current.with_suffix(
        ".json"
    ).resolve()


def test_dedup_report_rejects_non_report_names_and_missing_json(tmp_path):
    temp, old, _current, _strong = _reports(tmp_path)

    with pytest.raises(ValueError):
        read_dedup_report(temp, "../success.log")
    with pytest.raises(FileNotFoundError):
        dedup_report_path(temp, old.name, structured=True)
