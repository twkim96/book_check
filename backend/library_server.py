"""Independent local web server for file_check library operations."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Optional, Sequence

from flask import Flask, jsonify, request, send_file, send_from_directory

import decision_store
from library_catalog import catalog_listing, review_queue_listing
from library_appearance import read_appearance, reset_appearance, write_appearance
from library_explorer import (
    compare_files,
    file_detail,
    file_listing,
    folder_detail,
    folder_listing,
    quarantine_listing,
)
from library_jobs import JobActiveError, JobRunner, JobStore
from library_reports import (
    dedup_report_listing,
    dedup_report_path,
    export_dedup_report_text,
    read_dedup_report,
)
from library_review import (
    ReviewProviderRegistry,
    TitleCorrectionProvider,
    VolumeGroupProvider,
)
from library_services import LibraryServiceRegistry, ServiceBlocked
from normalizer import should_exclude_dir, should_exclude_file
from project_paths import FILE_INDEX, HOUSE_DIR, PROJECT_ROOT, STATE_DB, TEMP_DIR


SERVER_VERSION = "1.3.1"
DEFAULT_FRONTEND_DIST = PROJECT_ROOT / "library_frontend" / "dist"
DEFAULT_RUNTIME_DIR = STATE_DB.parent / "library-server"
SUPPORTED_EXTENSIONS = frozenset({".txt", ".epub", ".pdf"})


@dataclass(frozen=True)
class LibraryServerConfig:
    state_db: Path
    house_dir: Path
    temp_dir: Path
    index_path: Path
    runtime_dir: Path
    frontend_dist: Path
    project_root: Path


def _open_state_db_readonly_keeper(state_db: Path):
    """Keep one query-only normal connection alive to own WAL sidecars.

    Some macOS Python SQLite builds cannot be the first ``mode=ro`` opener of a
    WAL database after the last normal connection removed ``-wal``/``-shm``.
    A read-only keeper does not reliably retain those files on macOS.  Keep the
    normal opener itself alive with ``PRAGMA query_only`` so request-scoped
    ``mode=ro`` connections remain reliable without granting this keeper writes.
    """
    keeper = decision_store.connect_state_db(state_db)
    keeper.execute("PRAGMA query_only = ON")
    return keeper


def _count_supported(root: Path, *, intake_only: bool = False) -> int:
    if not root.is_dir():
        return 0
    count = 0
    for current, directories, filenames in os.walk(root, followlinks=False):
        if intake_only:
            directories[:] = [
                name
                for name in directories
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


def _index_counts(index_path: Path) -> dict:
    if not index_path.is_file():
        return {"exists": False, "files": 0, "directories": 0, "generated_at": None}
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        entries = payload.get("entries") or []
        return {
            "exists": True,
            "files": sum(item.get("type") == "file" for item in entries),
            "directories": sum(item.get("type") == "dir" for item in entries),
            "generated_at": payload.get("generated_at"),
            "normalizer_version": payload.get("normalizer_version"),
        }
    except (OSError, ValueError, TypeError):
        return {"exists": True, "files": 0, "directories": 0, "invalid": True}


def dashboard_snapshot(config: LibraryServerConfig, runner: JobRunner) -> dict:
    conn = decision_store.connect_state_db_readonly(config.state_db)
    try:
        active_by_source = {
            row["source"]: row["count"]
            for row in conn.execute(
                "SELECT source, COUNT(*) AS count FROM files "
                "WHERE active = 1 GROUP BY source"
            )
        }
        supported_house = conn.execute(
            """
            SELECT COUNT(*) FROM files AS f
            JOIN file_analysis AS fa ON fa.file_id = f.file_id
            WHERE f.active = 1 AND f.source = 'house'
            """
        ).fetchone()[0]
        catalog_titles = conn.execute("SELECT COUNT(*) FROM catalog_titles").fetchone()[0]
        pending_reviews = conn.execute(
            "SELECT COUNT(*) FROM review_items WHERE state IN ('pending', 'deferred')"
        ).fetchone()[0]
        no_ok_titles = conn.execute(
            """
            SELECT COUNT(DISTINCT fa.core_title)
            FROM files AS f JOIN file_analysis AS fa ON fa.file_id = f.file_id
            WHERE f.active = 1 AND f.source = 'house'
              AND NOT EXISTS (
                SELECT 1 FROM catalog_platform_stats AS cps
                WHERE cps.title_key = fa.core_title AND cps.status = 'ok'
              )
            """
        ).fetchone()[0]
        # The dashboard is informational.  Keep its status check DB-only and
        # leave the expensive full integrity + 16k-file identity Doctor to the
        # actual mutation preflight, where it remains fail-closed.
        issues = decision_store.doctor_issues(
            conn,
            verify_files=False,
            check_integrity=False,
        )
        integrity = "deferred"
    finally:
        conn.close()
    warning_dir = config.temp_dir / "trash_bin" / "warning"
    folderling_pending = _count_supported(config.temp_dir, intake_only=True)
    warning_files = _count_supported(warning_dir)
    jobs = runner.list(limit=10)
    next_actions = []
    if issues:
        next_actions.append({
            "code": "doctor",
            "label": f"Doctor 문제 {len(issues)}건 확인",
            "detail": str(issues[0]),
            "href": "/services/folderling",
            "severity": "error",
        })
    if folderling_pending:
        next_actions.append({
            "code": "folderling",
            "label": f"입고 대기 {folderling_pending:,}개",
            "detail": "Folderling 사전 검사 후 실제 입고할 수 있습니다.",
            "href": "/services/folderling",
            "severity": "action",
        })
    if warning_files or pending_reviews:
        next_actions.append({
            "code": "review_queue",
            "label": f"검토 큐 {warning_files + pending_reviews:,}건",
            "detail": "1.3.0에서는 근거를 읽기 전용으로 확인합니다.",
            "href": "/review/queue",
            "severity": "warning",
        })
    if no_ok_titles:
        next_actions.append({
            "code": "metadata",
            "label": f"메타데이터 미확인 작품 {no_ok_titles:,}개",
            "detail": "카탈로그에서 원본 제목과 플랫폼 상태를 확인할 수 있습니다.",
            "href": "/catalog?status=missing",
            "severity": "info",
        })
    latest_failed = next(
        (job for job in jobs if job.get("state") in {"failed", "interrupted"}),
        None,
    )
    if latest_failed is not None:
        next_actions.append({
            "code": "failed_job",
            "label": "최근 실패 작업 확인",
            "detail": str(latest_failed.get("message") or latest_failed["job_id"]),
            "href": f"/jobs/{latest_failed['job_id']}",
            "severity": "error",
        })
    return {
        "version": SERVER_VERSION,
        "database": {
            "path": str(config.state_db),
            "integrity": integrity,
            "doctor_scope": "operational",
            "doctor_ok": not issues,
            "doctor_issue_count": len(issues),
            "doctor_first_issue": issues[0] if issues else None,
            "active_by_source": active_by_source,
            "supported_house_files": supported_house,
            "catalog_titles": catalog_titles,
            "titles_without_ok_metadata": no_ok_titles,
            "pending_reviews": pending_reviews,
        },
        "filesystem": {
            "folderling_pending": folderling_pending,
            "warning_files": warning_files,
            "index": _index_counts(config.index_path),
        },
        "next_actions": next_actions,
        "jobs": jobs,
    }


def _json_body() -> dict:
    if not request.is_json:
        raise ValueError("application/json 요청만 허용됩니다")
    value = request.get_json(silent=False)
    if not isinstance(value, dict):
        raise ValueError("JSON object가 필요합니다")
    return value


def create_app(
    *,
    state_db: Path = STATE_DB,
    house_dir: Path = HOUSE_DIR,
    temp_dir: Path = TEMP_DIR,
    index_path: Path = FILE_INDEX,
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    frontend_dist: Path = DEFAULT_FRONTEND_DIST,
    project_root: Path = PROJECT_ROOT,
) -> Flask:
    config = LibraryServerConfig(
        state_db=Path(state_db).expanduser().resolve(),
        house_dir=Path(house_dir).expanduser().resolve(),
        temp_dir=Path(temp_dir).expanduser().resolve(),
        index_path=Path(index_path).expanduser().resolve(),
        runtime_dir=Path(runtime_dir).expanduser().resolve(),
        frontend_dist=Path(frontend_dist).expanduser().resolve(),
        project_root=Path(project_root).expanduser().resolve(),
    )
    readonly_keeper = (
        _open_state_db_readonly_keeper(config.state_db)
        if config.state_db.is_file() else None
    )
    store = JobStore(config.runtime_dir)
    store.mark_interrupted()
    runner = JobRunner(store)
    registry = ReviewProviderRegistry()
    title_provider = TitleCorrectionProvider(
        state_db=config.state_db,
        house_dir=config.house_dir,
        temp_dir=config.temp_dir,
        index_path=config.index_path,
    )
    volume_provider = VolumeGroupProvider(
        state_db=config.state_db,
        house_dir=config.house_dir,
        temp_dir=config.temp_dir,
        index_path=config.index_path,
    )
    registry.register(title_provider)
    registry.register(volume_provider)

    def apply_title_job(payload, progress):
        return title_provider.apply_plan(
            payload["changes"],
            confirm_count=payload["confirm_count"],
            confirm_plan_sha256=payload["confirm_plan_sha256"],
            progress=lambda current, total, name: progress(
                current, total, f"제목 교정 {current:,}/{total:,}: {name}"
            ),
        )

    runner.register("title_requeue", apply_title_job)

    def apply_volume_job(payload, progress):
        return volume_provider.apply_plan(
            payload,
            confirm_count=payload["confirm_count"],
            confirm_plan_sha256=payload["confirm_plan_sha256"],
            progress=lambda current, total, name: progress(
                current, total, f"분권 묶기 {current:,}/{total:,}: {name}"
            ),
        )

    runner.register("volume_group_merge", apply_volume_job)

    services = LibraryServiceRegistry(
        state_db=config.state_db,
        house_dir=config.house_dir,
        temp_dir=config.temp_dir,
        index_path=config.index_path,
        project_root=config.project_root,
        runner=runner,
    )

    app = Flask(__name__)
    app.config["library_server_config"] = config
    app.extensions["library_state_db_readonly_keeper"] = readonly_keeper
    app.extensions["library_review_registry"] = registry
    app.extensions["library_job_runner"] = runner
    app.extensions["library_service_registry"] = services
    appearance_path = config.runtime_dir / "appearance.json"

    @app.after_request
    def response_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(KeyError)
    def handle_missing(exc):
        return jsonify({"ok": False, "error": {"code": "not_found", "message": str(exc)}}), 404

    @app.errorhandler(FileNotFoundError)
    def handle_missing_file(exc):
        return jsonify(
            {"ok": False, "error": {"code": "missing_resource", "message": str(exc)}}
        ), 503

    @app.errorhandler(ValueError)
    def handle_value(exc):
        return jsonify({"ok": False, "error": {"code": "invalid_request", "message": str(exc)}}), 400

    @app.errorhandler(sqlite3.Error)
    def handle_sqlite(exc):
        return jsonify({"ok": False, "error": {"code": "database_error", "message": str(exc)}}), 500

    @app.errorhandler(JobActiveError)
    def handle_active_job(exc):
        return jsonify({
            "ok": False,
            "error": {
                "code": "job_active",
                "message": "다른 변경 작업이 실행 중입니다.",
            },
            "data": {"active_job_id": exc.job_id},
        }), 409

    @app.get("/health")
    def health():
        if not config.state_db.is_file():
            return jsonify({
                "ok": False,
                "version": SERVER_VERSION,
                "state_db": str(config.state_db),
                "database": "missing",
            }), 503
        try:
            conn = decision_store.connect_state_db_readonly(config.state_db)
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return jsonify({
                "ok": False,
                "version": SERVER_VERSION,
                "state_db": str(config.state_db),
                "database": "unavailable",
                "error": str(exc),
            }), 503
        return jsonify(
            {
                "ok": True,
                "version": SERVER_VERSION,
                "state_db": str(config.state_db),
                "database": "ok",
            }
        )

    @app.get("/api/dashboard")
    def dashboard():
        return jsonify({"ok": True, "data": dashboard_snapshot(config, runner)})

    @app.get("/api/explorer/files")
    def explorer_files():
        return jsonify({"ok": True, "data": file_listing(
            config.state_db,
            search=request.args.get("search", ""),
            source=request.args.get("source", "active"),
            extension=request.args.get("extension", "all"),
            sort=request.args.get("sort", "name"),
            direction=request.args.get("direction", "asc"),
            limit=request.args.get("limit", 50, type=int),
            cursor=request.args.get("cursor") or None,
        )})

    @app.get("/api/explorer/files/<file_id>")
    def explorer_file(file_id):
        return jsonify({"ok": True, "data": file_detail(config.state_db, file_id)})

    @app.get("/api/explorer/compare")
    def explorer_compare():
        return jsonify({"ok": True, "data": compare_files(
            config.state_db,
            request.args.get("left", ""),
            request.args.get("right", ""),
        )})

    @app.get("/api/explorer/folders")
    def explorer_folders():
        return jsonify({"ok": True, "data": folder_listing(
            config.state_db,
            config.house_dir,
            search=request.args.get("search", ""),
            state=request.args.get("state", "all"),
            sort=request.args.get("sort", "name"),
            direction=request.args.get("direction", "asc"),
            limit=request.args.get("limit", 50, type=int),
            cursor=request.args.get("cursor") or None,
            refresh=request.args.get("refresh") == "1",
        )})

    @app.get("/api/explorer/folders/detail")
    def explorer_folder_detail():
        folder_path = request.args.get("path", "")
        if not folder_path:
            raise ValueError("path is required")
        return jsonify({"ok": True, "data": folder_detail(
            config.state_db, config.house_dir, folder_path
        )})

    @app.get("/api/explorer/quarantine")
    def explorer_quarantine():
        return jsonify({"ok": True, "data": quarantine_listing(
            config.state_db,
            config.temp_dir,
            search=request.args.get("search", ""),
            state=request.args.get("state", "all"),
            limit=request.args.get("limit", 50, type=int),
            cursor=request.args.get("cursor") or None,
        )})

    @app.get("/api/settings/appearance")
    def appearance_settings():
        settings, persisted = read_appearance(appearance_path)
        return jsonify({
            "ok": True,
            "data": {"settings": settings, "persisted": persisted},
        })

    @app.put("/api/settings/appearance")
    def update_appearance_settings():
        body = _json_body()
        if not isinstance(body.get("settings"), dict):
            raise ValueError("settings 객체가 필요합니다")
        settings = write_appearance(appearance_path, body["settings"])
        return jsonify({
            "ok": True,
            "data": {"settings": settings, "persisted": True},
        })

    @app.delete("/api/settings/appearance")
    def reset_appearance_settings():
        settings = reset_appearance(appearance_path)
        return jsonify({
            "ok": True,
            "data": {"settings": settings, "persisted": False},
        })

    @app.get("/api/providers")
    def providers():
        return jsonify({"ok": True, "data": registry.descriptors()})

    @app.get("/api/services")
    def service_catalog():
        return jsonify({"ok": True, "data": services.descriptors()})

    @app.get("/api/services/<service_id>")
    def service_detail(service_id):
        return jsonify({"ok": True, "data": services.descriptor(service_id)})

    @app.get("/api/catalog")
    def catalog():
        return jsonify({
            "ok": True,
            "data": catalog_listing(
                config.state_db,
                search=request.args.get("search", ""),
                status=request.args.get("status", "all"),
                limit=request.args.get("limit", 50, type=int),
                cursor=request.args.get("cursor"),
            ),
        })

    @app.get("/api/review/queue")
    def review_queue():
        return jsonify({
            "ok": True,
            "data": review_queue_listing(
                config.state_db,
                config.temp_dir,
                search=request.args.get("search", ""),
                category=request.args.get("category", "all"),
                physical=request.args.get("physical", "all"),
                limit=request.args.get("limit", 100, type=int),
            ),
        })

    @app.post("/api/services/<service_id>/start")
    def service_start(service_id):
        body = _json_body()
        source = str(body.get("source") or "service_detail")
        if source not in {"dashboard", "service_detail"}:
            raise ValueError("unknown service start source")
        try:
            record = services.start(service_id, source=source)
        except ServiceBlocked as exc:
            return jsonify({
                "ok": False,
                "error": {
                    "code": str(exc.descriptor.get("blocked_code") or "service_blocked"),
                    "message": str(exc),
                },
                "data": exc.descriptor,
            }), 409
        return jsonify({"ok": True, "data": record}), 202

    @app.get("/api/review/titles")
    def title_cases():
        result = title_provider.list_cases(
            search=request.args.get("search", ""),
            status_filter=request.args.get("status", "all"),
            cursor=request.args.get("cursor") or None,
            limit=request.args.get("limit", 50, type=int),
            sort=request.args.get("sort", "name"),
            direction=request.args.get("direction", "asc"),
        )
        return jsonify({"ok": True, "data": result})

    @app.get("/api/review/titles/<file_id>")
    def title_case(file_id):
        return jsonify({"ok": True, "data": title_provider.get_case(file_id)})

    @app.post("/api/review/titles/preview")
    def title_preview():
        body = _json_body()
        result = title_provider.preview(body)
        return jsonify({"ok": True, "data": result})

    @app.post("/api/review/titles/plan")
    def title_plan():
        body = _json_body()
        changes = body.get("changes")
        if not isinstance(changes, list):
            raise ValueError("changes 배열이 필요합니다")
        result = title_provider.build_plan(changes)
        return jsonify({"ok": True, "data": result})

    @app.post("/api/review/titles/apply")
    def title_apply():
        body = _json_body()
        changes = body.get("changes")
        if not isinstance(changes, list):
            raise ValueError("changes 배열이 필요합니다")
        confirm_count = int(body.get("confirm_count", -1))
        confirm_sha = str(body.get("confirm_plan_sha256") or "")
        plan = title_provider.build_plan(changes)
        if not plan["runnable"]:
            return jsonify({"ok": False, "error": {"code": "plan_blocked", "message": "실행할 수 없는 항목이 있습니다"}, "data": plan}), 409
        if confirm_count != plan["item_count"] or confirm_sha != plan["plan_sha256"]:
            return jsonify({"ok": False, "error": {"code": "confirmation_stale", "message": "확인한 계획과 현재 계획이 다릅니다"}, "data": plan}), 409
        record = runner.start_exclusive(
            "title_requeue",
            {
                "changes": changes,
                "confirm_count": confirm_count,
                "confirm_plan_sha256": confirm_sha,
            },
        )
        return jsonify({"ok": True, "data": record}), 202

    @app.get("/api/review/volumes")
    def volume_cases():
        result = volume_provider.list_cases(
            search=request.args.get("search", ""),
            classification=request.args.get("classification", "all"),
            cursor=request.args.get("cursor") or None,
            limit=request.args.get("limit", 50, type=int),
            sort=request.args.get("sort", "classification"),
            direction=request.args.get("direction", "asc"),
        )
        return jsonify({"ok": True, "data": result})

    @app.get("/api/review/volumes/<case_id>")
    def volume_case(case_id):
        return jsonify({"ok": True, "data": volume_provider.get_case(case_id)})

    @app.post("/api/review/volumes/preview")
    def volume_preview():
        return jsonify({"ok": True, "data": volume_provider.preview(_json_body())})

    @app.post("/api/review/volumes/apply")
    def volume_apply():
        body = _json_body()
        confirm_count = int(body.get("confirm_count", -1))
        confirm_sha = str(body.get("confirm_plan_sha256") or "")
        plan = volume_provider.preview(body)
        if not plan["apply_available"]:
            return jsonify({
                "ok": False,
                "error": {"code": "plan_blocked", "message": "실행할 수 없는 분권 계획입니다"},
                "data": plan,
            }), 409
        if confirm_count != plan["item_count"] or confirm_sha != plan["plan_sha256"]:
            return jsonify({
                "ok": False,
                "error": {"code": "confirmation_stale", "message": "확인한 계획과 현재 계획이 다릅니다"},
                "data": plan,
            }), 409
        payload = {
            "case_id": body.get("case_id"),
            "source_revision": body.get("source_revision"),
            "selected_file_ids": body.get("selected_file_ids"),
            "target_folder_name": body.get("target_folder_name"),
            "allow_duplicate_coordinates": body.get("allow_duplicate_coordinates") is True,
            "confirm_count": confirm_count,
            "confirm_plan_sha256": confirm_sha,
        }
        record = runner.start_exclusive("volume_group_merge", payload)
        return jsonify({"ok": True, "data": record}), 202

    @app.get("/api/jobs")
    def jobs():
        return jsonify({"ok": True, "data": runner.list(limit=request.args.get("limit", 50, type=int))})

    @app.get("/api/reports/dedup")
    def dedup_reports():
        return jsonify({
            "ok": True,
            "data": dedup_report_listing(
                config.temp_dir,
                search=request.args.get("search", ""),
                kind=request.args.get("kind", "all"),
                limit=request.args.get("limit", 200, type=int),
            ),
        })

    @app.get("/api/reports/dedup/<name>")
    def dedup_report(name):
        return jsonify({
            "ok": True,
            "data": read_dedup_report(config.temp_dir, name),
        })

    @app.get("/api/reports/dedup/<name>/download")
    def dedup_report_download(name):
        report_format = request.args.get("format", "text")
        if report_format not in {"text", "json"}:
            raise ValueError("지원하지 않는 dedup 보고서 다운로드 형식입니다")
        if report_format == "text":
            download_name, text = export_dedup_report_text(config.temp_dir, name)
            return send_file(
                BytesIO(text.encode("utf-8")),
                as_attachment=True,
                download_name=download_name,
                mimetype="text/plain; charset=utf-8",
            )
        path = dedup_report_path(config.temp_dir, name, structured=True)
        return send_file(
            path,
            as_attachment=True,
            download_name=path.name,
            mimetype="application/json",
        )

    @app.get("/api/jobs/<job_id>")
    def job(job_id):
        return jsonify({"ok": True, "data": runner.get(job_id)})

    @app.get("/api/jobs/<job_id>/log")
    def job_log(job_id):
        record = runner.get(job_id)
        path = Path(record["log_path"])
        return jsonify(
            {
                "ok": True,
                "data": {
                    "job_id": job_id,
                    "text": path.read_text(encoding="utf-8") if path.is_file() else "",
                },
            }
        )

    @app.get("/api/jobs/<job_id>/log/download")
    def job_log_download(job_id):
        record = runner.get(job_id)
        path = Path(record["log_path"]).resolve()
        try:
            path.relative_to(store.logs_dir)
        except ValueError as exc:
            raise ValueError("job log path is outside the runtime log directory") from exc
        if not path.is_file():
            raise FileNotFoundError(f"job log is missing: {job_id}")
        return send_file(
            path,
            as_attachment=True,
            download_name=f"file-check-{job_id}.log",
            mimetype="text/plain; charset=utf-8",
        )

    @app.get("/api/jobs/<job_id>/events")
    def job_events(job_id):
        return jsonify({
            "ok": True,
            "data": {
                "job_id": job_id,
                "items": store.events(
                    job_id, limit=request.args.get("limit", 500, type=int)
                ),
            },
        })

    def index_response():
        index = config.frontend_dist / "index.html"
        if index.is_file():
            return send_file(index)
        return (
            "<!doctype html><meta charset='utf-8'><title>file_check</title>"
            "<body style='font-family:system-ui;padding:32px'>"
            "<h1>도서 관리 서버 1.3.1</h1>"
            "<p>프런트 빌드가 없습니다. library_frontend에서 npm run build를 실행하세요.</p>"
            "</body>",
            503,
            {"Content-Type": "text/html; charset=utf-8"},
        )

    @app.get("/")
    def index():
        return index_response()

    @app.get("/<path:path>")
    def frontend(path: str):
        if path.startswith("api/"):
            return jsonify({"ok": False, "error": {"code": "not_found", "message": "API not found"}}), 404
        candidate = (config.frontend_dist / path).resolve()
        try:
            candidate.relative_to(config.frontend_dist)
        except ValueError:
            candidate = None
        if candidate is not None and candidate.is_file():
            return send_from_directory(config.frontend_dist, path)
        return index_response()

    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="file_check 독립 도서 관리 서버")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "9012")))
    parser.add_argument("--server", choices=("flask", "waitress"), default="waitress")
    parser.add_argument("--state-db", default=str(STATE_DB))
    parser.add_argument("--house", default=str(HOUSE_DIR))
    parser.add_argument("--temp", default=str(TEMP_DIR))
    parser.add_argument("--index", default=str(FILE_INDEX))
    parser.add_argument("--runtime-dir", default=str(DEFAULT_RUNTIME_DIR))
    parser.add_argument("--frontend-dist", default=str(DEFAULT_FRONTEND_DIST))
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    app = create_app(
        state_db=Path(args.state_db),
        house_dir=Path(args.house),
        temp_dir=Path(args.temp),
        index_path=Path(args.index),
        runtime_dir=Path(args.runtime_dir),
        frontend_dist=Path(args.frontend_dist),
        project_root=Path(args.project_root),
    )
    if args.server == "flask":
        app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    else:
        try:
            from waitress import serve
        except ImportError:
            print("waitress가 없습니다. pip install -r requirements.txt를 실행하세요.", file=sys.stderr)
            return 2
        serve(app, host=args.host, port=args.port, threads=8, channel_timeout=120)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
