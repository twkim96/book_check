"""Safe file/folder organization and folder quarantine workflows."""

from __future__ import annotations

import hashlib
import ctypes
import errno
import json
import os
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping

import decision_store
from library_management import _file_row, _public_file, _require_current_file
from mutation_io import (
    ensure_directory_nofollow,
    evidence_matches,
    inspect_regular_file,
    mutation_lock_for_roots,
)
from normalizer import SUPPORTED_EXTENSIONS, should_exclude_dir


ACTION = "library_file_relocate"
MANAGED_FOLDER_CREATE_ACTION = "managed_folder_create"
MANAGED_FOLDER_ADOPT_ACTION = "managed_folder_adopt"
MANAGED_FOLDER_RELOCATE_ACTION = "managed_folder_relocate"
MANAGED_FOLDER_ITEM_ACTION = "managed_folder_relocate_item"
FOLDER_QUARANTINE_ACTION = "user_folder_quarantine"
FOLDER_QUARANTINE_ITEM_ACTION = "user_quarantine"
FOLDER_ROLES = frozenset({"primary", "edition", "auxiliary"})
MAX_FOLDER_ITEMS = 5_000
ANALYSIS_FIELDS = (
    "core_title", "readable_title", "catalog_query_title", "title_override_json",
    "author", "max_number", "effective_max", "unit", "complete", "disambig",
)
COORDINATE_FIELDS = (
    "coordinate_kind", "part_num", "part_den", "volume_num", "volume_den",
    "coordinate_symbol", "coordinate_sort_key", "episode_start", "episode_end",
    "coordinate_raw", "span_ambiguous",
)


def _hash(payload: Mapping[str, object]) -> str:
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _backup_path(state_db: Path, label: str = "library_file_relocate") -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return (
        Path(state_db).resolve().parent / "backups" /
        f"before_{label}_{stamp}_{uuid.uuid4().hex[:8]}.sqlite3"
    )


def _safe_name(value: str, source_suffix: str) -> str:
    name = unicodedata.normalize("NFC", str(value or "").strip())
    if not name or name in {".", ".."} or "\x00" in name or Path(name).name != name:
        raise ValueError("파일명에는 하나의 안전한 이름만 입력할 수 있습니다")
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS or suffix != source_suffix.lower():
        raise ValueError("파일 확장자는 기존 TXT·EPUB·PDF 형식을 유지해야 합니다")
    return name


def _safe_directory_name(value: str) -> str:
    name = unicodedata.normalize("NFC", str(value or "").strip())
    if (
        not name
        or name in {".", ".."}
        or "\x00" in name
        or Path(name).name != name
        or should_exclude_dir(name)
    ):
        raise ValueError("관리 폴더에는 하나의 안전한 이름만 입력할 수 있습니다")
    return name


def _descendant_pattern(path: Path) -> str:
    value = str(path).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return value + os.sep + "%"


def _safe_existing_house_directory(house_dir: Path, value: str) -> Path:
    house = Path(house_dir).expanduser().resolve()
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = house / candidate
    absolute = Path(os.path.abspath(candidate))
    resolved = absolute.resolve(strict=True)
    if resolved != absolute:
        raise ValueError("심볼릭 링크가 포함된 목적 폴더는 사용할 수 없습니다")
    try:
        relative = resolved.relative_to(house)
    except ValueError as exc:
        raise ValueError("목적 폴더는 house 내부여야 합니다") from exc
    if not relative.parts or any(should_exclude_dir(part) for part in relative.parts):
        raise ValueError("house 루트나 관리 폴더는 목적지로 사용할 수 없습니다")
    if not resolved.is_dir():
        raise ValueError("목적 폴더가 존재하지 않습니다")
    return resolved


def _projection_diff(
    row,
    before_analysis: Mapping[str, object],
    after_analysis: Mapping[str, object],
    after_coordinate: Mapping[str, object],
) -> dict:
    before_coordinate = {field: row[field] for field in COORDINATE_FIELDS}
    analysis = {
        field: {"before": before_analysis.get(field), "after": after_analysis.get(field)}
        for field in ANALYSIS_FIELDS
        if before_analysis.get(field) != after_analysis.get(field)
    }
    coordinate = {
        field: {"before": before_coordinate[field], "after": after_coordinate.get(field)}
        for field in COORDINATE_FIELDS
        if before_coordinate[field] != after_coordinate.get(field)
    }
    return {"analysis": analysis, "coordinate": coordinate}


def file_relocate_preview(
    state_db: Path,
    *,
    house_dir: Path,
    file_id: str,
    target_directory: str,
    new_name: str | None = None,
) -> dict:
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        source = _file_row(conn, file_id, active=True)
        blockers: list[str] = []
        try:
            _require_current_file(source)
        except RuntimeError as exc:
            blockers.append(f"source_stale:{exc}")
        source_path = Path(source["canonical_path"])
        destination_dir = _safe_existing_house_directory(house_dir, target_directory)
        target_name = _safe_name(new_name or source_path.name, source_path.suffix)
        destination = destination_dir / target_name
        if destination == source_path:
            blockers.append("no_changes")
        elif destination.exists() or destination.is_symlink():
            blockers.append("destination_occupied")

        after_analysis = decision_store.build_effective_file_analysis(
            conn, file_id, target_name
        )
        before_analysis_row = conn.execute(
            "SELECT * FROM file_analysis WHERE file_id = ?", (file_id,)
        ).fetchone()
        if before_analysis_row is None:
            blockers.append("file_analysis_missing")
            before_analysis = {}
        else:
            before_analysis = dict(before_analysis_row)
        after_coordinate = decision_store.coordinate_fields_from_name(target_name)
        projection_diff = _projection_diff(
            source, before_analysis, after_analysis, after_coordinate
        )
        projection_same = not projection_diff["analysis"] and not projection_diff["coordinate"]
        if not projection_same:
            blockers.append("analysis_change_requires_title_correction")

        payload = {
            "file_id": file_id,
            "fingerprint_id": source["current_fingerprint_id"],
            "source_path": str(source_path),
            "destination_path": str(destination),
            "projection_same": projection_same,
        }
        return {
            "version": "1.3.3",
            "kind": "file_relocate",
            "item_count": 1,
            "source": _public_file(source),
            "target_directory": str(destination_dir),
            "target_name": target_name,
            "destination_path": str(destination),
            "rename": source_path.name != target_name,
            "move": source_path.parent != destination_dir,
            "projection_same": projection_same,
            "projection_diff": projection_diff,
            "route": "journaled_relocate" if projection_same else "title_correction",
            "title_correction_search": source_path.name,
            "blocked_reasons": blockers,
            "apply_available": not blockers,
            "plan_sha256": _hash(payload),
            "readonly": True,
        }
    finally:
        conn.close()


