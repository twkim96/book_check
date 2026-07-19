#!/usr/bin/env python3
"""Terminal control-server entry point for platform catalog collection."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import decision_store
import platform_catalog
from mutation_io import mutation_lock_for_roots
from project_paths import FILE_INDEX, HOUSE_DIR, STATE_DB, TEMP_DIR


NOVELPIA_AUTH_RETRY_SETTING_KEY = "platform_novelpia_auth_retry_once_v1"
FAILED_RETRY_SETTING_KEY = "platform_failed_retry_cycle_v2"


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
        help="한 제목 처리 뒤 다음 제목까지의 최소 지연 (기본 1초)",
    )
    refresh.add_argument("--timeout", type=float, default=platform_catalog.DEFAULT_TIMEOUT_SECONDS)
    refresh.add_argument("--retry-not-found", action="store_true")
    refresh.add_argument("--refresh-after-days", type=float)
    refresh.add_argument("--force", action="store_true")
    refresh.add_argument("--dry-run", action="store_true", help="DB/네트워크 변경 없이 대상만 미리 봄")
    refresh.add_argument(
        "--sync-sheet", action="store_true",
        help="플랫폼 수집이 정상 종료된 뒤 Google Sheet도 갱신",
    )
    refresh.add_argument(
        "--error-retry-seconds", type=int,
        default=platform_catalog.DEFAULT_ERROR_RETRY_SECONDS,
    )
    refresh.add_argument(
        "--require-novelpia-auth", action="store_true",
        help="인증 환경변수가 없거나 로그인이 실패하면 일반 수집 전에 종료",
    )

    refresh_existing = subparsers.add_parser(
        "refresh-existing",
        help="기존 성공 플랫폼만 재조회해 증가한 인기값과 평점을 갱신합니다.",
    )
    refresh_existing.add_argument(
        "--limit", type=int, default=platform_catalog.DEFAULT_LIMIT
    )
    refresh_existing.add_argument(
        "--all", action="store_true", help="안전 기본 batch 제한 없이 모든 대상"
    )
    refresh_existing.add_argument(
        "--delay-seconds", type=float, default=platform_catalog.DEFAULT_DELAY_SECONDS,
        help="한 제목 처리 뒤 다음 제목까지의 최소 지연 (기본 1초)",
    )
    refresh_existing.add_argument(
        "--timeout", type=float, default=platform_catalog.DEFAULT_TIMEOUT_SECONDS
    )
    refresh_existing.add_argument(
        "--dry-run", action="store_true", help="DB/네트워크 변경 없이 대상 수만 확인"
    )
    refresh_existing.add_argument(
        "--require-novelpia-auth", action="store_true",
        help="인증 환경변수가 없거나 로그인이 실패하면 갱신 전에 종료",
    )

    retry_failed = subparsers.add_parser(
        "retry-failed",
        help="현재 not_found/error 플랫폼 행을 플랫폼 쌍 규칙으로 재검사합니다.",
    )
    retry_failed.add_argument(
        "--delay-seconds", type=float, default=platform_catalog.DEFAULT_DELAY_SECONDS,
        help="한 제목 처리 뒤 다음 제목까지의 최소 지연 (기본 1초)",
    )
    retry_failed.add_argument(
        "--timeout", type=float, default=platform_catalog.DEFAULT_TIMEOUT_SECONDS
    )
    retry_failed.add_argument(
        "--error-retry-seconds", type=int,
        default=platform_catalog.DEFAULT_ERROR_RETRY_SECONDS,
    )
    retry_failed.add_argument(
        "--dry-run", action="store_true", help="DB/네트워크 변경 없이 대상 수만 확인"
    )
    retry_failed.add_argument(
        "--require-novelpia-auth", action="store_true",
        help="인증 환경변수가 없거나 로그인이 실패하면 재검사 전에 종료",
    )

    retry_novelpia = subparsers.add_parser(
        "retry-novelpia-auth",
        help="세 플랫폼 모두 not_found인 기존 제목을 인증된 노벨피아 검색으로 한 번 재검사합니다.",
    )
    retry_novelpia.add_argument("--limit", type=int)
    retry_novelpia.add_argument(
        "--delay-seconds", type=float, default=platform_catalog.DEFAULT_DELAY_SECONDS,
        help="한 제목 처리 뒤 다음 제목까지의 최소 지연 (기본 1초)",
    )
    retry_novelpia.add_argument(
        "--timeout", type=float, default=platform_catalog.DEFAULT_TIMEOUT_SECONDS
    )
    retry_novelpia.add_argument(
        "--error-retry-seconds", type=int,
        default=platform_catalog.DEFAULT_ERROR_RETRY_SECONDS,
    )
    retry_novelpia.add_argument(
        "--dry-run", action="store_true", help="로그인/DB 변경 없이 대상 수만 확인"
    )

    metadata = subparsers.add_parser(
        "file-metadata-sync",
        help="파일명 분석 메타데이터를 schema v10 DB에 안전하게 동기화합니다.",
    )
    metadata.add_argument(
        "--dry-run", action="store_true", help="DB 변경 없이 대상 수만 확인"
    )

    sheet = subparsers.add_parser(
        "sheet-sync", help="SQLite 현재 상태를 조회용 Google Sheet에 단방향 복제합니다."
    )
    sheet.add_argument(
        "--dry-run", action="store_true",
        help="Google 네트워크/SQLite 변경 없이 행 수만 확인",
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


def _metadata_rekey_backup_path(state_db: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return state_db.parent / "backups" / (
        f"before_normalizer_rekey_{stamp}_{uuid.uuid4().hex[:8]}.sqlite3"
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
        conn = decision_store.initialize_state_db(state_db, migrate=True)
        conn.close()
        return backup


def _duration_text(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}시간 {minutes}분"
    if minutes:
        return f"{minutes}분 {secs}초"
    return f"{secs}초"


def _progress_reporter():
    started_at = None

    def report(event):
        nonlocal started_at
        phase = event.get("phase")
        if phase == "file_analysis":
            print(
                f"🧭 파일 분석 DB 동기화 {event['completed']:,}/{event['total']:,} "
                f"(변경 {event['changed']:,})",
                flush=True,
            )
            return
        if phase == "sync_start":
            print("🔄 플랫폼 카탈로그 제목 동기화 시작", flush=True)
            return
        if phase == "start":
            started_at = time.monotonic()
            print(
                "🚀 플랫폼 카탈로그 수집 시작: "
                f"전체 제목 {event['discovered_titles']:,}개, "
                f"이번 대상 {event['selected_titles']:,}개 / "
                f"플랫폼 {event['selected_platforms']:,}건",
                flush=True,
            )
            return
        if phase == "auth_start":
            started_at = time.monotonic()
            print(
                "🔐 인증 노벨피아 보완 수집 시작: "
                f"이번 대상 {event['selected_titles']:,}개",
                flush=True,
            )
            return
        if phase == "existing_start":
            started_at = time.monotonic()
            print(
                "📈 기존 플랫폼 인기값 갱신 시작: "
                f"전체 제목 {event['discovered_titles']:,}개, "
                f"이번 대상 {event['selected_titles']:,}개 / "
                f"플랫폼 {event['selected_platforms']:,}건",
                flush=True,
            )
            return
        if phase not in {"progress", "auth_progress", "existing_progress"}:
            return
        completed = int(event["completed_titles"])
        total = int(event["selected_titles"])
        if completed != 1 and completed != total and completed % 10:
            return
        elapsed = max(0.001, time.monotonic() - (started_at or time.monotonic()))
        rate = completed / elapsed
        remaining = (total - completed) / rate if rate > 0 else 0
        percent = (completed / total * 100) if total else 100.0
        if phase == "existing_progress":
            counts = event["outcome_counts"]
            print(
                "📈 갱신 진행 "
                + f"{completed:,}/{total:,} ({percent:.1f}%) | "
                f"updated={counts.get('updated', 0):,} "
                f"unchanged={counts.get('unchanged', 0):,} "
                f"not_found={counts.get('not_found', 0):,} "
                f"error={counts.get('error', 0):,} | "
                f"경과 {_duration_text(elapsed)} / 예상 잔여 {_duration_text(remaining)}",
                flush=True,
            )
            return
        counts = event["status_counts"]
        print(
            ("🔐 인증 진행 " if phase == "auth_progress" else "⏳ 진행 ")
            + f"{completed:,}/{total:,} ({percent:.1f}%) | "
            f"ok={counts.get('ok', 0):,} "
            f"not_found={counts.get('not_found', 0):,} "
            f"error={counts.get('error', 0):,} | "
            f"경과 {_duration_text(elapsed)} / 예상 잔여 {_duration_text(remaining)}",
            flush=True,
        )

    return report


def _indexed_file_paths(state_db_path: str):
    if Path(state_db_path).resolve() != STATE_DB.resolve():
        return None
    if not FILE_INDEX.is_file():
        raise RuntimeError("file_index.json is missing; run Scanner first")
    payload = json.loads(FILE_INDEX.read_text(encoding="utf-8"))
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError("file_index.json entries are invalid; run Scanner again")
    house_root = decision_store.canonicalize_path(HOUSE_DIR)
    paths = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "file":
            continue
        rel_path = entry.get("rel_path")
        if not isinstance(rel_path, str) or not rel_path:
            raise RuntimeError("file_index.json contains an invalid file path")
        path = decision_store.canonicalize_path(HOUSE_DIR / rel_path)
        if os.path.commonpath((house_root, path)) != house_root:
            raise RuntimeError("file_index.json contains a path outside the house root")
        paths.add(path)
    return paths


def file_metadata_status(state_db_path: str) -> dict:
    eligible_paths = _indexed_file_paths(state_db_path)
    conn = decision_store.connect_state_db_readonly(state_db_path)
    try:
        return decision_store.file_analysis_sync_status(
            conn, eligible_paths=eligible_paths
        )
    finally:
        conn.close()


def sync_file_metadata(state_db_path: str, *, progress=None):
    """Own the schema backup and backfill before any platform network request."""
    eligible_paths = _indexed_file_paths(state_db_path)
    backup = ensure_catalog_schema(state_db_path)
    with mutation_lock_for_roots(HOUSE_DIR, TEMP_DIR, "file-metadata-sync"):
        conn = decision_store.connect_state_db(state_db_path)
        try:
            decision_store.validate_schema(conn)
            if backup is None:
                stale_versions = conn.execute(
                    "SELECT COUNT(*) FROM file_analysis WHERE normalizer_version != ?",
                    (platform_catalog.NORMALIZER_VERSION,),
                ).fetchone()[0]
                if stale_versions:
                    backup = decision_store.backup_state_db(
                        conn, _metadata_rekey_backup_path(Path(state_db_path))
                    )
            with decision_store.transaction(conn):
                result = decision_store.sync_active_file_analysis(
                    conn, eligible_paths=eligible_paths, progress=progress
                )
        finally:
            conn.close()
    return backup, result


def sync_google_sheet(state_db_path: str, *, dry_run: bool = False) -> dict:
    import platform_sheet_export

    snapshot = platform_sheet_export.build_sheet_snapshot(state_db_path)
    preview = {
        "works_rows": len(snapshot.works.rows),
        "error_rows": len(snapshot.errors.rows),
        "works_columns": len(snapshot.works.headers),
        "error_columns": len(snapshot.errors.headers),
        "synced_at": snapshot.synced_at,
    }
    if dry_run:
        return {"dry_run": True, **preview}
    with mutation_lock_for_roots(
        Path(state_db_path).resolve(),
        Path(state_db_path).resolve().parent / "google-sheet-sync",
        "google-sheet-sync",
    ):
        client = platform_sheet_export.GoogleSheetsRestClient.from_environment()
        result = platform_sheet_export.sync_snapshot_to_google(snapshot, client)
    return {"dry_run": False, **result}


def _platform_refresh_lock(state_db_path: str, owner: str):
    state_db = Path(state_db_path).resolve()
    return mutation_lock_for_roots(
        state_db,
        state_db.parent / "platform-catalog-refresh",
        owner,
    )


def _failed_retry_state(state_db_path: str, *, create: bool) -> dict:
    connector = (
        decision_store.connect_state_db
        if create else decision_store.connect_state_db_readonly
    )
    conn = connector(state_db_path)
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (FAILED_RETRY_SETTING_KEY,),
        ).fetchone()
        previous = None
        if row is not None:
            try:
                previous = json.loads(row["value"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError("failed retry cycle state is invalid") from exc
            cutoff = platform_catalog._parse_time(previous.get("cutoff"))
            if previous.get("state") not in {"active", "completed"} or cutoff is None:
                raise RuntimeError("failed retry cycle state is invalid")
            if previous["state"] == "active" or not create:
                return previous

        now_text = platform_catalog._utc_text(platform_catalog.utc_now())
        payload = {
            "state": "active" if create else "preview",
            "cycle": int((previous or {}).get("cycle", 0)) + 1,
            "cutoff": now_text,
            "started_at": now_text,
        }
        if create:
            with decision_store.transaction(conn):
                conn.execute(
                    """
                    INSERT INTO settings(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (FAILED_RETRY_SETTING_KEY, json.dumps(payload, sort_keys=True)),
                )
        return payload
    finally:
        conn.close()


