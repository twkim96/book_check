"""Read-only 1.2.7 title cleanup audit and FOUND-ZERO-DIFF gate."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from normalizer import (
    NORMALIZER_VERSION,
    extract_catalog_query_title,
    extract_core_title,
    extract_readable_title,
)
from project_paths import FILE_INDEX, STATE_DB
from title_cleanup_rules import apply_title_cleanup_rules, rule_ids


PLATFORMS = ("series", "kakao", "novelpia")


class ProtectedTitleDiff(RuntimeError):
    """Raised when a candidate would alter a title with an existing success."""


def _sha256_file(path: Optional[Path]) -> Optional[str]:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_rows(rows: Iterable[Sequence[object]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            json.dumps(list(row), ensure_ascii=False, separators=(",", ":"), default=str)
            .encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _logical_snapshot(conn: sqlite3.Connection) -> Dict[str, str]:
    return {
        "active_house": _hash_rows(conn.execute(
            """
            SELECT file_id, canonical_path, size, mtime_ns, ctime_ns, active, source
            FROM files WHERE active = 1 AND source = 'house'
            ORDER BY file_id
            """
        )),
        "file_analysis": _hash_rows(conn.execute(
            """
            SELECT file_id, normalizer_version, analyzed_name, core_title,
                   readable_title, catalog_query_title, title_override_json, analyzed_size,
                   analyzed_mtime_ns, analyzed_ctime_ns
            FROM file_analysis ORDER BY file_id
            """
        )),
        "catalog": _hash_rows(conn.execute(
            """
            SELECT title_key, display_title, query_title, normalizer_version
            FROM catalog_titles ORDER BY title_key
            """
        )),
        "platform_stats": _hash_rows(conn.execute(
            """
            SELECT title_key, platform, status, remote_id, remote_title,
                   download_count, interest_count, view_count, recommend_count,
                   rating, rating_count, last_attempt_at, last_success_at,
                   retry_after, error_message
            FROM catalog_platform_stats ORDER BY title_key, platform
            """
        )),
    }


def _title_states(conn: sqlite3.Connection) -> Dict[str, dict]:
    states: Dict[str, dict] = {}
    for row in conn.execute(
        "SELECT title_key, display_title, query_title FROM catalog_titles ORDER BY title_key"
    ):
        states[row["title_key"]] = {
            "display_title": row["display_title"],
            "query_title": row["query_title"],
            "statuses": {},
        }
    for row in conn.execute(
        "SELECT title_key, platform, status FROM catalog_platform_stats "
        "ORDER BY title_key, platform"
    ):
        state = states.setdefault(row["title_key"], {
            "display_title": row["title_key"],
            "query_title": row["title_key"],
            "statuses": {},
        })
        state["statuses"][row["platform"]] = row["status"]
    for state in states.values():
        statuses = state["statuses"]
        state["has_ok"] = any(status == "ok" for status in statuses.values())
        state["all_not_found"] = (
            set(statuses) == set(PLATFORMS)
            and all(statuses[platform] == "not_found" for platform in PLATFORMS)
        )
    return states


def _source_category(state: Mapping[str, object]) -> str:
    if state.get("has_ok"):
        return "protected"
    if state.get("all_not_found"):
        return "all_not_found"
    return "error_or_missing"


def _source_state(states: Mapping[str, dict], title_key: str) -> dict:
    return states.get(title_key, {
        "display_title": title_key,
        "query_title": title_key,
        "statuses": {},
        "has_ok": False,
        "all_not_found": False,
    })


def _candidate_rows(conn: sqlite3.Connection, states: Mapping[str, dict]) -> List[dict]:
    rows = conn.execute(
        """
        SELECT f.file_id, f.canonical_path, f.size, f.mtime_ns,
               a.analyzed_name, a.normalizer_version,
               a.readable_title, a.catalog_query_title, a.core_title,
               a.title_override_json
        FROM files AS f
        JOIN file_analysis AS a ON a.file_id = f.file_id
        WHERE f.active = 1 AND f.source = 'house'
        ORDER BY f.canonical_path
        """
    ).fetchall()
    candidates = []
    for row in rows:
        # 사용자 literal 제목은 자동 정리 규칙보다 우선한다. 다시 바꾸려면
        # 1.2.8 제목 교정 화면에서 사용자가 명시적으로 수정한다.
        if row["title_override_json"]:
            continue
        proposal = apply_title_cleanup_rules(row["analyzed_name"])
        if not proposal.matched:
            continue
        proposed_readable = extract_readable_title(proposal.candidate_name).strip()
        proposed_query = extract_catalog_query_title(proposal.candidate_name).strip()
        proposed_core = extract_core_title(proposal.candidate_name).strip()
        changed_fields = [
            field
            for field, before, after in (
                ("readable_title", row["readable_title"], proposed_readable),
                ("query_title", row["catalog_query_title"], proposed_query),
                ("core_title", row["core_title"], proposed_core),
            )
            if str(before or "").strip() != str(after or "").strip()
        ]
        source_state = _source_state(states, row["core_title"])
        target_state = states.get(proposed_core)
        candidates.append({
            "file_id": row["file_id"],
            "canonical_path": row["canonical_path"],
            "analyzed_name": row["analyzed_name"],
            "candidate_name": proposal.candidate_name,
            "rule_ids": list(proposal.rule_ids),
            "changed_fields": changed_fields,
            "source_category": _source_category(source_state),
            "source_statuses": dict(source_state.get("statuses", {})),
            "source_protected": bool(source_state.get("has_ok")),
            "before_readable_title": row["readable_title"],
            "after_readable_title": proposed_readable,
            "before_query_title": row["catalog_query_title"],
            "after_query_title": proposed_query,
            "before_core_title": row["core_title"],
            "after_core_title": proposed_core,
            "target_exists": target_state is not None,
            "target_protected": bool(target_state and target_state.get("has_ok")),
            "target_statuses": dict(target_state.get("statuses", {})) if target_state else {},
        })
    return candidates


def _unique_by_source(rows: Iterable[dict]) -> Dict[str, dict]:
    return {row["before_core_title"]: row for row in rows}


def _stats_for_rows(rows: List[dict], sample_limit: int) -> dict:
    changed = [row for row in rows if row["changed_fields"]]
    sources = _unique_by_source(changed)
    matched_sources = _unique_by_source(rows)
    return {
        "matched_files": len(rows),
        "matched_source_keys": len(matched_sources),
        "changed_files": len(changed),
        "changed_source_keys": len(sources),
        "all_not_found_source_keys": sum(
            row["source_category"] == "all_not_found" for row in sources.values()
        ),
        "error_or_missing_source_keys": sum(
            row["source_category"] == "error_or_missing" for row in sources.values()
        ),
        "protected_source_diffs": sum(
            row["source_protected"] for row in sources.values()
        ),
        "target_collisions": sum(
            row["target_exists"]
            and row["after_core_title"] != row["before_core_title"]
            for row in sources.values()
        ),
        "protected_target_collisions": sum(
            row["target_protected"]
            and row["after_core_title"] != row["before_core_title"]
            for row in sources.values()
        ),
        "samples": [
            {
                "analyzed_name": row["analyzed_name"],
                "candidate_name": row["candidate_name"],
                "before_query_title": row["before_query_title"],
                "after_query_title": row["after_query_title"],
                "before_core_title": row["before_core_title"],
                "after_core_title": row["after_core_title"],
                "source_category": row["source_category"],
                "target_protected": row["target_protected"],
            }
            for row in changed[:sample_limit]
        ],
    }


def audit_candidates(
    state_db: Path,
    *,
    index_path: Optional[Path] = None,
    sample_limit: int = 8,
) -> dict:
    state_db = Path(state_db).expanduser().resolve()
    if not state_db.is_file():
        raise FileNotFoundError(f"state DB not found: {state_db}")
    index_path = Path(index_path).expanduser().resolve() if index_path else None
    index_before = _sha256_file(index_path)
    uri = f"file:{state_db.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity_check failed: {integrity}")
        before = _logical_snapshot(conn)
        states = _title_states(conn)
        candidates = _candidate_rows(conn, states)
        after = _logical_snapshot(conn)
        if conn.total_changes != 0 or before != after:
            raise RuntimeError("read-only audit changed the logical SQLite snapshot")
    finally:
        conn.close()
    index_after = _sha256_file(index_path)
    if index_before != index_after:
        raise RuntimeError("read-only audit changed the file index")

    per_rule = {}
    for rule_id in rule_ids():
        per_rule[rule_id] = _stats_for_rows(
            [row for row in candidates if rule_id in row["rule_ids"]], sample_limit
        )
    combined = _stats_for_rows(candidates, sample_limit)
    protected_diffs = [
        row for row in candidates if row["source_protected"] and row["changed_fields"]
    ]
    return {
        "normalizer_version": NORMALIZER_VERSION,
        "state_db": str(state_db),
        "index_path": str(index_path) if index_path else None,
        "read_only": True,
        "integrity_check": "ok",
        "input_unchanged": before == after and index_before == index_after,
        "input_evidence": {
            "logical_snapshot": before,
            "index_sha256": index_before,
        },
        "catalog_title_keys": len(states),
        "protected_title_keys": sum(bool(state.get("has_ok")) for state in states.values()),
        "protected_ok_rows": sum(
            status == "ok"
            for state in states.values()
            for status in state.get("statuses", {}).values()
        ),
        "rules": per_rule,
        "combined": combined,
        "protected_diff_count": len(_unique_by_source(protected_diffs)),
        "protected_diffs": protected_diffs,
        "candidates": candidates,
    }


CSV_FIELDS = (
    "file_id", "canonical_path", "analyzed_name", "candidate_name",
    "rule_ids", "changed_fields", "source_category", "source_protected",
    "before_readable_title", "after_readable_title",
    "before_query_title", "after_query_title",
    "before_core_title", "after_core_title", "target_exists", "target_protected",
)


def write_json_report(report: Mapping[str, object], path: Path) -> Path:
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return path


def write_csv_report(rows: Iterable[Mapping[str, object]], path: Path) -> Path:
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            record = {field: row.get(field) for field in CSV_FIELDS}
            record["rule_ids"] = ",".join(row.get("rule_ids", []))
            record["changed_fields"] = ",".join(row.get("changed_fields", []))
            writer.writerow(record)
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="1.2.7 제목 정리 후보를 SQLite/index 변경 없이 전수 감사합니다."
    )
    parser.add_argument("--state-db", default=str(STATE_DB))
    parser.add_argument("--index", default=str(FILE_INDEX))
    parser.add_argument("--json-out")
    parser.add_argument("--csv-out")
    parser.add_argument("--sample-limit", type=int, default=8)
    return parser


def _print_summary(report: Mapping[str, object]) -> None:
    combined = report["combined"]
    print("🔎 1.2.7 제목 후보 읽기 전용 감사")
    print(f"  catalog title key : {report['catalog_title_keys']:,}")
    print(f"  보호 title key    : {report['protected_title_keys']:,}")
    print(f"  문법 일치 파일     : {combined['matched_files']:,}")
    print(f"  실제 변경 source   : {combined['changed_source_keys']:,}")
    print(f"  보호 source diff   : {report['protected_diff_count']:,}")
    print(f"  target 충돌        : {combined['target_collisions']:,}")
    print(f"  보호 target 충돌   : {combined['protected_target_collisions']:,}")
    print(f"  입력 불변 확인      : {'ok' if report['input_unchanged'] else 'failed'}")
    for rule_id, stats in report["rules"].items():
        if not stats["matched_files"]:
            continue
        print(
            f"  - {rule_id}: match={stats['matched_files']:,} "
            f"changed_keys={stats['changed_source_keys']:,} "
            f"protected={stats['protected_source_diffs']:,}"
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = audit_candidates(
            Path(args.state_db),
            index_path=Path(args.index) if args.index else None,
            sample_limit=max(0, args.sample_limit),
        )
        _print_summary(report)
        if args.json_out:
            print(f"  JSON: {write_json_report(report, Path(args.json_out))}")
        if args.csv_out:
            print(f"  CSV : {write_csv_report(report['candidates'], Path(args.csv_out))}")
        if report["protected_diff_count"]:
            print("❌ FOUND-ZERO-DIFF 실패: 보호 작품 제목 변경 후보가 있습니다.")
            return 3
        print("✅ FOUND-ZERO-DIFF 통과")
        return 0
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"❌ 제목 후보 감사 실패: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
