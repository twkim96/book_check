"""휴지통(trash_bin) 검토 큐 폴더의 파일을 원래 위치로 복원하는 스크립트.

검토 큐(`suspected_duplicates`, `author_conflicts`)는 자동 삭제 대상이 아니라
사람 검토를 위한 격리 폴더입니다. 검토 결과 잘못 이동된 항목을 직전 dedup 로그
기준으로 빠르게 되돌릴 때 사용합니다.

사용 예시:
    python3 restore_suspects.py                   # 가장 최신 dedup 로그를 자동 사용, dry-run
    python3 restore_suspects.py --run             # 실제 이동
    python3 restore_suspects.py --log <경로>      # 특정 로그를 지정
    python3 restore_suspects.py --temp <경로> --house <경로>

별개 작품/별도 판본 판정은 `txt_temp/pass/` 이동이나 `〔P〕` 마커로 만들지 않습니다.
파일을 원래 위치로 복원한 뒤 `dedup_decisions.py`로 정확한 pair verdict를 기록하세요.
"""
import json
import os
import re
import shutil
import sys

from project_paths import HOUSE_DIR, STATE_DB, TEMP_DIR

DEFAULT_TEMP_DIR = str(TEMP_DIR)
DEFAULT_HOUSE_DIR = str(HOUSE_DIR)
DEFAULT_STATE_DB = str(STATE_DB)

TRASH_SUBDIRS = (
    "suspected_duplicates", "author_conflicts", "warning",
    "house_cleanup_review", "house_cleanup_warning",
    "house_human_review",
)

# 휴지통 꼬리표 제거는 normalizer의 단일 구현을 재사용한다(정규식 표류 방지).
from normalizer import strip_trash_suffix


def parse_args(argv):
    args = {
        "temp_dir": DEFAULT_TEMP_DIR,
        "house_dir": DEFAULT_HOUSE_DIR,
        "log_path": None,
        "dry_run": True,
        "state_db_path": DEFAULT_STATE_DB,
        "file_id": None,
        "review_id": None,
    }
    for i, arg in enumerate(argv):
        if arg == "--run":
            args["dry_run"] = False
        elif arg == "--dry-run":
            args["dry_run"] = True
        elif arg == "--temp" and i + 1 < len(argv):
            args["temp_dir"] = argv[i + 1]
        elif arg == "--house" and i + 1 < len(argv):
            args["house_dir"] = argv[i + 1]
        elif arg == "--log" and i + 1 < len(argv):
            args["log_path"] = argv[i + 1]
        elif arg == "--state-db" and i + 1 < len(argv):
            args["state_db_path"] = argv[i + 1]
        elif arg == "--file-id" and i + 1 < len(argv):
            args["file_id"] = argv[i + 1]
        elif arg == "--review-id" and i + 1 < len(argv):
            args["review_id"] = int(argv[i + 1])
    if args["file_id"] and args["review_id"] is not None:
        raise ValueError("--file-id and --review-id are mutually exclusive")
    return args