def _relocate_file(conn, *, plan: Mapping[str, object], run_id: str) -> dict:
    actual_run = decision_store.assert_active_actual_run(conn, run_id)
    source_path = Path(str(plan["source"]["canonical_path"]))
    destination = Path(str(plan["destination_path"]))
    decision_store.assert_actual_run_path(actual_run, source_path, "house_root")
    decision_store.assert_actual_run_path(actual_run, destination, "house_root")
    source = _file_row(conn, str(plan["source"]["file_id"]), active=True)
    _require_current_file(source)
    source_evidence = inspect_regular_file(source_path)
    decision_store.assert_manifest_source(
        actual_run, source_path, "house_root", source_evidence
    )
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    ensure_directory_nofollow(destination.parent)

    with decision_store.transaction(conn):
        operation_id = decision_store.create_operation(
            conn,
            run_id=run_id,
            action=ACTION,
            source_path=str(source_path),
            dest_path=str(destination),
            file_id=source["file_id"],
            expected_size=source["size"],
            expected_mtime_ns=source["mtime_ns"],
            expected_fingerprint_id=source["current_fingerprint_id"],
            source_dev=source_evidence.dev,
            source_ino=source_evidence.ino,
            source_ctime_ns=source_evidence.ctime_ns,
            source_sha256=source_evidence.sha256,
        )

    def guard() -> None:
        decision_store.assert_active_actual_run(conn, run_id)
        current = _file_row(conn, source["file_id"], active=True)
        if (
            current["canonical_path"] != str(source_path)
            or current["current_fingerprint_id"] != source["current_fingerprint_id"]
            or not evidence_matches(inspect_regular_file(source_path), source_evidence)
        ):
            raise RuntimeError("file relocation source or destination changed before consume")

    destination_evidence = decision_store.copy_record_consume_operation(
        conn, operation_id, source_path, destination, source_evidence, guard=guard
    )
    after_analysis = decision_store.build_effective_file_analysis(
        conn, source["file_id"], destination.name
    )
    before_analysis = conn.execute(
        "SELECT * FROM file_analysis WHERE file_id = ?", (source["file_id"],)
    ).fetchone()
    if before_analysis is None:
        raise RuntimeError("file relocation analysis is missing after approval")
    after_coordinate = decision_store.coordinate_fields_from_name(destination.name)
    projection_diff = _projection_diff(
        source, dict(before_analysis), after_analysis, after_coordinate
    )
    if projection_diff["analysis"] or projection_diff["coordinate"]:
        raise RuntimeError("file relocation analysis changed after approval")

    with decision_store.transaction(conn):
        conn.execute(
            """
            UPDATE files SET canonical_path = ?, dev = ?, ino = ?, ctime_ns = ?,
                size = ?, mtime_ns = ?, last_seen_at = CURRENT_TIMESTAMP,
                coordinate_kind = ?, part_num = ?, part_den = ?, volume_num = ?,
                volume_den = ?, coordinate_symbol = ?, coordinate_sort_key = ?,
                episode_start = ?, episode_end = ?, coordinate_raw = ?, span_ambiguous = ?
            WHERE file_id = ? AND active = 1 AND source = 'house'
            """,
            (
                str(destination), destination_evidence.dev, destination_evidence.ino,
                destination_evidence.ctime_ns, destination_evidence.size,
                destination_evidence.mtime_ns,
                *(after_coordinate[field] for field in COORDINATE_FIELDS),
                source["file_id"],
            ),
        )
        decision_store.upsert_file_analysis(
            conn,
            source["file_id"],
            destination,
            analysis=after_analysis,
            stat_result=os.stat(destination, follow_symlinks=False),
        )
        decision_store.transition_operation(conn, operation_id, "db_done")

    with decision_store.transaction(conn):
        decision_store.transition_operation(conn, operation_id, "committed")
    issues = decision_store.doctor_issues(conn, allowed_active_run_id=run_id)
    return {
        "operation_id": operation_id,
        "file_id": source["file_id"],
        "source_path": str(source_path),
        "destination_path": str(destination),
        "doctor_issue_count": len(issues),
        "doctor_first_issue": issues[0] if issues else None,
    }


def apply_file_relocate(
    state_db: Path,
    *,
    house_dir: Path,
    temp_dir: Path,
    index_path: Path,
    file_id: str,
    target_directory: str,
    new_name: str | None,
    confirm_count: int,
    confirm_plan_sha256: str,
    progress=None,
) -> dict:
    from library_review import _refresh_review_index

    with mutation_lock_for_roots(house_dir, temp_dir, "library-file-relocate-1.3.3"):
        plan = file_relocate_preview(
            state_db,
            house_dir=house_dir,
            file_id=file_id,
            target_directory=target_directory,
            new_name=new_name,
        )
        if not plan["apply_available"]:
            raise RuntimeError(
                "file relocation plan is blocked: " + ",".join(plan["blocked_reasons"])
            )
        if confirm_count != 1 or confirm_plan_sha256 != plan["plan_sha256"]:
            raise RuntimeError("file relocation confirmation is stale")

        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(
                    f"doctor failed before file relocation: {issues[0]['kind']}"
                )
            backup = decision_store.backup_state_db(conn, _backup_path(state_db))
            decision_store.issue_actual_run_token(
                conn, str(backup), house_dir=house_dir, temp_dir=temp_dir
            )
        finally:
            conn.close()

        run_id, manifest_path = decision_store.prepare_actual_run(
            state_db,
            house_dir,
            temp_dir,
            manifest_paths=[plan["source"]["canonical_path"]],
        )
        try:
            conn = decision_store.connect_state_db(state_db)
            try:
                result = _relocate_file(conn, plan=plan, run_id=run_id)
                decision_store.finish_actual_run(conn, run_id, success=True)
            finally:
                conn.close()
        except BaseException as exc:
            conn = decision_store.connect_state_db(state_db)
            try:
                decision_store.finish_actual_run(
                    conn, run_id, success=False, error=str(exc)
                )
            finally:
                conn.close()
            raise

        if progress:
            progress(1, 1, plan["target_name"])
        try:
            index = _refresh_review_index(
                state_db=state_db, house_dir=house_dir, index_path=index_path
            )
        except Exception as exc:
            index = {
                "index_updated": False,
                "house_index_synced": False,
                "warning": f"파일 이동은 완료됐지만 index 갱신에 실패했습니다: {exc}",
            }
        output = {
            **result,
            "run_id": run_id,
            "manifest_path": manifest_path,
            "backup_path": str(backup),
            **index,
        }
        if result["doctor_issue_count"]:
            output["_job_state"] = "needs_review"
            output["_job_message"] = "파일 정리는 완료됐지만 Doctor 검토가 필요합니다"
        return output


def managed_folder_preview(
    state_db: Path,
    *,
    house_dir: Path,
    work_bucket_id: int,
    parent_directory: str,
    folder_name: str,
    role: str,
) -> dict:
    role = str(role or "").strip()
    if role not in FOLDER_ROLES:
        raise ValueError("폴더 역할은 primary, edition, auxiliary 중 하나여야 합니다")
    parent = _safe_existing_house_directory(house_dir, parent_directory)
    name = _safe_directory_name(folder_name)
    destination = parent / name
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        work = conn.execute(
            "SELECT work_bucket_id, display_title FROM works WHERE work_bucket_id = ?",
            (int(work_bucket_id),),
        ).fetchone()
        if work is None:
            raise KeyError(work_bucket_id)
        blockers: list[str] = []
        if destination.exists() or destination.is_symlink():
            blockers.append("destination_occupied")
        existing = conn.execute(
            "SELECT folder_id, state, role FROM work_folders WHERE canonical_path = ?",
            (str(destination),),
        ).fetchone()
        if existing is not None:
            blockers.append("folder_path_already_registered")
        if role == "primary" and conn.execute(
            "SELECT 1 FROM work_folders "
            "WHERE work_bucket_id = ? AND role = 'primary' AND state = 'active' LIMIT 1",
            (int(work_bucket_id),),
        ).fetchone():
            blockers.append("work_primary_folder_exists")
        payload = {
            "work_bucket_id": int(work_bucket_id),
            "parent_directory": str(parent),
            "folder_name": name,
            "destination_path": str(destination),
            "role": role,
        }
        return {
            "version": "1.3.3",
            "kind": "managed_folder_create",
            "item_count": 1,
            "work": dict(work),
            "parent_directory": str(parent),
            "folder_name": name,
            "destination_path": str(destination),
            "role": role,
            "blocked_reasons": blockers,
            "apply_available": not blockers,
            "plan_sha256": _hash(payload),
            "readonly": True,
        }
    finally:
        conn.close()


def _owned_empty_directory(path: Path, evidence: tuple[int, int, int]) -> bool:
    try:
        info = os.stat(path, follow_symlinks=False)
        return (
            path.is_dir()
            and not path.is_symlink()
            and (info.st_dev, info.st_ino, info.st_ctime_ns) == evidence
            and not any(path.iterdir())
        )
    except (FileNotFoundError, OSError):
        return False


def _mkdir_no_clobber(path: Path) -> tuple[int, int, int]:
    path = Path(path)
    parent_fd = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
        getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.mkdir(path.name, mode=0o755, dir_fd=parent_fd)
        info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        return info.st_dev, info.st_ino, info.st_ctime_ns
    finally:
        os.close(parent_fd)


