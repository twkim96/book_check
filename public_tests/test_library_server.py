import json
import sqlite3

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
            "enabled": False,
            "planned_version": "1.2.9",
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
