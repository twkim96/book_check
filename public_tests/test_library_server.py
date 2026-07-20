import json
import sqlite3
import time

import decision_store
from library_jobs import JobStore
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


def test_server_bootstraps_wal_before_opening_readonly_keeper(tmp_path, monkeypatch):
    state_db = tmp_path / "state.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    conn.close()
    events = []
    writer_open = False
    real_writer = decision_store.connect_state_db
    real_reader = decision_store.connect_state_db_readonly

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

    def open_reader(path, *args, **kwargs):
        assert writer_open is True
        events.append("reader_open")
        return real_reader(path, *args, **kwargs)

    monkeypatch.setattr(decision_store, "connect_state_db", open_writer)
    monkeypatch.setattr(decision_store, "connect_state_db_readonly", open_reader)
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
        assert events == ["writer_open", "reader_open", "writer_close"]
    finally:
        keeper.close()


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