def _create_managed_folder(
    conn,
    *,
    plan: Mapping[str, object],
    run_id: str,
    manifest_path: str,
) -> dict:
    actual_run = decision_store.assert_active_actual_run(conn, run_id)
    destination = Path(str(plan["destination_path"]))
    decision_store.assert_actual_run_path(actual_run, destination, "house_root")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    _safe_existing_house_directory(
        Path(actual_run["house_root"]), str(destination.parent)
    )

    with decision_store.transaction(conn):
        group_id = decision_store.create_operation_group(
            conn,
            run_id=run_id,
            action=MANAGED_FOLDER_CREATE_ACTION,
            plan_sha256=str(plan["plan_sha256"]),
            dest_path=str(destination),
            item_count=0,
            manifest_path=manifest_path,
        )
        folder_id = int(conn.execute(
            """
            INSERT INTO work_folders(
                work_bucket_id, canonical_path, role, state, operation_group_id
            ) VALUES (?, ?, ?, 'planned', ?)
            """,
            (
                int(plan["work"]["work_bucket_id"]), str(destination),
                str(plan["role"]), group_id,
            ),
        ).lastrowid)

    created_evidence = None
    try:
        created_evidence = _mkdir_no_clobber(destination)
        with decision_store.transaction(conn):
            conn.execute(
                """
                UPDATE operation_groups SET destination_dev = ?, destination_ino = ?,
                    destination_ctime_ns = ?, updated_at = CURRENT_TIMESTAMP
                WHERE group_id = ?
                """,
                (*created_evidence, group_id),
            )
            decision_store.transition_operation_group(conn, group_id, "fs_done")
        with decision_store.transaction(conn):
            conn.execute(
                """
                UPDATE work_folders SET state = 'active', dev = ?, ino = ?, ctime_ns = ?,
                    updated_at = CURRENT_TIMESTAMP WHERE folder_id = ? AND state = 'planned'
                """,
                (*created_evidence, folder_id),
            )
            decision_store.transition_operation_group(conn, group_id, "db_done")
        with decision_store.transaction(conn):
            decision_store.transition_operation_group(conn, group_id, "committed")
    except BaseException as exc:
        removed = False
        if created_evidence and _owned_empty_directory(destination, created_evidence):
            destination.rmdir()
            removed = True
        with decision_store.transaction(conn):
            group = conn.execute(
                "SELECT state FROM operation_groups WHERE group_id = ?", (group_id,)
            ).fetchone()
            if group and group["state"] in {"planned", "fs_done", "db_done"}:
                target = "rolled_back" if removed else "failed"
                decision_store.transition_operation_group(
                    conn, group_id, target, error=str(exc)
                )
            conn.execute(
                "UPDATE work_folders SET state = 'failed', updated_at = CURRENT_TIMESTAMP "
                "WHERE folder_id = ? AND state IN ('planned', 'active')",
                (folder_id,),
            )
        raise
    return {
        "group_id": group_id,
        "folder_id": folder_id,
        "work_bucket_id": int(plan["work"]["work_bucket_id"]),
        "role": plan["role"],
        "destination_path": str(destination),
    }


def apply_managed_folder_create(
    state_db: Path,
    *,
    house_dir: Path,
    temp_dir: Path,
    work_bucket_id: int,
    parent_directory: str,
    folder_name: str,
    role: str,
    confirm_count: int,
    confirm_plan_sha256: str,
    progress=None,
) -> dict:
    with mutation_lock_for_roots(
        house_dir, temp_dir, "managed-folder-create-1.3.3"
    ):
        plan = managed_folder_preview(
            state_db,
            house_dir=house_dir,
            work_bucket_id=work_bucket_id,
            parent_directory=parent_directory,
            folder_name=folder_name,
            role=role,
        )
        if not plan["apply_available"]:
            raise RuntimeError(
                "managed folder plan is blocked: " + ",".join(plan["blocked_reasons"])
            )
        if confirm_count != 1 or confirm_plan_sha256 != plan["plan_sha256"]:
            raise RuntimeError("managed folder confirmation is stale")
        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(
                    f"doctor failed before managed folder create: {issues[0]['kind']}"
                )
            backup = decision_store.backup_state_db(
                conn, _backup_path(state_db, "managed_folder_create")
            )
            decision_store.issue_actual_run_token(
                conn, str(backup), house_dir=house_dir, temp_dir=temp_dir
            )
        finally:
            conn.close()
        run_id, manifest_path = decision_store.prepare_actual_run(
            state_db, house_dir, temp_dir, manifest_paths=[]
        )
        try:
            conn = decision_store.connect_state_db(state_db)
            try:
                result = _create_managed_folder(
                    conn,
                    plan=plan,
                    run_id=run_id,
                    manifest_path=manifest_path,
                )
                result["doctor_issues"] = decision_store.doctor_issues(
                    conn, allowed_active_run_id=run_id
                )
                decision_store.finish_actual_run(conn, run_id, success=True)
            finally:
                conn.close()
        except BaseException as exc:
            conn = decision_store.connect_state_db(state_db)
            try:
                decision_store.finish_actual_run(
                    conn, run_id, success=False, error=str(exc)
                )
            finally:
                conn.close()
            raise
        if progress:
            progress(1, 1, plan["folder_name"])
        output = {
            **result,
            "run_id": run_id,
            "manifest_path": manifest_path,
            "backup_path": str(backup),
        }
        if result["doctor_issues"]:
            output["_job_state"] = "needs_review"
            output["_job_message"] = "폴더 생성은 완료됐지만 Doctor 검토가 필요합니다"
        return output


def _directory_identity(path: Path) -> tuple[int, int, int]:
    info = os.stat(path, follow_symlinks=False)
    if not path.is_dir() or path.is_symlink():
        raise RuntimeError(f"managed folder is not a real directory: {path}")
    return info.st_dev, info.st_ino, info.st_ctime_ns


