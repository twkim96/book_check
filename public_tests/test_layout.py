import os
from pathlib import Path
import subprocess
import sys

import project_paths


ROOT = Path(__file__).resolve().parents[1]


def test_backend_keeps_mutable_runtime_at_project_root():
    assert project_paths.PROJECT_ROOT == ROOT
    assert project_paths.STATE_DB == ROOT / ".dedup_state" / "dedup_decisions.sqlite3"
    assert project_paths.FILE_LIST == ROOT / "file_list.json"
    assert project_paths.FILE_INDEX == ROOT / "file_index.json"


def test_machine_paths_are_environment_overridable():
    default_house = Path.home() / "Documents" / "txt_house"
    default_temp = Path.home() / "Documents" / "txt_temp"
    assert Path(os.environ.get("FILE_CHECK_HOUSE_DIR", default_house)).resolve() == project_paths.HOUSE_DIR
    assert Path(os.environ.get("FILE_CHECK_TEMP_DIR", default_temp)).resolve() == project_paths.TEMP_DIR


def test_control_server_entry_point_remains_at_root():
    completed = subprocess.run(
        [sys.executable, str(ROOT / "run_folderling_one_button.py"), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert "--state-db" in completed.stdout


def test_platform_catalog_control_entry_point_is_available_at_root():
    completed = subprocess.run(
        [sys.executable, str(ROOT / "run_platform_catalog.py"), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert "refresh" in completed.stdout
    assert "status" in completed.stdout


def test_title_cleanup_candidate_audit_entry_point_is_available_at_root():
    completed = subprocess.run(
        [sys.executable, str(ROOT / "run_title_cleanup_candidates.py"), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert "--state-db" in completed.stdout
    assert "--json-out" in completed.stdout


def test_title_cleanup_apply_entry_point_is_dry_run_first():
    completed = subprocess.run(
        [sys.executable, str(ROOT / "run_title_cleanup_apply.py"), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert "--manifest-out" in completed.stdout
    assert "--confirm-count" in completed.stdout
    assert "--confirm-plan-sha256" in completed.stdout


def test_library_server_entry_point_is_available_at_root():
    completed = subprocess.run(
        [sys.executable, str(ROOT / "run_library_server.py"), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert "--host" in completed.stdout
    assert "--port" in completed.stdout
    assert "--frontend-dist" in completed.stdout


def test_public_source_has_no_literal_user_home_path():
    user_home_prefix = "/" + "Users/"
    paths = [*ROOT.glob("*.py"), *ROOT.glob("backend/*.py")]
    for path in paths:
        if path.is_file():
            assert user_home_prefix not in path.read_text(encoding="utf-8"), path
