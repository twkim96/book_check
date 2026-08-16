import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import decision_store
import library_server
from dedup_mutations import _ensure_intake_fingerprint, _file_state
from library_jobs import JobActiveError, JobNeedsReview, JobRunner, JobStore
from library_server import _interrupted_folderling_run_id, create_app


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
        project_root=tmp_path,
    )
    app.config.update(TESTING=True)
    return app, row["file_id"]


def test_file_relocate_preview_and_apply_api(tmp_path):
    app, file_id = _server_fixture(tmp_path)
    config = app.config["library_server_config"]
    conn = decision_store.connect_state_db(config.state_db)
    try:
        _ensure_intake_fingerprint(conn, _file_state(conn, file_id))
    finally:
        conn.close()
    target = config.house_dir / "정리된 작품"
    target.mkdir()
    client = app.test_client()
    payload = {
        "file_id": file_id,
        "target_directory": str(target),
        "new_name": "수동 교정 작품 146.txt",
    }
    response = client.post("/api/management/files/relocate/preview", json=payload)
    assert response.status_code == 200
    plan = response.get_json()["data"]
    assert plan["apply_available"] is True
    assert plan["move"] is True and plan["rename"] is False

    started = client.post(
        "/api/management/files/relocate/apply",
        json={
            **payload,
            "confirm_count": plan["item_count"],
            "confirm_plan_sha256": plan["plan_sha256"],
        },
    )
    assert started.status_code == 202
    job_id = started.get_json()["data"]["job_id"]
    deadline = time.time() + 3
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").get_json()["data"]
        if job["state"] in {"succeeded", "failed", "needs_review", "interrupted"}:
            break
        time.sleep(0.01)
    assert job["state"] == "succeeded"
    assert (target / payload["new_name"]).is_file()


