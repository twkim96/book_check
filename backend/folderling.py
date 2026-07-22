import errno
import os
import shutil
import sys
from datetime import datetime

from tqdm import tqdm

from normalizer import (
    add_pass_marker,
    get_chosung,
    is_supported_file,
    materialize_title_markup,
    normalize_filename,
    normalize_nfc,
    should_exclude_dir,
    should_exclude_file,
    strip_pass_marker,
    strip_disambig_marker,
    strip_trash_suffix,
    SUPPORTED_EXTENSIONS,
)
from scanner import (
    IndexSnapshotStale,
    generate_file_list,
    generate_file_list_from_state_db,
    validate_index_snapshot,
)
from deduplicator import clean_duplicates, unique_path
from project_paths import HOUSE_DIR, PROJECT_ROOT, TEMP_DIR


DEFAULT_SRC_DIR = str(TEMP_DIR)
DEFAULT_DST_DIR = str(HOUSE_DIR)
PASS_DIR_NAME = "pass"
UNPACK_DIR_NAME = "unpack"
LEGACY_UNPACK_PREFIX = "___"

# 자모 폴더에 같은 이름의 파일이 이미 있을 때 새 파일에 붙는 충돌 회피 suffix.
# 일반 입고와 pass 입고를 분리하여 의미가 섞이지 않게 한다.
NORMAL_CONFLICT_SUFFIX = "_dup"
PASS_CONFLICT_SUFFIX = "_pass"
EXTENSION_INDEX_PATH = os.path.join("extension", "file_index.json")
HOUSE_INDEX_FILENAME = "file_index.json"


class VolumeCoordinateConflict(RuntimeError):
    """One incoming volume overlaps an existing coordinate and must be held."""

    def __init__(self, decision):
        self.decision = dict(decision)
        coordinate = (
            self.decision.get("coordinate_kind"),
            self.decision.get("coordinate_num"),
            self.decision.get("coordinate_den"),
        )
        super().__init__(f"existing volume coordinate conflict: {coordinate}")


def get_now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def emit_folderling_event(event_callback, phase, **payload):
    """Publish one structured event without coupling Folderling to the web UI."""
    if event_callback is not None:
        event_callback({"phase": phase, **payload})


def create_recent_link(dst_path, clean_name, recent_dir):
    """최근 파일에 대한 심볼릭 링크 생성"""
    from mutation_io import ensure_directory_nofollow
    ensure_directory_nofollow(recent_dir)
    link_path = os.path.join(recent_dir, clean_name)
    abs_dst_path = os.path.abspath(dst_path)

    if os.path.lexists(link_path):
        raise FileExistsError(
            f"_최근 기존 경로는 소유권을 증명할 수 없어 보존합니다: {link_path}"
        )

    os.symlink(abs_dst_path, link_path)


def ensure_recent_link_slot(clean_name, recent_dir):
    """Fail before intake when a user-owned non-link occupies the recent path."""
    link_path = os.path.join(recent_dir, clean_name)
    if os.path.lexists(link_path):
        raise FileExistsError(
            f"_최근 기존 경로는 소유권을 증명할 수 없어 입고를 중단합니다: {link_path}"
        )


def cleanup_recent_links(recent_dir, max_days=30):
    """소유권 정보가 없는 기존 recent link를 보존한다."""
    if not os.path.exists(recent_dir):
        return

    for item in os.listdir(recent_dir):
        link_path = os.path.join(recent_dir, item)

        if os.path.islink(link_path):
            # 1.2.2: 기존 링크의 creator identity가 없으므로 사용자 데이터로 보존한다.
            continue


def parse_args(argv):
    src_dir = DEFAULT_SRC_DIR
    dst_dir = DEFAULT_DST_DIR

    for i, arg in enumerate(argv):
        if arg == "--src" and i + 1 < len(argv):
            src_dir = argv[i + 1]
        elif arg == "--dst" and i + 1 < len(argv):
            dst_dir = argv[i + 1]

    return src_dir, dst_dir


def should_skip_source_item(item):
    return should_exclude_dir(item)


def is_unpack_source_dir(name):
    """Recognize the explicit unpack inbox and legacy ``___*`` wrappers."""
    normalized = normalize_nfc(name).strip()
    return bool(normalized) and (
        normalized.casefold() == UNPACK_DIR_NAME
        or normalized.startswith(LEGACY_UNPACK_PREFIX)
    )


def _unpack_roots(src_dir):
    roots = []
    if not os.path.isdir(src_dir):
        return roots
    for name in sorted(os.listdir(src_dir)):
        path = os.path.join(src_dir, name)
        if os.path.isdir(path) and not os.path.islink(path) and is_unpack_source_dir(name):
            roots.append((name, path, name.strip().casefold() == UNPACK_DIR_NAME))
    return roots


def iter_unpack_supported_files(root):
    """Yield supported regular files below one unpack wrapper in stable order."""
    paths = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = [
            name for name in sorted(directories)
            if not should_exclude_dir(name)
            and not os.path.islink(os.path.join(current, name))
        ]
        for filename in sorted(filenames):
            path = os.path.join(current, filename)
            if (
                should_exclude_file(filename)
                or not is_supported_file(filename)
                or os.path.islink(path)
                or not os.path.isfile(path)
            ):
                continue
            paths.append(path)
    return paths


def _tree_has_symlink(root):
    for current, directories, filenames in os.walk(root, followlinks=False):
        if any(os.path.islink(os.path.join(current, name)) for name in directories):
            return True
        if any(os.path.islink(os.path.join(current, name)) for name in filenames):
            return True
    return False


