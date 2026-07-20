import errno
import os
import shutil
import sys
from datetime import datetime

from tqdm import tqdm

from normalizer import (
    add_pass_marker,
    get_chosung,
    normalize_filename,
    should_exclude_dir,
    strip_pass_marker,
    strip_disambig_marker,
    strip_trash_suffix,
    SUPPORTED_EXTENSIONS,
)
from scanner import generate_file_list
from deduplicator import clean_duplicates, unique_path
from project_paths import HOUSE_DIR, PROJECT_ROOT, TEMP_DIR


DEFAULT_SRC_DIR = str(TEMP_DIR)
DEFAULT_DST_DIR = str(HOUSE_DIR)
PASS_DIR_NAME = "pass"

# 자모 폴더에 같은 이름의 파일이 이미 있을 때 새 파일에 붙는 충돌 회피 suffix.
# 일반 입고와 pass 입고를 분리하여 의미가 섞이지 않게 한다.
NORMAL_CONFLICT_SUFFIX = "_dup"
PASS_CONFLICT_SUFFIX = "_pass"
EXTENSION_INDEX_PATH = os.path.join("extension", "file_index.json")
HOUSE_INDEX_FILENAME = "file_index.json"


def get_now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


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
    if state_db_path and os.path.isfile(src_path) and not is_pass:
        import decision_store
        from volume_group_mutations import suggest_folderling_volume_target

        conn = decision_store.connect_state_db(state_db_path)
        try:
            source_row = conn.execute(
                "SELECT file_id FROM files WHERE canonical_path = ? AND active = 1",
                (decision_store.canonicalize_path(src_path),),
            ).fetchone()
            if source_row is not None:
                auto_volume = suggest_folderling_volume_target(
                    conn,
                    source_file_id=source_row["file_id"],
                    house_root=dst_dir,
                )
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
            ingest_to_house(
                conn,
                source_file_id=row["file_id"],
                destination=dst_path,
                run_id=run_id or f"folderling-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
            )
            if auto_volume is not None:
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

    if os.path.exists(src_dir):
        for item in sorted(os.listdir(src_dir)):
            if item == PASS_DIR_NAME:
                continue
            normal_items.append((item, os.path.join(src_dir, item), False))

    # 1.2.1: pass/는 더 이상 사람 판정 입력이 아니다. 기존 내용은 손대지 않고
    # dedup_decisions.py 사용 안내만 출력하며 폴더링 대상에서 제외한다.
    return normal_items