def _complete_failed_retry(state_db_path: str, state: dict, result: dict) -> None:
    conn = decision_store.connect_state_db(state_db_path)
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (FAILED_RETRY_SETTING_KEY,),
        ).fetchone()
        if row is None:
            raise RuntimeError("failed retry cycle state disappeared")
        payload = json.loads(row["value"])
        if (
            payload.get("state") != "active"
            or payload.get("cycle") != state.get("cycle")
            or payload.get("cutoff") != state.get("cutoff")
        ):
            raise RuntimeError("failed retry cycle state changed unexpectedly")
        payload.update({
            "state": "completed",
            "completed_at": platform_catalog._utc_text(platform_catalog.utc_now()),
            "selected_titles": result["selected_titles"],
            "selected_platforms": result["selected_platforms"],
        })
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE settings SET value = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE key = ?",
                (json.dumps(payload, sort_keys=True), FAILED_RETRY_SETTING_KEY),
            )
    finally:
        conn.close()


def _novelpia_auth_retry_state(state_db_path: str, *, create: bool) -> dict:
    connector = (
        decision_store.connect_state_db
        if create else decision_store.connect_state_db_readonly
    )
    conn = connector(state_db_path)
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (NOVELPIA_AUTH_RETRY_SETTING_KEY,),
        ).fetchone()
        if row is not None:
            try:
                payload = json.loads(row["value"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "one-time authenticated NovelPia retry state is invalid"
                ) from exc
            cutoff = platform_catalog._parse_time(payload.get("cutoff"))
            if payload.get("state") not in {"active", "completed"} or cutoff is None:
                raise RuntimeError(
                    "one-time authenticated NovelPia retry state is invalid"
                )
            return payload

        now_text = platform_catalog._utc_text(platform_catalog.utc_now())
        payload = {
            "state": "active" if create else "preview",
            "cutoff": now_text,
            "started_at": now_text,
        }
        if create:
            with decision_store.transaction(conn):
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?)",
                    (
                        NOVELPIA_AUTH_RETRY_SETTING_KEY,
                        json.dumps(payload, sort_keys=True),
                    ),
                )
        return payload
    finally:
        conn.close()