def _tree_file_stats(root):
    count = 0
    size = 0
    for current, _, filenames in os.walk(root, followlinks=False):
        for filename in filenames:
            path = os.path.join(current, filename)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            count += 1
            try:
                size += os.path.getsize(path)
            except OSError:
                pass
    return count, size


def _cleanup_unpack_tree_owned(root, *, reusable):
    """Delete only entries observed in one no-follow snapshot.

    A file arriving after the snapshot is never passed to unlink.  It instead
    keeps a directory non-empty (or leaves the reusable inbox non-empty), so the
    caller reports cleanup_failed and preserves the late arrival.
    """
    root = os.path.abspath(root)
    files = []
    directories = []
    for current, child_dirs, filenames in os.walk(root, topdown=True, followlinks=False):
        for name in child_dirs:
            path = os.path.join(current, name)
            info = os.lstat(path)
            if not os.path.isdir(path) or os.path.islink(path):
                raise OSError(f"unpack directory is not owned safely: {path}")
            directories.append((path, info.st_dev, info.st_ino))
        for name in filenames:
            path = os.path.join(current, name)
            info = os.lstat(path)
            if not os.path.isfile(path) or os.path.islink(path):
                raise OSError(f"unpack file is not owned safely: {path}")
            files.append((
                path, info.st_dev, info.st_ino, info.st_ctime_ns,
                info.st_size, info.st_mtime_ns,
            ))

    removed_files = 0
    removed_bytes = 0
    for path, dev, ino, ctime_ns, size, mtime_ns in files:
        current = os.lstat(path)
        if (
            current.st_dev, current.st_ino, current.st_ctime_ns,
            current.st_size, current.st_mtime_ns,
        ) != (dev, ino, ctime_ns, size, mtime_ns):
            raise OSError(f"unpack file changed during cleanup: {path}")
        os.unlink(path)
        removed_files += 1
        removed_bytes += size

    for path, dev, ino in sorted(
        directories, key=lambda item: len(item[0].split(os.sep)), reverse=True
    ):
        current = os.lstat(path)
        if (current.st_dev, current.st_ino) != (dev, ino):
            raise OSError(f"unpack directory changed during cleanup: {path}")
        os.rmdir(path)

    if reusable:
        if os.listdir(root):
            raise OSError("unpack inbox changed during cleanup")
    else:
        os.rmdir(root)
    return removed_files, removed_bytes


def cleanup_unpack_sources(src_dir):
    """Discard unpack wrappers only after every supported file left safely.

    ``txt_temp/unpack`` remains as the reusable inbox. Legacy ``___*`` wrapper
    directories are removed completely. Unsupported cover/archive assets are
    intentionally discarded with the wrapper, matching the historic contract.
    A symlink or any remaining supported file fails closed and preserves the
    complete remaining tree for inspection or retry.
    """
    results = []
    for name, root, reusable in _unpack_roots(src_dir):
        remaining = iter_unpack_supported_files(root)
        if remaining:
            results.append({
                "name": name,
                "path": root,
                "status": "pending_supported_files",
                "remaining_supported": len(remaining),
                "discarded_files": 0,
                "discarded_bytes": 0,
            })
            continue
        if _tree_has_symlink(root):
            results.append({
                "name": name,
                "path": root,
                "status": "symlink_blocked",
                "remaining_supported": 0,
                "discarded_files": 0,
                "discarded_bytes": 0,
            })
            continue
        try:
            discarded_files, discarded_bytes = _cleanup_unpack_tree_owned(
                root, reusable=reusable
            )
            status = "cleaned"
        except OSError as exc:
            status = "cleanup_failed"
            results.append({
                "name": name,
                "path": root,
                "status": status,
                "remaining_supported": 0,
                "discarded_files": 0,
                "discarded_bytes": 0,
                "error": str(exc),
            })
            continue
        results.append({
            "name": name,
            "path": root,
            "status": status,
            "remaining_supported": 0,
            "discarded_files": discarded_files,
            "discarded_bytes": discarded_bytes,
        })
    return results


def prune_empty_intake_tree(path):
    """Remove only empty directories below one successfully handled temp item.

    Folder intake journals files individually so its source directory shells can
    remain after every payload file reaches house. Walk bottom-up without
    following links and remove only directories empty at removal time.
    """
    path = os.path.abspath(path)
    if os.path.islink(path) or not os.path.isdir(path):
        return 0

    removed = 0
    for current, _, _ in os.walk(path, topdown=False, followlinks=False):
        if os.path.islink(current):
            continue
        try:
            os.rmdir(current)
            removed += 1
        except OSError as exc:
            if exc.errno in {errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT}:
                continue
            raise
    return removed


def directory_has_files(path):
    """Return whether a directory tree contains any file-like intake payload."""
    for _, _, files in os.walk(path, followlinks=False):
        if files:
            return True
    return False


def sync_extension_index(file_index_json, script_dir):
    extension_index_json = os.path.join(script_dir, EXTENSION_INDEX_PATH)
    extension_dir = os.path.dirname(extension_index_json)

    if not os.path.isdir(extension_dir):
        print("⚠️ 확장 폴더를 찾을 수 없어 브라우저 확장용 인덱스 복사를 건너뜁니다.")
        return False

    shutil.copy2(file_index_json, extension_index_json)
    print(f"✨ 브라우저 확장용 인덱스 동기화 완료: {extension_index_json}")
    return True