def test_library_server_migrates_v11_with_owned_backup(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup.sqlite3"
    conn = decision_store.initialize_state_db(state_db)
    try:
        conn.execute("DROP TABLE work_folders")
        conn.execute("DROP TABLE operation_groups")
        conn.execute("PRAGMA user_version = 11")
        conn.commit()
    finally:
        conn.close()
    house, temp, runtime, frontend = (
        tmp_path / "house", tmp_path / "temp", tmp_path / "runtime", tmp_path / "dist"
    )
    for path in (house, temp, frontend):
        path.mkdir()
    (frontend / "index.html").write_text("ok", encoding="utf-8")

    app = create_app(
        state_db=state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=tmp_path / "file_index.json",
        runtime_dir=runtime,
        frontend_dist=frontend,
    )
    assert app.test_client().get("/health").status_code == 200
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        assert (
            conn.execute("PRAGMA user_version").fetchone()[0]
            == decision_store.SCHEMA_VERSION
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('operation_groups', 'work_folders')"
        ).fetchone()[0] == 2
    finally:
        conn.close()
    backups = list((state_db.parent / "backups").glob("before_library_server_schema_*.sqlite3"))
    assert len(backups) == 1


def test_managed_folder_create_api_appears_in_folder_catalog(tmp_path):
    app, _ = _server_fixture(tmp_path)
    config = app.config["library_server_config"]
    conn = decision_store.connect_state_db(config.state_db)
    try:
        with decision_store.transaction(conn):
            work_id = int(conn.execute(
                "INSERT INTO works(display_title) VALUES ('API 관리 작품')"
            ).lastrowid)
    finally:
        conn.close()
    parent = config.house_dir / "ㄱ"
    parent.mkdir()
    client = app.test_client()
    payload = {
        "work_bucket_id": work_id,
        "parent_directory": str(parent),
        "folder_name": "API 관리 작품",
        "role": "primary",
    }
    preview = client.post(
        "/api/management/folders/create/preview", json=payload
    )
    assert preview.status_code == 200
    plan = preview.get_json()["data"]
    assert plan["apply_available"] is True
    started = client.post(
        "/api/management/folders/create/apply",
        json={
            **payload,
            "confirm_count": plan["item_count"],
            "confirm_plan_sha256": plan["plan_sha256"],
        },
    )
    assert started.status_code == 202
    job_id = started.get_json()["data"]["job_id"]
    deadline = time.time() + 3
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").get_json()["data"]
        if job["state"] in {"succeeded", "failed", "needs_review", "interrupted"}:
            break
        time.sleep(0.01)
    assert job["state"] == "succeeded"
    listing = client.get(
        "/api/explorer/folders?state=managed&refresh=1"
    ).get_json()["data"]
    assert listing["total"] == 1
    assert listing["items"][0]["managed_role"] == "primary"
    assert listing["items"][0]["file_count"] == 0
    target_parent = config.house_dir / "ㄴ"
    target_parent.mkdir()
    relocate = client.post(
        "/api/management/folders/relocate/preview",
        json={
            "folder_id": listing["items"][0]["managed_folder_id"],
            "target_parent": str(target_parent),
            "new_name": "API 이동 작품",
        },
    )
    assert relocate.status_code == 200
    assert relocate.get_json()["data"]["apply_available"] is True


def test_work_merge_management_api_runs_as_exclusive_job(tmp_path):
    app, file_id = _server_fixture(tmp_path)
    config = app.config["library_server_config"]
    conn = decision_store.connect_state_db(config.state_db)
    try:
        _ensure_intake_fingerprint(conn, _file_state(conn, file_id))
        with decision_store.transaction(conn):
            source_work = int(conn.execute(
                "INSERT INTO works(display_title) VALUES ('합칠 작품')"
            ).lastrowid)
            target_work = int(conn.execute(
                "INSERT INTO works(display_title) VALUES ('유지 작품')"
            ).lastrowid)
            source_variant = int(conn.execute(
                "INSERT INTO variants(work_bucket_id, variant_kind) VALUES (?, 'base')",
                (source_work,),
            ).lastrowid)
            conn.execute(
                "UPDATE files SET variant_id = ?, assignment_state = 'managed', "
                "assignment_origin = 'human_decision', protected = 1 WHERE file_id = ?",
                (source_variant, file_id),
            )
            conn.execute(
                "INSERT INTO representatives(variant_id, file_id) VALUES (?, ?)",
                (source_variant, file_id),
            )
    finally:
        conn.close()
    client = app.test_client()
    detail = client.get(f"/api/management/works/{source_work}")
    assert detail.status_code == 200
    assert detail.get_json()["data"]["work"]["status"] == "active"
    preview = client.post(
        "/api/management/works/merge/preview",
        json={"source_work_id": source_work, "target_work_id": target_work},
    )
    assert preview.status_code == 200
    plan = preview.get_json()["data"]
    assert plan["apply_available"] is True
    started = client.post(
        "/api/management/works/merge/apply",
        json={
            "source_work_id": source_work,
            "target_work_id": target_work,
            "confirm_count": plan["item_count"],
            "confirm_plan_sha256": plan["plan_sha256"],
        },
    )
    assert started.status_code == 202
    job_id = started.get_json()["data"]["job_id"]
    deadline = time.time() + 3
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").get_json()["data"]
        if job["state"] in {"succeeded", "failed", "needs_review", "interrupted"}:
            break
        time.sleep(0.01)
    assert job["state"] == "succeeded", job
    conn = decision_store.connect_state_db_readonly(config.state_db)
    try:
        assert conn.execute(
            "SELECT status FROM works WHERE work_bucket_id = ?", (source_work,)
        ).fetchone()[0] == "retired"
        assert conn.execute(
            "SELECT work_bucket_id FROM variants WHERE variant_id = ?", (source_variant,)
        ).fetchone()[0] == target_work
        assert decision_store.doctor_issues(conn) == []
    finally:
        conn.close()


def test_health_dashboard_and_title_review_api(tmp_path):
    app, file_id = _server_fixture(tmp_path)
    client = app.test_client()
    health = client.get("/health").get_json()
    assert health["ok"] is True
    assert health["version"] == "1.4.21"
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


def test_custom_appearance_presets_are_added_listed_and_deleted(tmp_path):
    app, _ = _server_fixture(tmp_path)
    client = app.test_client()
    config = app.config["library_server_config"]

    initial = client.get("/api/settings/appearance/presets")
    assert initial.status_code == 200
    assert initial.get_json()["data"] == {"presets": [], "persisted": False}

    created = client.post(
        "/api/settings/appearance/presets",
        json={
            "preset": {
                "name": "  밝은   그린  ",
                "settings": {
                    "backgroundColor": "#E0E0E0",
                    "textColor": "#1B1A18",
                    "accentColor": "#149058",
                },
            }
        },
    )
    assert created.status_code == 201
    data = created.get_json()["data"]
    assert data["persisted"] is True
    assert len(data["presets"]) == 1
    preset = data["preset"]
    assert preset == data["presets"][0]
    assert preset["name"] == "밝은 그린"
    assert len(preset["id"]) == 32
    assert preset["settings"] == {
        "backgroundColor": "#e0e0e0",
        "textColor": "#1b1a18",
        "accentColor": "#149058",
    }

    store = config.runtime_dir / "appearance-presets.json"
    stored = json.loads(store.read_text(encoding="utf-8"))
    assert stored == {"version": 1, "presets": [preset]}
    assert client.get("/api/settings/appearance/presets").get_json()["data"] == {
        "presets": [preset],
        "persisted": True,
    }

    duplicate = client.post(
        "/api/settings/appearance/presets",
        json={"preset": {"name": "밝은 그린", "settings": preset["settings"]}},
    )
    assert duplicate.status_code == 400
    builtin = client.post(
        "/api/settings/appearance/presets",
        json={"preset": {"name": "기본 블루", "settings": preset["settings"]}},
    )
    assert builtin.status_code == 400

    deleted = client.delete(f"/api/settings/appearance/presets/{preset['id']}")
    assert deleted.status_code == 200
    assert deleted.get_json()["data"] == {"presets": [], "persisted": False}
    assert not store.exists()


def test_custom_appearance_preset_rejects_invalid_names_and_ids(tmp_path):
    app, _ = _server_fixture(tmp_path)
    client = app.test_client()
    colors = {
        "backgroundColor": "#101820",
        "textColor": "#f1f5f9",
        "accentColor": "#8b5cf6",
    }

    missing_name = client.post(
        "/api/settings/appearance/presets",
        json={"preset": {"name": "  ", "settings": colors}},
    )
    assert missing_name.status_code == 400
    missing_settings = client.post(
        "/api/settings/appearance/presets",
        json={"preset": {"name": "테스트"}},
    )
    assert missing_settings.status_code == 400
    assert client.delete("/api/settings/appearance/presets/not-an-id").status_code == 400
    assert client.delete(
        "/api/settings/appearance/presets/00000000000000000000000000000000"
    ).status_code == 404


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


def test_metadata_consistency_service_requires_review_for_unresolved_results(
    tmp_path, monkeypatch
):
    import run_platform_catalog

    app, _ = _server_fixture(tmp_path)
    registry = app.extensions["library_service_registry"]
    events = []

    def fake_run(_args, *, progress):
        progress({
            "phase": "identity_start",
            "discovered_titles": 1,
            "selected_titles": 1,
            "selected_platforms": 2,
        })
        return {
            "selected_titles": 1,
            "selected_platforms": 2,
            "outcome_counts": {
                "revalidated": 1,
                "identity_conflict": 1,
                "unavailable": 0,
                "stale_target": 0,
                "error": 0,
                "skipped": 0,
            },
        }

    def progress(current, total, message, *, stage="running", event=None):
        events.append((current, total, message, stage, dict(event or {})))

    monkeypatch.setattr(run_platform_catalog, "run", fake_run)
    result = registry._run_platform(
        "revalidate-metadata-consistency", ("--all",), progress
    )

    assert result["_job_state"] == "needs_review"
    assert result["unresolved_count"] == 1
    assert "잔여 1건" in result["_job_message"]
    assert events[-1][4]["status"] == "needs_review"


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
    assert detail["actions"]["quarantine"] is True

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
    assert quarantine["state"] == "present"
    assert quarantine["total"] == 0
    assert client.get("/api/explorer/compare", query_string={"left": file_id}).status_code == 400


def test_management_relationship_preview_and_apply_routes(tmp_path):
    app, first_id = _server_fixture(tmp_path)
    config = app.config["library_server_config"]
    second_path = config.house_dir / "수동 교정 작품 extra.txt"
    second_path.write_text("서로 다른 extra 본문", encoding="utf-8")
    conn = decision_store.connect_state_db(config.state_db)
    try:
        _ensure_intake_fingerprint(conn, _file_state(conn, first_id))
        with decision_store.transaction(conn):
            second = decision_store.reconcile_file_metadata(conn, second_path, source="house")
        _ensure_intake_fingerprint(conn, _file_state(conn, second["file_id"]))
    finally:
        conn.close()

    payload = {
        "left_file_id": first_id,
        "right_file_id": second["file_id"],
        "verdict": "same_work_distinct_variant",
        "variant_kind": "other",
        "note": "API fixture",
    }
    client = app.test_client()
    preview = client.post("/api/management/relationships/preview", json=payload)
    assert preview.status_code == 200
    plan = preview.get_json()["data"]
    assert plan["apply_available"] is True

    runner = app.extensions["library_job_runner"]
    entered = threading.Event()
    release = threading.Event()

    def blocking_job(_payload, _progress):
        entered.set()
        assert release.wait(5)
        return {"released": True}

    runner.register("fixture_blocking_mutation", blocking_job)
    runner.enqueue("fixture_blocking_mutation", {})
    assert entered.wait(5)

    applied = client.post(
        "/api/management/relationships/apply",
        json={
            **payload,
            "confirm_count": plan["item_count"],
            "confirm_plan_sha256": plan["plan_sha256"],
        },
    )
    assert applied.status_code == 202
    accepted = applied.get_json()["data"]
    assert accepted["state"] == "queued"
    assert accepted["queue_position"] == 2
    job_id = accepted["job_id"]
    release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").get_json()["data"]
        if job["state"] in {"succeeded", "failed", "needs_review"}:
            break
        time.sleep(0.01)
    assert job["state"] == "succeeded"
    assert job["result"]["decision_id"]

    quarantine = client.post(
        "/api/management/quarantine/preview",
        json={"source_file_id": first_id, "keep_file_id": second["file_id"]},
    )
    assert quarantine.status_code == 200
    assert quarantine.get_json()["data"]["apply_available"] is True


def test_queued_relationship_requires_reconfirmation_after_endpoint_changes(tmp_path):
    app, first_id = _server_fixture(tmp_path)
    config = app.config["library_server_config"]
    second_path = config.house_dir / "대기 중 바뀔 extra.txt"
    second_path.write_text("서로 다른 extra 본문", encoding="utf-8")
    conn = decision_store.connect_state_db(config.state_db)
    try:
        _ensure_intake_fingerprint(conn, _file_state(conn, first_id))
        with decision_store.transaction(conn):
            second = decision_store.reconcile_file_metadata(
                conn, second_path, source="house"
            )
        _ensure_intake_fingerprint(conn, _file_state(conn, second["file_id"]))
    finally:
        conn.close()

    payload = {
        "left_file_id": first_id,
        "right_file_id": second["file_id"],
        "verdict": "same_work_distinct_variant",
        "variant_kind": "other",
        "note": "queued stale fixture",
    }
    client = app.test_client()
    plan = client.post(
        "/api/management/relationships/preview", json=payload
    ).get_json()["data"]
    runner = app.extensions["library_job_runner"]
    entered = threading.Event()
    release = threading.Event()

    def blocking_job(_payload, _progress):
        entered.set()
        assert release.wait(5)
        return {}

    runner.register("fixture_stale_blocker", blocking_job)
    runner.enqueue("fixture_stale_blocker", {})
    assert entered.wait(5)
    accepted = client.post(
        "/api/management/relationships/apply",
        json={
            **payload,
            "confirm_count": plan["item_count"],
            "confirm_plan_sha256": plan["plan_sha256"],
        },
    ).get_json()["data"]
    assert accepted["state"] == "queued"

    conn = decision_store.connect_state_db(config.state_db)
    try:
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE files SET active = 0 WHERE file_id = ?",
                (second["file_id"],),
            )
    finally:
        conn.close()
    release.set()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = runner.get(accepted["job_id"])
        if job["state"] in {"needs_review", "failed", "succeeded"}:
            break
        time.sleep(0.01)
    assert job["state"] == "needs_review"
    assert job["error"]["code"] == "reconfirmation_required"

    conn = decision_store.connect_state_db_readonly(config.state_db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE active = 1 AND "
            "((left_file_id = ? AND right_file_id = ?) OR "
            "(left_file_id = ? AND right_file_id = ?))",
            (first_id, second["file_id"], second["file_id"], first_id),
        ).fetchone()[0] == 0
    finally:
        conn.close()


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
    unpack = config.temp_dir / "unpack" / "20260701 완결"
    unpack.mkdir(parents=True)
    (unpack / "unpacked.txt").write_text("book", encoding="utf-8")
    legacy_unpack = config.temp_dir / "___기존 묶음"
    legacy_unpack.mkdir()
    (legacy_unpack / "unpacked.epub").write_text("book", encoding="utf-8")

    dashboard = app.test_client().get("/api/dashboard").get_json()["data"]
    assert dashboard["filesystem"]["folderling_pending"] == 4
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
    maintenance = client.get("/api/services/platform-metadata")
    assert maintenance.status_code == 200
    assert maintenance.get_json()["data"]["quick_action"] is False
    identity = client.get("/api/services/platform-identity")
    assert identity.status_code == 200
    assert "유지보수" in identity.get_json()["data"]["label"]
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