def _rename_directory_no_clobber(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    if os.uname().sysname == "Darwin":
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        renameatx_np = libc.renameatx_np
        renameatx_np.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameatx_np.restype = ctypes.c_int
        at_fdcwd = -2
        rename_excl = 0x00000004
        result = renameatx_np(
            at_fdcwd, os.fsencode(source), at_fdcwd, os.fsencode(destination),
            rename_excl,
        )
        if result != 0:
            code = ctypes.get_errno()
            if code in {errno.EEXIST, errno.ENOTEMPTY}:
                raise FileExistsError(destination)
            raise OSError(code, os.strerror(code), str(destination))
        return
    # Other supported test environments rely on the command-wide mutation lock.
    # Refuse a visible destination immediately before the single rename syscall.
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    os.rename(source, destination)


def _folder_inventory(
    conn, source: Path, *, require_fingerprint: bool = True
) -> tuple[list[dict], list[str]]:
    db_rows = conn.execute(
        """
        SELECT f.file_id, f.canonical_path, f.size, f.mtime_ns, f.dev, f.ino,
               f.ctime_ns, f.current_fingerprint_id
        FROM files AS f
        WHERE f.active = 1 AND f.source = 'house'
          AND f.canonical_path LIKE ? ESCAPE '\\'
        ORDER BY f.canonical_path
        """,
        (_descendant_pattern(source),),
    ).fetchall()
    by_path = {
        decision_store.canonicalize_path(row["canonical_path"]): row
        for row in db_rows
    }
    items: list[dict] = []
    directories: list[str] = []
    for current, child_dirs, filenames in os.walk(source, followlinks=False):
        current_path = Path(current)
        if current_path.is_symlink():
            raise RuntimeError(f"folder contains symlink directory: {current_path}")
        relative_directory = current_path.relative_to(source)
        if relative_directory.parts:
            directories.append(str(relative_directory))
        for name in child_dirs:
            child = current_path / name
            if child.is_symlink():
                raise RuntimeError(f"folder contains symlink directory: {child}")
        for name in filenames:
            path = current_path / name
            if path.is_symlink():
                raise RuntimeError(f"folder contains symlink file: {path}")
            if len(items) >= MAX_FOLDER_ITEMS:
                raise RuntimeError(f"folder inventory exceeds {MAX_FOLDER_ITEMS} files")
            info = os.stat(path, follow_symlinks=False)
            canonical = decision_store.canonicalize_path(path)
            row = by_path.get(canonical)
            if row is not None:
                expected = (
                    row["dev"], row["ino"], row["ctime_ns"],
                    row["size"], row["mtime_ns"],
                )
                actual = (
                    info.st_dev, info.st_ino, info.st_ctime_ns,
                    info.st_size, info.st_mtime_ns,
                )
                if expected != actual:
                    raise RuntimeError(f"registered file snapshot is stale: {path}")
                if require_fingerprint and row["current_fingerprint_id"] is None:
                    raise RuntimeError(f"registered file fingerprint is missing: {path}")
            items.append({
                "relative_path": str(path.relative_to(source)),
                "source_path": str(path),
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
                "dev": info.st_dev,
                "ino": info.st_ino,
                "ctime_ns": info.st_ctime_ns,
                "registered": row is not None,
                "file_id": row["file_id"] if row else None,
                "fingerprint_id": row["current_fingerprint_id"] if row else None,
            })
    seen = {decision_store.canonicalize_path(item["source_path"]) for item in items}
    missing = sorted(set(by_path) - seen)
    if missing:
        raise RuntimeError(f"managed folder DB files are missing: {missing[0]}")
    return items, directories


_FOLDER_ITEM_SNAPSHOT_FIELDS = (
    "relative_path", "source_path", "size", "mtime_ns", "dev", "ino",
    "ctime_ns", "registered", "file_id", "fingerprint_id",
)


def _assert_folder_plan_sources_current(
    conn,
    actual_run,
    plan: Mapping[str, object],
    *,
    require_fingerprint: bool,
) -> None:
    """Revalidate every planned child immediately before the atomic rename."""
    source = Path(str(plan["source_path"]))
    try:
        current_items, current_directories = _folder_inventory(
            conn, source, require_fingerprint=require_fingerprint
        )
    except RuntimeError as exc:
        raise RuntimeError(
            f"folder contents changed after confirmation: {exc}"
        ) from exc

    def snapshot(items):
        return [
            tuple(item.get(field) for field in _FOLDER_ITEM_SNAPSHOT_FIELDS)
            for item in items
        ]

    if snapshot(current_items) != snapshot(plan["items"]):
        raise RuntimeError("folder contents changed after confirmation")
    if sorted(current_directories) != sorted(plan["directories"]):
        raise RuntimeError("folder directories changed after confirmation")

    for item in current_items:
        evidence = SimpleNamespace(
            dev=item["dev"],
            ino=item["ino"],
            ctime_ns=item["ctime_ns"],
            size=item["size"],
            mtime_ns=item["mtime_ns"],
        )
        decision_store.assert_manifest_source(
            actual_run, item["source_path"], "house_root", evidence
        )


def managed_folder_relocate_preview(
    state_db: Path,
    *,
    house_dir: Path,
    folder_id: int,
    target_parent: str,
    new_name: str | None = None,
) -> dict:
    target = _safe_existing_house_directory(house_dir, target_parent)
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        folder = conn.execute(
            """
            SELECT wf.*, w.display_title
            FROM work_folders AS wf
            JOIN works AS w ON w.work_bucket_id = wf.work_bucket_id
            WHERE wf.folder_id = ? AND wf.state = 'active'
            """,
            (int(folder_id),),
        ).fetchone()
        if folder is None:
            raise KeyError(folder_id)
        source = Path(folder["canonical_path"])
        current_identity = _directory_identity(source)
        if folder["dev"] is not None and current_identity[:2] != (
            folder["dev"], folder["ino"]
        ):
            raise RuntimeError("managed folder identity is stale")
        name = _safe_directory_name(new_name or source.name)
        destination = target / name
        blockers: list[str] = []
        if destination == source:
            blockers.append("no_changes")
        elif destination.exists() or destination.is_symlink():
            blockers.append("destination_occupied")
        try:
            target.relative_to(source)
        except ValueError:
            pass
        else:
            blockers.append("destination_inside_source")
        try:
            items, directories = _folder_inventory(conn, source)
        except RuntimeError as exc:
            items, directories = [], []
            blockers.append(f"inventory_blocked:{exc}")
        payload = {
            "folder_id": int(folder_id),
            "work_bucket_id": int(folder["work_bucket_id"]),
            "source_path": str(source),
            "source_identity": current_identity,
            "destination_path": str(destination),
            "items": items,
            "directories": directories,
        }
        return {
            "version": "1.3.3",
            "kind": "managed_folder_relocate",
            "item_count": len(items),
            "folder": dict(folder),
            "source_path": str(source),
            "target_parent": str(target),
            "target_name": name,
            "destination_path": str(destination),
            "rename": source.name != name,
            "move": source.parent != target,
            "registered_count": sum(item["registered"] for item in items),
            "auxiliary_count": sum(not item["registered"] for item in items),
            "directory_count": len(directories),
            "total_size": sum(item["size"] for item in items),
            "items": items,
            "directories": directories,
            "blocked_reasons": blockers,
            "apply_available": not blockers,
            "plan_sha256": _hash(payload),
            "readonly": True,
        }
    finally:
        conn.close()


def managed_folder_adopt_preview(
    state_db: Path,
    *,
    house_dir: Path,
    folder_path: str,
    work_bucket_id: int,
    role: str,
) -> dict:
    role = str(role or "").strip()
    if role not in FOLDER_ROLES:
        raise ValueError("폴더 역할은 primary, edition, auxiliary 중 하나여야 합니다")
    folder = _safe_existing_house_directory(house_dir, folder_path)
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        work = conn.execute(
            "SELECT work_bucket_id, display_title FROM works WHERE work_bucket_id = ?",
            (int(work_bucket_id),),
        ).fetchone()
        if work is None:
            raise KeyError(work_bucket_id)
        blockers: list[str] = []
        existing = conn.execute(
            "SELECT folder_id FROM work_folders WHERE canonical_path = ? AND state = 'active'",
            (str(folder),),
        ).fetchone()
        if existing:
            blockers.append("folder_already_managed")
        if role == "primary" and conn.execute(
            "SELECT 1 FROM work_folders WHERE work_bucket_id = ? "
            "AND role = 'primary' AND state = 'active' LIMIT 1",
            (int(work_bucket_id),),
        ).fetchone():
            blockers.append("work_primary_folder_exists")
        try:
            items, directories = _folder_inventory(conn, folder)
        except RuntimeError as exc:
            items, directories = [], []
            blockers.append(f"inventory_blocked:{exc}")
        found_work_ids = sorted({
            int(row[0])
            for row in conn.execute(
                """
                SELECT DISTINCT v.work_bucket_id
                FROM files AS f JOIN variants AS v ON v.variant_id = f.variant_id
                WHERE f.active = 1 AND f.source = 'house'
                  AND f.canonical_path LIKE ? ESCAPE '\\'
                """,
                (_descendant_pattern(folder),),
            )
            if row[0] is not None
        })
        if any(value != int(work_bucket_id) for value in found_work_ids):
            blockers.append("folder_contains_other_work")
        identity = _directory_identity(folder)
        payload = {
            "folder_path": str(folder),
            "folder_identity": identity,
            "work_bucket_id": int(work_bucket_id),
            "role": role,
            "items": items,
            "directories": directories,
        }
        return {
            "version": "1.3.3",
            "kind": "managed_folder_adopt",
            "item_count": len(items),
            "folder_path": str(folder),
            "work": dict(work),
            "role": role,
            "found_work_ids": found_work_ids,
            "registered_count": sum(item["registered"] for item in items),
            "auxiliary_count": sum(not item["registered"] for item in items),
            "directory_count": len(directories),
            "items": items,
            "directories": directories,
            "blocked_reasons": blockers,
            "apply_available": not blockers,
            "plan_sha256": _hash(payload),
            "readonly": True,
        }
    finally:
        conn.close()


def apply_managed_folder_adopt(
    state_db: Path,
    *,
    house_dir: Path,
    temp_dir: Path,
    folder_path: str,
    work_bucket_id: int,
    role: str,
    confirm_count: int,
    confirm_plan_sha256: str,
    progress=None,
) -> dict:
    with mutation_lock_for_roots(house_dir, temp_dir, "managed-folder-adopt-1.3.3"):
        plan = managed_folder_adopt_preview(
            state_db,
            house_dir=house_dir,
            folder_path=folder_path,
            work_bucket_id=work_bucket_id,
            role=role,
        )
        if not plan["apply_available"]:
            raise RuntimeError(
                "managed folder adopt is blocked: " + ",".join(plan["blocked_reasons"])
            )
        if (
            confirm_count != plan["item_count"]
            or confirm_plan_sha256 != plan["plan_sha256"]
        ):
            raise RuntimeError("managed folder adopt confirmation is stale")
        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(
                    f"doctor failed before managed folder adopt: {issues[0]['kind']}"
                )
            backup = decision_store.backup_state_db(
                conn, _backup_path(state_db, "managed_folder_adopt")
            )
            decision_store.issue_actual_run_token(
                conn, str(backup), house_dir=house_dir, temp_dir=temp_dir
            )
        finally:
            conn.close()
        run_id, manifest_path = decision_store.prepare_actual_run(
            state_db,
            house_dir,
            temp_dir,
            manifest_paths=[item["source_path"] for item in plan["items"]],
        )
        try:
            conn = decision_store.connect_state_db(state_db)
            try:
                decision_store.assert_active_actual_run(conn, run_id)
                identity = _directory_identity(Path(plan["folder_path"]))
                group_id = None
                folder_id = None
                recovered_after_error = None
                try:
                    with decision_store.transaction(conn):
                        group_id = decision_store.create_operation_group(
                            conn,
                            run_id=run_id,
                            action=MANAGED_FOLDER_ADOPT_ACTION,
                            plan_sha256=plan["plan_sha256"],
                            source_path=plan["folder_path"],
                            dest_path=plan["folder_path"],
                            item_count=plan["item_count"],
                            manifest_path=manifest_path,
                            source_manifest_json=json.dumps(
                                {"items": plan["items"], "directories": plan["directories"]},
                                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                            ),
                        )
                        conn.execute(
                            "UPDATE operation_groups SET source_dev = ?, source_ino = ?, "
                            "source_ctime_ns = ?, destination_dev = ?, destination_ino = ?, "
                            "destination_ctime_ns = ? WHERE group_id = ?",
                            (*identity, *identity, group_id),
                        )
                        decision_store.transition_operation_group(conn, group_id, "fs_done")
                    with decision_store.transaction(conn):
                        folder_id = int(conn.execute(
                            """
                            INSERT INTO work_folders(
                                work_bucket_id, canonical_path, role, state,
                                operation_group_id, dev, ino, ctime_ns
                            ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
                            """,
                            (
                                int(plan["work"]["work_bucket_id"]), plan["folder_path"],
                                plan["role"], group_id, *identity,
                            ),
                        ).lastrowid)
                        decision_store.transition_operation_group(conn, group_id, "db_done")
                    with decision_store.transaction(conn):
                        decision_store.transition_operation_group(conn, group_id, "committed")
                except BaseException as exc:
                    if group_id is None or recover_operation_group(conn, group_id) != "committed":
                        raise
                    recovered_after_error = str(exc)
                    folder_id = int(conn.execute(
                        "SELECT folder_id FROM work_folders WHERE operation_group_id = ?",
                        (group_id,),
                    ).fetchone()[0])
                doctor_issues = decision_store.doctor_issues(
                    conn, allowed_active_run_id=run_id
                )
                decision_store.finish_actual_run(conn, run_id, success=True)
            finally:
                conn.close()
        except BaseException as exc:
            conn = decision_store.connect_state_db(state_db)
            try:
                decision_store.finish_actual_run(
                    conn, run_id, success=False, error=str(exc)
                )
            finally:
                conn.close()
            raise
        if progress:
            progress(1, 1, Path(plan["folder_path"]).name)
        output = {
            "group_id": group_id,
            "folder_id": folder_id,
            "work_bucket_id": int(plan["work"]["work_bucket_id"]),
            "role": plan["role"],
            "folder_path": plan["folder_path"],
            "run_id": run_id,
            "manifest_path": manifest_path,
            "backup_path": str(backup),
            "doctor_issues": doctor_issues,
            "recovered_after_error": recovered_after_error,
        }
        if doctor_issues:
            output["_job_state"] = "needs_review"
            output["_job_message"] = "폴더 등록은 완료됐지만 Doctor 검토가 필요합니다"
        return output


def _group_row(conn, group_id: int):
    row = conn.execute(
        "SELECT * FROM operation_groups WHERE group_id = ?", (int(group_id),)
    ).fetchone()
    if row is None:
        raise KeyError(group_id)
    return row


def recover_managed_folder_group(conn, group_id: int) -> str:
    group = _group_row(conn, group_id)
    if group["action"] != MANAGED_FOLDER_RELOCATE_ACTION:
        raise ValueError("operation group is not a managed folder relocation")
    if group["state"] not in {"planned", "fs_done", "db_done"}:
        return group["state"]
    source = Path(group["source_path"])
    destination = Path(group["dest_path"])
    source_exists = source.is_dir() and not source.is_symlink()
    destination_exists = destination.is_dir() and not destination.is_symlink()
    expected_inode = (group["source_dev"], group["source_ino"])

    def inode_matches(path: Path) -> bool:
        try:
            info = os.stat(path, follow_symlinks=False)
            return (info.st_dev, info.st_ino) == expected_inode
        except (FileNotFoundError, OSError):
            return False

    if group["state"] in {"planned", "fs_done"}:
        if source_exists and inode_matches(source) and not destination_exists:
            pass
        elif destination_exists and inode_matches(destination) and not source_exists:
            _rename_directory_no_clobber(destination, source)
        else:
            target_state = "stale" if group["state"] == "planned" else "failed"
            with decision_store.transaction(conn):
                decision_store.transition_operation_group(
                    conn, group_id, target_state,
                    error="folder recovery path identity mismatch",
                )
            return target_state
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE operations SET state = 'rolled_back', updated_at = CURRENT_TIMESTAMP "
                "WHERE run_id = ? AND action = ? AND state IN ('planned','fs_done','db_done')",
                (group["run_id"], MANAGED_FOLDER_ITEM_ACTION),
            )
            decision_store.transition_operation_group(conn, group_id, "rolled_back")
        return "rolled_back"

    folder = conn.execute(
        "SELECT canonical_path, state FROM work_folders "
        "WHERE canonical_path = ? AND state = 'active'",
        (str(destination),),
    ).fetchone()
    if (
        destination_exists and inode_matches(destination) and not source_exists
        and folder is not None and folder["canonical_path"] == str(destination)
        and folder["state"] == "active"
    ):
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE operations SET state = 'committed', updated_at = CURRENT_TIMESTAMP "
                "WHERE run_id = ? AND action = ? AND state = 'db_done'",
                (group["run_id"], MANAGED_FOLDER_ITEM_ACTION),
            )
            decision_store.transition_operation_group(conn, group_id, "committed")
        return "committed"
    with decision_store.transaction(conn):
        decision_store.transition_operation_group(
            conn, group_id, "failed", error="folder db_done recovery state mismatch"
        )
    return "failed"