def find_latest_log(temp_dir):
    log_dir = os.path.join(temp_dir, "dedup_logs")
    if not os.path.isdir(log_dir):
        return None
    candidates = [
        os.path.join(log_dir, name)
        for name in os.listdir(log_dir)
        if name.startswith("dedup_") and name.endswith((".txt", ".json"))
    ]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def parse_log_moves(log_path):
    """로그에서 검토 큐 이동 라인의 source(temp/house)와 rel_path를 추출.

    - 신규 마커: [검토 큐로 이동], [작가 충돌 큐로 이동]
    - warning 이동 마커: [애매 → warning 보류], [metadata_only → warning 보류]
    - 호환 마커: [이동 대상], [작가 충돌 이동]
    - format_entry 출력 형식: "<marker> [source] <name> | <rel_path> | ..."
    - 반환: { 파일명: [(source, rel_path), ...] }  (같은 basename이 여러 경로에서
      이동될 수 있으므로 리스트로 누적해 앞 기록이 덮이지 않게 한다.)
    """
    if str(log_path).lower().endswith(".json"):
        with open(log_path, "r", encoding="utf-8") as stream:
            payload = json.load(stream)
        mapping = {}
        move_statuses = {"moved", "author_review", "warning", "metadata_only"}
        for record in payload.get("suspect_move_records") or []:
            if record.get("status") not in move_statuses:
                continue
            entry = record.get("entry") or {}
            name = str(entry.get("name") or "").strip()
            source = str(entry.get("source") or "").strip().lower()
            rel_path = str(entry.get("rel_path") or "").strip()
            if name and source and rel_path:
                mapping.setdefault(name, []).append((source, rel_path))
        return mapping

    mapping = {}
    in_suspect_section = False
    move_marker_re = re.compile(
        r"\s+\[(?:중복 확정 → suspected|애매·작가충돌 → author_conflicts|애매 → warning 보류|metadata_only → warning 보류|"
        r"검토 큐로 이동|작가 충돌 큐로 이동|이동 대상|작가 충돌 이동)\]\s+"
        r"\[(?P<source>[^\]]+)\]\s+(?P<name>.+?)\s+\|\s+(?P<rel>.+?)\s+\|"
    )
    section_re = re.compile(r"\[2단계\] 제목 기반 (?:검토 큐|의심 중복)")

    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            if section_re.search(line):
                in_suspect_section = True
                continue
            if not in_suspect_section:
                continue

            match = move_marker_re.match(line)
            if match:
                source = match.group("source").strip().lower()
                name = match.group("name").strip()
                rel_path = match.group("rel").strip()
                mapping.setdefault(name, []).append((source, rel_path))

    return mapping


def iter_trash_files(temp_dir):
    for sub in TRASH_SUBDIRS:
        folder = os.path.join(temp_dir, "trash_bin", sub)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            full = os.path.join(folder, name)
            if os.path.isfile(full):
                yield sub, name, full


def _queued_file_for_review(conn, review_id):
    review = conn.execute(
        "SELECT candidate_file_id, reference_file_id FROM review_items WHERE review_id = ?",
        (review_id,),
    ).fetchone()
    if review is None:
        raise KeyError(f"review not found: {review_id}")
    rows = conn.execute(
        """
        SELECT file_id FROM files
        WHERE file_id IN (?, ?) AND active = 1 AND source = 'queue'
        """,
        (review["candidate_file_id"], review["reference_file_id"]),
    ).fetchall()
    if len(rows) != 1:
        raise RuntimeError(
            f"review {review_id} must have exactly one active queue endpoint; found {len(rows)}"
        )
    return rows[0]["file_id"]


def _restore_managed_queue(
    temp_dir, state_db_path, dry_run, *, file_id=None, review_id=None
):
    import decision_store

    conn = decision_store.connect_state_db(state_db_path)
    restored = 0
    failed = 0
    try:
        selected_file_id = file_id
        if review_id is not None:
            selected_file_id = _queued_file_for_review(conn, review_id)
        selected_found = False
        for sub, filename, src in iter_trash_files(temp_dir):
            row = conn.execute(
                "SELECT file_id FROM files WHERE canonical_path = ? AND active = 1 AND source = 'queue'",
                (os.path.abspath(src),),
            ).fetchone()
            if row is None:
                print(f"⚠️ [{sub}] DB queue identity 없음 (스킵): {filename}")
                failed += 1
                continue
            if selected_file_id is not None and row["file_id"] != selected_file_id:
                continue
            selected_found = True
            origin = conn.execute(
                """
                SELECT source_path, action FROM operations
                WHERE file_id = ? AND action IN (
                    'suspected_move', 'warning_move', 'house_review_move'
                )
                  AND state = 'committed'
                ORDER BY operation_id DESC LIMIT 1
                """,
                (row["file_id"],),
            ).fetchone()
            if origin is None:
                print(f"⚠️ [{sub}] DB 원래 경로 없음 (스킵): {filename}")
                failed += 1
                continue
            if dry_run:
                source_label = "house" if origin["action"] == "house_review_move" else "temp"
                print(f"[미리보기] [{sub}] {filename} → [{source_label}] {origin['source_path']}")
            else:
                decision_store.restore_committed_queue_file(conn, row["file_id"])
                source_label = "house" if origin["action"] == "house_review_move" else "temp"
                print(f"✅ [{sub}] {filename} → [{source_label}] {origin['source_path']}")
            restored += 1
        if selected_file_id is not None and not selected_found:
            raise RuntimeError(f"active queue file not found: {selected_file_id}")
    finally:
        conn.close()
    label = "미리보기" if dry_run else "실행"
    print(f"\n{label} 완료(DB): 복원 {restored}개, 실패/스킵 {failed}개")