def _complete_novelpia_auth_retry(
    state_db_path: str, cutoff: str, result: dict
) -> None:
    conn = decision_store.connect_state_db(state_db_path)
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (NOVELPIA_AUTH_RETRY_SETTING_KEY,),
        ).fetchone()
        if row is None:
            raise RuntimeError("authenticated NovelPia retry state disappeared")
        payload = json.loads(row["value"])
        if payload.get("state") != "active" or payload.get("cutoff") != cutoff:
            raise RuntimeError("authenticated NovelPia retry state changed unexpectedly")
        payload.update({
            "state": "completed",
            "completed_at": platform_catalog._utc_text(platform_catalog.utc_now()),
            "selected_titles": result["selected_titles"],
            "selected_platforms": result["selected_platforms"],
        })
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE settings SET value = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE key = ?",
                (
                    json.dumps(payload, sort_keys=True),
                    NOVELPIA_AUTH_RETRY_SETTING_KEY,
                ),
            )
    finally:
        conn.close()


def retry_failed(args: argparse.Namespace, *, progress=None) -> tuple:
    auth_client = platform_catalog.AuthenticatedNovelpiaClient.from_environment(
        timeout=args.timeout,
        required=args.require_novelpia_auth,
    )
    if args.dry_run:
        result = platform_catalog.preview_catalog_refresh(
            args.state_db,
            limit=None,
            failed_retry=True,
        )
        result.pop("titles", None)
        return None, {
            **result,
            "authenticated_novelpia_configured": auth_client is not None,
        }

    if auth_client is not None:
        # The all-three-failed branch can use the same adult-title fallback as
        # the normal first collection. Fail before DB work if auth is required.
        auth_client.login()
    backup, metadata = sync_file_metadata(args.state_db, progress=progress)
    with _platform_refresh_lock(args.state_db, "platform-failed-retry"):
        state = _failed_retry_state(args.state_db, create=True)
        cutoff = platform_catalog._parse_time(state["cutoff"])
        assert cutoff is not None
        result = platform_catalog.refresh_catalog(
            args.state_db,
            limit=None,
            delay_seconds=args.delay_seconds,
            timeout=args.timeout,
            failed_retry=True,
            failure_retry_cutoff=cutoff,
            error_retry_seconds=args.error_retry_seconds,
            authenticated_novelpia_lookup=(
                auth_client.lookup if auth_client is not None else None
            ),
            progress=progress,
        )
        remaining = platform_catalog.preview_catalog_refresh(
            args.state_db,
            limit=1,
            failed_retry=True,
            failure_retry_cutoff=cutoff,
        )["selected_titles"]
        result = {**result, "remaining_titles": remaining}
        if remaining == 0:
            _complete_failed_retry(args.state_db, state, result)
    return backup, {
        "file_metadata": metadata,
        "retry_cycle": state["cycle"],
        "retry_cutoff": state["cutoff"],
        **result,
    }