def sync_house_index(file_index_json, dst_dir):
    house_index_json = os.path.join(dst_dir, HOUSE_INDEX_FILENAME)
    if os.path.abspath(file_index_json) == os.path.abspath(house_index_json):
        print(f"✨ txt_house 인덱스 최신 상태: {house_index_json}")
        return True

    shutil.copy2(file_index_json, house_index_json)
    print(f"✨ txt_house 인덱스 동기화 완료: {house_index_json}")
    return True


def move_to_house(
    src_path, dst_dir, recent_dir, clean_name, s_log, source_label,
    is_pass=False, state_db_path=None, run_id=None,
):
    # 자모 폴더는 마커(〔P〕/〔Dn〕)가 없는 원래 제목 첫 글자로 결정한다.
    sort_name = strip_disambig_marker(strip_pass_marker(clean_name))
    first_char = sort_name[0] if sort_name else clean_name[0]
    folder_name = get_chosung(first_char)

    # pass 경유 입고는 파일명 자체에 마커를 새겨, 다음 회차 dedup에서 제외되게 한다.
    final_name_candidate = add_pass_marker(clean_name) if is_pass else clean_name

    # 충돌 시 붙는 suffix를 일반/pass로 분리한다.
    # - 일반 입고 충돌: _dup_N (원래 자리에 같은 이름이 이미 있다는 의미)
    # - pass 입고 충돌: _pass_N (사용자가 명시적으로 통과시킨 항목)
    conflict_suffix = PASS_CONFLICT_SUFFIX if is_pass else NORMAL_CONFLICT_SUFFIX

    target_folder = os.path.join(dst_dir, folder_name)
    auto_volume = None
    explicit_route = None
    if state_db_path and os.path.isfile(src_path) and not is_pass:
        import decision_store
        from library_work_management import resolve_work_route
        from volume_group_mutations import classify_folderling_volume_target

        conn = decision_store.connect_state_db(state_db_path)
        try:
            source_row = conn.execute(
                """
                SELECT f.file_id, fa.core_title, fa.readable_title
                FROM files AS f
                LEFT JOIN file_analysis AS fa ON fa.file_id = f.file_id
                WHERE f.canonical_path = ? AND f.active = 1
                """,
                (decision_store.canonicalize_path(src_path),),
            ).fetchone()
            if source_row is not None:
                explicit_route = resolve_work_route(
                    conn,
                    core_title=source_row["core_title"],
                    readable_title=source_row["readable_title"],
                    folder_name=os.path.basename(os.path.dirname(src_path)),
                )
                if explicit_route["status"] == "target":
                    target_folder = explicit_route["target_folder"]
                elif explicit_route["status"] == "route_conflict":
                    raise RuntimeError(
                        "사람 지정 작품 alias가 서로 다른 작품을 가리킵니다: "
                        f"works={explicit_route['work_bucket_ids']}"
                    )
                elif explicit_route.get("matched"):
                    raise RuntimeError(
                        "사람 지정 작품 alias는 있지만 활성 목적 폴더가 없습니다: "
                        f"work={explicit_route.get('work_bucket_id')}"
                    )
                else:
                    explicit_route = None
                    volume_decision = classify_folderling_volume_target(
                        conn,
                        source_file_id=source_row["file_id"],
                        house_root=dst_dir,
                    )
                    if volume_decision["status"] == "coordinate_conflict":
                        raise VolumeCoordinateConflict(volume_decision)
                    if volume_decision["status"] == "target":
                        auto_volume = volume_decision
        finally:
            conn.close()
        if auto_volume is not None:
            proposed = os.path.join(auto_volume["target_folder"], final_name_candidate)
            if os.path.exists(proposed) or os.path.islink(proposed):
                auto_volume = None
            else:
                target_folder = auto_volume["target_folder"]
    candidate_path = os.path.join(target_folder, final_name_candidate)
    if os.path.exists(candidate_path):
        if explicit_route is not None:
            raise FileExistsError(
                "explicit work route destination already exists; "
                f"manual variant review required: {candidate_path}"
            )
        if os.path.isdir(src_path):
            raise FileExistsError(
                f"directory intake destination already exists; manual review required: {candidate_path}"
            )
        dst_path = unique_path(target_folder, final_name_candidate, conflict_suffix)
    else:
        dst_path = candidate_path
    final_name = os.path.basename(dst_path)

    ensure_recent_link_slot(final_name, recent_dir)

    from mutation_io import ensure_directory_nofollow
    ensure_directory_nofollow(target_folder)
    if state_db_path and os.path.isfile(src_path):
        import decision_store
        from dedup_mutations import ingest_to_house
        from volume_group_mutations import (
            ensure_volume_fingerprints,
            link_volume_relationships,
        )

        conn = decision_store.connect_state_db(state_db_path)
        try:
            row = conn.execute(
                "SELECT file_id FROM files WHERE canonical_path = ? AND active = 1",
                (decision_store.canonicalize_path(src_path),),
            ).fetchone()
            if row is None:
                raise RuntimeError("temp file is not reconciled in the decision DB")
            ingest_result = ingest_to_house(
                conn,
                source_file_id=row["file_id"],
                destination=dst_path,
                run_id=run_id or f"folderling-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
                routing=explicit_route,
            )
            if explicit_route is not None:
                relationship = ingest_result["routing"]
                source_label += (
                    f"[work-route work={relationship['work_bucket_id']} "
                    f"alias={relationship['alias_id']}] "
                )
            elif auto_volume is not None:
                volume_file_ids = auto_volume["existing_file_ids"] + [row["file_id"]]
                ensure_volume_fingerprints(conn, volume_file_ids)
                with decision_store.transaction(conn):
                    relationship = link_volume_relationships(
                        conn,
                        file_ids=volume_file_ids,
                        display_title=auto_volume["display_title"],
                        origin="strong_match",
                    )
                source_label += (
                    f"[volume-auto work={relationship['work_bucket_id']} "
                    f"core={auto_volume['core_title']}] "
                )
        finally:
            conn.close()
    elif state_db_path and os.path.isdir(src_path):
        import decision_store
        from dedup_mutations import ingest_to_house

        conn = decision_store.connect_state_db(state_db_path)
        try:
            source_files = []
            for root, _, files in os.walk(src_path):
                for filename in files:
                    source_files.append(os.path.join(root, filename))
            for source_file in sorted(source_files):
                relative = os.path.relpath(source_file, src_path)
                destination_file = os.path.join(dst_path, relative)
                canonical_source = decision_store.canonicalize_path(source_file)
                row = conn.execute(
                    "SELECT file_id FROM files WHERE canonical_path = ? AND active = 1",
                    (canonical_source,),
                ).fetchone()
                if row is None:
                    # 표지 등 scanner 비대상 파일도 직접 shutil.move하지 않는다.
                    # stable ID와 raw fingerprint를 만든 뒤 같은 journal sink로 입고한다.
                    with decision_store.transaction(conn):
                        row = decision_store.reconcile_file_metadata(
                            conn, source_file, source="temp"
                        )
                ingest_to_house(
                    conn,
                    source_file_id=row["file_id"],
                    destination=destination_file,
                    run_id=run_id or f"folderling-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
                )
        finally:
            conn.close()
    else:
        raise RuntimeError("journaled state DB and active run are required for house intake")
    pass_flag = "pass=Y" if is_pass else "pass=N"
    s_log.write(
        f"[{get_now()}] {pass_flag} {source_label}{src_path} -> {dst_path}\n"
    )
    create_recent_link(dst_path, final_name, recent_dir)

    return dst_path
