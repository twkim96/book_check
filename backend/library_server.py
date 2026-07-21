"""Independent local web server for file_check library operations."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from flask import Flask, jsonify, request, send_file, send_from_directory

import decision_store
from library_jobs import JobRunner, JobStore
from library_review import (
    ReviewProviderRegistry,
    TitleCorrectionProvider,
    VolumeGroupProvider,
)
from normalizer import should_exclude_dir, should_exclude_file
from project_paths import FILE_INDEX, HOUSE_DIR, PROJECT_ROOT, STATE_DB, TEMP_DIR


SERVER_VERSION = "1.2.11"
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


def _open_state_db_readonly_keeper(state_db: Path):
    """Prepare WAL sidecars, then keep one read-only connection alive.

    Some macOS Python SQLite builds cannot be the first ``mode=ro`` opener of a
    WAL database after the last writer removed ``-wal``/``-shm``.  A short
    normal connection recreates only those SQLite coordination files.  Opening
    the read-only keeper before closing the bootstrap connection makes all
    request-scoped read-only connections reliable for the server lifetime.
    """
    bootstrap = decision_store.connect_state_db(state_db)
    try:
        bootstrap.execute("PRAGMA query_only = ON")
        keeper = decision_store.connect_state_db_readonly(state_db)
    finally:
        bootstrap.close()
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
        issues = decision_store.doctor_issues(conn)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    warning_dir = config.temp_dir / "trash_bin" / "warning"
    return {
        "version": SERVER_VERSION,
        "database": {
            "path": str(config.state_db),
            "integrity": integrity,
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
            "folderling_pending": _count_supported(config.temp_dir, intake_only=True),
            "warning_files": _count_supported(warning_dir),
            "index": _index_counts(config.index_path),
        },
        "jobs": runner.list(limit=10),
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
) -> Flask:
    config = LibraryServerConfig(
        state_db=Path(state_db).expanduser().resolve(),
        house_dir=Path(house_dir).expanduser().resolve(),
        temp_dir=Path(temp_dir).expanduser().resolve(),
        index_path=Path(index_path).expanduser().resolve(),
        runtime_dir=Path(runtime_dir).expanduser().resolve(),
        frontend_dist=Path(frontend_dist).expanduser().resolve(),
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

    app = Flask(__name__)
    app.config["library_server_config"] = config
    app.extensions["library_state_db_readonly_keeper"] = readonly_keeper
    app.extensions["library_review_registry"] = registry
    app.extensions["library_job_runner"] = runner

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

    @app.get("/health")
    def health():
        return jsonify(
            {
                "ok": config.state_db.is_file(),
                "version": SERVER_VERSION,
                "state_db": str(config.state_db),
            }
        ), (200 if config.state_db.is_file() else 503)

    @app.get("/api/dashboard")
    def dashboard():
        return jsonify({"ok": True, "data": dashboard_snapshot(config, runner)})

    @app.get("/api/providers")
    def providers():
        return jsonify({"ok": True, "data": registry.descriptors()})

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
        record = runner.start(
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
        record = runner.start("volume_group_merge", payload)
        return jsonify({"ok": True, "data": record}), 202

    @app.get("/api/jobs")
    def jobs():
        return jsonify({"ok": True, "data": runner.list(limit=request.args.get("limit", 50, type=int))})

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

    def index_response():
        index = config.frontend_dist / "index.html"
        if index.is_file():
            return send_file(index)
        return (
            "<!doctype html><meta charset='utf-8'><title>file_check</title>"
            "<body style='font-family:system-ui;padding:32px'>"
            "<h1>도서 관리 서버 1.2.11</h1>"
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