def _process_items_authorized(src_dir, dst_dir, script_dir, actual_run_id, manifest_path):
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

    from mutation_io import ensure_directory_nofollow
    ensure_directory_nofollow(dst_dir)

    if os.path.isdir(pass_dir):
        legacy_pass_count = len(os.listdir(pass_dir))
        if legacy_pass_count:
            print(
                f"⚠️ legacy pass/ 항목 {legacy_pass_count}개는 자동 입고하지 않습니다. "
                "dedup_decisions.py로 pair 판정을 등록하세요."
            )

    # 1.2.1: ___ 폴더를 선행 평탄화하지 않는다. 내부 지원 파일은 감사 후
    # journaled directory intake가 stable file_id/상대 경로를 보존하며 처리한다.

    # ── 1단계: 중복 제거 + 검토 큐 격리 (house + temp 통합 스캔) ──
    print("=" * 60)
    print("📦 1단계: 중복/검토 큐 정리 (house + temp 통합)")
    print("=" * 60)
    dedup_summary = clean_duplicates(
        house_dir=dst_dir,
        temp_dir=src_dir,
        dry_run=False,
        index_path=file_index_json,
        rescan=True,
        move_suspects=True,
        delete_exact=True,
        include_temp=True,
        audit_suspects=True,
        update_index_after_run=False,
        state_db_path=state_db_path,
        require_state_db=True,
        authorized_run_id=actual_run_id,
    )

    # ── 2단계: 폴더링 (temp에 남아있는 파일을 house로 이동) ──
    print("=" * 60)
    print("📂 2단계: 폴더링 (temp → house)")
    print("=" * 60)
    move_count = 0
    pass_count = 0
    failure_count = 0
    empty_dir_cleanup_count = 0

    with open(success_log, "w", encoding="utf-8") as s_log, \
         open(fail_log, "w", encoding="utf-8") as f_log:

        items = iter_process_items(src_dir, pass_dir)

        for item, src_path, is_pass in tqdm(items, desc="분류 및 이동 중"):
            if not is_pass and should_skip_source_item(item):
                continue

            now_str = get_now()

            try:
                # 휴지통 꼬리표(_suspect_N / _dup_N / _pass_N)는 어디서 발견되든
                # normalize_filename(_→공백 치환)이 돌기 전에 미리 떼어낸다.
                raw_name = strip_trash_suffix(item)

                clean_name = normalize_filename(raw_name)
                if not clean_name:
                    failure_count += 1
                    f_log.write(
                        f"[{now_str}] {src_path} | 이름이 비어있어 실패 | "
                        "조치: 원본 파일명을 확인하고 직접 입고하거나 삭제\n"
                    )
                    continue

                is_dir = os.path.isdir(src_path)
                is_file = os.path.isfile(src_path)
                ext = os.path.splitext(clean_name)[1].lower()

                if is_file and ext not in SUPPORTED_EXTENSIONS:
                    continue
                if not is_dir and not is_file:
                    continue
                if is_dir and not directory_has_files(src_path):
                    removed = prune_empty_intake_tree(src_path)
                    empty_dir_cleanup_count += removed
                    s_log.write(
                        f"[{now_str}] [empty-dir] {src_path} | "
                        f"빈 디렉터리 {removed}개 정리\n"
                    )
                    continue

                label = "[pass] " if is_pass else ""
                move_to_house(
                    src_path, dst_dir, recent_dir, clean_name, s_log, label,
                    is_pass=is_pass, state_db_path=state_db_path, run_id=intake_run_id,
                )
                if is_dir:
                    empty_dir_cleanup_count += prune_empty_intake_tree(src_path)

                if is_pass:
                    pass_count += 1
                else:
                    move_count += 1

            except Exception as e:
                failure_count += 1
                f_log.write(
                    f"[{now_str}] {src_path} | 예외: {e} | "
                    "조치: 경로/권한 확인 후 재실행 또는 수동 입고\n"
                )

    cleanup_recent_links(recent_dir, max_days=30)

    print(f"✅ 폴더링 완료: 입고 {move_count}개")
    if empty_dir_cleanup_count:
        print(f"  - temp 빈 디렉터리 {empty_dir_cleanup_count}개 정리")
    if pass_count > 0:
        print(f"  - pass 폴더 승인 항목 {pass_count}개 강제 입고됨")
    print(f"→ 로그 파일({script_dir} 위치)을 확인하세요.")
    print("  - success.log / fail.log")

    # ── 3단계: 인덱스 갱신 ──
    print()
    print("=" * 60)
    print("🔄 3단계: 인덱스 갱신")
    print("=" * 60)
    try:
        index_ok = generate_file_list(
            [dst_dir],
            file_list_json,
            file_index_json,
            state_db_path=os.path.join(script_dir, ".dedup_state", "dedup_decisions.sqlite3"),
        )
        if not index_ok:
            raise RuntimeError("scanner index generation returned failure")
        print("✨ file_list.json / file_index.json 업데이트 완료")
        if not sync_house_index(file_index_json, dst_dir):
            raise RuntimeError("house index sync failed")
        if not sync_extension_index(file_index_json, script_dir):
            raise RuntimeError("extension index sync failed")
    except Exception as e:
        failure_count += 1
        print(f"⚠️ 파일 인덱스 업데이트 중 에러가 발생했습니다: {e}")

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
    return {
        "dedup_summary": dedup_summary,
        "move_count": move_count,
        "pass_count": pass_count,
        "empty_dir_cleanup_count": empty_dir_cleanup_count,
        "failure_count": failure_count,
    }


def _process_items_with_lock_held(src_dir, dst_dir, script_dir, state_db_path=None):
    """Run Folderling while the caller owns the roots mutation lock."""
    import decision_store

    state_db_path = state_db_path or os.path.join(
        script_dir, ".dedup_state", "dedup_decisions.sqlite3"
    )
    actual_run_id, manifest_path = decision_store.prepare_actual_run(
        state_db_path, dst_dir, src_dir
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
        result = _process_items_authorized(
            src_dir, dst_dir, script_dir, actual_run_id, manifest_path
        )
        result["review_action_summary"] = action_summary
    except (Exception, KeyboardInterrupt) as exc:
        conn = decision_store.connect_state_db(state_db_path)
        try:
            decision_store.finish_actual_run(
                conn, actual_run_id, success=False, error=str(exc)
            )
        finally:
            conn.close()
        raise
    failure_count = result.get("failure_count", 0)
    conn = decision_store.connect_state_db(state_db_path)
    try:
        decision_store.finish_actual_run(
            conn,
            actual_run_id,
            success=failure_count == 0,
            error=(f"folderling partial failure count: {failure_count}" if failure_count else None),
        )
    finally:
        conn.close()
    return result


def process_items(src_dir, dst_dir, script_dir, state_db_path=None):
    """Consume and own one persistent actual run for the complete folderling workflow."""
    from mutation_io import mutation_lock_for_roots
    with mutation_lock_for_roots(dst_dir, src_dir, "folderling-command"):
        return _process_items_with_lock_held(
            src_dir, dst_dir, script_dir, state_db_path=state_db_path
        )


def main():
    src_dir, dst_dir = parse_args(sys.argv)
    script_dir = str(PROJECT_ROOT)
    result = process_items(src_dir, dst_dir, script_dir)
    return 2 if result.get("failure_count", 0) else 0


if __name__ == "__main__":
    sys.exit(main())
