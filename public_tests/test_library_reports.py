import json

import pytest
import library_reports

from library_reports import (
    dedup_report_listing,
    dedup_report_path,
    export_dedup_report_text,
    read_dedup_report,
)


def _payload(*, exact_count=0, suspect_group_count=0):
    return {
        "schema_version": 1,
        "kind": "folderling_dedup",
        "generated_at": "2026-07-21T15:30:00+09:00",
        "summary": {
            "dry_run": False,
            "managed_mode": True,
            "include_temp": True,
            "exact_count": exact_count,
            "exact_mutation_count": exact_count,
            "suspect_group_count": suspect_group_count,
            "suspect_move_count": 0,
        },
        "exact_records": [],
        "suspect_groups": [],
        "suspect_move_records": [],
        "disambig_records": [],
        "blocked_strong_relations": [],
    }


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
    paired = root / "dedup_20260721_141500_123456.txt"
    paired.write_text(
        "[중복/검토 큐 정리 로그]\n"
        "모드: 실제 실행 | 정확 중복 quarantine 0개 | 검토 큐 그룹 0개\n",
        encoding="utf-8",
    )
    paired.with_suffix(".json").write_text(
        json.dumps(_payload(), ensure_ascii=False), encoding="utf-8"
    )
    current = root / "dedup_20260721_153000_654321.json"
    current.write_text(
        json.dumps(_payload(exact_count=3), ensure_ascii=False), encoding="utf-8"
    )
    strong = root / "strong_candidates_20260720_120535_063869.txt"
    strong.write_text(
        "강력 후보 감사: house 100개 / temp 0개 / 메타 후보 2쌍\n",
        encoding="utf-8",
    )
    cleanup = root / "manual_house_cleanup_1_4_0_20260728_120535_063869.json"
    cleanup.write_text(
        json.dumps({
            "schema_version": 2,
            "kind": "manual_house_cleanup_1_4_0",
            "generated_at": "2026-07-28T12:05:35+09:00",
            "result": {"quarantined": []},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "terminal.code-workspace").write_text("ignored", encoding="utf-8")
    return temp, old, paired, current, strong, cleanup


def test_dedup_report_listing_groups_legacy_pairs_and_json_only_reports(tmp_path):
    temp, _old, paired, current, _strong, cleanup = _reports(tmp_path)

    listing = dedup_report_listing(temp)

    assert listing["readonly"] is True
    assert listing["total"] == 5
    cleanup_item = next(
        item for item in listing["items"] if item["report_id"] == cleanup.stem
    )
    assert cleanup_item["kind"] == "cleanup"
    assert cleanup_item["text_available"] is False
    assert cleanup_item["structured_available"] is True
    current_item = next(
        item for item in listing["items"] if item["report_id"] == current.stem
    )
    assert current_item["text_available"] is False
    assert current_item["structured_available"] is True
    assert "정확 중복 quarantine 3개" in current_item["summary"]
    paired_item = next(item for item in listing["items"] if item["report_id"] == paired.stem)
    assert paired_item["text_available"] is True
    assert paired_item["structured_available"] is True
    old = next(item for item in listing["items"] if item["name"].startswith("dedup_20260720"))
    assert old["structured_available"] is False
    assert "정확 중복 quarantine 1개" in old["summary"]


def test_dedup_report_listing_pages_twenty_reports_at_a_time(tmp_path):
    root = tmp_path / "dedup_logs"
    root.mkdir()
    for index in range(25):
        (root / f"dedup_20260721_{index:06d}.txt").write_text(
            f"모드: page fixture {index}", encoding="utf-8"
        )

    first = dedup_report_listing(tmp_path, limit=20)
    assert first["total"] == 25
    assert len(first["items"]) == 20
    assert first["cursor"] is None
    assert first["next_cursor"] == "20"

    second = dedup_report_listing(tmp_path, limit=20, cursor=first["next_cursor"])
    assert len(second["items"]) == 5
    assert second["cursor"] == "20"
    assert second["next_cursor"] is None
    assert {item["report_id"] for item in first["items"]}.isdisjoint(
        item["report_id"] for item in second["items"]
    )


def test_listing_reads_report_contents_only_for_requested_page(
    tmp_path, monkeypatch,
):
    root = tmp_path / "dedup_logs"
    root.mkdir()
    for index in range(25):
        (root / f"dedup_20260721_{index:06d}.txt").write_text(
            f"모드: page fixture {index}", encoding="utf-8"
        )
    calls = []
    original = library_reports._report_item

    def tracked(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(library_reports, "_report_item", tracked)

    listing = dedup_report_listing(tmp_path, limit=5)

    assert listing["total"] == 25
    assert len(listing["items"]) == 5
    assert len(calls) == 5


def test_json_only_listing_reads_early_summary_without_parsing_large_tail(
    tmp_path, monkeypatch,
):
    root = tmp_path / "dedup_logs"
    root.mkdir()
    path = root / "dedup_20260721_153000_654321.json"
    payload = _payload(exact_count=4)
    payload["large_tail"] = ["x" * 1024] * 5000
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        library_reports,
        "_read_structured",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("listing must not parse the complete JSON report")
        ),
    )

    listing = dedup_report_listing(tmp_path, limit=1)

    assert listing["total"] == 1
    assert "정확 중복 quarantine 4개" in listing["items"][0]["summary"]


def test_json_only_detail_renders_text_and_exports_without_creating_txt(tmp_path):
    temp, _old, _paired, current, _strong, _cleanup = _reports(tmp_path)

    detail = read_dedup_report(temp, current.stem)
    export_name, export_text = export_dedup_report_text(temp, current.name)

    assert detail["structured_summary"]["exact_count"] == 3
    assert "[중복/검토 큐 정리 로그]" in detail["text"]
    assert "정확 중복 quarantine 3개" in detail["text"]
    assert export_name == f"{current.stem}.txt"
    assert export_text == detail["text"]
    assert not current.with_suffix(".txt").exists()
    assert dedup_report_path(temp, current.stem, structured=True) == current.resolve()
    with pytest.raises(FileNotFoundError):
        dedup_report_path(temp, current.stem)


def test_legacy_text_detail_and_export_remain_compatible(tmp_path):
    temp, old, _paired, _current, _strong, _cleanup = _reports(tmp_path)

    detail = read_dedup_report(temp, old.name)
    export_name, export_text = export_dedup_report_text(temp, old.stem)

    assert detail["text"] == old.read_text(encoding="utf-8")
    assert export_name == old.name
    assert export_text == detail["text"]
    assert dedup_report_path(temp, old.stem) == old.resolve()


def test_dedup_report_rejects_non_report_names_and_missing_json(tmp_path):
    temp, old, _paired, _current, _strong, _cleanup = _reports(tmp_path)

    with pytest.raises(ValueError):
        read_dedup_report(temp, "../success.log")
    with pytest.raises(FileNotFoundError):
        dedup_report_path(temp, old.name, structured=True)