def test_folderling_is_ready_for_actionable_strong_review_without_temp_input(
    tmp_path,
):
    app, _ = _server_fixture(tmp_path)
    config = app.config["library_server_config"]
    short_path = config.house_dir / "대마법사의 귀촌 생활 1-5 완.txt"
    long_path = config.house_dir / "대마법사의 귀촌 생활 001~127 완결.txt"
    short_path.write_text("귀촌 생활 앞부분", encoding="utf-8")
    long_path.write_text("귀촌 생활 앞부분과 뒷부분", encoding="utf-8")

    conn = decision_store.connect_state_db(config.state_db)
    try:
        with decision_store.transaction(conn):
            short = decision_store.reconcile_file_metadata(
                conn, short_path, source="house"
            )
            long = decision_store.reconcile_file_metadata(
                conn, long_path, source="house"
            )
        _ensure_intake_fingerprint(conn, _file_state(conn, short["file_id"]))
        _ensure_intake_fingerprint(conn, _file_state(conn, long["file_id"]))
        decision_store.add_review_item(
            conn,
            candidate_file_id=short["file_id"],
            reference_file_id=long["file_id"],
            classification="ordered_body_match",
        )
    finally:
        conn.close()

    descriptor = app.extensions["library_service_registry"].descriptor(
        "folderling"
    )

    assert descriptor["ready"] is True
    assert descriptor["blocked_code"] is None
    assert descriptor["target_count"] == 1
    assert descriptor["preview"] == {
        "intake_count": 0,
        "actionable_strong_review_count": 1,
        "decided_review_cleanup_count": 0,
    }


def test_google_sheet_service_uses_private_local_config(tmp_path, monkeypatch):
    app, _ = _server_fixture(tmp_path)
    registry = app.extensions["library_service_registry"]
    credentials = tmp_path / "service-account.json"
    credentials.write_text("{}", encoding="utf-8")
    config = tmp_path / "google-sheet.json"
    config.write_text(
        json.dumps(
            {
                "credentials_path": str(credentials),
                "spreadsheet_id": "sheet-id",
            }
        ),
        encoding="utf-8",
    )
    config.chmod(0o600)
    monkeypatch.delenv("FILE_CHECK_GOOGLE_CREDENTIALS", raising=False)
    monkeypatch.delenv("FILE_CHECK_GOOGLE_SPREADSHEET_ID", raising=False)
    monkeypatch.setenv("FILE_CHECK_GOOGLE_CONFIG", str(config))
    monkeypatch.setattr(registry, "_production_layout", lambda: True)

    descriptor = registry.descriptor(
        "google-sheet",
        context={
            "jobs": [],
            "active": None,
            "doctor_ok": True,
            "doctor_issue_count": 0,
            "supported_house_files": 1,
            "platform_previews": {
                "platform-update": (1, {"discovered_titles": 1})
            },
            "platform_error": None,
        },
    )

    assert descriptor["ready"] is True
    assert descriptor["configured"] is True
    assert descriptor["target_count"] == 1
    assert descriptor["blocked_code"] is None


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