def restore_files(
    temp_dir,
    house_dir,
    log_path,
    dry_run,
    state_db_path=None,
    *,
    file_id=None,
    review_id=None,
):
    if state_db_path and os.path.isfile(state_db_path):
        return _restore_managed_queue(
            temp_dir,
            state_db_path,
            dry_run,
            file_id=file_id,
            review_id=review_id,
        )
    if not dry_run:
        raise RuntimeError(
            "managed state DB is required for --run; legacy log restore is preview-only"
        )
    if log_path is None:
        log_path = find_latest_log(temp_dir)
    if not log_path or not os.path.exists(log_path):
        print(f"❌ dedup 로그를 찾을 수 없습니다: {log_path}")
        return

    mapping = parse_log_moves(log_path)
    print(f"📋 로그 파싱: {log_path}")
    total_moves = sum(len(v) for v in mapping.values())
    print(f"   → 이동 기록 {total_moves}건 (고유 파일명 {len(mapping)})\n")

    base_by_source = {"house": house_dir, "temp": temp_dir}

    restored = 0
    failed = 0
    # 같은 basename이 여러 후보를 가질 때, 이미 복원에 쓴 (source, rel_path)는 소비 처리.
    used = set()

    for sub, filename, src in iter_trash_files(temp_dir):
        clean_name = strip_trash_suffix(filename)
        candidates = mapping.get(clean_name)
        if not candidates:
            print(f"⚠️ [{sub}] 로그에서 원래 경로 없음 (스킵): {filename}")
            failed += 1
            continue

        # 후보 중 아직 안 쓴 것 + 대상이 비어 있는 것을 우선 선택.
        chosen = None
        for source, rel_path in candidates:
            key = (source, rel_path)
            if key in used:
                continue
            base = base_by_source.get(source)
            if not base:
                continue
            dst = os.path.join(base, rel_path)
            if os.path.exists(dst):
                continue
            chosen = (source, rel_path, base, dst)
            break
        if chosen is None:
            # 쓸 수 있는 후보가 없음: 사유를 구분해 출력.
            srcs = {s for s, _ in candidates}
            if not (srcs & set(base_by_source)):
                print(f"⚠️ [{sub}] 알 수 없는 source={sorted(srcs)!r} (스킵): {filename}")
            else:
                print(f"⚠️ [{sub}] 복원 대상이 이미 존재하거나 후보 소진 (스킵): {filename}")
            failed += 1
            continue

        source, rel_path, base, dst = chosen
        used.add((source, rel_path))

        label = f"[{source}] {rel_path}"
        if dry_run:
            print(f"[미리보기] [{sub}] {filename} → {label}")
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            print(f"✅ [{sub}] {filename} → {label}")
        restored += 1

    label = "미리보기" if dry_run else "실행"
    print(f"\n{label} 완료: 복원 {restored}개, 실패/스킵 {failed}개")


if __name__ == "__main__":
    options = parse_args(sys.argv[1:])
    restore_files(
        temp_dir=options["temp_dir"],
        house_dir=options["house_dir"],
        log_path=options["log_path"],
        dry_run=options["dry_run"],
        state_db_path=options["state_db_path"],
        file_id=options["file_id"],
        review_id=options["review_id"],
    )
