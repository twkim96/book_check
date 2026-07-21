import json
import hashlib
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from normalizer import (
    analyze_name,
    has_pass_marker,
    is_supported_file,
    normalize_nfc,
    read_disambig_marker,
    should_exclude_dir,
    should_exclude_file,
    NORMALIZER_VERSION,
)
from project_paths import FILE_INDEX, FILE_LIST, HOUSE_DIR, STATE_DB

DEFAULT_STATE_DB = str(STATE_DB)

# ==========================================
# [설정] 소설 파일들이 있는 폴더 경로들을 리스트로 입력하세요.
TARGET_DIRECTORIES = [
    str(HOUSE_DIR)
]
# ==========================================


def _default_index_path(output_path):
    return os.path.join(os.path.dirname(output_path), "file_index.json")


def _backup_before_normalizer_reanalysis(conn, state_db_path):
    stale = conn.execute(
        "SELECT COUNT(*) FROM file_analysis WHERE normalizer_version != ?",
        (NORMALIZER_VERSION,),
    ).fetchone()[0]
    if not stale:
        return None
    from decision_store import backup_state_db

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = (
        Path(state_db_path).resolve().parent
        / "backups"
        / f"before_normalizer_rekey_{stamp}_{uuid.uuid4().hex[:8]}.sqlite3"
    )
    backup = backup_state_db(conn, backup_path)
    print(f"  💾 정규화 재분석 전 DB 백업: {backup}")
    return backup


def _build_entry(path, base_dir, entry_type, decision_projection=None, analysis=None):
    name = normalize_nfc(os.path.basename(path))
    rel_path = normalize_nfc(os.path.relpath(path, base_dir))
    info = analysis or analyze_name(name)
    entry = {
        "type": entry_type,
        "name": name,
        "rel_path": rel_path,
        "ext": info["ext"],
        "size": None,
        "core_title": info["core_title"],
        "author": info["author"],
        "max_number": info["max_number"],
        "effective_max": info["effective_max"],
        "unit": info["unit"],
        "start_number": info["start_number"],
        "end_number": info["end_number"],
        "span_ambiguous": info["span_ambiguous"],
        "disambig": info["disambig"],
        "complete": info["complete"],
        "title_override": bool(info.get("title_override")),
    }

    if entry_type == "file":
        try:
            entry["size"] = os.path.getsize(path)
        except OSError:
            entry["size"] = None

        legacy_marker = has_pass_marker(name) or read_disambig_marker(name) > 1
        projection = (decision_projection or {}).get(os.path.abspath(path))
        if projection is None and decision_projection:
            from decision_store import canonicalize_path

            projection = decision_projection.get(canonicalize_path(path))
        if projection:
            assignment_state = projection["assignment_state"]
            entry.update({
                "file_id": projection["file_id"],
                "work_bucket_id": projection["work_bucket_id"],
                "variant_id": projection["variant_id"],
                "assignment_state": assignment_state,
                "protected": bool(projection["protected"]),
                "representative": bool(projection["representative"]),
                "legacy_marker": legacy_marker,
                "legacy_unresolved": assignment_state == "legacy_unresolved",
            })
        else:
            assignment_state = "legacy_unresolved" if legacy_marker else "unassigned"
            entry.update({
                "file_id": None,
                "work_bucket_id": None,
                "variant_id": None,
                "assignment_state": assignment_state,
                "protected": False,
                "representative": False,
                "legacy_marker": legacy_marker,
                "legacy_unresolved": legacy_marker,
            })

    return entry


class IndexSnapshotStale(RuntimeError):
    """The DB/index snapshot cannot prove the current house inventory."""