def test_volume_review_api_requires_explicit_side_story_override(tmp_path):
    app, _ = _server_fixture(tmp_path)
    config = app.config["library_server_config"]
    conn = decision_store.connect_state_db(config.state_db)
    try:
        for name in ("외전 확인 1권.txt", "외전 확인 외전.epub"):
            path = config.house_dir / "ㅇ" / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(name, encoding="utf-8")
            with decision_store.transaction(conn):
                decision_store.reconcile_file_metadata(conn, path, source="house")
    finally:
        conn.close()

    client = app.test_client()
    listing = client.get(
        "/api/review/volumes?classification=review_required"
    ).get_json()["data"]
    [case] = listing["items"]
    payload = {
        "case_id": case["case_id"],
        "source_revision": case["source_revision"],
    }
    blocked = client.post(
        "/api/review/volumes/preview", json=payload
    ).get_json()["data"]
    assert blocked["apply_available"] is False
    assert "side_story_requires_two_main_coordinates" in blocked["blocked_reasons"]

    approved = client.post(
        "/api/review/volumes/preview",
        json={
            **payload,
            "allow_side_story_without_two_main_coordinates": True,
        },
    ).get_json()["data"]
    assert approved["apply_available"] is True
    assert approved["allow_side_story_without_two_main_coordinates"] is True


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


def _interrupted_folderling_fixture(
    tmp_path, *, with_operation=False, with_event=True, mutation_started=False
):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    frontend = tmp_path / "dist"
    runtime = tmp_path / "runtime"
    house.mkdir()
    temp.mkdir()
    frontend.mkdir()
    (frontend / "index.html").write_text("fixture", encoding="utf-8")
    source = house / "interrupt fixture.txt"
    source.write_text("stable bytes", encoding="utf-8")
    conn = decision_store.initialize_state_db(state_db)
    with decision_store.transaction(conn):
        decision_store.reconcile_file_metadata(conn, source, source="house")
    backup = decision_store.backup_state_db(
        conn, state_db.parent / "before_interrupted_folderling.sqlite3"
    )
    run_id = decision_store.issue_actual_run_token(
        conn, str(backup), house_dir=house, temp_dir=temp
    )
    conn.close()
    activated_run_id, _ = decision_store.prepare_actual_run(
        state_db, house, temp
    )
    assert activated_run_id == run_id
    if with_operation:
        conn = decision_store.connect_state_db(state_db)
        with decision_store.transaction(conn):
            decision_store.create_operation_group(
                conn,
                run_id=run_id,
                action="fixture_interrupted_group",
                plan_sha256="fixture-plan",
                item_count=1,
            )
        conn.close()
    if mutation_started:
        conn = decision_store.connect_state_db(state_db)
        with decision_store.transaction(conn):
            decision_store.mark_actual_run_mutation_started(conn, run_id)
        conn.close()

    store = JobStore(runtime)
    job = store.create("service_folderling", {"source": "fixture"})
    store.update(
        job["job_id"], state="running", stage="running",
        started_at="2026-08-13T00:00:00+00:00",
    )
    if with_event:
        store.append_event(job["job_id"], {
            "phase": "actual_run_started",
            "status": "running",
            "run_id": run_id,
        })
    return state_db, house, temp, frontend, runtime, job["job_id"], run_id