def retry_novelpia_auth(args: argparse.Namespace, *, progress=None) -> tuple:
    state = _novelpia_auth_retry_state(args.state_db, create=False)
    cutoff = platform_catalog._parse_time(state["cutoff"])
    assert cutoff is not None
    if state["state"] == "completed":
        return None, {
            "dry_run": bool(args.dry_run),
            "already_completed": True,
            "retry_state": state,
        }
    if args.dry_run:
        result = platform_catalog.preview_authenticated_novelpia_refresh(
            args.state_db,
            limit=args.limit,
            attempted_before=cutoff,
        )
        result.pop("titles", None)
        return None, {**result, "retry_cutoff": state["cutoff"]}

    client = platform_catalog.AuthenticatedNovelpiaClient.from_environment(
        timeout=args.timeout,
        required=True,
    )
    assert client is not None
    client.login()
    backup, metadata = sync_file_metadata(args.state_db, progress=progress)
    state = _novelpia_auth_retry_state(args.state_db, create=True)
    if state["state"] == "completed":
        return backup, {
            "dry_run": False,
            "already_completed": True,
            "file_metadata": metadata,
            "retry_state": state,
        }
    with _platform_refresh_lock(args.state_db, "platform-novelpia-auth-retry-once"):
        state = _novelpia_auth_retry_state(args.state_db, create=True)
        if state["state"] == "completed":
            return backup, {
                "dry_run": False,
                "already_completed": True,
                "file_metadata": metadata,
                "retry_state": state,
            }
        cutoff = platform_catalog._parse_time(state["cutoff"])
        assert cutoff is not None
        result = platform_catalog.refresh_authenticated_novelpia(
            args.state_db,
            client,
            limit=args.limit,
            attempted_before=cutoff,
            delay_seconds=args.delay_seconds,
            timeout=args.timeout,
            error_retry_seconds=args.error_retry_seconds,
            progress=progress,
        )
        remaining = platform_catalog.preview_authenticated_novelpia_refresh(
            args.state_db,
            limit=1,
            attempted_before=cutoff,
        )["selected_titles"]
        result = {**result, "remaining_titles": remaining}
        if remaining == 0:
            _complete_novelpia_auth_retry(args.state_db, state["cutoff"], result)
    return backup, {
        "file_metadata": metadata,
        "retry_cutoff": state["cutoff"],
        **result,
    }


