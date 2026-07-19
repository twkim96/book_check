"""Dry-run-first 1.2.7 title correction requeue workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional, Sequence
from urllib.parse import unquote

import decision_store
from mutation_io import mutation_lock_for_roots
from project_paths import FILE_INDEX, HOUSE_DIR, STATE_DB, TEMP_DIR
from title_cleanup_candidates import audit_candidates, write_json_report
from title_cleanup_mutations import requeue_unassigned_title_file


def _materialized_filename(candidate_name: str) -> str:
    value = unicodedata.normalize("NFC", unquote(candidate_name or "")).strip()
    if not value or value in {".", ".."} or "\x00" in value:
        raise ValueError("candidate filename is empty or invalid")
    if Path(value).name != value or "/" in value or "\\" in value:
        raise ValueError(f"candidate filename contains a path separator: {value!r}")
    return value


def _under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _transport_collision_dir(temp_dir: Path, counter: int) -> Path:
    return temp_dir / f"title_cleanup_collision_{counter}"


def _plan_sha256(items) -> str:
    payload = [
        {
            "file_id": item["file_id"],
            "source_path": item["source_path"],
            "destination_path": item["destination_path"],
            "before_core_title": item["before_core_title"],
            "after_core_title": item["after_core_title"],
            "blocked_reasons": item["blocked_reasons"],
        }
        for item in items
    ]
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def build_requeue_plan(
    state_db: Path,
    *,
    house_dir: Path,
    temp_dir: Path,
    index_path: Optional[Path] = None,
) -> dict:
    state_db = Path(state_db).expanduser().resolve()
    house_dir = Path(house_dir).expanduser().resolve()
    temp_dir = Path(temp_dir).expanduser().resolve()
    report = audit_candidates(state_db, index_path=index_path, sample_limit=3)
    if report["protected_diff_count"]:
        raise RuntimeError("FOUND-ZERO-DIFF failed; refusing title cleanup plan")

    conn = sqlite3.connect(f"file:{state_db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        states = {
            row["file_id"]: row
            for row in conn.execute(
                """
                SELECT f.file_id, f.canonical_path, f.source, f.active,
                       f.size, f.mtime_ns, f.dev, f.ino, f.ctime_ns,
                       f.assignment_state, f.variant_id, f.protected,
                       f.current_fingerprint_id,
                       CASE WHEN r.file_id IS NULL THEN 0 ELSE 1 END AS representative
                FROM files AS f
                LEFT JOIN representatives AS r ON r.file_id = f.file_id
                WHERE f.active = 1 AND f.source = 'house'
                """
            )
        }
    finally:
        conn.close()

    items = []
    destination_owners = {}
    for candidate in report["candidates"]:
        if not candidate["changed_fields"]:
            continue
        blocked = []
        row = states.get(candidate["file_id"])
        if row is None:
            blocked.append("active_house_state_missing")
            source = Path(candidate["canonical_path"])
        else:
            source = Path(row["canonical_path"])
            if (
                row["assignment_state"] not in {"unassigned", "decision_required"}
                or row["variant_id"] is not None
                or row["protected"]
                or row["representative"]
            ):
                blocked.append("managed_or_protected_source")
        if candidate["source_protected"]:
            blocked.append("protected_source_diff")
        if not _under_root(source, house_dir):
            blocked.append("source_outside_house")
        if not source.is_file() or source.is_symlink():
            blocked.append("source_missing_or_not_regular")
        elif row is not None:
            stat_result = source.stat()
            actual = (
                stat_result.st_dev, stat_result.st_ino, stat_result.st_ctime_ns,
                stat_result.st_size, stat_result.st_mtime_ns,
            )
            expected = (
                row["dev"], row["ino"], row["ctime_ns"],
                row["size"], row["mtime_ns"],
            )
            if actual != expected:
                blocked.append("source_identity_stale")
        try:
            filename = _materialized_filename(candidate["candidate_name"])
        except ValueError as exc:
            filename = ""
            blocked.append(f"invalid_candidate_name:{exc}")
        corrected_filename = filename
        destination = temp_dir / filename if filename else temp_dir
        transport_subdir = None
        if filename:
            owner = destination_owners.get(str(destination))
            if owner is not None and owner != candidate["file_id"]:
                counter = 1
                while True:
                    collision_dir = _transport_collision_dir(temp_dir, counter)
                    transport_destination = collision_dir / filename
                    if (
                        str(transport_destination) not in destination_owners
                        and not transport_destination.exists()
                        and not transport_destination.is_symlink()
                    ):
                        destination = transport_destination
                        transport_subdir = collision_dir.name
                        break
                    counter += 1
            destination_owners[str(destination)] = candidate["file_id"]
            if destination.exists() or destination.is_symlink():
                blocked.append("temp_destination_exists")
        items.append({
            "file_id": candidate["file_id"],
            "source_path": str(source),
            "destination_path": str(destination),
            "original_name": candidate["analyzed_name"],
            "candidate_name": corrected_filename,
            "transport_name": filename,
            "transport_subdir": transport_subdir,
            "rule_ids": candidate["rule_ids"],
            "before_core_title": candidate["before_core_title"],
            "after_core_title": candidate["after_core_title"],
            "before_query_title": candidate["before_query_title"],
            "after_query_title": candidate["after_query_title"],
            "target_exists": candidate["target_exists"],
            "target_protected": candidate["target_protected"],
            "current_fingerprint_missing": bool(
                row is not None and row["current_fingerprint_id"] is None
            ),
            "blocked_reasons": blocked,
        })

    blocked_items = [item for item in items if item["blocked_reasons"]]
    plan_sha256 = _plan_sha256(items)
    return {
        "version": "1.2.7",
        "read_only": True,
        "state_db": str(state_db),
        "house_dir": str(house_dir),
        "temp_dir": str(temp_dir),
        "candidate_audit": {
            "input_unchanged": report["input_unchanged"],
            "protected_diff_count": report["protected_diff_count"],
            "changed_source_keys": report["combined"]["changed_source_keys"],
            "target_collisions": report["combined"]["target_collisions"],
            "protected_target_collisions": report["combined"]["protected_target_collisions"],
        },
        "item_count": len(items),
        "plan_sha256": plan_sha256,
        "blocked_count": len(blocked_items),
        "missing_fingerprint_count": sum(
            item["current_fingerprint_missing"] for item in items
        ),
        "runnable": bool(items) and not blocked_items,
        "items": items,
    }


def _backup_path(state_db: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return state_db.parent / "backups" / (
        f"before_title_cleanup_requeue_{stamp}_{uuid.uuid4().hex[:8]}.sqlite3"
    )


def apply_requeue_plan(
    state_db: Path,
    *,
    house_dir: Path,
    temp_dir: Path,
    index_path: Optional[Path],
    confirm_count: int,
    confirm_plan_sha256: str,
    progress=None,
) -> dict:
    state_db = Path(state_db).expanduser().resolve()
    house_dir = Path(house_dir).expanduser().resolve()
    temp_dir = Path(temp_dir).expanduser().resolve()
    with mutation_lock_for_roots(house_dir, temp_dir, "title-cleanup-1.2.7"):
        plan = build_requeue_plan(
            state_db,
            house_dir=house_dir,
            temp_dir=temp_dir,
            index_path=index_path,
        )
        if not plan["runnable"]:
            raise RuntimeError(
                f"title cleanup plan is not runnable: blocked={plan['blocked_count']}"
            )
        if confirm_count != plan["item_count"]:
            raise RuntimeError(
                "title cleanup confirmation count mismatch: "
                f"expected={plan['item_count']} provided={confirm_count}"
            )
        if confirm_plan_sha256 != plan["plan_sha256"]:
            raise RuntimeError(
                "title cleanup plan SHA-256 mismatch: "
                f"expected={plan['plan_sha256']} provided={confirm_plan_sha256}"
            )
        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(
                    f"doctor failed before title cleanup: {len(issues)} issue(s), "
                    f"first={issues[0].get('kind')}"
                )
            backup = decision_store.backup_state_db(conn, _backup_path(state_db))
            decision_store.issue_actual_run_token(
                conn,
                str(backup),
                house_dir=house_dir,
                temp_dir=temp_dir,
            )
        finally:
            conn.close()

        run_id, manifest_path = decision_store.prepare_actual_run(
            state_db, house_dir, temp_dir
        )
        completed = []
        try:
            conn = decision_store.connect_state_db(state_db)
            try:
                for index, item in enumerate(plan["items"], start=1):
                    result = requeue_unassigned_title_file(
                        conn,
                        file_id=item["file_id"],
                        destination=item["destination_path"],
                        run_id=run_id,
                    )
                    completed.append(result)
                    if progress is not None:
                        progress(index, plan["item_count"], item)
            finally:
                conn.close()
        except (Exception, KeyboardInterrupt) as exc:
            conn = decision_store.connect_state_db(state_db)
            try:
                decision_store.finish_actual_run(
                    conn, run_id, success=False, error=str(exc)
                )
            finally:
                conn.close()
            raise
        conn = decision_store.connect_state_db(state_db)
        try:
            decision_store.finish_actual_run(conn, run_id, success=True)
        finally:
            conn.close()
        return {
            "run_id": run_id,
            "manifest_path": manifest_path,
            "backup_path": str(backup),
            "planned": plan["item_count"],
            "completed": len(completed),
            "next_action": "run Folderling one-button",
            "operations": completed,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="1.2.7 제목 교정 파일을 dry-run 후 txt_temp로 안전 재입고합니다."
    )
    parser.add_argument("--state-db", default=str(STATE_DB))
    parser.add_argument("--index", default=str(FILE_INDEX))
    parser.add_argument("--house", default=str(HOUSE_DIR))
    parser.add_argument("--temp", default=str(TEMP_DIR))
    parser.add_argument("--manifest-out")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--confirm-count", type=int)
    parser.add_argument("--confirm-plan-sha256")
    return parser


def _progress(index: int, total: int, item: Mapping[str, object]) -> None:
    if index == 1 or index == total or index % 25 == 0:
        print(
            f"⏳ 제목 교정 재입고 {index:,}/{total:,}: {item['candidate_name']}",
            flush=True,
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    state_db = Path(args.state_db)
    index_path = Path(args.index) if args.index else None
    try:
        if args.run:
            if args.confirm_count is None or not args.confirm_plan_sha256:
                raise ValueError(
                    "--run requires --confirm-count and --confirm-plan-sha256 "
                    "from the latest dry-run"
                )
            result = apply_requeue_plan(
                state_db,
                house_dir=Path(args.house),
                temp_dir=Path(args.temp),
                index_path=index_path,
                confirm_count=args.confirm_count,
                confirm_plan_sha256=args.confirm_plan_sha256,
                progress=_progress,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        plan = build_requeue_plan(
            state_db,
            house_dir=Path(args.house),
            temp_dir=Path(args.temp),
            index_path=index_path,
        )
        print("🔎 1.2.7 제목 교정 재입고 dry-run")
        print(f"  대상             : {plan['item_count']:,}")
        print(f"  차단             : {plan['blocked_count']:,}")
        print(f"  fingerprint 준비 : {plan['missing_fingerprint_count']:,}")
        print(f"  plan SHA-256     : {plan['plan_sha256']}")
        print(f"  실행 가능        : {'yes' if plan['runnable'] else 'no'}")
        if args.manifest_out:
            print(f"  manifest         : {write_json_report(plan, Path(args.manifest_out))}")
        if plan["runnable"]:
            print(
                "실제 실행은 같은 상태에서 --run "
                f"--confirm-count {plan['item_count']} "
                f"--confirm-plan-sha256 {plan['plan_sha256']} 를 명시해야 합니다."
            )
            return 0
        return 3
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"❌ 제목 교정 재입고 실패: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