def iter_process_items(src_dir, pass_dir):
    normal_items = []
    unpack_items = []

    if os.path.exists(src_dir):
        for item in sorted(os.listdir(src_dir)):
            if item == PASS_DIR_NAME:
                continue
            item_path = os.path.join(src_dir, item)
            if is_unpack_source_dir(item):
                if os.path.isdir(item_path) and not os.path.islink(item_path):
                    unpack_items.extend(
                        (os.path.basename(path), path, False)
                        for path in iter_unpack_supported_files(item_path)
                    )
                continue
            normal_items.append((item, item_path, False))

    # 1.2.1: pass/는 더 이상 사람 판정 입력이 아니다. 기존 내용은 손대지 않고
    # dedup_decisions.py 사용 안내만 출력하며 폴더링 대상에서 제외한다.
    return normal_items + unpack_items


def _process_items_authorized(
    src_dir,
    dst_dir,
    script_dir,
    actual_run_id,
    manifest_path,
    *,
    event_callback=None,
):
    recent_dir = os.path.join(dst_dir, "_최근")
    success_log = os.path.join(script_dir, "success.log")
    fail_log = os.path.join(script_dir, "fail.log")
    file_list_json = os.path.join(script_dir, "file_list.json")
    file_index_json = os.path.join(script_dir, "file_index.json")
    pass_dir = os.path.join(src_dir, PASS_DIR_NAME)
    state_db_path = os.path.join(script_dir, ".dedup_state", "dedup_decisions.sqlite3")

    intake_run_id = actual_run_id
    print(f"🔐 일회성 actual 승인 소비: {actual_run_id}")
    print(f"🧾 실행 전 manifest: {manifest_path}")
    emit_folderling_event(
        event_callback,
        "workflow_started",
        run_id=actual_run_id,
        manifest_path=str(manifest_path),
        source_root=os.path.abspath(src_dir),
        destination_root=os.path.abspath(dst_dir),
    )

    from mutation_io import ensure_directory_nofollow
    ensure_directory_nofollow(dst_dir)

    if os.path.isdir(pass_dir):
        legacy_pass_count = len(os.listdir(pass_dir))
        if legacy_pass_count:
            print(
                f"⚠️ legacy pass/ 항목 {legacy_pass_count}개는 자동 입고하지 않습니다. "
                "dedup_decisions.py로 pair 판정을 등록하세요."
            )
            emit_folderling_event(
                event_callback,
                "legacy_pass_skipped",
                status="needs_review",
                item_count=legacy_pass_count,
                source_path=os.path.abspath(pass_dir),
                reason="legacy_pass_requires_pair_decision",
            )

    # ``unpack``과 기존 ``___*`` 묶음은 dedup/auditor에는 일반 temp 파일로
    # 참여하고, 아래 intake 단계에서 지원 파일만 개별 항목으로 펼쳐 입고한다.

    # ── 1단계: 중복 제거 + 검토 큐 격리 (house + temp 통합 스캔) ──
    print("=" * 60)
    print("📦 1단계: 중복/검토 큐 정리 (house + temp 통합)")
    print("=" * 60)
    emit_folderling_event(
        event_callback,
        "dedup_start",
        status="running",
        source_root=os.path.abspath(src_dir),
        house_root=os.path.abspath(dst_dir),
    )
    snapshot = validate_index_snapshot(
        dst_dir,
        file_index_json,
        state_db_path,
        allowed_active_run_id=actual_run_id,
    )
    pre_index_mode = "verified_snapshot" if snapshot["valid"] else "full_scan_fallback"
    emit_folderling_event(
        event_callback,
        "snapshot_result",
        status="succeeded" if snapshot["valid"] else "fallback",
        index_mode=pre_index_mode,
        inventory_revision=snapshot.get("inventory_revision"),
        fallback_reason=snapshot.get("reason"),
    )
    if snapshot["valid"]:
        print(
            "⚡ 기존 house index 재사용: "
            f"revision={snapshot['inventory_revision'][:12]}"
        )
    else:
        print(
            "🔄 house 전체 Scanner fallback: "
            f"{snapshot['reason']}"
        )
    dedup_summary = clean_duplicates(
        house_dir=dst_dir,
        temp_dir=src_dir,
        dry_run=False,
        index_path=file_index_json,
        rescan=not snapshot["valid"],
        move_suspects=True,
        delete_exact=True,
        include_temp=True,
        audit_suspects=True,
        update_index_after_run=False,
        state_db_path=state_db_path,
        require_state_db=True,
        authorized_run_id=actual_run_id,
        event_callback=event_callback,
    )
    emit_folderling_event(
        event_callback,
        "dedup_result",
        status=(
            "needs_review"
            if dedup_summary.get("review_queue_move_count", 0)
            or dedup_summary.get("managed_report_only_count", 0)
            else "succeeded"
        ),
        **{
            key: value
            for key, value in dedup_summary.items()
            if key not in {"pure_plan", "write_surfaces"}
        },
    )

    # ── 2단계: 폴더링 (temp에 남아있는 파일을 house로 이동) ──
    print("=" * 60)
    print("📂 2단계: 폴더링 (temp → house)")
    print("=" * 60)
    move_count = 0
    pass_count = 0
    failure_count = 0
    empty_dir_cleanup_count = 0
    volume_conflict_hold_count = 0
    unpack_cleanup_results = []
    unpack_cleanup_issue_count = 0
    unpack_discarded_file_count = 0
    unpack_discarded_bytes = 0

    with open(success_log, "w", encoding="utf-8") as s_log, \
         open(fail_log, "w", encoding="utf-8") as f_log:

        items = iter_process_items(src_dir, pass_dir)
        item_total = len(items)
        emit_folderling_event(
            event_callback,
            "intake_start",
            status="running",
            total=item_total,
        )

        for item_index, (item, src_path, is_pass) in enumerate(
            tqdm(items, desc="분류 및 이동 중"), start=1
        ):
            if not is_pass and should_skip_source_item(item):
                emit_folderling_event(
                    event_callback,
                    "file_result",
                    stage="intake",
                    status="skipped",
                    reason="excluded_source_item",
                    source_path=os.path.abspath(src_path),
                    source_name=item,
                    completed=item_index,
                    total=item_total,
                )
                continue

            now_str = get_now()

            try:
                # 휴지통 꼬리표(_suspect_N / _dup_N / _pass_N)는 어디서 발견되든
                # normalize_filename(_→공백 치환)이 돌기 전에 미리 떼어낸다.
                raw_name = strip_trash_suffix(item)

                transport_name = normalize_filename(raw_name)
                if not transport_name:
                    failure_count += 1
                    f_log.write(
                        f"[{now_str}] {src_path} | 이름이 비어있어 실패 | "
                        "조치: 원본 파일명을 확인하고 직접 입고하거나 삭제\n"
                    )
                    emit_folderling_event(
                        event_callback,
                        "file_result",
                        stage="intake",
                        status="failed",
                        reason="empty_normalized_name",
                        source_path=os.path.abspath(src_path),
                        source_name=item,
                        error="정규화 후 파일명이 비었습니다.",
                        next_action="원본 파일명을 확인하고 다시 입고",
                        completed=item_index,
                        total=item_total,
                    )
                    continue

                is_dir = os.path.isdir(src_path)
                is_file = os.path.isfile(src_path)
                # ``[[...]]`` 제목 literal과 ``{{...}}`` 구조 힌트는 temp의
                # 새 file_id로 분석 의도를 운반한다. 중복 감사가 끝난 뒤 house
                # 표시 파일명에서는 운반용 괄호만 제거한다.
                clean_name = (
                    materialize_title_markup(transport_name)
                    if is_file else transport_name
                )
                ext = os.path.splitext(clean_name)[1].lower()

                if is_file and ext not in SUPPORTED_EXTENSIONS:
                    emit_folderling_event(
                        event_callback,
                        "file_result",
                        stage="intake",
                        status="skipped",
                        reason="unsupported_extension",
                        source_path=os.path.abspath(src_path),
                        source_name=item,
                        extension=ext,
                        completed=item_index,
                        total=item_total,
                    )
                    continue
                if not is_dir and not is_file:
                    emit_folderling_event(
                        event_callback,
                        "file_result",
                        stage="intake",
                        status="skipped",
                        reason="source_missing_or_not_regular",
                        source_path=os.path.abspath(src_path),
                        source_name=item,
                        completed=item_index,
                        total=item_total,
                    )
                    continue
                if is_dir and not directory_has_files(src_path):
                    removed = prune_empty_intake_tree(src_path)
                    empty_dir_cleanup_count += removed
                    s_log.write(
                        f"[{now_str}] [empty-dir] {src_path} | "
                        f"빈 디렉터리 {removed}개 정리\n"
                    )
                    emit_folderling_event(
                        event_callback,
                        "file_result",
                        stage="intake",
                        status="empty_directory_cleaned",
                        reason="empty_directory",
                        source_path=os.path.abspath(src_path),
                        source_name=item,
                        removed_directories=removed,
                        completed=item_index,
                        total=item_total,
                    )
                    continue

                label = "[pass] " if is_pass else ""
                destination_path = move_to_house(
                    src_path, dst_dir, recent_dir, clean_name, s_log, label,
                    is_pass=is_pass, state_db_path=state_db_path, run_id=intake_run_id,
                )
                if is_dir:
                    empty_dir_cleanup_count += prune_empty_intake_tree(src_path)

                if is_pass:
                    pass_count += 1
                else:
                    move_count += 1
                emit_folderling_event(
                    event_callback,
                    "file_result",
                    stage="intake",
                    status="pass_ingested" if is_pass else "ingested",
                    reason="journaled_house_ingest",
                    source_path=os.path.abspath(src_path),
                    source_name=item,
                    destination_path=os.path.abspath(destination_path),
                    source_type="directory" if is_dir else "file",
                    completed=item_index,
                    total=item_total,
                )

            except VolumeCoordinateConflict as conflict_exc:
                try:
                    import decision_store
                    from volume_group_mutations import hold_folderling_volume_conflict

                    conn = decision_store.connect_state_db(state_db_path)
                    try:
                        source_row = conn.execute(
                            "SELECT file_id FROM files "
                            "WHERE canonical_path = ? AND active = 1 AND source = 'temp'",
                            (decision_store.canonicalize_path(src_path),),
                        ).fetchone()
                        if source_row is None:
                            raise RuntimeError(
                                "volume coordinate conflict source is not active in temp"
                            )
                        held = hold_folderling_volume_conflict(
                            conn,
                            source_file_id=source_row["file_id"],
                            temp_root=src_dir,
                            run_id=actual_run_id,
                            conflict=conflict_exc.decision,
                        )
                    finally:
                        conn.close()
                    volume_conflict_hold_count += 1
                    conflicts = ", ".join(held["conflicting_paths"])
                    s_log.write(
                        f"[{now_str}] [volume-coordinate-hold] "
                        f"{held['source_path']} -> {held['dest_path']} | "
                        f"existing={conflicts}\n"
                    )
                    print(
                        "  ⚠️ 동일 권 좌표 보류: "
                        f"{os.path.basename(held['source_path'])} "
                        f"→ {held['dest_path']}"
                    )
                    emit_folderling_event(
                        event_callback,
                        "file_result",
                        stage="intake",
                        status="warning",
                        reason="volume_coordinate_conflict",
                        source_path=str(held["source_path"]),
                        source_name=os.path.basename(str(held["source_path"])),
                        destination_path=str(held["dest_path"]),
                        existing_paths=list(held["conflicting_paths"]),
                        operation_id=held["operation_id"],
                        completed=item_index,
                        total=item_total,
                        next_action="기존 동일 권 파일과 직접 비교",
                    )
                except Exception as hold_exc:
                    failure_count += 1
                    f_log.write(
                        f"[{now_str}] {src_path} | 동일 권 좌표 보류 실패: {hold_exc} | "
                        "조치: 원본과 기존 동일 권 파일을 직접 비교\n"
                    )
                    emit_folderling_event(
                        event_callback,
                        "file_result",
                        stage="intake",
                        status="failed",
                        reason="volume_coordinate_hold_failed",
                        source_path=os.path.abspath(src_path),
                        source_name=item,
                        existing_paths=list(
                            conflict_exc.decision.get("conflicting_paths") or ()
                        ),
                        error=str(hold_exc),
                        completed=item_index,
                        total=item_total,
                        next_action="원본과 기존 동일 권 파일을 직접 비교",
                    )
            except Exception as e:
                failure_count += 1
                f_log.write(
                    f"[{now_str}] {src_path} | 예외: {e} | "
                    "조치: 경로/권한 확인 후 재실행 또는 수동 입고\n"
                )
                emit_folderling_event(
                    event_callback,
                    "file_result",
                    stage="intake",
                    status="failed",
                    reason=type(e).__name__,
                    source_path=os.path.abspath(src_path),
                    source_name=item,
                    error=str(e),
                    completed=item_index,
                    total=item_total,
                    next_action="경로·권한 확인 후 재실행",
                )

        unpack_cleanup_results = cleanup_unpack_sources(src_dir)
        for cleanup in unpack_cleanup_results:
            status = cleanup["status"]
            if status == "cleaned":
                unpack_discarded_file_count += cleanup["discarded_files"]
                unpack_discarded_bytes += cleanup["discarded_bytes"]
                s_log.write(
                    f"[{get_now()}] [unpack-cleanup] {cleanup['path']} | "
                    f"부속 파일 {cleanup['discarded_files']}개 "
                    f"({cleanup['discarded_bytes']} bytes) 삭제\n"
                )
            else:
                unpack_cleanup_issue_count += 1
                f_log.write(
                    f"[{get_now()}] [unpack-preserved] {cleanup['path']} | "
                    f"status={status} "
                    f"remaining_supported={cleanup['remaining_supported']} "
                    f"error={cleanup.get('error', '')}\n"
                )
            emit_folderling_event(
                event_callback,
                "unpack_cleanup",
                status="succeeded" if status == "cleaned" else "needs_review",
                source_path=os.path.abspath(cleanup["path"]),
                source_name=cleanup["name"],
                cleanup_status=status,
                remaining_supported=cleanup["remaining_supported"],
                discarded_files=cleanup["discarded_files"],
                discarded_bytes=cleanup["discarded_bytes"],
                error=cleanup.get("error"),
                next_action=(
                    None if status == "cleaned"
                    else "남은 지원 파일 또는 심볼릭 링크를 확인한 뒤 재실행"
                ),
            )

        emit_folderling_event(
            event_callback,
            "intake_result",
            status=(
                "needs_review"
                if failure_count or volume_conflict_hold_count or unpack_cleanup_issue_count
                else "succeeded"
            ),
            total=item_total,
            move_count=move_count,
            pass_count=pass_count,
            failure_count=failure_count,
            volume_conflict_hold_count=volume_conflict_hold_count,
            empty_dir_cleanup_count=empty_dir_cleanup_count,
            unpack_cleanup_issue_count=unpack_cleanup_issue_count,
            unpack_discarded_file_count=unpack_discarded_file_count,
            unpack_discarded_bytes=unpack_discarded_bytes,
        )

    cleanup_recent_links(recent_dir, max_days=30)

    print(f"✅ 폴더링 완료: 입고 {move_count}개")
    if empty_dir_cleanup_count:
        print(f"  - temp 빈 디렉터리 {empty_dir_cleanup_count}개 정리")
    if pass_count > 0:
        print(f"  - pass 폴더 승인 항목 {pass_count}개 강제 입고됨")
    if volume_conflict_hold_count:
        print(f"  - 동일 권 좌표 보류 {volume_conflict_hold_count}개")
    if unpack_discarded_file_count:
        print(
            "  - unpack 부속 파일 삭제 "
            f"{unpack_discarded_file_count}개 ({unpack_discarded_bytes} bytes)"
        )
    if unpack_cleanup_issue_count:
        print(f"  - unpack 정리 보류 {unpack_cleanup_issue_count}개 묶음")
    print(f"→ 로그 파일({script_dir} 위치)을 확인하세요.")
    print("  - success.log / fail.log")

    # ── 3단계: 인덱스 갱신 ──
    print()
    print("=" * 60)
    print("🔄 3단계: 인덱스 갱신")
    print("=" * 60)
    emit_folderling_event(
        event_callback,
        "index_start",
        status="running",
    )
    index_mode = "state_db_projection"
    index_fallback_reason = None
    index_ready = False
    index_error = None
    index_deployment_error = None
    try:
        projection = generate_file_list_from_state_db(
            dst_dir,
            file_list_json,
            file_index_json,
            state_db_path,
            allowed_active_run_id=actual_run_id,
        )
        if not projection["ok"]:
            raise RuntimeError("DB snapshot index generation returned failure")
        index_ready = True
        print("✨ file_list.json / file_index.json 증분 projection 완료")
    except IndexSnapshotStale as exc:
        index_mode = "full_scan_fallback"
        index_fallback_reason = str(exc)
        print(f"🔄 DB snapshot 검증 실패, 전체 Scanner fallback: {exc}")
        try:
            index_ok = generate_file_list(
                [dst_dir],
                file_list_json,
                file_index_json,
                state_db_path=state_db_path,
            )
            if not index_ok:
                raise RuntimeError("scanner index generation returned failure")
            index_ready = True
            print("✨ file_list.json / file_index.json 전체 갱신 완료")
        except Exception as fallback_exc:
            failure_count += 1
            index_error = str(fallback_exc)
            print(f"⚠️ 파일 인덱스 fallback 중 에러가 발생했습니다: {fallback_exc}")
    except Exception as e:
        failure_count += 1
        index_error = str(e)
        print(f"⚠️ 파일 인덱스 업데이트 중 에러가 발생했습니다: {e}")

    if index_ready:
        try:
            if not sync_house_index(file_index_json, dst_dir):
                raise RuntimeError("house index sync failed")
            if not sync_extension_index(file_index_json, script_dir):
                raise RuntimeError("extension index sync failed")
        except Exception as e:
            failure_count += 1
            index_deployment_error = str(e)
            print(f"⚠️ 파일 인덱스 배포 중 에러가 발생했습니다: {e}")
    emit_folderling_event(
        event_callback,
        "index_result",
        status="succeeded" if index_ready and not index_deployment_error else "failed",
        index_ready=index_ready,
        index_mode=index_mode,
        fallback_reason=index_fallback_reason,
        error=index_error,
        deployment_error=index_deployment_error,
    )

    # ── 요약 ──
    print()
    print("=" * 60)
    print("📊 최종 요약")
    print("=" * 60)
    if dedup_summary:
        print(
            f"  중복/검토 큐: 정확 중복 {dedup_summary['exact_count']}개, "
            f"검토 큐 {dedup_summary.get('review_queue_move_count', dedup_summary['suspect_move_count'])}개 격리 "
            f"(같은 작가/미상 {dedup_summary.get('same_author_count', 0)}, "
            f"작가 충돌 {dedup_summary.get('author_conflict_count', 0)})"
        )
    print(f"  폴더링  : 입고 {move_count}개, pass {pass_count}개")
    print(f"  좌표 충돌: warning 보류 {volume_conflict_hold_count}개")
    print(
        "  unpack   : 부속 삭제 "
        f"{unpack_discarded_file_count}개, 정리 보류 {unpack_cleanup_issue_count}개"
    )
    print(f"  빈 폴더 : temp 디렉터리 {empty_dir_cleanup_count}개 정리")
    if failure_count:
        print(f"  실패/부분 완료: {failure_count}건 (actual run은 failed 처리)")

    total = 0
    print("\n📊 폴더별 파일 개수 요약")
    for folder in sorted(os.listdir(dst_dir)):
        if folder == "_최근":
            continue
        path = os.path.join(dst_dir, folder)
        if os.path.isdir(path):
            count = len(os.listdir(path))
            total += count
            print(f"{folder}: {count}개")
    print(f"\n총합: {total}개")
    result = {
        "dedup_summary": dedup_summary,
        "move_count": move_count,
        "pass_count": pass_count,
        "empty_dir_cleanup_count": empty_dir_cleanup_count,
        "failure_count": failure_count,
        "volume_conflict_hold_count": volume_conflict_hold_count,
        "unpack_cleanup_results": unpack_cleanup_results,
        "unpack_cleanup_issue_count": unpack_cleanup_issue_count,
        "unpack_discarded_file_count": unpack_discarded_file_count,
        "unpack_discarded_bytes": unpack_discarded_bytes,
        "pre_index_mode": pre_index_mode,
        "pre_index_fallback_reason": snapshot["reason"],
        "index_mode": index_mode,
        "index_fallback_reason": index_fallback_reason,
    }
    emit_folderling_event(
        event_callback,
        "folderling_summary",
        status=(
            "needs_review"
            if failure_count or volume_conflict_hold_count or unpack_cleanup_issue_count
            else "succeeded"
        ),
        **result,
    )
    return result


