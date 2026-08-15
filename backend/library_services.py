"""1.3.0 service catalog and fixed-default execution adapters."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import decision_store
from library_jobs import ACTIVE_STATES, JobRunner
from normalizer import should_exclude_dir, should_exclude_file
from project_paths import FILE_INDEX, HOUSE_DIR, PROJECT_ROOT, STATE_DB, TEMP_DIR


SUPPORTED_EXTENSIONS = frozenset({".txt", ".epub", ".pdf"})
PLATFORM_PREVIEW_CACHE_SECONDS = 15.0
_PLATFORM_PROGRESS_THROTTLE_PHASES = frozenset({
    "progress",
    "auth_progress",
    "existing_progress",
    "metadata_progress",
    "identity_progress",
})


def _should_emit_platform_progress(phase: str, current: int, total: int) -> bool:
    if phase not in _PLATFORM_PROGRESS_THROTTLE_PHASES:
        return True
    return current in {1, total} or current % 10 == 0


@dataclass(frozen=True)
class ServiceDefinition:
    service_id: str
    job_type: str
    label: str
    summary: str
    category: str
    quick_action: bool
    target_label: str
    read_scope: tuple[str, ...]
    write_scope: tuple[str, ...]
    defaults: tuple[str, ...]
    production_layout_required: bool = False


SERVICE_DEFINITIONS = (
    ServiceDefinition(
        "folderling",
        "service_folderling",
        "Folderling 실제 입고",
        "txt_temp의 신규 파일과 자동 정리 가능한 강한 중복을 판정하고 house·warning·index·DB에 안전하게 반영합니다.",
        "입고",
        True,
        "입고/자동 정리 대기",
        ("txt_temp", "txt_house", "SQLite", "현재 index"),
        ("txt_house", "warning 격리", "SQLite", "file_list/index"),
        ("전체 입고 대기 파일", "doctor 사전·사후 검증", "backup + journal + recovery"),
    ),
    ServiceDefinition(
        "scanner",
        "service_scanner",
        "Scanner / index 갱신",
        "house 전체 파일과 DB 분석을 대조해 file_list와 구조화 index를 다시 만듭니다.",
        "입고",
        False,
        "house 파일",
        ("txt_house", "SQLite"),
        ("file_list/index", "house index"),
        ("전체 house 스캔", "normalizer·DB 분석 동기화", "파일 내용은 변경하지 않음"),
    ),
    ServiceDefinition(
        "platform-update",
        "service_platform_update",
        "플랫폼 인기 DB 업데이트",
        "신규 작품과 아직 수집하지 않은 플랫폼만 조회하고 실패 결과는 자동 재시도하지 않습니다.",
        "메타데이터",
        True,
        "대상 작품",
        ("SQLite 파일 메타데이터", "시리즈·카카오·노벨피아"),
        ("플랫폼 상태·인기 지표",),
        ("전체 미수집 대상", "제목 안에서 공개 플랫폼 최대 3개 병렬", "제목 간 안전 지연"),
        True,
    ),
    ServiceDefinition(
        "platform-retry",
        "service_platform_retry",
        "플랫폼 실패 결과 재검사",
        "현재 not_found/error인 플랫폼만 쌍 규칙에 맞춰 이번 재검사 cycle에서 다시 확인합니다.",
        "메타데이터",
        True,
        "재검사 작품",
        ("SQLite 실패 상태", "플랫폼 검색"),
        ("실패 플랫폼 상태·지표", "재검사 cycle"),
        ("시리즈·카카오 쌍 규칙", "필요한 플랫폼만 조회", "인증 노벨피아 보완 가능"),
        True,
    ),
    ServiceDefinition(
        "platform-refresh",
        "service_platform_refresh",
        "기존 인기값 상향 갱신",
        "이미 값이 있는 성공 플랫폼만 다시 조회하고 기존 카운터보다 커질 때만 반영합니다.",
        "메타데이터",
        True,
        "갱신 작품",
        ("기존 성공 플랫폼 지표", "플랫폼 검색"),
        ("증가한 카운터", "조건 통과 시 평점"),
        ("현재 ok인 플랫폼만", "카운터 하향 금지", "평점은 상향 조건 통과 시 함께 갱신"),
        True,
    ),
    ServiceDefinition(
        "platform-metadata",
        "service_platform_metadata",
        "플랫폼 메타데이터 backfill",
        "이미 성공한 작품 중 장르 또는 지원 태그 snapshot이 없는 플랫폼만 다시 조회해 메타데이터를 채웁니다.",
        "메타데이터",
        True,
        "메타데이터 미수집 작품",
        ("기존 성공 플랫폼 상태", "시리즈 상세", "카카오 BFF", "노벨피아 검색 JSON"),
        ("플랫폼 장르", "카카오·노벨피아 태그", "수집 시각"),
        ("기존 인기 지표는 변경하지 않음", "장르/태그 없음과 미수집 구분", "인증 노벨피아 보완 가능"),
        True,
    ),
    ServiceDefinition(
        "platform-identity",
        "service_platform_identity",
        "플랫폼 metadata consistency 재검증",
        "시리즈·카카오 성공 행을 저장된 remote ID에서 다시 읽어 제목과 장르·태그 provenance를 같은 원격 객체 기준으로 재검증합니다.",
        "메타데이터",
        False,
        "consistency 재검증 작품",
        ("기존 Series/Kakao 성공 remote ID", "같은 ID의 플랫폼 상세/BFF"),
        ("같은 ID의 remote_title/remote_url", "장르", "카카오 태그", "수집 시각"),
        ("다른 remote ID로 자동 전환 금지", "인기 지표 변경 금지", "제목/remote ID CAS 검증"),
        True,
    ),
    ServiceDefinition(
        "novelpia-auth-retry",
        "service_novelpia_auth_retry",
        "노벨피아 인증 누락 재검사",
        "세 플랫폼이 모두 not_found였던 기존 작품을 로그인 세션으로 한 번 보완 검사합니다.",
        "메타데이터",
        False,
        "인증 재검사 작품",
        ("SQLite triple-not-found 상태", "노벨피아 인증 검색"),
        ("노벨피아 상태·지표", "1회성 cutoff 상태"),
        ("환경변수 계정 사용", "20작품 단위 세션 보호", "완료된 1회 cycle 재실행 금지"),
        True,
    ),
    ServiceDefinition(
        "google-sheet",
        "service_google_sheet",
        "Google Sheet 동기화",
        "SQLite 원본을 수정하지 않고 기존 Spreadsheet의 조회용 두 탭을 안전하게 교체합니다.",
        "내보내기",
        True,
        "내보낼 작품",
        ("SQLite 고정 snapshot",),
        ("Google Spreadsheet 작품 현황·수집 오류 탭",),
        ("SQLite 읽기 전용", "기존 Spreadsheet 링크 유지", "Sheet 수정 내용은 DB로 가져오지 않음"),
    ),
)


class ServiceBlocked(RuntimeError):
    def __init__(self, descriptor: Mapping[str, object]):
        self.descriptor = dict(descriptor)
        super().__init__(str(descriptor.get("blocked_reason") or "service is not ready"))


def _count_supported(root: Path, *, intake_only: bool = False) -> int:
    if not root.is_dir():
        return 0
    count = 0
    for current, directories, filenames in os.walk(root, followlinks=False):
        if intake_only:
            directories[:] = [
                name for name in directories
                if not should_exclude_dir(name)
                and not (Path(current) / name).is_symlink()
            ]
        for filename in filenames:
            path = Path(current) / filename
            if intake_only and should_exclude_file(filename):
                continue
            if (
                path.is_file()
                and not path.is_symlink()
                and path.suffix.lower() in SUPPORTED_EXTENSIONS
            ):
                count += 1
    return count


class LibraryServiceRegistry:
    def __init__(
        self,
        *,
        state_db: Path,
        house_dir: Path,
        temp_dir: Path,
        index_path: Path,
        project_root: Path,
        runner: JobRunner,
    ):
        self.state_db = Path(state_db).resolve()
        self.house_dir = Path(house_dir).resolve()
        self.temp_dir = Path(temp_dir).resolve()
        self.index_path = Path(index_path).resolve()
        self.project_root = Path(project_root).resolve()
        self.runner = runner
        self.definitions = {item.service_id: item for item in SERVICE_DEFINITIONS}
        self._platform_preview_lock = threading.RLock()
        self._platform_preview_cache: dict[str, tuple[int, dict]] | None = None
        self._platform_preview_deadline = 0.0
        self.handlers: dict[str, Callable] = {
            "folderling": self._run_folderling,
            "scanner": self._run_scanner,
            "platform-update": lambda payload, progress: self._run_platform(
                "refresh", ("--all",), progress
            ),
            "platform-retry": lambda payload, progress: self._run_platform(
                "retry-failed", (), progress
            ),
            "platform-refresh": lambda payload, progress: self._run_platform(
                "refresh-existing", ("--all",), progress
            ),
            "platform-metadata": lambda payload, progress: self._run_platform(
                "refresh-metadata", ("--all",), progress
            ),
            "platform-identity": lambda payload, progress: self._run_platform(
                "revalidate-metadata-consistency", ("--all",), progress
            ),
            "novelpia-auth-retry": lambda payload, progress: self._run_platform(
                "retry-novelpia-auth", (), progress
            ),
            "google-sheet": lambda payload, progress: self._run_platform(
                "sheet-sync", (), progress
            ),
        }
        for service_id, definition in self.definitions.items():
            runner.register(definition.job_type, self.handlers[service_id])

    def _production_layout(self) -> bool:
        return (
            self.state_db == STATE_DB.resolve()
            and self.house_dir == HOUSE_DIR.resolve()
            and self.temp_dir == TEMP_DIR.resolve()
            and self.index_path == FILE_INDEX.resolve()
            and self.project_root == PROJECT_ROOT.resolve()
        )

    def _active_job(self):
        return next(
            (
                record for record in self.runner.list(limit=200)
                if record.get("state") in ACTIVE_STATES
            ),
            None,
        )

    def _latest_job(self, definition: ServiceDefinition, *, jobs=None):
        return next(
            (
                record for record in (jobs if jobs is not None else self.runner.list(limit=200))
                if record.get("job_type") == definition.job_type
            ),
            None,
        )

    def _doctor_state(self) -> tuple[bool, int, int]:
        if not self.state_db.is_file():
            return False, 0, 0
        conn = decision_store.connect_state_db_readonly(self.state_db)
        try:
            issues = decision_store.doctor_issues(
                conn,
                verify_files=False,
                check_integrity=False,
            )
            supported_house_files = conn.execute(
                """
                SELECT COUNT(*) FROM files AS f
                JOIN file_analysis AS fa ON fa.file_id = f.file_id
                WHERE f.active = 1 AND f.source = 'house'
                """
            ).fetchone()[0]
            return not issues, len(issues), supported_house_files
        finally:
            conn.close()

    def _compute_platform_previews(self) -> dict[str, tuple[int, dict]]:
        import platform_catalog
        import run_platform_catalog

        conn = decision_store.connect_state_db_readonly(self.state_db)
        try:
            # Read-only button counts do not need a full integrity scan.  The
            # selected service validates with the default fail-closed path when
            # the job actually starts.
            decision_store.validate_schema(conn, check_integrity=False)
            titles = platform_catalog.discover_catalog_titles(conn)
            stats = platform_catalog._stats_by_title(conn)
            current = platform_catalog.utc_now()
            update_targets = platform_catalog._refresh_targets(
                titles, stats, limit=None, now=current
            )
            retry_targets = platform_catalog._refresh_targets(
                titles, stats, limit=None, now=current, failed_retry=True
            )
            existing_targets = []
            for title in sorted(titles, key=lambda item: item.title_key):
                rows = stats.get(title.title_key, {})
                platforms = tuple(
                    platform
                    for platform in platform_catalog.PLATFORMS
                    if rows.get(platform) is not None
                    and rows[platform]["status"] == "ok"
                    and platform_catalog._row_has_growth_metric(platform, rows[platform])
                )
                if platforms:
                    existing_targets.append((title, platforms))
            metadata_targets = platform_catalog.select_metadata_backfill_targets(
                conn, limit=None
            )
            identity_targets = platform_catalog.select_metadata_identity_targets(
                conn, limit=None
            )

            state = run_platform_catalog._novelpia_auth_retry_state(
                str(self.state_db), create=False
            )
            if state["state"] == "completed":
                novelpia_targets = []
                novelpia_preview = {"already_completed": True, "retry_state": state}
            else:
                cutoff = platform_catalog._parse_time(state["cutoff"])
                novelpia_targets = platform_catalog.select_authenticated_novelpia_targets(
                    conn, limit=None, attempted_before=cutoff
                )
                novelpia_preview = {
                    "dry_run": True,
                    "selected_titles": len(novelpia_targets),
                    "selected_platforms": len(novelpia_targets),
                    "already_completed": False,
                }
        finally:
            conn.close()

        return {
            "platform-update": (
                len(update_targets),
                {
                    "dry_run": True,
                    "discovered_titles": len(titles),
                    "selected_titles": len(update_targets),
                    "selected_platforms": sum(len(item.platforms) for item in update_targets),
                },
            ),
            "platform-retry": (
                len(retry_targets),
                {
                    "dry_run": True,
                    "discovered_titles": len(titles),
                    "selected_titles": len(retry_targets),
                    "selected_platforms": sum(len(item.platforms) for item in retry_targets),
                },
            ),
            "platform-refresh": (
                len(existing_targets),
                {
                    "dry_run": True,
                    "discovered_titles": len(titles),
                    "selected_titles": len(existing_targets),
                    "selected_platforms": sum(len(platforms) for _title, platforms in existing_targets),
                },
            ),
            "platform-metadata": (
                len(metadata_targets),
                {
                    "dry_run": True,
                    "discovered_titles": len(titles),
                    "selected_titles": len(metadata_targets),
                    "selected_platforms": sum(
                        len(item.platforms) for item in metadata_targets
                    ),
                },
            ),
            "platform-identity": (
                len(identity_targets),
                {
                    "dry_run": True,
                    "discovered_titles": len(titles),
                    "selected_titles": len(identity_targets),
                    "selected_platforms": sum(
                        len(item.platforms) for item in identity_targets
                    ),
                },
            ),
            "novelpia-auth-retry": (
                len(novelpia_targets),
                novelpia_preview,
            ),
        }

    def _platform_previews(self) -> dict[str, tuple[int, dict]]:
        now = time.monotonic()
        with self._platform_preview_lock:
            if (
                self._platform_preview_cache is not None
                and now < self._platform_preview_deadline
            ):
                return self._platform_preview_cache
            previews = self._compute_platform_previews()
            self._platform_preview_cache = previews
            self._platform_preview_deadline = time.monotonic() + PLATFORM_PREVIEW_CACHE_SECONDS
            return previews

    def _invalidate_platform_previews(self) -> None:
        with self._platform_preview_lock:
            self._platform_preview_cache = None
            self._platform_preview_deadline = 0.0

    def _platform_preview(self, service_id: str) -> tuple[int, dict]:
        return self._platform_previews()[service_id]

    def _descriptor_context(self) -> dict:
        jobs = self.runner.list(limit=200)
        active = next(
            (record for record in jobs if record.get("state") in ACTIVE_STATES),
            None,
        )
        doctor_ok, doctor_issue_count, supported_house_files = self._doctor_state()
        context = {
            "jobs": jobs,
            "active": active,
            "doctor_ok": doctor_ok,
            "doctor_issue_count": doctor_issue_count,
            "supported_house_files": supported_house_files,
            "platform_previews": {},
            "platform_error": None,
        }
        if self.state_db.is_file() and self._production_layout():
            try:
                context["platform_previews"] = self._platform_previews()
            except Exception as exc:
                context["platform_error"] = str(exc)
        return context

    def descriptor(self, service_id: str, *, context=None) -> dict:
        if service_id not in self.definitions:
            raise KeyError(service_id)
        definition = self.definitions[service_id]
        if context is None:
            context = {
                "jobs": self.runner.list(limit=200),
                "active": self._active_job(),
                "platform_previews": {},
                "platform_error": None,
            }
            (
                context["doctor_ok"],
                context["doctor_issue_count"],
                context["supported_house_files"],
            ) = self._doctor_state()
        active = context["active"]
        doctor_ok = bool(context["doctor_ok"])
        doctor_issue_count = int(context["doctor_issue_count"])
        ready = self.state_db.is_file()
        blocked_code = None
        blocked_reason = None
        target_count = 0
        preview = {}
        configured = True

        if not ready:
            blocked_code = "missing_state_db"
            blocked_reason = "운영 SQLite 파일이 없습니다."
        elif definition.production_layout_required and not self._production_layout():
            ready = False
            blocked_code = "non_production_layout"
            blocked_reason = "플랫폼 서비스는 운영 경로로 시작한 서버에서만 실행할 수 있습니다."
        else:
            try:
                if service_id == "folderling":
                    from deduplicator import (
                        count_actionable_pending_strong_reviews,
                        count_pending_active_distinct_decision_reviews,
                    )

                    intake_count = _count_supported(
                        self.temp_dir, intake_only=True
                    )
                    strong_review_count = (
                        count_actionable_pending_strong_reviews(self.state_db)
                    )
                    decided_review_count = (
                        count_pending_active_distinct_decision_reviews(
                            self.state_db
                        )
                    )
                    target_count = (
                        intake_count + strong_review_count + decided_review_count
                    )
                    preview = {
                        "intake_count": intake_count,
                        "actionable_strong_review_count": strong_review_count,
                        "decided_review_cleanup_count": decided_review_count,
                    }
                    if not doctor_ok:
                        ready = False
                        blocked_code = "doctor_failed"
                        blocked_reason = f"Folderling 전 doctor 문제 {doctor_issue_count}건을 먼저 확인하세요."
                    elif target_count == 0:
                        ready = False
                        blocked_code = "no_targets"
                        blocked_reason = "현재 입고 또는 자동 정리할 대상이 없습니다."
                elif service_id == "scanner":
                    target_count = int(context.get("supported_house_files") or 0)
                    if not self.house_dir.is_dir():
                        ready = False
                        blocked_code = "missing_house"
                        blocked_reason = "house 폴더가 없습니다."
                elif service_id.startswith("platform-") or service_id == "novelpia-auth-retry":
                    if context.get("platform_error"):
                        raise RuntimeError(str(context["platform_error"]))
                    cached = context.get("platform_previews", {}).get(service_id)
                    target_count, preview = cached or self._platform_preview(service_id)
                    if service_id == "novelpia-auth-retry":
                        from platform_catalog import AuthenticatedNovelpiaClient

                        configured = AuthenticatedNovelpiaClient.environment_configured()
                        if preview.get("already_completed"):
                            ready = False
                            blocked_code = "already_completed"
                            blocked_reason = "인증 누락 1회 재검사가 이미 완료되었습니다."
                        elif not configured:
                            ready = False
                            blocked_code = "credentials_missing"
                            blocked_reason = "노벨피아 로그인 환경변수가 설정되지 않았습니다."
                    if ready and target_count == 0:
                        ready = False
                        blocked_code = "no_targets"
                        blocked_reason = "현재 실행 대상이 없습니다."
                elif service_id == "google-sheet":
                    from platform_sheet_export import resolve_google_sheet_settings

                    try:
                        resolve_google_sheet_settings()
                        configured = True
                    except (OSError, RuntimeError, ValueError):
                        configured = False
                    discovered = context.get("platform_previews", {}).get(
                        "platform-update", (0, {})
                    )[1].get("discovered_titles")
                    if discovered is None:
                        conn = decision_store.connect_state_db_readonly(self.state_db)
                        try:
                            discovered = conn.execute(
                                "SELECT COUNT(*) FROM catalog_titles"
                            ).fetchone()[0]
                        finally:
                            conn.close()
                    target_count = int(discovered)
                    if not configured:
                        ready = False
                        blocked_code = "credentials_missing"
                        blocked_reason = (
                            "Google 환경변수 또는 private 설정 파일과 인증 파일을 확인하세요."
                        )
            except Exception as exc:  # fail closed while keeping the catalog visible
                ready = False
                blocked_code = "preview_failed"
                blocked_reason = f"사전 검사 실패: {exc}"

        if active is not None:
            ready = False
            blocked_code = "job_active"
            blocked_reason = f"다른 작업이 실행 중입니다: {active['job_id']}"

        return {
            "id": definition.service_id,
            "job_type": definition.job_type,
            "label": definition.label,
            "summary": definition.summary,
            "category": definition.category,
            "quick_action": definition.quick_action,
            "target_label": definition.target_label,
            "target_count": target_count,
            "read_scope": list(definition.read_scope),
            "write_scope": list(definition.write_scope),
            "defaults": list(definition.defaults),
            "ready": ready,
            "blocked_code": blocked_code,
            "blocked_reason": blocked_reason,
            "configured": configured,
            "doctor_ok": doctor_ok,
            "preview": preview,
            "active_job": active,
            "latest_job": self._latest_job(definition, jobs=context["jobs"]),
        }

    def descriptors(self) -> list[dict]:
        context = self._descriptor_context()
        return [
            self.descriptor(item.service_id, context=context)
            for item in SERVICE_DEFINITIONS
        ]

    def start(self, service_id: str, *, source: str) -> dict:
        descriptor = self.descriptor(service_id)
        if not descriptor["ready"]:
            raise ServiceBlocked(descriptor)
        definition = self.definitions[service_id]
        return self.runner.start_exclusive(
            definition.job_type,
            {"service_id": service_id, "source": source, "defaults": list(definition.defaults)},
        )

    @staticmethod
    def _event_message(event: Mapping[str, object]) -> tuple[int, int, str]:
        phase = str(event.get("phase") or "running")
        current = int(event.get("completed_titles") or event.get("completed") or 0)
        total = int(event.get("selected_titles") or event.get("total") or 0)
        labels = {
            "file_analysis": "파일 분석 DB 동기화",
            "sync_start": "플랫폼 카탈로그 제목 동기화",
            "start": "플랫폼 신규·미수집 조회 시작",
            "progress": "플랫폼 신규·미수집 조회",
            "auth_start": "인증 노벨피아 조회 시작",
            "auth_progress": "인증 노벨피아 조회",
            "existing_start": "기존 인기값 갱신 시작",
            "existing_progress": "기존 인기값 갱신",
            "metadata_start": "플랫폼 메타데이터 backfill 시작",
            "metadata_progress": "플랫폼 메타데이터 backfill",
            "identity_start": "플랫폼 metadata consistency 재검증 시작",
            "identity_progress": "플랫폼 metadata consistency 재검증",
            "sheet_snapshot": "SQLite Sheet snapshot 준비",
            "sheet_write_start": "Google Sheet 임시 탭 쓰기 시작",
            "sheet_temp_tabs_created": "Google Sheet 임시 탭 생성",
            "sheet_values_written": "Google Sheet 값 쓰기 완료",
            "sheet_links_written": "Google Sheet 링크 쓰기 완료",
            "sheet_swap_completed": "Google Sheet 공개 탭 교체 완료",
        }
        label = labels.get(phase, phase)
        if total:
            label += f" {current:,}/{total:,}"
        return current, total, label

    def _run_platform(self, command: str, flags: tuple[str, ...], progress) -> dict:
        import run_platform_catalog

        arguments = ["--state-db", str(self.state_db), command, *flags]
        args = run_platform_catalog.build_parser().parse_args(arguments)
        started_at = time.monotonic()

        def report(event):
            enriched = dict(event)
            current, total, message = self._event_message(enriched)
            phase = str(enriched.get("phase") or "running")
            elapsed = max(0.0, time.monotonic() - started_at)
            enriched["elapsed_seconds"] = round(elapsed, 3)
            if total:
                enriched["percent"] = round(current / total * 100, 1)
                rate = current / elapsed if current and elapsed else 0.0
                enriched["eta_seconds"] = (
                    round(max(0.0, (total - current) / rate), 3)
                    if rate else None
                )
            if not _should_emit_platform_progress(phase, current, total):
                return
            progress(current, total, message, stage=phase, event=enriched)

        progress(0, 0, f"{command} 실행 준비", stage="validating", event={"phase": "validating"})
        try:
            result = run_platform_catalog.run(args, progress=report)
            terminal_state = "succeeded"
            terminal_message = "작업 완료"
            if command == "revalidate-metadata-consistency":
                counts = dict(result.get("outcome_counts") or {})
                unresolved = sum(
                    int(counts.get(key, 0) or 0)
                    for key in (
                        "identity_conflict",
                        "unavailable",
                        "stale_target",
                        "error",
                        "skipped",
                    )
                )
                if unresolved:
                    terminal_state = "needs_review"
                    terminal_message = (
                        "metadata consistency 재검증 완료 · "
                        f"잔여 {unresolved:,}건 검토 필요"
                    )
                    result = {
                        **result,
                        "unresolved_count": unresolved,
                        "_job_state": terminal_state,
                        "_job_message": terminal_message,
                    }
            result_phase = "sheet_result" if command == "sheet-sync" else "platform_result"
            progress(1, 1, f"{command} 검증 완료", stage="verifying", event={
                "phase": result_phase,
                "status": terminal_state,
                "elapsed_seconds": round(max(0.0, time.monotonic() - started_at), 3),
                **result,
            })
            return result
        finally:
            # Successful and partially failed jobs can both change retry/status
            # rows.  Never leave the button counts cached after an execution.
            self._invalidate_platform_previews()

    def _run_folderling(self, payload, progress) -> dict:
        import run_folderling_one_button

        phase_labels = {
            "preflight_start": "Folderling 사전 검사 시작",
            "preflight_result": "schema·doctor·backup 준비 완료",
            "volume_staging_recovery": "분권 staging 복구 확인",
            "actual_run_started": "일회성 actual run 시작",
            "review_actions_result": "검토 처리함 반영 완료",
            "workflow_started": "Folderling workflow 시작",
            "legacy_pass_skipped": "legacy pass 항목 보류",
            "dedup_start": "중복·검토 큐 판정 시작",
            "snapshot_result": "house snapshot 확인",
            "auditor_progress": "본문 중복 감사",
            "dedup_result": "중복·검토 큐 판정 완료",
            "intake_start": "temp → house 입고 시작",
            "intake_result": "파일 입고 단계 완료",
            "index_start": "index 갱신 시작",
            "index_result": "index 갱신 완료",
            "folderling_summary": "Folderling 결과 집계",
            "final_doctor_result": "최종 doctor 확인",
            "actual_run_finished": "actual run 종료",
            "workflow_failed": "Folderling workflow 실패",
            "preflight_failed": "Folderling 사전 검사 실패",
        }

        def report(event):
            phase = str(event.get("phase") or "running")
            current = int(event.get("completed") or 0)
            total = int(event.get("total") or 0)
            if phase == "auditor_progress":
                audit_labels = {
                    "text_analysis": "본문 기본 분석",
                    "epub_analysis": "EPUB 내용 분석",
                    "pair_classification": "후보 쌍 판정",
                    "deep_scan": "정밀 본문 비교",
                }
                audit_phase = str(event.get("audit_phase") or "analysis")
                label = audit_labels.get(audit_phase, audit_phase)
                read_gib = int(event.get("read_bytes") or 0) / (1024 ** 3)
                message = f"{label} {current:,}/{total:,} ({read_gib:.2f} GiB read)"
                stage = f"auditor_{audit_phase}"
            elif phase == "file_result":
                status = str(event.get("status") or "result")
                name = str(
                    event.get("source_name")
                    or os.path.basename(str(event.get("source_path") or "파일"))
                )
                message = f"{status}: {name}"
                stage = str(event.get("stage") or "intake")
            else:
                message = phase_labels.get(phase, phase)
                stage = phase
            progress(current, total, message, stage=stage, event=event)

        result = run_folderling_one_button.run(
            self.temp_dir,
            self.house_dir,
            self.state_db,
            script_dir=self.project_root,
            event_callback=report,
        )
        for filename in ("success.log", "fail.log"):
            path = self.project_root / filename
            if path.is_file():
                progress.log(
                    f"--- {filename} ---\n" + path.read_text(encoding="utf-8", errors="replace")
                )
        if (
            result.get("failure_count")
            or result.get("review_required_count")
        ):
            result["_job_state"] = "needs_review"
            result["_job_message"] = "Folderling 완료 · 검토할 결과 있음"
        return result

    def _run_scanner(self, payload, progress) -> dict:
        from mutation_io import mutation_lock_for_roots

        with mutation_lock_for_roots(
            self.house_dir, self.temp_dir, "library-service-scanner"
        ):
            return self._run_scanner_locked(payload, progress)

    def _run_scanner_locked(self, payload, progress) -> dict:
        from folderling import (
            _authorize_index_deployment,
            _capture_index_deployment,
            _discard_index_deployment_snapshot,
            _restore_index_deployment,
            recover_pending_index_deployments,
            sync_extension_index,
            sync_house_index,
        )
        from scanner import (
            INDEX_GENERATION_FILENAME,
            generate_file_list,
            validate_index_generation,
        )

        total = _count_supported(self.house_dir)
        progress(0, total, "house 전체 Scanner 시작", stage="running", event={
            "phase": "scanner_start", "selected_files": total
        })
        file_list = self.index_path.with_name("file_list.json")
        extension_index = self.project_root / "extension" / "file_index.json"
        pending = recover_pending_index_deployments(
            self.project_root,
            file_list_path=file_list,
            file_index_path=self.index_path,
            house_index_path=self.house_dir / "file_index.json",
            extension_index_path=extension_index,
        )
        needs_review = [item for item in pending if item["status"] == "needs_review"]
        if needs_review:
            raise RuntimeError(
                "pending index deployment needs review: " + needs_review[0]["error"]
            )
        deployment_snapshot = _capture_index_deployment(
            [
                file_list,
                self.index_path,
                self.index_path.with_name(INDEX_GENERATION_FILENAME),
                self.house_dir / "file_index.json",
                extension_index,
            ],
            self.project_root,
        )
        try:
            ok = generate_file_list(
                [str(self.house_dir)],
                str(file_list),
                str(self.index_path),
                state_db_path=str(self.state_db),
                temp_root=str(self.temp_dir),
            )
            if not ok:
                raise RuntimeError("Scanner index generation failed")
            validate_index_generation(self.index_path, file_list)
            from mutation_io import inspect_regular_file

            list_sha256 = inspect_regular_file(file_list).sha256
            index_sha256 = inspect_regular_file(self.index_path).sha256
            manifest_path = self.index_path.with_name(INDEX_GENERATION_FILENAME)
            manifest_sha256 = inspect_regular_file(manifest_path).sha256
            _authorize_index_deployment(
                deployment_snapshot,
                {
                    str(file_list.resolve()): list_sha256,
                    str(self.index_path.resolve()): index_sha256,
                    str(manifest_path.resolve()): manifest_sha256,
                    str((self.house_dir / "file_index.json").resolve()): index_sha256,
                    str(extension_index.resolve()): index_sha256,
                },
            )
            if not sync_house_index(str(self.index_path), str(self.house_dir)):
                raise RuntimeError("house index sync failed")
            if extension_index.parent.is_dir() and not sync_extension_index(
                str(self.index_path), str(self.project_root)
            ):
                raise RuntimeError("extension index sync failed")
        except BaseException:
            _restore_index_deployment(deployment_snapshot)
            raise
        else:
            _discard_index_deployment_snapshot(deployment_snapshot)
        payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        files = sum(item.get("type") == "file" for item in payload.get("entries", []))
        directories = sum(item.get("type") == "dir" for item in payload.get("entries", []))
        result = {
            "files": files,
            "directories": directories,
            "generated_at": payload.get("generated_at"),
            "index_mode": payload.get("index_mode", "full_scan"),
            "index_path": str(self.index_path),
        }
        progress(files, files, "Scanner/index 동기화 완료", stage="verifying", event={
            "phase": "scanner_result", **result
        })
        return result