def _recover_managed_folder_create_group(conn, group_id: int) -> str:
    group = _group_row(conn, group_id)
    if group["action"] != MANAGED_FOLDER_CREATE_ACTION:
        raise ValueError("operation group is not a managed folder creation")
    if group["state"] not in {"planned", "fs_done", "db_done"}:
        return group["state"]
    destination = Path(group["dest_path"])
    folder = conn.execute(
        "SELECT folder_id, state FROM work_folders WHERE operation_group_id = ?",
        (int(group_id),),
    ).fetchone()
    evidence = (
        group["destination_dev"], group["destination_ino"],
        group["destination_ctime_ns"],
    )
    has_evidence = all(value is not None for value in evidence)
    if group["state"] == "db_done":
        if (
            folder is not None and folder["state"] == "active"
            and destination.is_dir() and not destination.is_symlink()
            and has_evidence and _directory_identity(destination)[:2] == evidence[:2]
        ):
            with decision_store.transaction(conn):
                decision_store.transition_operation_group(conn, group_id, "committed")
            return "committed"
        with decision_store.transaction(conn):
            decision_store.transition_operation_group(
                conn, group_id, "failed", error="managed folder db_done state mismatch"
            )
        return "failed"
    if not destination.exists() and not destination.is_symlink():
        with decision_store.transaction(conn):
            if folder is not None:
                conn.execute(
                    "UPDATE work_folders SET state = 'failed', updated_at = CURRENT_TIMESTAMP "
                    "WHERE folder_id = ?",
                    (folder["folder_id"],),
                )
            decision_store.transition_operation_group(conn, group_id, "rolled_back")
        return "rolled_back"
    if not has_evidence or not _owned_empty_directory(destination, evidence):
        target = "stale" if group["state"] == "planned" else "failed"
        with decision_store.transaction(conn):
            decision_store.transition_operation_group(
                conn, group_id, target,
                error="managed folder create recovery cannot prove empty-folder ownership",
            )
        return target
    destination.rmdir()
    with decision_store.transaction(conn):
        if folder is not None:
            conn.execute(
                "UPDATE work_folders SET state = 'failed', updated_at = CURRENT_TIMESTAMP "
                "WHERE folder_id = ?",
                (folder["folder_id"],),
            )
        decision_store.transition_operation_group(conn, group_id, "rolled_back")
    return "rolled_back"


def recover_operation_group(conn, group_id: int) -> str:
    group = _group_row(conn, group_id)
    if group["action"] == MANAGED_FOLDER_CREATE_ACTION:
        return _recover_managed_folder_create_group(conn, group_id)
    if group["action"] == MANAGED_FOLDER_RELOCATE_ACTION:
        return recover_managed_folder_group(conn, group_id)
    if group["action"] == MANAGED_FOLDER_ADOPT_ACTION:
        if group["state"] not in {"planned", "fs_done", "db_done"}:
            return group["state"]
        folder = conn.execute(
            "SELECT folder_id, state FROM work_folders WHERE operation_group_id = ?",
            (int(group_id),),
        ).fetchone()
        if group["state"] == "db_done" and folder and folder["state"] == "active":
            with decision_store.transaction(conn):
                decision_store.transition_operation_group(conn, group_id, "committed")
            return "committed"
        if group["state"] in {"planned", "fs_done"} and folder is None:
            with decision_store.transaction(conn):
                decision_store.transition_operation_group(conn, group_id, "rolled_back")
            return "rolled_back"
        with decision_store.transaction(conn):
            target = "failed" if group["state"] == "fs_done" else "stale"
            decision_store.transition_operation_group(
                conn, group_id, target, error="managed folder adopt recovery mismatch"
            )
        return target
    if group["action"] == FOLDER_QUARANTINE_ACTION:
        return recover_folder_quarantine_group(conn, group_id)
    raise ValueError(f"unsupported operation group action: {group['action']}")


