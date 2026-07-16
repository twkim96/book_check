#!/usr/bin/env python3
"""Terminal control-server entry point for platform catalog collection."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import decision_store
import platform_catalog
from mutation_io import mutation_lock_for_roots
from project_paths import HOUSE_DIR, STATE_DB, TEMP_DIR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="플랫폼 인기/평점 카탈로그를 안전한 소량 batch로 갱신합니다."
    )
    parser.add_argument("--state-db", default=str(STATE_DB))
    subparsers = parser.add_subparsers(dest="command", required=True)

    refresh = subparsers.add_parser(
        "refresh", help="미수집 또는 재시도 가능한 플랫폼 지표를 채웁니다."
    )
    refresh.add_argument("--limit", type=int, default=platform_catalog.DEFAULT_LIMIT)
    refresh.add_argument("--all", action="store_true", help="안전 기본 batch 제한 없이 모든 대상")
    refresh.add_argument(
        "--delay-seconds", type=float, default=platform_catalog.DEFAULT_DELAY_SECONDS,
        help="한 제목 처리 뒤 다음 제목까지의 최소 지연 (기본 3초)",
    )
    refresh.add_argument("--timeout", type=float, default=platform_catalog.DEFAULT_TIMEOUT_SECONDS)
    refresh.add_argument("--retry-not-found", action="store_true")
    refresh.add_argument("--refresh-after-days", type=float)
    refresh.add_argument("--force", action="store_true")
    refresh.add_argument("--dry-run", action="store_true", help="DB/네트워크 변경 없이 대상만 미리 봄")
    refresh.add_argument(
        "--error-retry-seconds", type=int,
        default=platform_catalog.DEFAULT_ERROR_RETRY_SECONDS,
    )

    subparsers.add_parser("status", help="카탈로그 수집 현황을 표시합니다.")
    top = subparsers.add_parser("top", help="플랫폼 지표 상위 작품을 표시합니다.")
    top.add_argument("--order-by", choices=sorted(platform_catalog._ORDER_COLUMNS), required=True)
    top.add_argument("--limit", type=int, default=20)
    return parser


def _schema_version(path: Path) -> int:
    if not path.exists():
        return 0
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _backup_path(state_db: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return state_db.parent / "backups" / (
        f"before_platform_catalog_schema_{stamp}_{uuid.uuid4().hex[:8]}.sqlite3"
    )


def ensure_catalog_schema(state_db_path: str) -> Optional[Path]:
    """Back up an existing older schema before adding the independent catalog."""
    state_db = Path(state_db_path)
    with mutation_lock_for_roots(HOUSE_DIR, TEMP_DIR, "platform-catalog-schema"):
        original_version = _schema_version(state_db)
        if original_version > decision_store.SCHEMA_VERSION:
            raise RuntimeError(
                f"state DB schema is newer than this program: {original_version}"
            )
        backup = None
        if state_db.is_file() and original_version < decision_store.SCHEMA_VERSION:
            conn = decision_store.connect_state_db(state_db)
            try:
                backup = decision_store.backup_state_db(conn, _backup_path(state_db))
            finally:
                conn.close()
        conn = decision_store.initialize_state_db(state_db)
        conn.close()
        return backup


def run(args: argparse.Namespace):
    if args.command == "refresh":
        limit = None if args.all else args.limit
        if args.dry_run:
            backup = None
            result = platform_catalog.preview_catalog_refresh(
                args.state_db,
                limit=limit,
                retry_not_found=args.retry_not_found,
                refresh_after_days=args.refresh_after_days,
                force=args.force,
            )
        else:
            backup = ensure_catalog_schema(args.state_db)
            result = platform_catalog.refresh_catalog(
                args.state_db,
                limit=limit,
                delay_seconds=args.delay_seconds,
                timeout=args.timeout,
                retry_not_found=args.retry_not_found,
                refresh_after_days=args.refresh_after_days,
                force=args.force,
                error_retry_seconds=args.error_retry_seconds,
            )
    elif args.command == "status":
        backup = None
        result = platform_catalog.catalog_status(args.state_db)
    elif args.command == "top":
        backup = None
        result = {
            "order_by": args.order_by,
            "rows": platform_catalog.top_catalog_metrics(
                args.state_db, order_by=args.order_by, limit=args.limit,
            ),
        }
    else:  # argparse owns this branch; retain a fail-closed API contract.
        raise ValueError(f"unknown command: {args.command}")
    if backup is not None:
        result = {"schema_backup": str(backup), **result}
    return result


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"❌ 플랫폼 카탈로그 실행 실패: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