def _atomic_json_write(path, payload):
    """Replace one JSON surface atomically without exposing a partial index."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _write_index_surfaces(
    entries,
    output_path,
    index_output_path,
    *,
    index_mode="full_scan",
    inventory_revision=None,
):
    files = sorted({entry["name"] for entry in entries})
    payload = {
        "version": 2,
        "normalizer_version": NORMALIZER_VERSION,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "entries": entries,
    }
    if index_mode:
        payload["index_mode"] = index_mode
    if inventory_revision:
        payload["inventory_revision"] = inventory_revision
    _atomic_json_write(output_path, files)
    _atomic_json_write(index_output_path, payload)
    return {
        "ok": True,
        "index_mode": index_mode,
        "file_name_count": len(files),
        "file_entry_count": sum(entry["type"] == "file" for entry in entries),
        "entry_count": len(entries),
        "inventory_revision": inventory_revision,
    }


def _strict_identity_matches(row, stat_result):
    return bool(
        row["size"] == stat_result.st_size
        and row["mtime_ns"] == stat_result.st_mtime_ns
        and (row["dev"] is None or row["dev"] == stat_result.st_dev)
        and (row["ino"] is None or row["ino"] == stat_result.st_ino)
        and (row["ctime_ns"] is None or row["ctime_ns"] == stat_result.st_ctime_ns)
    )


def _analysis_identity_matches(row, name, stat_result):
    return bool(
        row["analysis_normalizer_version"] == NORMALIZER_VERSION
        and row["analyzed_name"] == name
        and row["analyzed_size"] == stat_result.st_size
        and row["analyzed_mtime_ns"] == stat_result.st_mtime_ns
        and (
            row["analyzed_ctime_ns"] is None
            or row["analyzed_ctime_ns"] == stat_result.st_ctime_ns
        )
    )


def _state_projection_rows(conn):
    return conn.execute(
        """
        SELECT f.file_id, f.canonical_path, f.size, f.mtime_ns,
               f.dev, f.ino, f.ctime_ns, f.variant_id,
               f.assignment_state, f.protected,
               f.coordinate_kind, f.episode_start, f.episode_end,
               f.span_ambiguous,
               v.work_bucket_id,
               CASE WHEN r.file_id IS NULL THEN 0 ELSE 1 END AS representative,
               a.normalizer_version AS analysis_normalizer_version,
               a.analyzed_name, a.core_title, a.author,
               a.max_number, a.effective_max, a.unit, a.complete, a.disambig,
               a.analyzed_size, a.analyzed_mtime_ns, a.analyzed_ctime_ns,
               a.title_override_json
        FROM files AS f
        LEFT JOIN variants AS v ON v.variant_id = f.variant_id
        LEFT JOIN representatives AS r ON r.file_id = f.file_id
        LEFT JOIN file_analysis AS a ON a.file_id = f.file_id
        WHERE f.active = 1 AND f.source = 'house'
        ORDER BY f.canonical_path
        """
    ).fetchall()


def build_index_entries_from_state_db(
    house_dir,
    state_db_path,
    *,
    allowed_active_run_id=None,
    verify_doctor=True,
):
    """Project the current index from journaled DB rows after a cheap path walk.

    The walk proves that no supported file was added or removed outside the
    program.  File identity and stored analysis must match exactly.  Any gap is
    fail-closed so the caller can fall back to the complete Scanner reconcile.
    """
    import decision_store

    house_root = Path(house_dir).expanduser().resolve()
    if not house_root.is_dir():
        raise IndexSnapshotStale(f"house root is missing: {house_root}")

    conn = decision_store.connect_state_db(state_db_path)
    try:
        if verify_doctor:
            issues = decision_store.doctor_issues(
                conn, allowed_active_run_id=allowed_active_run_id
            )
            if issues:
                raise IndexSnapshotStale(
                    f"doctor issues: {len(issues)} ({issues[0]['kind']})"
                )
        rows = _state_projection_rows(conn)
    finally:
        conn.close()

    rows_by_path = {row["canonical_path"]: row for row in rows}
    expected_paths = set()
    for row in rows:
        path = Path(row["canonical_path"])
        try:
            relative = path.relative_to(house_root)
        except ValueError as exc:
            raise IndexSnapshotStale(
                f"active house row is outside the configured root: {path}"
            ) from exc
        if (
            any(should_exclude_dir(part) for part in relative.parts[:-1])
            or should_exclude_file(relative.name)
            or not is_supported_file(relative.name)
        ):
            continue
        expected_paths.add(normalize_nfc(str(relative)))

    entries = []
    seen_paths = set()
    revision_rows = []
    base_dir = str(house_root)
    for root, dirs, files in os.walk(house_root, followlinks=False):
        dirs[:] = [directory for directory in dirs if not should_exclude_dir(directory)]
        for filename in files:
            if should_exclude_file(filename) or not is_supported_file(filename):
                continue
            path = Path(root) / filename
            canonical_path = decision_store.canonicalize_path(path)
            row = rows_by_path.get(canonical_path)
            if row is None:
                raise IndexSnapshotStale(
                    f"supported house file is absent from state DB: {path}"
                )
            try:
                stat_result = os.stat(path, follow_symlinks=False)
            except OSError as exc:
                raise IndexSnapshotStale(f"house file cannot be statted: {path}") from exc
            if not _strict_identity_matches(row, stat_result):
                raise IndexSnapshotStale(f"house file identity changed: {path}")
            normalized_name = normalize_nfc(filename)
            if not _analysis_identity_matches(row, normalized_name, stat_result):
                raise IndexSnapshotStale(f"file analysis is stale: {path}")

            analysis = analyze_name(normalized_name)
            analysis.update({
                "core_title": row["core_title"],
                "author": row["author"],
                "max_number": row["max_number"],
                "effective_max": row["effective_max"],
                "unit": row["unit"],
                "complete": bool(row["complete"]),
                "disambig": row["disambig"],
                "title_override": bool(row["title_override_json"]),
            })
            projection = {
                "file_id": row["file_id"],
                "variant_id": row["variant_id"],
                "assignment_state": row["assignment_state"],
                "protected": row["protected"],
                "work_bucket_id": row["work_bucket_id"],
                "representative": row["representative"],
            }
            entries.append(
                _build_entry(
                    str(path), base_dir, "file",
                    {canonical_path: projection}, analysis=analysis,
                )
            )
            relative = normalize_nfc(os.path.relpath(path, house_root))
            seen_paths.add(relative)
            revision_rows.append((
                row["file_id"], relative, stat_result.st_size,
                stat_result.st_mtime_ns, stat_result.st_ctime_ns,
                row["analysis_normalizer_version"], row["core_title"],
                row["variant_id"], row["assignment_state"], row["protected"],
                row["work_bucket_id"], row["representative"],
            ))

        if os.path.abspath(root) != base_dir:
            for directory_name in dirs:
                entries.append(
                    _build_entry(
                        os.path.join(root, directory_name), base_dir, "dir"
                    )
                )

    if seen_paths != expected_paths:
        missing = sorted(expected_paths - seen_paths)[:3]
        extra = sorted(seen_paths - expected_paths)[:3]
        raise IndexSnapshotStale(
            f"house inventory differs from state DB: missing={missing}, extra={extra}"
        )

    entries.sort(key=lambda item: (item["rel_path"], item["name"]))
    revision = hashlib.sha256(
        json.dumps(
            revision_rows, ensure_ascii=False, sort_keys=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return entries, revision


def validate_index_snapshot(
    house_dir,
    index_path,
    state_db_path,
    *,
    allowed_active_run_id=None,
):
    """Return a reusable projected snapshot or a fail-closed fallback reason."""
    try:
        with open(index_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if payload.get("version") != 2 or not isinstance(payload.get("entries"), list):
            raise IndexSnapshotStale("index is not a v2 payload")
        if payload.get("normalizer_version") != NORMALIZER_VERSION:
            raise IndexSnapshotStale("normalizer version changed")
        entries, revision = build_index_entries_from_state_db(
            house_dir,
            state_db_path,
            allowed_active_run_id=allowed_active_run_id,
        )
        if payload["entries"] != entries:
            raise IndexSnapshotStale("index entries differ from the verified DB projection")
        return {
            "valid": True,
            "entries": entries,
            "inventory_revision": revision,
            "reason": None,
        }
    except Exception as exc:
        return {
            "valid": False,
            "entries": None,
            "inventory_revision": None,
            "reason": str(exc),
        }


def generate_file_list_from_state_db(
    house_dir,
    output_path,
    index_output_path,
    state_db_path,
    *,
    allowed_active_run_id=None,
):
    """Write final public indexes without a second Scanner reconciliation."""
    entries, revision = build_index_entries_from_state_db(
        house_dir,
        state_db_path,
        allowed_active_run_id=allowed_active_run_id,
    )
    result = _write_index_surfaces(
        entries,
        output_path,
        index_output_path,
        index_mode="state_db_projection",
        inventory_revision=revision,
    )
    print(
        "\n✅ DB snapshot index 완료: "
        f"파일 {result['file_entry_count']}개, revision={revision[:12]}"
    )
    print(f"👉 {output_path}")
    print(f"👉 {index_output_path}")
    return result


def get_file_entries(
    directory_list,
    progress_interval=1000,
    progress_seconds=5.0,
    state_db_path=None,
):
    directory_list = list(directory_list)
    entries = []
    decision_conn = None
    seen_file_ids = set()
    analysis_rekeys = []
    if state_db_path:
        from decision_store import initialize_state_db

        decision_conn = initialize_state_db(state_db_path)
        _backup_before_normalizer_reanalysis(decision_conn, state_db_path)
        decision_conn.execute("BEGIN IMMEDIATE")
    processed = 0
    last_reported = 0
    last_report_time = time.monotonic()

    try:
        for directory in directory_list:
            print(f"📂 스캔 시작: {directory}")

            if not os.path.exists(directory):
                print(f"⚠️ 경고: 경로를 찾을 수 없습니다 -> {directory}")
                continue

            base_dir = os.path.abspath(directory)

            for root, dirs, files in os.walk(directory):
                dirs[:] = [d for d in dirs if not should_exclude_dir(d)]

                for file in files:
                    processed += 1
                    now = time.monotonic()
                    count_due = progress_interval and processed - last_reported >= progress_interval
                    time_due = progress_seconds and now - last_report_time >= progress_seconds
                    if count_due or time_due:
                        print(f"  ... {processed}개 처리 중")
                        last_reported = processed
                        last_report_time = now
                    if should_exclude_file(file) or not is_supported_file(file):
                        continue
                    path = os.path.join(root, file)
                    projection = None
                    analysis = None
                    if decision_conn is not None:
                        from decision_store import (
                            build_file_analysis,
                            canonicalize_path,
                            reconcile_file_metadata,
                        )

                        name = normalize_nfc(file)
                        analysis = build_file_analysis(name)
                        legacy_marker = has_pass_marker(name) or read_disambig_marker(name) > 1
                        previous = decision_conn.execute(
                            """
                            SELECT f.file_id, a.core_title
                            FROM files AS f
                            JOIN file_analysis AS a ON a.file_id = f.file_id
                            WHERE f.canonical_path = ? AND f.active = 1
                            """,
                            (canonicalize_path(path),),
                        ).fetchone()
                        if previous is not None:
                            from decision_store import build_effective_file_analysis

                            analysis = build_effective_file_analysis(
                                decision_conn, previous["file_id"], name
                            )
                        if previous is not None and previous["core_title"] != analysis["core_title"]:
                            analysis_rekeys.append(
                                (previous["core_title"], analysis["core_title"])
                            )
                        projection = dict(reconcile_file_metadata(
                            decision_conn,
                            path,
                            source="house",
                            legacy_marker=legacy_marker,
                            analysis=analysis,
                        ))
                        stored_analysis = decision_conn.execute(
                            """
                            SELECT core_title, readable_title, catalog_query_title,
                                   author, max_number, effective_max, unit, complete,
                                   disambig, title_override_json
                            FROM file_analysis WHERE file_id = ?
                            """,
                            (projection["file_id"],),
                        ).fetchone()
                        if stored_analysis is not None:
                            analysis.update({
                                "core_title": stored_analysis["core_title"],
                                "readable_title": stored_analysis["readable_title"],
                                "catalog_query_title": stored_analysis["catalog_query_title"],
                                "author": stored_analysis["author"],
                                "max_number": stored_analysis["max_number"],
                                "effective_max": stored_analysis["effective_max"],
                                "unit": stored_analysis["unit"],
                                "complete": bool(stored_analysis["complete"]),
                                "disambig": stored_analysis["disambig"],
                                "title_override": bool(stored_analysis["title_override_json"]),
                            })
                        seen_file_ids.add(projection["file_id"])
                    if projection:
                        from decision_store import canonicalize_path

                        projection_map = {canonicalize_path(path): projection}
                    else:
                        projection_map = None
                    entries.append(_build_entry(
                        path, base_dir, "file", projection_map, analysis=analysis
                    ))

                # 카테고리 루트 폴더 자체는 제외하고, 그 아래 묶음 폴더만 인덱싱한다.
                if os.path.abspath(root) != base_dir:
                    for directory_name in dirs:
                        entries.append(
                            _build_entry(os.path.join(root, directory_name), base_dir, "dir")
                        )
    except Exception:
        if decision_conn is not None:
            decision_conn.rollback()
        raise
    else:
        if decision_conn is not None:
            from decision_store import (
                migrate_catalog_title_keys,
                prune_file_analysis_projection,
            )

            prune_file_analysis_projection(
                decision_conn,
                seen_file_ids=seen_file_ids,
                scanned_roots=directory_list,
            )
            rekey_result = migrate_catalog_title_keys(decision_conn, analysis_rekeys)
            if rekey_result["migrated"]:
                print(
                    "  🔑 정규화 제목 키 이전: "
                    f"{rekey_result['migrated']}개, "
                    f"성공 메타데이터 보존 {rekey_result['successful_rows_preserved']}건, "
                    f"실패 결과 폐기 {rekey_result['failed_rows_discarded']}건"
                )
            decision_conn.commit()
    finally:
        if decision_conn is not None:
            decision_conn.close()

    return sorted(entries, key=lambda item: (item["rel_path"], item["name"]))


def get_file_list(directory_list, state_db_path=None):
    entries = get_file_entries(directory_list, state_db_path=state_db_path)
    return sorted({entry["name"] for entry in entries})


def generate_file_list(
    directory_list,
    output_path,
    index_output_path=None,
    state_db_path=None,
):
    """지정된 디렉토리들을 스캔하여 레거시 목록과 구조화 인덱스를 저장합니다."""
    entries = get_file_entries(directory_list, state_db_path=state_db_path)
    index_output_path = index_output_path or _default_index_path(output_path)

    try:
        if not entries:
            print("\nℹ️ 스캔된 파일이 없습니다. 빈 인덱스를 저장합니다.")
        result = _write_index_surfaces(
            entries,
            output_path,
            index_output_path,
            index_mode="full_scan",
        )
        print(
            f"\n✅ 스캔 완료! 총 {result['file_name_count']}개의 파일 목록을 "
            "다음 위치에 저장했습니다:"
        )
        print(f"👉 {output_path}")
        print(f"👉 {index_output_path}")
        return True
    except PermissionError:
        print(f"\n❌ 오류: '{output_path}' 또는 '{index_output_path}' 경로에 파일을 저장할 권한이 없습니다.")
        return False
    except Exception as e:
        print(f"\n❌ 오류 발생: {e}")
        return False


if __name__ == "__main__":
    default_output_path = str(FILE_LIST)
    default_index_path = str(FILE_INDEX)

    generate_file_list(
        TARGET_DIRECTORIES,
        default_output_path,
        default_index_path,
        state_db_path=DEFAULT_STATE_DB,
    )
    print("이제 크롬 확장에서 file_list.json을 선택하여 업로드하세요.")