def folder_quarantine_preview(
    state_db: Path,
    *,
    house_dir: Path,
    temp_dir: Path,
    folder_path: str,
) -> dict:
    house = Path(house_dir).resolve()
    source = Path(folder_path).expanduser().resolve()
    try:
        relative = source.relative_to(house)
    except ValueError as exc:
        raise ValueError("격리 폴더가 house 밖에 있습니다") from exc
    if not relative.parts:
        raise ValueError("house 루트는 격리할 수 없습니다")
    current_identity = _directory_identity(source)
    destination = (
        Path(temp_dir).resolve() / "trash_bin" /
        "user_approved_folder_discard" / relative
    )
    conn = decision_store.connect_state_db_readonly(state_db)
    try:
        blockers: list[str] = []
        if destination.exists() or destination.is_symlink():
            blockers.append("quarantine_destination_occupied")
        try:
            items, directories = _folder_inventory(
                conn, source, require_fingerprint=False
            )
        except RuntimeError as exc:
            items, directories = [], []
            blockers.append(f"inventory_blocked:{exc}")
        registered = [item for item in items if item["registered"]]
        if not registered:
            blockers.append("folder_has_no_registered_books")
        managed_folders = [dict(row) for row in conn.execute(
            """
            SELECT folder_id, work_bucket_id, canonical_path, role
            FROM work_folders
            WHERE state = 'active' AND (canonical_path = ? OR canonical_path LIKE ? ESCAPE '\\')
            ORDER BY canonical_path
            """,
            (str(source), _descendant_pattern(source)),
        )]
        work_ids = sorted({int(row[0]) for row in conn.execute(
            """
            SELECT DISTINCT v.work_bucket_id
            FROM files f JOIN variants v ON v.variant_id = f.variant_id
            WHERE f.active = 1 AND f.source = 'house'
              AND f.canonical_path LIKE ? ESCAPE '\\'
            """,
            (_descendant_pattern(source),),
        )})
        related_folders: list[dict] = []
        if work_ids:
            placeholders = ",".join("?" for _ in work_ids)
            rows = conn.execute(
                f"""
                SELECT wf.folder_id, wf.work_bucket_id, wf.canonical_path, wf.role,
                       w.display_title
                FROM work_folders wf JOIN works w ON w.work_bucket_id = wf.work_bucket_id
                WHERE wf.state = 'active' AND wf.work_bucket_id IN ({placeholders})
                  AND NOT (wf.canonical_path = ? OR wf.canonical_path LIKE ? ESCAPE '\\')
                ORDER BY wf.role, wf.canonical_path
                """,
                [*work_ids, str(source), _descendant_pattern(source)],
            ).fetchall()
            related_folders = [dict(row) for row in rows]
        confirmation_items = [
            {key: item[key] for key in (
                "relative_path", "source_path", "size", "mtime_ns", "dev", "ino",
                "ctime_ns", "registered", "file_id",
            )}
            for item in items
        ]
        payload = {
            "source_path": str(source),
            "source_identity": current_identity,
            "destination_path": str(destination),
            "items": confirmation_items,
            "directories": directories,
            "managed_folder_ids": [row["folder_id"] for row in managed_folders],
        }
        return {
            "version": "1.3.5",
            "kind": "user_folder_quarantine",
            "item_count": len(items),
            "source_path": str(source),
            "source_identity": current_identity,
            "destination_path": str(destination),
            "registered_count": len(registered),
            "auxiliary_count": len(items) - len(registered),
            "directory_count": len(directories),
            "total_size": sum(int(item["size"]) for item in items),
            "work_bucket_ids": work_ids,
            "managed_folders": managed_folders,
            "related_folders": related_folders,
            "items": items,
            "directories": directories,
            "blocked_reasons": blockers,
            "apply_available": not blockers,
            "plan_sha256": _hash(payload),
            "readonly": True,
        }
    finally:
        conn.close()


def recover_folder_quarantine_group(conn, group_id: int) -> str:
    group = _group_row(conn, group_id)
    if group["action"] != FOLDER_QUARANTINE_ACTION:
        raise ValueError("operation group is not a folder quarantine")
    if group["state"] not in {"planned", "fs_done", "db_done"}:
        return group["state"]
    source = Path(group["source_path"])
    destination = Path(group["dest_path"])
    expected_inode = (group["source_dev"], group["source_ino"])

    def matches(path: Path) -> bool:
        try:
            return _directory_identity(path)[:2] == expected_inode
        except (FileNotFoundError, OSError, RuntimeError):
            return False

    if group["state"] in {"planned", "fs_done"}:
        if matches(source) and not destination.exists():
            pass
        elif matches(destination) and not source.exists():
            ensure_directory_nofollow(source.parent)
            _rename_directory_no_clobber(destination, source)
        else:
            target = "stale" if group["state"] == "planned" else "failed"
            with decision_store.transaction(conn):
                decision_store.transition_operation_group(
                    conn, group_id, target, error="folder quarantine recovery identity mismatch"
                )
            return target
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE operations SET state = 'rolled_back', updated_at = CURRENT_TIMESTAMP "
                "WHERE run_id = ? AND action = ? AND state IN ('planned','fs_done','db_done')",
                (group["run_id"], FOLDER_QUARANTINE_ITEM_ACTION),
            )
            decision_store.transition_operation_group(conn, group_id, "rolled_back")
        return "rolled_back"

    active = conn.execute(
        "SELECT COUNT(*) FROM files WHERE active = 1 AND source = 'house' "
        "AND canonical_path LIKE ? ESCAPE '\\'",
        (_descendant_pattern(source),),
    ).fetchone()[0]
    quarantined = conn.execute(
        "SELECT COUNT(*) FROM files WHERE active = 0 AND source = 'quarantine' "
        "AND canonical_path LIKE ? ESCAPE '\\'",
        (_descendant_pattern(destination),),
    ).fetchone()[0]
    if matches(destination) and not source.exists() and active == 0 and quarantined > 0:
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE operations SET state = 'committed', updated_at = CURRENT_TIMESTAMP "
                "WHERE run_id = ? AND action = ? AND state = 'db_done'",
                (group["run_id"], FOLDER_QUARANTINE_ITEM_ACTION),
            )
            decision_store.transition_operation_group(conn, group_id, "committed")
        return "committed"
    with decision_store.transaction(conn):
        decision_store.transition_operation_group(
            conn, group_id, "failed", error="folder quarantine db_done recovery mismatch"
        )
    return "failed"


