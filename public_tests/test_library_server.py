import json
import sqlite3
import time
from pathlib import Path

import pytest

import decision_store
from library_jobs import JobActiveError, JobRunner, JobStore
from library_server import create_app


def _server_fixture(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    frontend = tmp_path / "dist"
    runtime = tmp_path / "runtime"
    house.mkdir()
    temp.mkdir()
    frontend.mkdir()
    (frontend / "index.html").write_text("<h1>library ui</h1>", encoding="utf-8")
    path = house / "수동 교정 작품 146.txt"
    path.write_text("수동 교정 본문", encoding="utf-8")
    conn = decision_store.initialize_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            row = decision_store.reconcile_file_metadata(conn, path, source="house")
            analysis = conn.execute(
                "SELECT * FROM file_analysis WHERE file_id = ?", (row["file_id"],)
            ).fetchone()
            conn.execute(
                "INSERT INTO catalog_titles(title_key, display_title, query_title, normalizer_version) "
                "VALUES (?, ?, ?, ?)",
                (
                    analysis["core_title"],
                    analysis["readable_title"],
                    analysis["catalog_query_title"],
                    analysis["normalizer_version"],
                ),
            )
            for platform in ("series", "kakao", "novelpia"):
                conn.execute(
                    "INSERT INTO catalog_platform_stats(title_key, platform, status) "
                    "VALUES (?, ?, 'not_found')",
                    (analysis["core_title"], platform),
                )
    finally:
        conn.close()
    index = tmp_path / "file_index.json"
    index.write_text(
        json.dumps({"entries": [{"type": "file"}], "normalizer_version": "1.2.7"}),
        encoding="utf-8",
    )
    app = create_app(
        state_db=state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=index,
        runtime_dir=runtime,
        frontend_dist=frontend,
    )
    app.config.update(TESTING=True)
    return app, row["file_id"]


def test_health_dashboard_and_title_review_api(tmp_path):
    app, file_id = _server_fixture(tmp_path)
    client = app.test_client()
    assert client.get("/health").get_json()["ok"] is True
    providers = client.get("/api/providers").get_json()["data"]
    assert providers == [
        {"id": "title_correction", "label": "제목 교정", "enabled": True},
        {
            "id": "volume_group",
            "label": "분권 묶기",
            "enabled": True,
        },
    ]
    dashboard = client.get("/api/dashboard").get_json()["data"]
    assert dashboard["database"]["doctor_ok"] is True
    assert dashboard["database"]["supported_house_files"] == 1
    assert dashboard["filesystem"]["index"]["files"] == 1

    listing = client.get("/api/review/titles").get_json()["data"]
    assert listing["total"] == 1
    [case] = listing["items"]
    assert case["file_id"] == file_id

    preview = client.post(
        "/api/review/titles/preview",
        json={
            "file_id": file_id,
            "source_revision": case["source_revision"],
            "new_body": "수동 교정 작품 1-146",
        },
    ).get_json()["data"]
    assert preview["runnable"] is True
    plan = client.post(
        "/api/review/titles/plan",
        json={
            "changes": [
                {
                    "file_id": file_id,
                    "source_revision": case["source_revision"],
                    "new_body": "수동 교정 작품 1-146",
                }
            ]
        },
    ).get_json()["data"]
    assert plan["runnable"] is True
    assert len(plan["plan_sha256"]) == 64
    assert client.get("/review/titles").status_code == 200


def test_appearance_settings_are_persisted_and_reset_in_runtime_dir(tmp_path):
    app, _ = _server_fixture(tmp_path)
    client = app.test_client()
    config = app.config["library_server_config"]

    initial = client.get("/api/settings/appearance")
    assert initial.status_code == 200
    assert initial.get_json()["data"] == {
        "settings": {
            "backgroundColor": "#0a0c10",
            "textColor": "#edf1f7",
            "accentColor": "#3976da",
        },
        "persisted": False,
    }

    saved = client.put(
        "/api/settings/appearance",
        json={
            "settings": {
                "backgroundColor": "#101820",
                "textColor": "#F1F5F9",
                "accentColor": "#8B5CF6",
            }
        },
    )
    assert saved.status_code == 200
    assert saved.get_json()["data"] == {
        "settings": {
            "backgroundColor": "#101820",
            "textColor": "#f1f5f9",
            "accentColor": "#8b5cf6",
        },
        "persisted": True,
    }
    store = config.runtime_dir / "appearance.json"
    assert json.loads(store.read_text(encoding="utf-8")) == saved.get_json()["data"]["settings"]
    assert client.get("/api/settings/appearance").get_json()["data"]["persisted"] is True

    reset = client.delete("/api/settings/appearance")
    assert reset.status_code == 200
    assert reset.get_json()["data"]["persisted"] is False
    assert reset.get_json()["data"]["settings"]["backgroundColor"] == "#0a0c10"
    assert not store.exists()


def test_appearance_settings_require_an_object_and_normalize_invalid_fields(tmp_path):
    app, _ = _server_fixture(tmp_path)
    client = app.test_client()
    assert client.put("/api/settings/appearance", json={}).status_code == 400

    response = client.put(
        "/api/settings/appearance",
        json={"settings": {"backgroundColor": "invalid", "accentColor": "#ABCDEF"}},
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["settings"] == {
        "backgroundColor": "#0a0c10",
        "textColor": "#edf1f7",
        "accentColor": "#abcdef",
    }


def test_dashboard_defers_full_file_doctor_but_mutation_doctor_stays_strict(tmp_path):
    app, file_id = _server_fixture(tmp_path)
    config = app.config["library_server_config"]
    conn = decision_store.connect_state_db_readonly(config.state_db)
    try:
        path = conn.execute(
            "SELECT canonical_path FROM files WHERE file_id = ?", (file_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    Path(path).unlink()

    dashboard = app.test_client().get("/api/dashboard").get_json()["data"]
    assert dashboard["database"]["doctor_ok"] is True
    assert dashboard["database"]["doctor_scope"] == "operational"
    assert dashboard["database"]["integrity"] == "deferred"

    conn = decision_store.connect_state_db_readonly(config.state_db)
    try:
        issues = decision_store.doctor_issues(conn)
    finally:
        conn.close()
    assert any(issue["kind"] == "missing_file" for issue in issues)


def test_platform_service_preview_is_shared_briefly_and_invalidatable(tmp_path, monkeypatch):
    app, _ = _server_fixture(tmp_path)
    registry = app.extensions["library_service_registry"]
    calls = []
    expected = {"platform-update": (3, {"discovered_titles": 4})}

    def compute():
        calls.append("compute")
        return expected

    monkeypatch.setattr(registry, "_compute_platform_previews", compute)
    assert registry._platform_previews() is expected
    assert registry._platform_previews() is expected
    assert calls == ["compute"]

    registry._invalidate_platform_previews()
    assert registry._platform_previews() is expected
    assert calls == ["compute", "compute"]


def test_readonly_catalog_groups_owned_files_and_platform_status(tmp_path):
    app, file_id = _server_fixture(tmp_path)
    client = app.test_client()

    response = client.get("/api/catalog?status=missing&search=수동")

    assert response.status_code == 200
    listing = response.get_json()["data"]
    assert listing["readonly"] is True
    assert listing["total"] == 1
    [item] = listing["items"]
    assert item["display_title"] == "수동 교정 작품 146"
    assert item["files"][0]["file_id"] == file_id
    assert item["folders"]
    assert item["variant_ids"] == []
    assert item["work_bucket_ids"] == []
    assert item["platforms"]["series"]["status"] == "not_found"
    assert item["platforms"]["kakao"]["status"] == "not_found"
    assert item["platforms"]["novelpia"]["status"] == "not_found"
    assert client.get("/catalog").status_code == 200


def test_readonly_explorer_routes_expose_file_folder_and_quarantine(tmp_path):
    app, file_id = _server_fixture(tmp_path)
    client = app.test_client()

    files = client.get("/api/explorer/files?source=house&search=수동").get_json()["data"]
    assert files["readonly"] is True
    assert files["items"][0]["file_id"] == file_id

    detail = client.get(f"/api/explorer/files/{file_id}").get_json()["data"]
    assert detail["file"]["name"] == "수동 교정 작품 146.txt"
    assert detail["actions"]["quarantine"] is False

    folders = client.get("/api/explorer/folders?search=house&refresh=1").get_json()["data"]
    assert folders["readonly"] is True
    [folder] = folders["items"]
    folder_detail_response = client.get(
        "/api/explorer/folders/detail", query_string={"path": folder["path"]}
    )
    assert folder_detail_response.status_code == 200
    assert folder_detail_response.get_json()["data"]["registered_count"] == 1

    quarantine = client.get("/api/explorer/quarantine").get_json()["data"]
    assert quarantine["readonly"] is True
    assert quarantine["total"] == 0
    assert client.get("/api/explorer/compare", query_string={"left": file_id}).status_code == 400


def test_readonly_review_queue_lists_managed_warning_files(tmp_path):
    app, _ = _server_fixture(tmp_path)
    config = app.config["library_server_config"]
    warning = config.temp_dir / "trash_bin" / "warning"
    warning.mkdir(parents=True)
    queued = warning / "사람이 확인할 작품.txt"
    queued.write_text("review", encoding="utf-8")

    response = app.test_client().get(
        "/api/review/queue?category=warning&search=확인"
    )

    assert response.status_code == 200
    listing = response.get_json()["data"]
    assert listing["readonly"] is True
    [item] = listing["items"]
    assert item["kind"] == "filesystem"
    assert item["category"] == "warning"
    assert item["physical_state"] == "quarantined"
    assert item["path"] == str(queued.resolve())


def test_dashboard_pending_matches_folderling_intake_exclusions(tmp_path):
    app, _ = _server_fixture(tmp_path)
    config = app.config["library_server_config"]
    (config.temp_dir / "dedup_logs").mkdir()
    (config.temp_dir / "dedup_logs" / "report.txt").write_text("log", encoding="utf-8")
    warning = config.temp_dir / "trash_bin" / "warning"
    warning.mkdir(parents=True)
    (warning / "review.txt").write_text("warning", encoding="utf-8")
    nested = config.temp_dir / "title_cleanup_collision_1"
    nested.mkdir()
    (nested / "intake.epub").write_text("book", encoding="utf-8")
    (config.temp_dir / "direct.txt").write_text("book", encoding="utf-8")

    dashboard = app.test_client().get("/api/dashboard").get_json()["data"]
    assert dashboard["filesystem"]["folderling_pending"] == 2
    assert dashboard["filesystem"]["warning_files"] == 1


def test_historical_dedup_reports_are_readonly_searchable_and_downloadable(tmp_path):
    app, _ = _server_fixture(tmp_path)
    config = app.config["library_server_config"]
    reports = config.temp_dir / "dedup_logs"
    reports.mkdir()
    structured_path = reports / "dedup_20260721_141500_123456.json"
    structured_path.write_text(
        json.dumps({
            "schema_version": 1,
            "kind": "folderling_dedup",
            "summary": {
                "dry_run": False,
                "managed_mode": True,
                "include_temp": True,
                "exact_count": 0,
                "exact_mutation_count": 0,
                "suspect_group_count": 2,
                "suspect_move_count": 0,
            },
            "exact_records": [],
            "suspect_groups": [],
            "suspect_move_records": [],
            "disambig_records": [],
            "blocked_strong_relations": [],
        }),
        encoding="utf-8",
    )
    client = app.test_client()

    listing = client.get("/api/reports/dedup?search=quarantine").get_json()["data"]
    assert listing["total"] == 1
    assert listing["items"][0]["structured_available"] is True
    assert listing["items"][0]["text_available"] is False
    report_id = listing["items"][0]["report_id"]
    detail = client.get(f"/api/reports/dedup/{report_id}").get_json()["data"]
    assert detail["structured_summary"]["suspect_group_count"] == 2
    assert "[중복/검토 큐 정리 로그]" in detail["text"]
    download = client.get(f"/api/reports/dedup/{report_id}/download")
    assert download.status_code == 200
    assert "[중복/검토 큐 정리 로그]" in download.get_data(as_text=True)
    assert "filename=dedup_20260721_141500_123456.txt" in download.headers[
        "Content-Disposition"
    ]
    assert not structured_path.with_suffix(".txt").exists()
    structured = client.get(
        f"/api/reports/dedup/{report_id}/download?format=json"
    )
    assert structured.status_code == 200
    assert structured.mimetype == "application/json"


def test_service_catalog_exposes_readiness_and_fixed_scopes(tmp_path):
    app, _ = _server_fixture(tmp_path)
    client = app.test_client()

    response = client.get("/api/services")

    assert response.status_code == 200
    services = response.get_json()["data"]
    assert [item["id"] for item in services] == [
        "folderling",
        "scanner",
        "platform-update",
        "platform-retry",
        "platform-refresh",
        "novelpia-auth-retry",
        "google-sheet",
    ]
    scanner = next(item for item in services if item["id"] == "scanner")
    assert scanner["ready"] is True
    assert scanner["target_count"] == 1
    assert scanner["read_scope"] == ["txt_house", "SQLite"]
    folderling = next(item for item in services if item["id"] == "folderling")
    assert folderling["ready"] is False
    assert folderling["blocked_code"] == "no_targets"
    platform = next(item for item in services if item["id"] == "platform-update")
    assert platform["ready"] is False
    assert platform["blocked_code"] == "non_production_layout"


def test_scanner_service_runs_as_persistent_job_with_events_and_log(tmp_path):
    app, _ = _server_fixture(tmp_path)
    client = app.test_client()

    response = client.post(
        "/api/services/scanner/start", json={"source": "dashboard"}
    )
    assert response.status_code == 202
    job_id = response.get_json()["data"]["job_id"]
    runner = app.extensions["library_job_runner"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = runner.get(job_id)
        if job["state"] in {"succeeded", "failed"}:
            break
        time.sleep(0.01)

    assert job["state"] == "succeeded", job
    assert job["result"]["files"] == 1
    assert job["result"]["index_mode"] == "full_scan"
    events = client.get(f"/api/jobs/{job_id}/events").get_json()["data"]["items"]
    assert [event["phase"] for event in events] == [
        "scanner_start",
        "scanner_result",
    ]
    log = client.get(f"/api/jobs/{job_id}/log").get_json()["data"]["text"]
    assert "house 전체 Scanner 시작" in log
    assert "Scanner/index 동기화 완료" in log
    download = client.get(f"/api/jobs/{job_id}/log/download")
    assert download.status_code == 200
    assert download.mimetype == "text/plain"
    config = app.config["library_server_config"]
    assert (config.house_dir / "file_index.json").is_file()


def test_blocked_service_start_returns_current_descriptor(tmp_path):
    app, _ = _server_fixture(tmp_path)
    response = app.test_client().post(
        "/api/services/folderling/start", json={"source": "service_detail"}
    )

    assert response.status_code == 409
    payload = response.get_json()
    assert payload["error"]["code"] == "no_targets"
    assert payload["data"]["id"] == "folderling"
    assert payload["data"]["ready"] is False


def test_service_start_is_blocked_while_another_job_is_active(tmp_path):
    app, _ = _server_fixture(tmp_path)
    runner = app.extensions["library_job_runner"]
    active = runner.store.create("synthetic", {"source": "test"})

    response = app.test_client().post(
        "/api/services/scanner/start", json={"source": "dashboard"}
    )

    assert response.status_code == 409
    payload = response.get_json()
    assert payload["error"]["code"] == "job_active"
    assert active["job_id"] in payload["error"]["message"]


def test_volume_review_api_builds_confirmation_bound_plan(tmp_path):
    app, _ = _server_fixture(tmp_path)
    config = app.config["library_server_config"]
    conn = decision_store.connect_state_db(config.state_db)
    try:
        for number in (1, 2):
            path = config.house_dir / "ㅂ" / f"별빛 도서 {number}권.txt"
            path.parent.mkdir(exist_ok=True)
            path.write_text("volume", encoding="utf-8")
            with decision_store.transaction(conn):
                decision_store.reconcile_file_metadata(conn, path, source="house")
    finally:
        conn.close()

    client = app.test_client()
    listing = client.get("/api/review/volumes?classification=auto_ready").get_json()["data"]
    assert listing["total"] == 1
    [case] = listing["items"]
    preview = client.post(
        "/api/review/volumes/preview",
        json={
            "case_id": case["case_id"],
            "source_revision": case["source_revision"],
        },
    ).get_json()["data"]
    assert preview["plan_ready"] is True
    assert preview["apply_available"] is True
    response = client.post(
        "/api/review/volumes/apply",
        json={
            "case_id": case["case_id"],
            "source_revision": case["source_revision"],
            "selected_file_ids": preview["selected_file_ids"],
            "target_folder_name": preview["target_folder_name"],
            "confirm_count": preview["item_count"],
            "confirm_plan_sha256": preview["plan_sha256"],
        },
    )
    assert response.status_code == 202
    job_id = response.get_json()["data"]["job_id"]
    runner = app.extensions["library_job_runner"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = runner.get(job_id)
        if job["state"] in {"succeeded", "failed"}:
            break
        time.sleep(0.01)
    assert job["state"] == "succeeded", job
    assert job["result"]["index_updated"] is True
    assert job["result"]["index_mode"] == "state_db_projection"
    destination = config.house_dir / "ㅂ" / "별빛 도서"
    assert sorted(path.name for path in destination.iterdir()) == [
        "별빛 도서 1권.txt",
        "별빛 도서 2권.txt",
    ]
    index_payload = json.loads(config.index_path.read_text(encoding="utf-8"))
    indexed = {item["rel_path"] for item in index_payload["entries"] if item["type"] == "file"}
    assert "ㅂ/별빛 도서/별빛 도서 1권.txt" in indexed


def test_job_store_marks_running_records_interrupted_after_restart(tmp_path):
    store = JobStore(tmp_path / "runtime")
    record = store.create("synthetic", {"value": 1})
    store.update(record["job_id"], state="running", stage="running")
    assert store.mark_interrupted() == 1
    restored = store.get(record["job_id"])
    assert restored["state"] == "interrupted"
    assert restored["error"]["code"] == "server_restarted"
    [event] = store.events(record["job_id"])
    assert event["phase"] == "job_interrupted"
    assert event["status"] == "interrupted"


def test_job_runner_persists_structured_failure_event(tmp_path):
    runner = JobRunner(JobStore(tmp_path / "runtime"))
    try:
        def fail(_payload, _progress):
            raise RuntimeError("fixture failure")

        runner.register("failing", fail)
        record = runner.start("failing", {})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = runner.get(record["job_id"])
            if job["state"] == "failed":
                break
            time.sleep(0.01)
        assert job["state"] == "failed"
        event = runner.store.events(record["job_id"])[-1]
        assert event["phase"] == "job_failed"
        assert event["error_code"] == "RuntimeError"
        assert event["error_message"] == "fixture failure"
    finally:
        runner.shutdown()


def test_job_runner_exclusive_rejects_a_second_active_job(tmp_path):
    runner = JobRunner(JobStore(tmp_path / "runtime"))
    try:
        active = runner.store.create("first", {})
        with pytest.raises(JobActiveError) as raised:
            runner.start_exclusive("second", {})
        assert raised.value.job_id == active["job_id"]
    finally:
        runner.shutdown()


def test_missing_state_db_returns_structured_service_error(tmp_path):
    app = create_app(
        state_db=tmp_path / "missing.sqlite3",
        house_dir=tmp_path / "house",
        temp_dir=tmp_path / "temp",
        index_path=tmp_path / "index.json",
        runtime_dir=tmp_path / "runtime",
        frontend_dist=tmp_path / "dist",
    )
    app.config.update(TESTING=True)
    client = app.test_client()
    assert client.get("/health").status_code == 503
    response = client.get("/api/dashboard")
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "missing_resource"


def test_server_keeps_query_only_normal_connection_for_wal_sidecars(
    tmp_path, monkeypatch
):
    state_db = tmp_path / "state.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    conn.close()
    events = []
    writer_open = False
    real_writer = decision_store.connect_state_db

    class TrackedWriter:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, *args, **kwargs):
            return self.connection.execute(*args, **kwargs)

        def close(self):
            nonlocal writer_open
            events.append("writer_close")
            writer_open = False
            self.connection.close()

    def open_writer(path, *args, **kwargs):
        nonlocal writer_open
        events.append("writer_open")
        writer_open = True
        return TrackedWriter(real_writer(path, *args, **kwargs))

    monkeypatch.setattr(decision_store, "connect_state_db", open_writer)
    app = create_app(
        state_db=state_db,
        house_dir=tmp_path / "house",
        temp_dir=tmp_path / "temp",
        index_path=tmp_path / "index.json",
        runtime_dir=tmp_path / "runtime",
        frontend_dist=tmp_path / "dist",
    )

    keeper = app.extensions["library_state_db_readonly_keeper"]
    try:
        assert keeper.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
        assert keeper.execute("PRAGMA query_only").fetchone()[0] == 1
        assert writer_open is True
        assert events == ["writer_open"]
        assert app.test_client().get("/health").status_code == 200
    finally:
        keeper.close()
    assert events == ["writer_open", "writer_close"]


def test_readonly_connection_retries_a_transient_open_failure(tmp_path, monkeypatch):
    state_db = tmp_path / "state.sqlite3"
    conn = sqlite3.connect(state_db)
    conn.execute("CREATE TABLE sample(value INTEGER)")
    conn.commit()
    conn.close()

    real_connect = sqlite3.connect
    calls = 0

    def transient_connect(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(decision_store.sqlite3, "connect", transient_connect)
    readonly = decision_store.connect_state_db_readonly(state_db)
    try:
        assert readonly.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 0
    finally:
        readonly.close()
    assert calls == 2