def _process_items_with_lock_held(
    src_dir,
    dst_dir,
    script_dir,
    state_db_path=None,
    *,
    event_callback=None,
):
    """Run Folderling while the caller owns the roots mutation lock."""
    import decision_store

    state_db_path = state_db_path or os.path.join(
        script_dir, ".dedup_state", "dedup_decisions.sqlite3"
    )
    actual_run_id, manifest_path = decision_store.prepare_actual_run(
        state_db_path, dst_dir, src_dir
    )
    emit_folderling_event(
        event_callback,
        "actual_run_started",
        status="running",
        run_id=actual_run_id,
        manifest_path=str(manifest_path),
    )
    try:
        import review_actions
        action_summary = {"accepted": [], "discarded": []}
        if review_actions.has_action_files(src_dir):
            action_conn = decision_store.connect_state_db(state_db_path)
            try:
                action_summary = review_actions.process_claimed_actions(
                    action_conn,
                    temp_dir=src_dir,
                    house_dir=dst_dir,
                    run_id=actual_run_id,
                )
            finally:
                action_conn.close()
        action_count = (
            len(action_summary["accepted"]) + len(action_summary["discarded"])
        )
        if action_count:
            print(
                "📥 검토 처리함 완료: "
                f"house {len(action_summary['accepted'])}개, "
                f"delete {len(action_summary['discarded'])}개"
            )
        emit_folderling_event(
            event_callback,
            "review_actions_result",
            status="succeeded",
            accepted_count=len(action_summary["accepted"]),
            discarded_count=len(action_summary["discarded"]),
        )
        result = _process_items_authorized(
            src_dir,
            dst_dir,
            script_dir,
            actual_run_id,
            manifest_path,
            event_callback=event_callback,
        )
        result["review_action_summary"] = action_summary
    except (Exception, KeyboardInterrupt) as exc:
        emit_folderling_event(
            event_callback,
            "workflow_failed",
            status="failed",
            run_id=actual_run_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        conn = decision_store.connect_state_db(state_db_path)
        try:
            decision_store.finish_actual_run(
                conn, actual_run_id, success=False, error=str(exc)
            )
        finally:
            conn.close()
        raise
    failure_count = int(result.get("failure_count", 0))
    conn = decision_store.connect_state_db(state_db_path)
    try:
        final_issues = decision_store.doctor_issues(
            conn, allowed_active_run_id=actual_run_id
        )
        result["final_doctor_issue_count"] = len(final_issues)
        result["final_doctor_first_issue"] = final_issues[0] if final_issues else None
        if final_issues:
            failure_count += 1
            result["failure_count"] = failure_count
        emit_folderling_event(
            event_callback,
            "final_doctor_result",
            status="succeeded" if not final_issues else "failed",
            issue_count=len(final_issues),
            first_issue=final_issues[0] if final_issues else None,
        )
        decision_store.finish_actual_run(
            conn,
            actual_run_id,
            success=failure_count == 0,
            error=(f"folderling partial failure count: {failure_count}" if failure_count else None),
        )
    finally:
        conn.close()
    emit_folderling_event(
        event_callback,
        "actual_run_finished",
        status="succeeded" if failure_count == 0 else "needs_review",
        run_id=actual_run_id,
        failure_count=failure_count,
    )
    return result


def process_items(
    src_dir, dst_dir, script_dir, state_db_path=None, *, event_callback=None
):
    """Consume and own one persistent actual run for the complete folderling workflow."""
    from mutation_io import mutation_lock_for_roots
    with mutation_lock_for_roots(dst_dir, src_dir, "folderling-command"):
        return _process_items_with_lock_held(
            src_dir,
            dst_dir,
            script_dir,
            state_db_path=state_db_path,
            event_callback=event_callback,
        )


def main():
    src_dir, dst_dir = parse_args(sys.argv)
    script_dir = str(PROJECT_ROOT)
    result = process_items(src_dir, dst_dir, script_dir)
    return 2 if result.get("failure_count", 0) else 0


if __name__ == "__main__":
    sys.exit(main())