def _quarantine_folder(conn, *, plan: Mapping[str, object], run_id: str, manifest_path: str) -> dict:
    actual_run = decision_store.assert_active_actual_run(conn, run_id)
    source = Path(str(plan["source_path"]))
    destination = Path(str(plan["destination_path"]))
    decision_store.assert_actual_run_path(actual_run, source, "house_root")
    decision_store.assert_actual_run_path(actual_run, destination, "temp_root")
    current_identity = _directory_identity(source)
    expected_identity = tuple(plan["source_identity"])
    if current_identity != expected_identity:
        raise RuntimeError("folder identity changed before quarantine")
    ensure_directory_nofollow(destination.parent)
    manifest_json = json.dumps(
        {"items": plan["items"], "directories": plan["directories"]},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    operation_ids: list[int] = []
    with decision_store.transaction(conn):
        group_id = decision_store.create_operation_group(
            conn, run_id=run_id, action=FOLDER_QUARANTINE_ACTION,
            plan_sha256=str(plan["plan_sha256"]), source_path=str(source),
            dest_path=str(destination), item_count=int(plan["item_count"]),
            manifest_path=manifest_path, source_manifest_json=manifest_json,
        )
        conn.execute(
            "UPDATE operation_groups SET source_dev = ?, source_ino = ?, source_ctime_ns = ? "
            "WHERE group_id = ?", (*current_identity, group_id)
        )
        for item in plan["items"]:
            if not item["registered"]:
                continue
            operation_ids.append(decision_store.create_operation(
                conn, run_id=run_id, action=FOLDER_QUARANTINE_ITEM_ACTION,
                source_path=item["source_path"],
                quarantine_path=str(destination / item["relative_path"]),
                file_id=item["file_id"], expected_size=item["size"],
                expected_mtime_ns=item["mtime_ns"],
                expected_fingerprint_id=item["fingerprint_id"],
                source_dev=item["dev"], source_ino=item["ino"],
                source_ctime_ns=item["ctime_ns"],
            ))
    recovered_after_error = None
    try:
        _assert_folder_plan_sources_current(
            conn, actual_run, plan, require_fingerprint=False
        )
        _rename_directory_no_clobber(source, destination)
        destination_identity = _directory_identity(destination)
        if destination_identity[:2] != current_identity[:2]:
            raise RuntimeError("folder inode changed during quarantine")
        with decision_store.transaction(conn):
            conn.execute(
                "UPDATE operation_groups SET destination_dev = ?, destination_ino = ?, "
                "destination_ctime_ns = ?, updated_at = CURRENT_TIMESTAMP WHERE group_id = ?",
                (*destination_identity, group_id),
            )
            for operation_id in operation_ids:
                operation = conn.execute(
                    "SELECT quarantine_path FROM operations WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                evidence = inspect_regular_file(operation["quarantine_path"])
                decision_store.record_operation_destination(conn, operation_id, evidence)
                decision_store.transition_operation(conn, operation_id, "fs_done")
            decision_store.transition_operation_group(conn, group_id, "fs_done")

        with decision_store.transaction(conn):
            selected_ids = [item["file_id"] for item in plan["items"] if item["registered"]]
            selected_set = set(selected_ids)
            representatives = conn.execute(
                "SELECT variant_id, file_id FROM representatives WHERE file_id IN ({})".format(
                    ",".join("?" for _ in selected_ids)
                ), selected_ids,
            ).fetchall() if selected_ids else []
            for representative in representatives:
                replacement = conn.execute(
                    "SELECT file_id FROM files WHERE variant_id = ? AND active = 1 AND source = 'house' "
                    "ORDER BY protected DESC, canonical_path",
                    (representative["variant_id"],),
                ).fetchall()
                replacement_id = next(
                    (row["file_id"] for row in replacement if row["file_id"] not in selected_set),
                    None,
                )
                if replacement_id:
                    conn.execute(
                        "UPDATE representatives SET file_id = ?, updated_at = CURRENT_TIMESTAMP "
                        "WHERE variant_id = ?", (replacement_id, representative["variant_id"]),
                    )
                    conn.execute("UPDATE files SET protected = 1 WHERE file_id = ?", (replacement_id,))
                else:
                    conn.execute("DELETE FROM representatives WHERE variant_id = ?", (representative["variant_id"],))
            for item in plan["items"]:
                if not item["registered"]:
                    continue
                new_path = destination / item["relative_path"]
                evidence = inspect_regular_file(new_path)
                conn.execute(
                    """
                    UPDATE files SET canonical_path = ?, source = 'quarantine', active = 0,
                        protected = 0, dev = ?, ino = ?, ctime_ns = ?, size = ?, mtime_ns = ?
                    WHERE file_id = ? AND active = 1 AND source = 'house'
                    """,
                    (str(new_path), evidence.dev, evidence.ino, evidence.ctime_ns,
                     evidence.size, evidence.mtime_ns, item["file_id"]),
                )
                decision_store.supersede_open_reviews_for_file(
                    conn, item["file_id"], reason="user_approved_folder_discard"
                )
            conn.execute(
                "UPDATE work_folders SET state = 'retired', updated_at = CURRENT_TIMESTAMP "
                "WHERE state = 'active' AND (canonical_path = ? OR canonical_path LIKE ? ESCAPE '\\')",
                (str(source), _descendant_pattern(source)),
            )
            for operation_id in operation_ids:
                decision_store.transition_operation(conn, operation_id, "db_done")
            decision_store.transition_operation_group(conn, group_id, "db_done")
        with decision_store.transaction(conn):
            for operation_id in operation_ids:
                decision_store.transition_operation(conn, operation_id, "committed")
            decision_store.transition_operation_group(conn, group_id, "committed")
    except BaseException as exc:
        group = _group_row(conn, group_id)
        if group["state"] in {"planned", "fs_done", "db_done"}:
            outcome = recover_folder_quarantine_group(conn, group_id)
            if outcome == "committed":
                recovered_after_error = str(exc)
            else:
                raise
        else:
            raise
    return {
        "group_id": group_id, "operation_ids": operation_ids,
        "source_path": str(source), "destination_path": str(destination),
        "registered_count": int(plan["registered_count"]),
        "auxiliary_count": int(plan["auxiliary_count"]),
        "recovered_after_error": recovered_after_error,
    }


def apply_folder_quarantine(
    state_db: Path, *, house_dir: Path, temp_dir: Path, index_path: Path,
    folder_path: str, confirm_count: int, confirm_plan_sha256: str, progress=None,
) -> dict:
    from library_review import _refresh_review_index
    with mutation_lock_for_roots(house_dir, temp_dir, "folder-quarantine-1.3.5"):
        plan = folder_quarantine_preview(
            state_db, house_dir=house_dir, temp_dir=temp_dir, folder_path=folder_path
        )
        if not plan["apply_available"]:
            raise RuntimeError("folder quarantine is blocked: " + ",".join(plan["blocked_reasons"]))
        if confirm_count != plan["item_count"] or confirm_plan_sha256 != plan["plan_sha256"]:
            raise RuntimeError("folder quarantine confirmation is stale")
        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(f"doctor failed before folder quarantine: {issues[0]['kind']}")
            backup = decision_store.backup_state_db(conn, _backup_path(state_db, "folder_quarantine"))
            from dedup_mutations import _ensure_intake_fingerprint, _file_state
            for item in plan["items"]:
                if item["registered"] and item["fingerprint_id"] is None:
                    source_row = _file_state(conn, item["file_id"])
                    evidence = inspect_regular_file(source_row["canonical_path"])
                    existing = conn.execute(
                        """
                        SELECT fingerprint_id, raw_sha256
                        FROM fingerprints
                        WHERE file_id = ? AND canonical_path = ? AND size = ? AND mtime_ns = ?
                          AND raw_sha256 IS NOT NULL
                        ORDER BY fingerprint_id DESC
                        """,
                        (source_row["file_id"], source_row["canonical_path"],
                         source_row["size"], source_row["mtime_ns"]),
                    ).fetchall()
                    reusable = next(
                        (row for row in existing if row["raw_sha256"] == evidence.sha256),
                        None,
                    )
                    if reusable is not None:
                        with decision_store.transaction(conn):
                            conn.execute(
                                "UPDATE files SET current_fingerprint_id = ? WHERE file_id = ? "
                                "AND current_fingerprint_id IS NULL",
                                (reusable["fingerprint_id"], source_row["file_id"]),
                            )
                        fingerprinted = _file_state(conn, item["file_id"])
                    else:
                        fingerprinted = _ensure_intake_fingerprint(conn, source_row)
                    item["fingerprint_id"] = fingerprinted["current_fingerprint_id"]
            decision_store.issue_actual_run_token(conn, str(backup), house_dir=house_dir, temp_dir=temp_dir)
        finally:
            conn.close()
        run_id, manifest_path = decision_store.prepare_actual_run(
            state_db, house_dir, temp_dir,
            manifest_paths=[item["source_path"] for item in plan["items"]],
        )
        try:
            conn = decision_store.connect_state_db(state_db)
            try:
                result = _quarantine_folder(conn, plan=plan, run_id=run_id, manifest_path=manifest_path)
                result["doctor_issues"] = decision_store.doctor_issues(conn, allowed_active_run_id=run_id)
                decision_store.finish_actual_run(conn, run_id, success=True)
            finally:
                conn.close()
        except BaseException as exc:
            conn = decision_store.connect_state_db(state_db)
            try:
                decision_store.finish_actual_run(conn, run_id, success=False, error=str(exc))
            finally:
                conn.close()
            raise
        if progress:
            progress(plan["item_count"], plan["item_count"], Path(folder_path).name)
        try:
            index = _refresh_review_index(state_db=state_db, house_dir=house_dir, index_path=index_path)
        except Exception as exc:
            index = {"index_updated": False, "house_index_synced": False,
                     "warning": f"폴더 격리는 완료됐지만 index 갱신에 실패했습니다: {exc}"}
        output = {**result, "run_id": run_id, "manifest_path": manifest_path,
                  "backup_path": str(backup), **index}
        if result["doctor_issues"]:
            output["_job_state"] = "needs_review"
            output["_job_message"] = "폴더 격리는 완료됐지만 Doctor 검토가 필요합니다"
        return output


def _relocate_managed_folder(
    conn,
    *,
    plan: Mapping[str, object],
    run_id: str,
    manifest_path: str,
) -> dict:
    actual_run = decision_store.assert_active_actual_run(conn, run_id)
    source = Path(str(plan["source_path"]))
    destination = Path(str(plan["destination_path"]))
    decision_store.assert_actual_run_path(actual_run, source, "house_root")
    decision_store.assert_actual_run_path(actual_run, destination, "house_root")
    current_identity = _directory_identity(source)
    expected_identity = (
        plan["folder"]["dev"], plan["folder"]["ino"]
    )
    if current_identity[:2] != expected_identity:
        raise RuntimeError("managed folder identity changed before relocation")

    manifest_json = json.dumps(
        {"items": plan["items"], "directories": plan["directories"]},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    operation_ids: list[int] = []
    with decision_store.transaction(conn):
        group_id = decision_store.create_operation_group(
            conn,
            run_id=run_id,
            action=MANAGED_FOLDER_RELOCATE_ACTION,
            plan_sha256=str(plan["plan_sha256"]),
            source_path=str(source),
            dest_path=str(destination),
            item_count=int(plan["item_count"]),
            manifest_path=manifest_path,
            source_manifest_json=manifest_json,
        )
        conn.execute(
            "UPDATE operation_groups SET source_dev = ?, source_ino = ?, source_ctime_ns = ? "
            "WHERE group_id = ?",
            (*current_identity, group_id),
        )
        for item in plan["items"]:
            if not item["registered"]:
                continue
            operation_ids.append(decision_store.create_operation(
                conn,
                run_id=run_id,
                action=MANAGED_FOLDER_ITEM_ACTION,
                source_path=item["source_path"],
                dest_path=str(destination / item["relative_path"]),
                file_id=item["file_id"],
                expected_size=item["size"],
                expected_mtime_ns=item["mtime_ns"],
                expected_fingerprint_id=item["fingerprint_id"],
                source_dev=item["dev"],
                source_ino=item["ino"],
                source_ctime_ns=item["ctime_ns"],
            ))

    recovered_after_error = None
    try:
        _assert_folder_plan_sources_current(
            conn, actual_run, plan, require_fingerprint=True
        )
        _rename_directory_no_clobber(source, destination)
        destination_identity = _directory_identity(destination)
        if destination_identity[:2] != current_identity[:2]:
            raise RuntimeError("folder inode changed during atomic relocation")
        with decision_store.transaction(conn):
            conn.execute(
                """
                UPDATE operation_groups SET destination_dev = ?, destination_ino = ?,
                    destination_ctime_ns = ?, updated_at = CURRENT_TIMESTAMP
                WHERE group_id = ?
                """,
                (*destination_identity, group_id),
            )
            for operation_id in operation_ids:
                operation = conn.execute(
                    "SELECT dest_path FROM operations WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                evidence = inspect_regular_file(operation["dest_path"])
                decision_store.record_operation_destination(conn, operation_id, evidence)
                decision_store.transition_operation(conn, operation_id, "fs_done")
            decision_store.transition_operation_group(conn, group_id, "fs_done")

        with decision_store.transaction(conn):
            source_pattern = _descendant_pattern(source)
            rows = conn.execute(
                "SELECT file_id, canonical_path FROM files WHERE active = 1 AND source = 'house' "
                "AND canonical_path LIKE ? ESCAPE '\\'",
                (source_pattern,),
            ).fetchall()
            for row in rows:
                relative = Path(row["canonical_path"]).relative_to(source)
                new_path = destination / relative
                evidence = inspect_regular_file(new_path)
                conn.execute(
                    """
                    UPDATE files SET canonical_path = ?, dev = ?, ino = ?, ctime_ns = ?,
                        size = ?, mtime_ns = ?, last_seen_at = CURRENT_TIMESTAMP
                    WHERE file_id = ?
                    """,
                    (
                        str(new_path), evidence.dev, evidence.ino, evidence.ctime_ns,
                        evidence.size, evidence.mtime_ns, row["file_id"],
                    ),
                )
            folder_rows = conn.execute(
                "SELECT folder_id, canonical_path FROM work_folders WHERE state = 'active' "
                "AND (canonical_path = ? OR canonical_path LIKE ? ESCAPE '\\')",
                (str(source), source_pattern),
            ).fetchall()
            for row in folder_rows:
                relative = Path(row["canonical_path"]).relative_to(source)
                new_path = destination / relative
                identity = _directory_identity(new_path)
                conn.execute(
                    "UPDATE work_folders SET canonical_path = ?, dev = ?, ino = ?, ctime_ns = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE folder_id = ?",
                    (str(new_path), *identity, row["folder_id"]),
                )
            for operation_id in operation_ids:
                decision_store.transition_operation(conn, operation_id, "db_done")
            decision_store.transition_operation_group(conn, group_id, "db_done")
        with decision_store.transaction(conn):
            for operation_id in operation_ids:
                decision_store.transition_operation(conn, operation_id, "committed")
            decision_store.transition_operation_group(conn, group_id, "committed")
    except BaseException as exc:
        group = _group_row(conn, group_id)
        if group["state"] in {"planned", "fs_done", "db_done"}:
            outcome = recover_managed_folder_group(conn, group_id)
            if outcome == "committed":
                recovered_after_error = str(exc)
            else:
                raise
        else:
            raise
    return {
        "group_id": group_id,
        "folder_id": int(plan["folder"]["folder_id"]),
        "operation_ids": operation_ids,
        "source_path": str(source),
        "destination_path": str(destination),
        "registered_count": int(plan["registered_count"]),
        "auxiliary_count": int(plan["auxiliary_count"]),
        "recovered_after_error": recovered_after_error,
    }


def apply_managed_folder_relocate(
    state_db: Path,
    *,
    house_dir: Path,
    temp_dir: Path,
    index_path: Path,
    folder_id: int,
    target_parent: str,
    new_name: str | None,
    confirm_count: int,
    confirm_plan_sha256: str,
    progress=None,
) -> dict:
    from library_review import _refresh_review_index

    with mutation_lock_for_roots(
        house_dir, temp_dir, "managed-folder-relocate-1.3.3"
    ):
        plan = managed_folder_relocate_preview(
            state_db,
            house_dir=house_dir,
            folder_id=folder_id,
            target_parent=target_parent,
            new_name=new_name,
        )
        if not plan["apply_available"]:
            raise RuntimeError(
                "managed folder relocation is blocked: " +
                ",".join(plan["blocked_reasons"])
            )
        if (
            confirm_count != plan["item_count"]
            or confirm_plan_sha256 != plan["plan_sha256"]
        ):
            raise RuntimeError("managed folder relocation confirmation is stale")
        conn = decision_store.connect_state_db(state_db)
        try:
            issues = decision_store.doctor_issues(conn)
            if issues:
                raise RuntimeError(
                    f"doctor failed before managed folder relocation: {issues[0]['kind']}"
                )
            backup = decision_store.backup_state_db(
                conn, _backup_path(state_db, "managed_folder_relocate")
            )
            decision_store.issue_actual_run_token(
                conn, str(backup), house_dir=house_dir, temp_dir=temp_dir
            )
        finally:
            conn.close()
        manifest_paths = [item["source_path"] for item in plan["items"]]
        run_id, manifest_path = decision_store.prepare_actual_run(
            state_db, house_dir, temp_dir, manifest_paths=manifest_paths
        )
        try:
            conn = decision_store.connect_state_db(state_db)
            try:
                result = _relocate_managed_folder(
                    conn, plan=plan, run_id=run_id, manifest_path=manifest_path
                )
                result["doctor_issues"] = decision_store.doctor_issues(
                    conn, allowed_active_run_id=run_id
                )
                decision_store.finish_actual_run(conn, run_id, success=True)
            finally:
                conn.close()
        except BaseException as exc:
            conn = decision_store.connect_state_db(state_db)
            try:
                decision_store.finish_actual_run(
                    conn, run_id, success=False, error=str(exc)
                )
            finally:
                conn.close()
            raise
        if progress:
            progress(1, 1, plan["target_name"])
        try:
            index = _refresh_review_index(
                state_db=state_db, house_dir=house_dir, index_path=index_path
            )
        except Exception as exc:
            index = {
                "index_updated": False,
                "house_index_synced": False,
                "warning": f"폴더 이동은 완료됐지만 index 갱신에 실패했습니다: {exc}",
            }
        output = {
            **result,
            "run_id": run_id,
            "manifest_path": manifest_path,
            "backup_path": str(backup),
            **index,
        }
        if result["doctor_issues"]:
            output["_job_state"] = "needs_review"
            output["_job_message"] = "폴더 정리는 완료됐지만 Doctor 검토가 필요합니다"
        return output