def test_server_restart_recovers_unique_orphan_active_run_without_job(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    frontend = tmp_path / "dist"
    runtime = tmp_path / "runtime"
    house.mkdir()
    temp.mkdir()
    frontend.mkdir()
    (frontend / "index.html").write_text("fixture", encoding="utf-8")
    source = house / "orphan active fixture.txt"
    source.write_text("stable bytes", encoding="utf-8")
    conn = decision_store.initialize_state_db(state_db)
    with decision_store.transaction(conn):
        decision_store.reconcile_file_metadata(conn, source, source="house")
    backup = decision_store.backup_state_db(
        conn, state_db.parent / "before-orphan-active.sqlite3"
    )
    run_id = decision_store.issue_actual_run_token(
        conn, str(backup), house_dir=house, temp_dir=temp
    )
    conn.close()
    activated_run_id, _manifest = decision_store.prepare_actual_run(
        state_db, house, temp
    )
    assert activated_run_id == run_id

    app = create_app(
        state_db=state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=tmp_path / "file_index.json",
        runtime_dir=runtime,
        frontend_dist=frontend,
        project_root=tmp_path,
    )
    assert app.extensions["library_job_runner"].store.list() == []
    conn = decision_store.connect_state_db(state_db)
    try:
        assert conn.execute(
            "SELECT state FROM actual_runs WHERE run_id = ?", (run_id,)
        ).fetchone()["state"] == "failed"
        assert not decision_store.doctor_issues(conn)
    finally:
        conn.close()


def test_server_restart_closes_folderling_run_before_first_operation(tmp_path):
    state_db, house, temp, frontend, runtime, job_id, run_id = (
        _interrupted_folderling_fixture(tmp_path)
    )

    app = create_app(
        state_db=state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=tmp_path / "file_index.json",
        runtime_dir=runtime,
        frontend_dist=frontend,
        project_root=tmp_path,
    )
    store = app.extensions["library_job_runner"].store
    restored = store.get(job_id)
    events = store.events(job_id)
    assert restored["state"] == "interrupted"
    assert restored["error"]["code"] == "server_restarted_before_mutation"
    assert "다시 실행" in restored["error"]["message"]
    assert events[-1]["phase"] == "interrupted_run_recovered"
    assert events[-1]["run_id"] == run_id

    conn = decision_store.connect_state_db(state_db)
    assert conn.execute(
        "SELECT state FROM actual_runs WHERE run_id = ?", (run_id,)
    ).fetchone()["state"] == "failed"
    assert not decision_store.doctor_issues(conn)
    conn.close()


def test_server_restart_does_not_guess_job_binding_for_unrelated_orphan_run(tmp_path):
    state_db, house, temp, frontend, runtime, job_id, run_id = (
        _interrupted_folderling_fixture(tmp_path, with_event=False)
    )
    store = JobStore(runtime)
    store.create("service_folderling", {"source": "queued fixture"})

    app = create_app(
        state_db=state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=tmp_path / "file_index.json",
        runtime_dir=runtime,
        frontend_dist=frontend,
        project_root=tmp_path,
    )
    restored = app.extensions["library_job_runner"].store.get(job_id)
    assert restored.get("actual_run_id") is None
    assert restored["state"] == "interrupted"
    assert restored["error"]["code"] == "server_restarted"
    conn = decision_store.connect_state_db(state_db)
    try:
        assert conn.execute(
            "SELECT state FROM actual_runs WHERE run_id = ?", (run_id,)
        ).fetchone()["state"] == "failed"
    finally:
        conn.close()


@pytest.mark.parametrize("manifest_text", [None, '{\"partial\":', '{\"complete\": true}'])
def test_server_restart_cancels_claimed_approved_run_and_owned_orphan_manifest(
    tmp_path, manifest_text
):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    frontend = tmp_path / "dist"
    runtime = tmp_path / "runtime"
    house.mkdir()
    temp.mkdir()
    frontend.mkdir()
    (frontend / "index.html").write_text("fixture", encoding="utf-8")
    conn = decision_store.initialize_state_db(state_db)
    backup = decision_store.backup_state_db(
        conn, state_db.parent / "before-claimed-interruption.sqlite3"
    )
    run_id = decision_store.issue_actual_run_token(
        conn, str(backup), house_dir=house, temp_dir=temp
    )
    claim_id = "11111111-1111-4111-8111-111111111111"
    with decision_store.transaction(conn):
        conn.execute(
            "UPDATE actual_runs SET activation_claim = ? WHERE run_id = ?",
            (claim_id, run_id),
        )
        conn.execute(
            "UPDATE settings SET value = '0', updated_at = CURRENT_TIMESTAMP "
            "WHERE key = 'actual_mutation_enabled'"
        )
        conn.execute("DELETE FROM settings WHERE key = 'approved_run_id'")
    conn.close()
    manifest = state_db.parent / "manifests" / f"{run_id}-{claim_id}.json"
    if manifest_text is not None:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        with manifest.open("w", encoding="utf-8") as stream:
            stream.write(manifest_text)
            stream.flush()
            os.fsync(stream.fileno())

    store = JobStore(runtime)
    job = store.create("service_folderling", {})
    store.update(
        job["job_id"], state="running", stage="preflight_result",
        started_at="2026-08-15T00:00:00+00:00",
    )
    store.append_event(job["job_id"], {
        "phase": "preflight_result",
        "status": "succeeded",
        "approved_run_id": run_id,
    })

    app = create_app(
        state_db=state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=tmp_path / "file_index.json",
        runtime_dir=runtime,
        frontend_dist=frontend,
        project_root=tmp_path,
    )
    restored = app.extensions["library_job_runner"].store.get(job["job_id"])
    assert restored["recovery_complete"] is True
    assert restored["error"]["code"] == "server_restarted_before_activation"
    assert not manifest.exists()

    conn = decision_store.connect_state_db(state_db)
    assert conn.execute(
        "SELECT state FROM actual_runs WHERE run_id = ?", (run_id,)
    ).fetchone()["state"] == "cancelled"
    assert not decision_store.doctor_issues(conn)
    conn.close()


def _activation_fault_fixture(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    frontend = tmp_path / "dist"
    runtime = tmp_path / "runtime"
    house.mkdir()
    temp.mkdir()
    frontend.mkdir()
    (frontend / "index.html").write_text("fixture", encoding="utf-8")
    source = house / "activation fault fixture.txt"
    source.write_text("stable bytes", encoding="utf-8")
    conn = decision_store.initialize_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            decision_store.reconcile_file_metadata(conn, source, source="house")
        backup = decision_store.backup_state_db(
            conn, state_db.parent / "before-activation-fault.sqlite3"
        )
        run_id = decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()
    return state_db, house, temp, frontend, runtime, source, run_id


def _close_test_server(app):
    app.extensions["library_job_runner"].shutdown()
    keeper = app.extensions.get("library_state_db_readonly_keeper")
    if keeper is not None:
        keeper.close()


@pytest.mark.parametrize(
    "phase",
    (
        "claim_committed",
        "manifest_opened",
        "manifest_partial",
        "manifest_fsynced",
        "active_committed",
    ),
)
def test_actual_run_activation_sigkill_fault_converges_on_restart(tmp_path, phase):
    state_db, house, temp, frontend, runtime, source, run_id = (
        _activation_fault_fixture(tmp_path)
    )
    backend_dir = Path(__file__).resolve().parents[1] / "backend"
    child_code = f"""
import os
import signal
import decision_store

def failpoint(current):
    if current == {phase!r}:
        os.kill(os.getpid(), signal.SIGKILL)

decision_store.prepare_actual_run(
    {str(state_db)!r},
    {str(house)!r},
    {str(temp)!r},
    _test_failpoint=failpoint,
)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(backend_dir)
    child = subprocess.run(
        [sys.executable, "-c", child_code],
        env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
        check=False,
    )
    assert child.returncode == -signal.SIGKILL
    assert source.read_text(encoding="utf-8") == "stable bytes"

    app = create_app(
        state_db=state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=tmp_path / "file_index.json",
        runtime_dir=runtime,
        frontend_dist=frontend,
        project_root=tmp_path,
    )
    _close_test_server(app)
    conn = decision_store.connect_state_db(state_db)
    try:
        state = conn.execute(
            "SELECT state FROM actual_runs WHERE run_id = ?", (run_id,)
        ).fetchone()["state"]
        assert state == ("failed" if phase == "active_committed" else "cancelled")
        assert not decision_store.doctor_issues(conn)
    finally:
        conn.close()
    manifests = state_db.parent / "manifests"
    manifest_files = list(manifests.iterdir()) if manifests.exists() else []
    if phase == "active_committed":
        # Once the active transaction commits, the manifest is registered run
        # evidence and must be preserved with the failed terminal record.
        assert len(manifest_files) == 1
    else:
        assert manifest_files == []

    # A second restart must be idempotent and must not resurrect the run.
    second = create_app(
        state_db=state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=tmp_path / "file_index.json",
        runtime_dir=runtime,
        frontend_dist=frontend,
        project_root=tmp_path,
    )
    _close_test_server(second)
    conn = decision_store.connect_state_db(state_db)
    try:
        assert conn.execute(
            "SELECT state FROM actual_runs WHERE run_id = ?", (run_id,)
        ).fetchone()["state"] == state
        assert not decision_store.doctor_issues(conn)
    finally:
        conn.close()


def test_real_process_lock_defers_then_retries_orphan_run_cleanup(tmp_path):
    state_db, house, temp, frontend, runtime, _source, run_id = (
        _activation_fault_fixture(tmp_path)
    )
    backend_dir = Path(__file__).resolve().parents[1] / "backend"
    child_code = f"""
import time
from mutation_io import mutation_lock_for_roots
with mutation_lock_for_roots({str(house)!r}, {str(temp)!r}, "fixture-holder"):
    print("LOCKED", flush=True)
    time.sleep(60)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(backend_dir)
    holder = subprocess.Popen(
        [sys.executable, "-c", child_code],
        env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "LOCKED"
        first = create_app(
            state_db=state_db,
            house_dir=house,
            temp_dir=temp,
            index_path=tmp_path / "file_index.json",
            runtime_dir=runtime,
            frontend_dist=frontend,
            project_root=tmp_path,
        )
        _close_test_server(first)
        conn = decision_store.connect_state_db(state_db)
        try:
            assert conn.execute(
                "SELECT state FROM actual_runs WHERE run_id = ?", (run_id,)
            ).fetchone()["state"] == "approved"
        finally:
            conn.close()
    finally:
        holder.terminate()
        holder.wait(timeout=5)

    second = create_app(
        state_db=state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=tmp_path / "file_index.json",
        runtime_dir=runtime,
        frontend_dist=frontend,
        project_root=tmp_path,
    )
    _close_test_server(second)
    conn = decision_store.connect_state_db(state_db)
    try:
        assert conn.execute(
            "SELECT state FROM actual_runs WHERE run_id = ?", (run_id,)
        ).fetchone()["state"] == "cancelled"
        assert not decision_store.doctor_issues(conn)
    finally:
        conn.close()

    third = create_app(
        state_db=state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=tmp_path / "file_index.json",
        runtime_dir=runtime,
        frontend_dist=frontend,
        project_root=tmp_path,
    )
    _close_test_server(third)
    conn = decision_store.connect_state_db(state_db)
    try:
        assert conn.execute(
            "SELECT state FROM actual_runs WHERE run_id = ?", (run_id,)
        ).fetchone()["state"] == "cancelled"
    finally:
        conn.close()


def test_prepare_actual_run_fsyncs_manifest_file_and_parent_directory(
    tmp_path, monkeypatch
):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    house.mkdir()
    temp.mkdir()
    source = house / "manifest durability fixture.txt"
    source.write_text("stable bytes", encoding="utf-8")
    conn = decision_store.initialize_state_db(state_db)
    try:
        with decision_store.transaction(conn):
            decision_store.reconcile_file_metadata(conn, source, source="house")
        backup = decision_store.backup_state_db(
            conn, state_db.parent / "before-manifest-durability.sqlite3"
        )
        decision_store.issue_actual_run_token(
            conn, str(backup), house_dir=house, temp_dir=temp
        )
    finally:
        conn.close()

    observed = []
    real_fsync = decision_store.os.fsync

    def traced_fsync(fd):
        mode = os.fstat(fd).st_mode
        observed.append("directory" if stat.S_ISDIR(mode) else "file")
        return real_fsync(fd)

    monkeypatch.setattr(decision_store.os, "fsync", traced_fsync)
    run_id, manifest_path = decision_store.prepare_actual_run(
        state_db, house, temp
    )
    assert run_id.startswith("actual-")
    assert Path(manifest_path).is_file()
    assert "file" in observed
    assert "directory" in observed


def test_server_restart_rejects_noncanonical_activation_claim_without_unlink(
    tmp_path,
):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    frontend = tmp_path / "dist"
    runtime = tmp_path / "runtime"
    house.mkdir()
    temp.mkdir()
    frontend.mkdir()
    (frontend / "index.html").write_text("fixture", encoding="utf-8")
    outside = tmp_path / "must-not-delete.json"
    outside.write_text("owned by fixture", encoding="utf-8")
    conn = decision_store.initialize_state_db(state_db)
    backup = decision_store.backup_state_db(
        conn, state_db.parent / "before-bad-claim.sqlite3"
    )
    run_id = decision_store.issue_actual_run_token(
        conn, str(backup), house_dir=house, temp_dir=temp
    )
    with decision_store.transaction(conn):
        conn.execute(
            "UPDATE actual_runs SET activation_claim = '../../must-not-delete' "
            "WHERE run_id = ?",
            (run_id,),
        )
        conn.execute(
            "UPDATE settings SET value = '0' WHERE key = 'actual_mutation_enabled'"
        )
        conn.execute("DELETE FROM settings WHERE key = 'approved_run_id'")
    conn.close()

    store = JobStore(runtime)
    job = store.create("service_folderling", {})
    store.update(
        job["job_id"], state="running", stage="preflight_result",
        started_at="2026-08-15T00:00:00+00:00",
    )
    store.append_event(job["job_id"], {
        "phase": "preflight_result", "status": "succeeded",
        "approved_run_id": run_id,
    })

    app = create_app(
        state_db=state_db, house_dir=house, temp_dir=temp,
        index_path=tmp_path / "file_index.json", runtime_dir=runtime,
        frontend_dist=frontend, project_root=tmp_path,
    )
    restored_store = app.extensions["library_job_runner"].store
    restored = restored_store.get(job["job_id"])
    assert restored.get("recovery_complete") is not True
    assert restored_store.events(job["job_id"])[-1]["phase"] == (
        "interrupted_run_recovery_required"
    )
    assert outside.read_text(encoding="utf-8") == "owned by fixture"
    conn = decision_store.connect_state_db(state_db)
    try:
        assert conn.execute(
            "SELECT state FROM actual_runs WHERE run_id = ?", (run_id,)
        ).fetchone()["state"] == "approved"
    finally:
        conn.close()


def test_server_restart_does_not_reconcile_duplicate_job_bindings(tmp_path):
    state_db, house, temp, frontend, runtime, job_id, run_id = (
        _interrupted_folderling_fixture(tmp_path)
    )
    store = JobStore(runtime)
    duplicate = store.create("service_folderling", {})
    store.update(
        duplicate["job_id"], state="running", stage="running",
        started_at="2026-08-15T00:01:00+00:00", actual_run_id=run_id,
    )

    app = create_app(
        state_db=state_db, house_dir=house, temp_dir=temp,
        index_path=tmp_path / "file_index.json", runtime_dir=runtime,
        frontend_dist=frontend, project_root=tmp_path,
    )
    restored_store = app.extensions["library_job_runner"].store
    for current_job_id in (job_id, duplicate["job_id"]):
        record = restored_store.get(current_job_id)
        assert record["state"] == "interrupted"
        assert record.get("recovery_complete") is not True
        assert any(
            event.get("error_code") == "ambiguous_job_binding"
            for event in restored_store.events(current_job_id)
        )
    conn = decision_store.connect_state_db(state_db)
    try:
        # The run itself is independent of ambiguous job history and can still
        # converge through the orphan-run safety proof.
        assert conn.execute(
            "SELECT state FROM actual_runs WHERE run_id = ?", (run_id,)
        ).fetchone()["state"] == "failed"
    finally:
        conn.close()


def test_server_restart_retries_previously_interrupted_folderling_recovery(
    tmp_path, monkeypatch
):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    frontend = tmp_path / "dist"
    runtime = tmp_path / "runtime"
    house.mkdir()
    temp.mkdir()
    frontend.mkdir()
    (frontend / "index.html").write_text("fixture", encoding="utf-8")
    conn = decision_store.initialize_state_db(state_db)
    backup = decision_store.backup_state_db(
        conn, state_db.parent / "before-retry-interruption.sqlite3"
    )
    run_id = decision_store.issue_actual_run_token(
        conn, str(backup), house_dir=house, temp_dir=temp
    )
    conn.close()
    store = JobStore(runtime)
    job = store.create("service_folderling", {})
    store.update(
        job["job_id"], state="running", stage="preflight_result",
        started_at="2026-08-15T00:00:00+00:00",
    )
    store.append_event(job["job_id"], {
        "phase": "preflight_result", "status": "succeeded",
        "approved_run_id": run_id,
    })

    class BusyLock:
        def __enter__(self):
            raise RuntimeError("fixture root lock busy")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        library_server, "mutation_lock_for_roots", lambda *_args, **_kwargs: BusyLock()
    )
    first = create_app(
        state_db=state_db, house_dir=house, temp_dir=temp,
        index_path=tmp_path / "file_index.json", runtime_dir=runtime,
        frontend_dist=frontend, project_root=tmp_path,
    )
    first_store = first.extensions["library_job_runner"].store
    assert first_store.get(job["job_id"])["state"] == "interrupted"
    assert first_store.get(job["job_id"]).get("recovery_complete") is not True
    assert first_store.events(job["job_id"])[-1]["phase"] == (
        "interrupted_run_recovery_required"
    )
    first.extensions["library_job_runner"].shutdown()
    keeper = first.extensions.get("library_state_db_readonly_keeper")
    if keeper is not None:
        keeper.close()

    from mutation_io import mutation_lock_for_roots as real_lock
    monkeypatch.setattr(library_server, "mutation_lock_for_roots", real_lock)
    second = create_app(
        state_db=state_db, house_dir=house, temp_dir=temp,
        index_path=tmp_path / "file_index.json", runtime_dir=runtime,
        frontend_dist=frontend, project_root=tmp_path,
    )
    second_store = second.extensions["library_job_runner"].store
    restored = second_store.get(job["job_id"])
    assert restored["recovery_complete"] is True
    assert second_store.events(job["job_id"])[-1]["phase"] == "interrupted_run_recovered"
    conn = decision_store.connect_state_db(state_db)
    assert conn.execute(
        "SELECT state FROM actual_runs WHERE run_id = ?", (run_id,)
    ).fetchone()["state"] == "cancelled"
    conn.close()


def test_server_restart_cancels_bound_approved_run_before_activation(tmp_path):
    state_db = tmp_path / ".dedup_state" / "dedup_decisions.sqlite3"
    house = tmp_path / "house"
    temp = tmp_path / "temp"
    frontend = tmp_path / "dist"
    runtime = tmp_path / "runtime"
    house.mkdir()
    temp.mkdir()
    frontend.mkdir()
    (frontend / "index.html").write_text("fixture", encoding="utf-8")
    conn = decision_store.initialize_state_db(state_db)
    backup = decision_store.backup_state_db(
        conn, state_db.parent / "before-approved-interruption.sqlite3"
    )
    run_id = decision_store.issue_actual_run_token(
        conn, str(backup), house_dir=house, temp_dir=temp
    )
    conn.close()

    store = JobStore(runtime)
    job = store.create("service_folderling", {})
    store.update(
        job["job_id"], state="running", stage="preflight_result",
        started_at="2026-08-13T00:00:00+00:00",
    )
    store.append_event(job["job_id"], {
        "phase": "preflight_result",
        "status": "succeeded",
        "approved_run_id": run_id,
    })
    assert store.get(job["job_id"])["actual_run_id"] == run_id

    app = create_app(
        state_db=state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=tmp_path / "file_index.json",
        runtime_dir=runtime,
        frontend_dist=frontend,
        project_root=tmp_path,
    )
    restored = app.extensions["library_job_runner"].store.get(job["job_id"])
    assert restored["error"]["code"] == "server_restarted_before_activation"

    conn = decision_store.connect_state_db(state_db)
    assert conn.execute(
        "SELECT state FROM actual_runs WHERE run_id = ?", (run_id,)
    ).fetchone()["state"] == "cancelled"
    assert conn.execute(
        "SELECT value FROM settings WHERE key = 'actual_mutation_enabled'"
    ).fetchone()["value"] == "0"
    assert conn.execute(
        "SELECT 1 FROM settings WHERE key = 'approved_run_id'"
    ).fetchone() is None
    assert not decision_store.doctor_issues(conn)
    conn.close()


@pytest.mark.parametrize(
    ("actual_state", "summary_status", "expected_job_state"),
    [
        ("finished", "succeeded", "succeeded"),
        ("finished", "needs_review", "needs_review"),
        ("finished", None, "needs_review"),
        ("failed", "needs_review", "needs_review"),
        ("failed", None, "failed"),
    ],
)
def test_server_restart_reconciles_terminal_actual_run_job(
    tmp_path, actual_state, summary_status, expected_job_state
):
    state_db, house, temp, frontend, runtime, job_id, run_id = (
        _interrupted_folderling_fixture(tmp_path)
    )
    conn = decision_store.connect_state_db(state_db)
    decision_store.finish_actual_run(
        conn,
        run_id,
        success=actual_state == "finished",
        error=("fixture terminal failure" if actual_state == "failed" else None),
    )
    conn.close()
    store = JobStore(runtime)
    if summary_status is not None:
        store.append_event(job_id, {
            "phase": "folderling_summary",
            "status": summary_status,
            "failure_count": int(actual_state == "failed"),
            "review_required_count": int(summary_status == "needs_review"),
        })

    app = create_app(
        state_db=state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=tmp_path / "file_index.json",
        runtime_dir=runtime,
        frontend_dist=frontend,
        project_root=tmp_path,
    )
    restored_store = app.extensions["library_job_runner"].store
    restored = restored_store.get(job_id)
    assert restored["state"] == expected_job_state
    assert restored_store.events(job_id)[-1]["phase"] == (
        "interrupted_run_terminal_reconciled"
    )
    assert restored_store.events(job_id)[-1]["actual_run_state"] == actual_state
    if actual_state == "failed" and summary_status is None:
        assert restored["error"]["message"] == "fixture terminal failure"
    else:
        assert restored["error"] is None


def test_server_restart_keeps_zero_operation_run_after_mutation_phase(tmp_path):
    state_db, house, temp, frontend, runtime, job_id, run_id = (
        _interrupted_folderling_fixture(tmp_path, mutation_started=True)
    )

    app = create_app(
        state_db=state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=tmp_path / "file_index.json",
        runtime_dir=runtime,
        frontend_dist=frontend,
        project_root=tmp_path,
    )
    store = app.extensions["library_job_runner"].store
    restored = store.get(job_id)
    assert restored["error"]["code"] == "server_restarted"
    assert store.events(job_id)[-1]["phase"] == "interrupted_run_recovery_required"
    assert "mutation phase started" in store.events(job_id)[-1]["error_message"]

    conn = decision_store.connect_state_db(state_db)
    assert conn.execute(
        "SELECT state FROM actual_runs WHERE run_id = ?", (run_id,)
    ).fetchone()["state"] == "active"
    decision_store.disable_actual_run(conn)
    conn.close()


def test_job_store_interrupt_scan_and_legacy_event_lookup_are_unbounded(tmp_path):
    store = JobStore(tmp_path / "runtime")
    old = store.create("service_folderling", {})
    store.update(old["job_id"], state="running", stage="running")
    store.append_event(old["job_id"], {
        "phase": "actual_run_started",
        "status": "running",
        "run_id": "legacy-run-id",
    })
    store.update(old["job_id"], actual_run_id=None)
    for index in range(501):
        store.append_event(old["job_id"], {"phase": "progress", "index": index})
    for _ in range(201):
        recent = store.create("completed", {})
        store.update(recent["job_id"], state="succeeded")

    interrupted = store.mark_interrupted_records()

    assert [record["job_id"] for record in interrupted] == [old["job_id"]]
    assert _interrupted_folderling_run_id(store, store.get(old["job_id"])) == (
        "legacy-run-id"
    )


def test_exclusive_start_sees_active_job_beyond_display_limit(tmp_path):
    runner = JobRunner(JobStore(tmp_path / "runtime"))
    runner.register("synthetic", lambda _payload, _progress: {})
    active = runner.store.create("synthetic", {"old": True})
    runner.store.update(active["job_id"], state="running", stage="running")
    for index in range(201):
        recent = runner.store.create("synthetic", {"recent": index})
        runner.store.update(recent["job_id"], state="succeeded")

    with pytest.raises(JobActiveError) as exc_info:
        runner.start_exclusive("synthetic", {"new": True})

    assert exc_info.value.job_id == active["job_id"]
    runner.shutdown()


def test_job_list_scans_active_records_once_for_multiple_queued_jobs(
    tmp_path, monkeypatch
):
    runner = JobRunner(JobStore(tmp_path / "runtime"))
    for index in range(3):
        runner.store.create("synthetic", {"queued": index})
    calls = 0
    original = runner.store.active_records

    def counted_active_records():
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(runner.store, "active_records", counted_active_records)
    listing = runner.list(limit=3)

    assert len(listing) == 3
    assert calls == 1
    runner.shutdown()


def test_server_restart_keeps_journaled_folderling_run_for_manual_recovery(tmp_path):
    state_db, house, temp, frontend, runtime, job_id, run_id = (
        _interrupted_folderling_fixture(tmp_path, with_operation=True)
    )

    app = create_app(
        state_db=state_db,
        house_dir=house,
        temp_dir=temp,
        index_path=tmp_path / "file_index.json",
        runtime_dir=runtime,
        frontend_dist=frontend,
        project_root=tmp_path,
    )
    store = app.extensions["library_job_runner"].store
    restored = store.get(job_id)
    events = store.events(job_id)
    assert restored["state"] == "interrupted"
    assert restored["error"]["code"] == "server_restarted"
    assert events[-1]["phase"] == "interrupted_run_recovery_required"
    assert "operations=0, groups=1" in events[-1]["error_message"]

    conn = decision_store.connect_state_db(state_db)
    assert conn.execute(
        "SELECT state FROM actual_runs WHERE run_id = ?", (run_id,)
    ).fetchone()["state"] == "active"
    decision_store.disable_actual_run(conn)
    conn.close()


def test_job_cancel_api_only_cancels_waiting_record(tmp_path):
    app, _ = _server_fixture(tmp_path)
    runner = app.extensions["library_job_runner"]
    waiting = runner.store.create("synthetic", {})

    response = app.test_client().post(f"/api/jobs/{waiting['job_id']}/cancel")

    assert response.status_code == 200
    assert response.get_json()["data"]["state"] == "cancelled"
    assert runner.store.events(waiting["job_id"])[-1]["phase"] == "job_cancelled"


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


def test_job_runner_queues_mutations_in_order_and_cancels_waiting_job(tmp_path):
    runner = JobRunner(JobStore(tmp_path / "runtime"))
    entered = threading.Event()
    release = threading.Event()
    order = []
    try:
        def handle(payload, _progress):
            order.append(f"start:{payload['value']}")
            if payload["value"] == 1:
                entered.set()
                assert release.wait(5)
            order.append(f"finish:{payload['value']}")
            return {"value": payload["value"]}

        runner.register("mutation", handle)
        first = runner.enqueue("mutation", {"value": 1})
        assert entered.wait(5)
        with pytest.raises(RuntimeError, match="실행을 시작한 작업"):
            runner.cancel(first["job_id"])
        second = runner.enqueue("mutation", {"value": 2})
        duplicate = runner.enqueue("mutation", {"value": 2})
        third = runner.enqueue("mutation", {"value": 3})

        assert duplicate["job_id"] == second["job_id"]
        assert runner.get(second["job_id"])["queue_position"] == 2
        assert runner.get(second["job_id"])["jobs_ahead"] == 1
        assert runner.get(third["job_id"])["queue_position"] == 3
        cancelled = runner.cancel(third["job_id"])
        assert cancelled["state"] == "cancelled"

        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if runner.get(second["job_id"])["state"] == "succeeded":
                break
            time.sleep(0.01)
        assert runner.get(first["job_id"])["state"] == "succeeded"
        assert runner.get(second["job_id"])["state"] == "succeeded"
        assert runner.get(third["job_id"])["state"] == "cancelled"
        assert order == ["start:1", "finish:1", "start:2", "finish:2"]
    finally:
        release.set()
        runner.shutdown()


def test_job_runner_records_queue_time_plan_drift_as_needs_review(tmp_path):
    runner = JobRunner(JobStore(tmp_path / "runtime"))
    try:
        def needs_review(_payload, _progress):
            raise JobNeedsReview(
                "계획 재확인이 필요합니다.", cause="fixture plan changed"
            )

        runner.register("mutation", needs_review)
        record = runner.enqueue("mutation", {})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = runner.get(record["job_id"])
            if job["state"] == "needs_review":
                break
            time.sleep(0.01)
        assert job["error"]["code"] == "reconfirmation_required"
        event = runner.store.events(record["job_id"])[-1]
        assert event["phase"] == "job_needs_review"
        assert event["cause"] == "fixture plan changed"
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


def test_api_rejects_non_loopback_host_and_cross_origin(tmp_path):
    app, _ = _server_fixture(tmp_path)
    client = app.test_client()

    hostile_host = client.get(
        "/api/dashboard", headers={"Host": "library.example"}
    )
    assert hostile_host.status_code == 403
    assert hostile_host.get_json()["error"]["code"] == "local_access_required"

    hostile_origin = client.get(
        "/api/dashboard",
        headers={"Host": "localhost", "Origin": "https://attacker.example"},
    )
    assert hostile_origin.status_code == 403
    assert hostile_origin.get_json()["error"]["code"] == "origin_rejected"

    allowed = client.get(
        "/api/dashboard",
        headers={"Host": "localhost", "Origin": "http://localhost"},
    )
    assert allowed.status_code == 200