def run(args: argparse.Namespace, *, progress=None):
    if args.command == "refresh":
        limit = None if args.all else args.limit
        if args.dry_run:
            auth_client = platform_catalog.AuthenticatedNovelpiaClient.from_environment(
                timeout=args.timeout,
                required=args.require_novelpia_auth,
            )
            backup = None
            metadata = file_metadata_status(args.state_db)
            if (
                not metadata["schema_ready"]
                or metadata["stale"]
                or metadata["missing_files"]
                or metadata["index_missing_db"]
            ):
                result = {
                    "dry_run": True,
                    "platform_preview_blocked": True,
                    "reason": "file metadata sync required",
                    "file_metadata": metadata,
                }
            else:
                result = platform_catalog.preview_catalog_refresh(
                    args.state_db,
                    limit=limit,
                    retry_not_found=args.retry_not_found,
                    refresh_after_days=args.refresh_after_days,
                    force=args.force,
                )
            result = {
                **result,
                "authenticated_novelpia_configured": auth_client is not None,
            }
        else:
            auth_client = platform_catalog.AuthenticatedNovelpiaClient.from_environment(
                timeout=args.timeout,
                required=args.require_novelpia_auth,
            )
            if auth_client is not None:
                # Fail before public collection if credentials/CAPTCHA/adult auth are invalid.
                auth_client.login()
            backup, metadata = sync_file_metadata(args.state_db, progress=progress)
            with _platform_refresh_lock(args.state_db, "platform-catalog-refresh"):
                result = platform_catalog.refresh_catalog(
                    args.state_db,
                    limit=limit,
                    delay_seconds=args.delay_seconds,
                    timeout=args.timeout,
                    retry_not_found=args.retry_not_found,
                    refresh_after_days=args.refresh_after_days,
                    force=args.force,
                    error_retry_seconds=args.error_retry_seconds,
                    authenticated_novelpia_lookup=(
                        auth_client.lookup if auth_client is not None else None
                    ),
                    progress=progress,
                )
            result = {"file_metadata": metadata, **result}
            if getattr(args, "sync_sheet", False):
                result = {**result, "sheet_sync": sync_google_sheet(args.state_db)}
    elif args.command == "refresh-existing":
        limit = None if args.all else args.limit
        auth_client = platform_catalog.AuthenticatedNovelpiaClient.from_environment(
            timeout=args.timeout,
            required=args.require_novelpia_auth,
        )
        if args.dry_run:
            backup = None
            metadata = file_metadata_status(args.state_db)
            if (
                not metadata["schema_ready"]
                or metadata["stale"]
                or metadata["missing_files"]
                or metadata["index_missing_db"]
            ):
                result = {
                    "dry_run": True,
                    "platform_preview_blocked": True,
                    "reason": "file metadata sync required",
                    "file_metadata": metadata,
                }
            else:
                result = platform_catalog.preview_existing_metric_refresh(
                    args.state_db,
                    limit=limit,
                )
                result.pop("titles", None)
            result = {
                **result,
                "authenticated_novelpia_configured": auth_client is not None,
            }
        else:
            if auth_client is not None:
                auth_client.login()
            backup, metadata = sync_file_metadata(args.state_db, progress=progress)
            with _platform_refresh_lock(
                args.state_db, "platform-existing-metrics-refresh"
            ):
                result = platform_catalog.refresh_existing_metrics(
                    args.state_db,
                    limit=limit,
                    delay_seconds=args.delay_seconds,
                    timeout=args.timeout,
                    authenticated_novelpia_lookup=(
                        auth_client.lookup if auth_client is not None else None
                    ),
                    progress=progress,
                )
            result = {"file_metadata": metadata, **result}
    elif args.command == "retry-failed":
        backup, result = retry_failed(args, progress=progress)
    elif args.command == "retry-novelpia-auth":
        backup, result = retry_novelpia_auth(args, progress=progress)
    elif args.command == "file-metadata-sync":
        if args.dry_run:
            backup = None
            result = {"dry_run": True, **file_metadata_status(args.state_db)}
        else:
            backup, metadata = sync_file_metadata(args.state_db, progress=progress)
            result = {"dry_run": False, **metadata}
    elif args.command == "sheet-sync":
        backup = None
        result = sync_google_sheet(args.state_db, dry_run=args.dry_run)
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
        result = run(args, progress=_progress_reporter())
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"❌ 플랫폼 카탈로그 실행 실패: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
