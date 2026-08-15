import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from difflib import SequenceMatcher

from normalizer import (
    SUPPORTED_EXTENSIONS,
    NORMALIZER_VERSION,
    add_disambig_marker,
    analyze_name,
    has_pass_marker,
    has_legacy_marker,
    is_supported_file,
    materialize_title_markup,
    normalize_filename,
    normalize_nfc,
    should_exclude_dir,
    should_exclude_file,
    strip_disambig_marker,
    strip_trash_suffix,
    units_comparable,
)
from dedup_episode_relation import (
    DEDUP_SPECIAL_COORDINATE_MODES,
    classify_dedup_coordinate_relation,
    classify_loose_title_upgrade_relation,
    episode_profile,
)
from scanner import generate_file_list, validate_index_generation
from text_preview import (
    NormalizationDeferred,
    OrderedBodyCoverage,
    count_text_chars,
    ordered_body_coverage_sufficient,
    preview_similarity,
    read_text_edges,
)
from project_paths import EXTENSION_INDEX, HOUSE_DIR, PROJECT_ROOT, STATE_DB, TEMP_DIR


def _sync_extension_index(index_path):
    """재생성된 file_index.json을 확장 폴더(extension/file_index.json)로 복사.

    folderling 경유가 아닌 deduplicator 단독 실행에서도 확장 검색 인덱스가
    stale되지 않게 한다. 확장 폴더가 없으면 조용히 건너뛴다.
    """
    ext_index = str(EXTENSION_INDEX)
    ext_dir = os.path.dirname(ext_index)
    if not os.path.isdir(ext_dir):
        return False
    try:
        from mutation_io import atomic_publish_regular_file

        atomic_publish_regular_file(index_path, ext_index)
        print(f"✨ 브라우저 확장용 인덱스 동기화 완료: {ext_index}")
        return True
    except (OSError, RuntimeError) as e:
        print(f"⚠️ 확장 인덱스 동기화 실패: {e}")
        return False


DEFAULT_HOUSE_DIR = str(HOUSE_DIR)
DEFAULT_TEMP_DIR = str(TEMP_DIR)
DEFAULT_STATE_DB = str(STATE_DB)
FOLDERLING_AUDIT_READ_BUDGET = "20GiB"
FOLDERLING_REBASELINE_READ_BUDGET = "64GiB"
FOLDERLING_REBASELINE_DEEP_PAIRS_PER_FILE = 128
FOLDERLING_REBASELINE_STOP_REASONS = frozenset({
    "body_budget_exhausted", "deep_check_deferred",
})
STRONG_PROOF_POLICY_VERSION = "1.4.16-pinned-v1"
HOUSE_NEAR_DUPLICATE_MIN_COVERAGE_PPM = 990_000
_DISTRIBUTION_SUFFIX_RE = re.compile(
    r"(?:^|[-_\s])(?:현|로)?판\d{6}(?=(?:[^0-9]|$))", re.IGNORECASE
)
_EXPLICIT_EDITION_MARKER_RE = re.compile(
    r"개정|수정판|누락\s*수정|외전|특전|후일담|에필로그|"
    r"19\s*(?:n|금|禁)|성인판|무삭제|번역판",
    re.IGNORECASE,
)

AUDITOR_STRONG_CLASSES = frozenset({"text_equivalent", "epub_equivalent"})
AUDITOR_RELATION_CLASSES = frozenset({
    "text_equivalent", "epub_equivalent", "marker_recheck",
    "near_identical", "contained_exact", "contained_version", "ordered_body_match",
    "ordered_body_review",
    "longer_unresolved",
    "decode_lossy", "metadata_only", "insufficient_text", "boilerplate_only",
    "different",
})


def _legacy_marker_discard_allowed(discard, keep):
    """Admit a legacy loose file only as the discard side of a strong proof.

    ``〔P〕``/``〔Dn〕`` historically blocked every automatic mutation.  A
    current 95% ordered-body proof may now retire that marked copy only when a
    clean, independently mutable keep exists.  Protected, representative, or
    variant-bound legacy files remain fail-closed.
    """
    return bool(
        discard.get("assignment_state") == "legacy_unresolved"
        and keep.get("assignment_state") != "legacy_unresolved"
        and has_legacy_marker(discard.get("name", ""))
        and not has_legacy_marker(keep.get("name", ""))
        and discard.get("variant_id") is None
        and not discard.get("protected")
        and not discard.get("representative")
    )


def _current_strong_proof_not_sufficient(evidence_json):
    try:
        evidence = json.loads(evidence_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    marker = evidence.get("current_proof_not_sufficient")
    return bool(
        isinstance(marker, dict)
        and marker.get("policy_version") == STRONG_PROOF_POLICY_VERSION
    )


def _current_review_evidence_precludes_action(classification, evidence_json):
    """Return true when current persisted evidence already disproves a rule."""
    if _current_strong_proof_not_sufficient(evidence_json):
        return True
    if classification != "near_identical":
        return False
    try:
        evidence = json.loads(evidence_json or "{}")
        coverage = evidence.get("ordered_body_coverage")
        if evidence.get("ordered_body_checked") is not True or not isinstance(
            coverage, dict
        ):
            return False
        return not ordered_body_coverage_sufficient(
            OrderedBodyCoverage(**coverage)
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _record_current_strong_proof_not_sufficient(
    conn, review_id, classification, exc
):
    """Persist a current-fingerprint proof failure without closing review.

    The review row itself binds the evidence to both current fingerprint IDs.
    If either file changes, the row stops being current and a later auditor may
    prove the new bytes.  Under the same proof policy and fingerprints, service
    readiness must not repeatedly advertise a mutation that already failed its
    final pinned-descriptor proof.
    """
    row = conn.execute(
        "SELECT evidence_json FROM review_items WHERE review_id = ?",
        (review_id,),
    ).fetchone()
    if row is None:
        return
    try:
        evidence = json.loads(row["evidence_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        evidence = {"previous_evidence": row["evidence_json"]}
    evidence["current_proof_not_sufficient"] = {
        "policy_version": STRONG_PROOF_POLICY_VERSION,
        "classification": classification,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }
    import decision_store

    with decision_store.transaction(conn):
        conn.execute(
            """
            UPDATE review_items
            SET state = 'deferred', evidence_json = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE review_id = ? AND state IN ('pending', 'deferred')
            """,
            (json.dumps(evidence, ensure_ascii=False, sort_keys=True), review_id),
        )


def calculate_hash(file_path):
    """파일의 SHA-256 해시를 계산합니다.

    완전 중복은 기본적으로 자동 삭제되므로, 충돌 우려가 사실상 없는 SHA-256을 쓴다.
    """
    hasher = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception as e:
        print(f"⚠️ 파일 해시 계산 중 에러 ({file_path}): {e}")
        return None


def unique_path(directory, filename, suffix):
    os.makedirs(directory, exist_ok=True)
    dest_path = os.path.join(directory, filename)
    if not os.path.exists(dest_path):
        return dest_path

    base, ext = os.path.splitext(filename)
    counter = 1
    while True:
        candidate = os.path.join(directory, f"{base}{suffix}_{counter}{ext}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def parse_args(argv):
    script_dir = str(PROJECT_ROOT)
    args = {
        "house_dir": DEFAULT_HOUSE_DIR,
        "temp_dir": DEFAULT_TEMP_DIR,
        "index_path": os.path.join(script_dir, "file_index.json"),
        "dry_run": True,
        "rescan": False,
        "move_suspects": False,
        "delete_exact": True,
        "include_temp": False,
        "audit_suspects": False,
        "update_index_after_run": True,
        "state_db_path": DEFAULT_STATE_DB,
        "require_state_db": True,
        "pure_plan": False,
    }

    for i, arg in enumerate(argv):
        if arg == "--run":
            args["dry_run"] = False
        elif arg == "--dry-run":
            args["dry_run"] = True
        elif arg == "--pure-plan":
            args["dry_run"] = True
            args["pure_plan"] = True
        elif arg == "--rescan":
            args["rescan"] = True
        elif arg == "--move-suspects":
            args["move_suspects"] = True
        elif arg == "--delete-exact":
            args["delete_exact"] = True
        elif arg == "--keep-exact":
            args["delete_exact"] = False
        elif arg == "--include-temp":
            args["include_temp"] = True
        elif arg == "--audit-suspects":
            args["audit_suspects"] = True
        elif arg == "--house" and i + 1 < len(argv):
            args["house_dir"] = argv[i + 1]
        elif arg == "--temp" and i + 1 < len(argv):
            args["temp_dir"] = argv[i + 1]
        elif arg == "--index" and i + 1 < len(argv):
            args["index_path"] = argv[i + 1]
        elif arg == "--state-db" and i + 1 < len(argv):
            args["state_db_path"] = argv[i + 1]

    return args


def require_ready_state_db(state_db_path):
    """actual mutation 전에 1.2.1 state store가 준비됐는지 검증한다.

    Phase A의 decision_store가 아직 없거나 schema 검증에 실패하면 fail closed 한다.
    이 함수는 파일을 만들거나 migration하지 않는다.
    """
    try:
        from decision_store import verify_state_db_ready
    except (ImportError, ModuleNotFoundError) as exc:
        raise RuntimeError(
            "1.2.1 state DB/journal이 아직 준비되지 않아 actual mutation을 중단합니다."
        ) from exc

    ready, reason = verify_state_db_ready(state_db_path)
    if not ready:
        raise RuntimeError(
            f"1.2.1 state DB/journal 검증 실패로 actual mutation을 중단합니다: {reason}"
        )


def ensure_index(house_dir, index_path, rescan=False, state_db_path=None):
    if os.path.exists(index_path) and not rescan:
        return

    output_dir = os.path.dirname(index_path)
    file_list_path = os.path.join(output_dir, "file_list.json")
    print("ℹ️ 파일 인덱스를 갱신합니다...")
    generated = generate_file_list(
        [house_dir], file_list_path, index_path, state_db_path=state_db_path
    )
    if not generated or not os.path.isfile(index_path):
        raise RuntimeError("file index generation failed")


def load_index_entries(
    house_dir, index_path, rescan=False, state_db_path=None, allow_write=True,
    verified_entries=None,
):
    if verified_entries is not None and rescan:
        raise RuntimeError("verified index entries cannot be combined with rescan")
    if verified_entries is None and allow_write:
        ensure_index(
            house_dir, index_path, rescan=rescan, state_db_path=state_db_path
        )
    elif verified_entries is None and rescan:
        raise RuntimeError("--pure-plan cannot rescan or write an index")

    if verified_entries is None:
        try:
            validate_index_generation(index_path)
            with open(index_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            raise RuntimeError(f"file_index.json 읽기 실패: {e}") from e
    else:
        payload = {
            "version": 2,
            "normalizer_version": NORMALIZER_VERSION,
            "entries": verified_entries,
        }

    if payload.get("version") != 2 or not isinstance(payload.get("entries"), list):
        raise RuntimeError(
            "file_index.json 형식이 v2 인덱스가 아닙니다. scanner.py로 다시 생성하세요."
        )

    index_norm_version = payload.get("normalizer_version")
    if index_norm_version != NORMALIZER_VERSION:
        print(
            f"⚠️ file_index.json의 normalizer_version({index_norm_version!r})이 "
            f"현재 코드({NORMALIZER_VERSION!r})와 다릅니다. "
            "중복 판단은 현재 normalizer 결과를 우선 사용합니다. "
            "정확한 비교를 위해 --rescan 권장."
        )

    loaded = []
    for entry in payload["entries"]:
        if entry.get("type") != "file":
            continue

        name = normalize_nfc(entry.get("name", ""))
        if not is_supported_file(name):
            continue
        rel_path = normalize_nfc(entry.get("rel_path", ""))
        path = os.path.join(house_dir, rel_path) if rel_path else os.path.join(house_dir, name)
        if not os.path.exists(path):
            continue

        # 핵심 분류 필드는 항상 현재 normalizer 결과를 우선한다.
        # name/rel_path/size 같은 파일시스템 메타만 index/stat 값을 사용.
        info = analyze_name(name)
        if entry.get("title_override") and entry.get("core_title"):
            # Scanner가 DB의 사용자 제목 override를 검증해 기록한 경우에만
            # 깨끗한 실제 파일명에서 다시 제거된 제목보다 인덱스 key를 우선한다.
            info["core_title"] = str(entry["core_title"])
        try:
            size = os.path.getsize(path)
        except OSError:
            size = entry.get("size") or 0

        legacy_marker = has_pass_marker(name) or info.get("disambig", 1) > 1
        assignment_state = entry.get("assignment_state") or (
            "legacy_unresolved" if legacy_marker else "unassigned"
        )
        loaded.append({
            "name": name,
            "rel_path": rel_path,
            "path": path,
            "ext": info["ext"],
            "size": size,
            "core_title": info["core_title"],
            "author": info["author"],
            "max_number": info["max_number"],
            "effective_max": info.get("effective_max", 0),
            "unit": info.get("unit", "미상"),
            "disambig": info.get("disambig", 1),
            "complete": info["complete"],
            "volume_number": info["volume_number"],
            "start_number": info.get("start_number"),
            "end_number": info.get("end_number"),
            "span_ambiguous": info.get("span_ambiguous", False),
            "is_side_story": info.get("is_side_story", False),
            "source": "house",
            "legacy_marker": legacy_marker,
            "assignment_state": assignment_state,
            "mutation_eligible": assignment_state not in {"legacy_unresolved", "decision_required"},
            "file_id": entry.get("file_id"),
            "work_bucket_id": entry.get("work_bucket_id"),
            "variant_id": entry.get("variant_id"),
            "protected": bool(entry.get("protected", False)),
            "representative": bool(entry.get("representative", False)),
        })

    return loaded


def scan_temp_files(temp_dir):
    """temp 폴더의 지원 확장자 파일을 재귀적으로 스캔한다.

    - 관리 폴더(`pass`, `warning`, `trash_bin`, `dedup_logs`, `_최근`, 숨김 폴더,
      `__pycache__` 등)는 `should_exclude_dir`을 통해 진입을 차단한다.
    - `〔P〕`/`〔Dn〕` 마커 파일도 후보에는 포함하되 legacy_unresolved로 mutation을 막는다.
    - rel_path는 temp_dir 기준의 상대경로로 보존하여 로그/복원에서 사용한다.
    """
    loaded = []
    if not os.path.exists(temp_dir):
        return loaded

    base_dir = os.path.abspath(temp_dir)

    for root, dirs, files in os.walk(temp_dir):
        # 관리 폴더 진입 차단
        dirs[:] = [d for d in sorted(dirs) if not should_exclude_dir(d)]

        for filename in sorted(files):
            if should_exclude_file(filename):
                continue

            src_path = os.path.join(root, filename)
            if not os.path.isfile(src_path):
                continue

            # 휴지통에서 옮겨졌을 수 있는 _suspect_N 같은 꼬리표를 먼저 제거한다.
            # `_` → 공백 치환이 일어나는 normalize_filename보다 먼저 적용해야 한다.
            raw_name = strip_trash_suffix(filename)
            clean_name = normalize_filename(raw_name)
            if not clean_name or not is_supported_file(clean_name):
                continue
            info = analyze_name(clean_name)
            try:
                size = os.path.getsize(src_path)
            except OSError:
                size = 0

            rel_path = normalize_nfc(os.path.relpath(src_path, base_dir))

            legacy_marker = has_pass_marker(clean_name) or info.get("disambig", 1) > 1
            loaded.append({
                "name": clean_name,
                "rel_path": rel_path,
                "path": src_path,
                "ext": info["ext"],
                "size": size,
                "core_title": info["core_title"],
                "author": info["author"],
                "max_number": info["max_number"],
                "effective_max": info.get("effective_max", 0),
                "unit": info.get("unit", "미상"),
                "disambig": info.get("disambig", 1),
                "complete": info["complete"],
                "volume_number": info["volume_number"],
                "start_number": info.get("start_number"),
                "end_number": info.get("end_number"),
                "span_ambiguous": info.get("span_ambiguous", False),
                "is_side_story": info.get("is_side_story", False),
                "source": "temp",
                "legacy_marker": legacy_marker,
                "assignment_state": "legacy_unresolved" if legacy_marker else "unassigned",
                "mutation_eligible": not legacy_marker,
            })

    return loaded


# 글자수 비교 마진. 광고/후기/오타 같은 본문 외 군더더기에 흔들리지 않도록,
# 더 긴 쪽이 이 비율 넘게 길 때만 "더 완전한 본문"으로 인정한다.
CHAR_COUNT_MARGIN = 0.05


def _entry_char_count(entry):
    """entry의 공백 제거 글자수(txt 한정). 캐시. txt가 아니거나 실패면 -1."""
    if "char_count" in entry:
        return entry["char_count"]
    count = count_text_chars(entry["path"]) if entry.get("ext") == ".txt" else -1
    entry["char_count"] = count
    return count


def _effective_max(entry):
    if entry.get("span_ambiguous") is True:
        return 0
    value = entry.get("effective_max")
    if value is None and "span_ambiguous" not in entry:
        value = entry.get("max_number", 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def get_better_entry(a, b):
    """같은 작품으로 확정된 두 파일 중 더 완전/최신인 쪽을 고른다.

    주의: 이 함수는 "같은 작품"이라는 전제 하에 keep을 고르는 내부 규칙이다.
    같은 작품인지(same/different) 판정은 호출 측(본문 유사도)에서 먼저 한다.
    """
    a_unit = a.get("unit", "미상")
    b_unit = b.get("unit", "미상")
    a_max = _effective_max(a)
    b_max = _effective_max(b)

    # 1. 단위가 비교 가능(권 vs 화처럼 다르지 않음)하고 둘 다 본편 편수가 있으면 편수 우선.
    if units_comparable(a_unit, b_unit) and a_max > 0 and b_max > 0 and a_max != b_max:
        return a if a_max > b_max else b

    # 2. 단위가 다르거나 편수 동률/판독불가 → 공백 제거 글자수 비교(txt 한정, 마진 적용).
    a_chars = _entry_char_count(a)
    b_chars = _entry_char_count(b)
    if a_chars > 0 and b_chars > 0:
        larger = max(a_chars, b_chars)
        smaller = min(a_chars, b_chars)
        if smaller == 0 or (larger - smaller) / larger > CHAR_COUNT_MARGIN:
            return a if a_chars > b_chars else b
    else:
        # txt 글자수를 못 구하면(예: epub) 기존 바이트 비교로 폴백, 50KB 마진.
        if abs(a["size"] - b["size"]) > 50 * 1024:
            return a if a["size"] > b["size"] else b

    # 3. 완결 태그 유무.
    a_comp = 1 if a["complete"] else 0
    b_comp = 1 if b["complete"] else 0
    if a_comp != b_comp:
        return a if a_comp > b_comp else b

    # 4. 이름이 짧은(깔끔한) 것을 선호.
    if len(a["name"]) != len(b["name"]):
        return a if len(a["name"]) < len(b["name"]) else b

    # 5. 안정 타이브레이크: 이름 사전순.
    if a["name"] != b["name"]:
        return a if a["name"] < b["name"] else b

    return a


def compare_entries(a, b):
    better = get_better_entry(a, b)
    if better is a:
        return -1  # a comes first
    return 1  # b comes first


def choose_keep(entries):
    best = entries[0]
    for candidate in entries[1:]:
        best = get_better_entry(best, candidate)
    return best


def choose_keep_exact(entries):
    """완전 중복(내용 동일) 그룹의 keep 선정.

    내용이 같으므로 운영 기준으로 기존 house 원본을 보존하고 temp를 지운다.
    house가 하나 이상이면 house 후보들 중에서, 없으면 전체에서 일반 규칙으로 고른다.
    """
    protected = [e for e in entries if e.get("protected")]
    representatives = [e for e in entries if e.get("representative")]
    house = [e for e in entries if e.get("source") == "house"]
    pool = representatives or protected or house or entries
    return choose_keep(pool)


# primary bounded edge preview 임계값. 감사기의 전체 지문/적응형 앵커와 별도 경로다.
EDGE_SAME_THRESHOLD = 0.92
EDGE_DIFFERENT_THRESHOLD = 0.35
EDGE_MIN_CHARS = 512


def _entry_edges(entry):
    """파일당 최대 앞/뒤 64KiB만 읽은 primary preview를 entry에 캐시."""
    if "edge_preview" in entry:
        return entry["edge_preview"]
    edges = read_text_edges(entry["path"]) if entry.get("ext") == ".txt" else None
    entry["edge_preview"] = edges
    return edges


def classify_pair_details(a, b):
    """두 txt 항목을 same/different/ambiguous로 분류한다.

    판정은 작가 충돌 여부와 무관하게 항상 본문 유사도로 한다.
    "제목 같고 작가 다름"은 "제목 같고 내용 다름"의 한 종류일 뿐이라, 작가명이 아니라
    본문으로 가린다(필명만 바꾼 같은 작품은 본문이 같으므로 same으로 잡힌다).
    작가 충돌은 same/different를 가르는 기준이 아니라, ambiguous 구간에서의 행선지
    (author_conflicts vs warning) 결정에만 쓴다(find_suspect_groups 참조).

    - 비txt는 metadata_only, preview를 확실히 못 읽으면 ambiguous.
    - 앞/뒤가 모두 충분히 유사할 때만 same.
    - 앞/뒤가 모두 낮고 최소 길이 가드를 만족할 때만 different.
    """
    if a.get("ext") != ".txt" or b.get("ext") != ".txt":
        return "metadata_only", {"reason": "non_txt_no_body_evidence"}
    left = _entry_edges(a)
    right = _entry_edges(b)
    if not left or not right or left.uncertain or right.uncertain:
        return "ambiguous", {"reason": "edge_decode_uncertain"}
    if not left.front or not right.front or not left.tail or not right.tail:
        return "ambiguous", {"reason": "edge_missing"}
    front_ratio = preview_similarity(left.front, right.front)
    tail_ratio = preview_similarity(left.tail, right.tail)
    evidence = {
        "front_similarity": front_ratio,
        "tail_similarity": tail_ratio,
        "left_read_bytes": left.read_bytes,
        "right_read_bytes": right.read_bytes,
    }
    if min(len(left.front), len(right.front), len(left.tail), len(right.tail)) < EDGE_MIN_CHARS:
        evidence["reason"] = "edge_too_short"
        return "ambiguous", evidence
    if front_ratio >= EDGE_SAME_THRESHOLD and tail_ratio >= EDGE_SAME_THRESHOLD:
        evidence["reason"] = "front_tail_similar"
        return "same", evidence
    if front_ratio >= EDGE_SAME_THRESHOLD and tail_ratio < EDGE_SAME_THRESHOLD:
        evidence["reason"] = "common_front_tail_differs"
        return "ambiguous", evidence
    if front_ratio < EDGE_DIFFERENT_THRESHOLD and tail_ratio < EDGE_DIFFERENT_THRESHOLD:
        evidence["reason"] = "front_tail_different"
        return "different", evidence
    evidence["reason"] = "mixed_edge_evidence"
    return "ambiguous", evidence


def classify_pair(a, b):
    return classify_pair_details(a, b)[0]


def _author_token_set(value):
    if not value:
        return frozenset()
    parts = re.split(r"[\s,/&·]+", value)
    return frozenset(p.strip().lower() for p in parts if p and p.strip())


def _authors_conflict(a, b):
    ta = _author_token_set(a.get("author"))
    tb = _author_token_set(b.get("author"))
    if not ta or not tb:
        return False
    return not (ta & tb)


def _suspect_route(keep, entry):
    """검토 큐 라우팅 결정: 'move'(suspected_duplicates) 또는 'warning'(애매 보류).

    - txt: keep과 본문 same이면 중복으로 보고 move, 그 외(ambiguous)는 warning.
      different는 이미 assign_disambig에서 다른 버킷으로 갈렸으므로 여기 오지 않는다.
    - epub 등 preview 불가: 본문 비교가 불가하므로 기존 메타데이터 기반 동작을 유지한다.
      즉 제목/권수로 같은 버킷에 모인 항목은 종전처럼 suspected 큐로 move 한다
      (1.1.0에서 epub 로직은 변경하지 않음).
    """
    verdict, evidence = classify_pair_details(keep, entry)
    entry["_primary_classification"] = verdict
    entry["_primary_evidence"] = evidence
    if verdict == "same":
        return "move"
    if verdict == "metadata_only":
        return "metadata_only"
    if verdict == "different":
        return "different"
    return "warning"


def _primary_bucket_eligible(entry):
    return not entry.get("span_ambiguous", False) and entry.get("start_number") is not None and entry.get("end_number") is not None


def assign_disambig(entries, dry_run):
    """같은 버킷(같은 제목·회차범위) 안에서 본문이 다른 별개 작품에 〔Dn〕을 부여한다.

    - 비교 단위는 core_title이 아니라 버킷 키
      `(core_title, ext, volume_number, start_number, is_side_story)`다.
      core_title만으로 묶으면 같은 작품의 분권(`11-20화`, `21-30화`...)이 한 묶음에
      들어가 도입부가 서로 달라 전부 별개로 오판된다. 회차 범위가 다른 분권은
      애초에 다른 버킷이므로 본문 비교 대상에서 제외해야 한다.
    - 마커 없는(또는 D1) 파일을 같은 버킷의 기존 disambig 대표(D1/D2/...)와 본문
      비교해 same이면 그 번호 재사용, 모두 different면 다음 빈 번호를 부여한다
      (ambiguous는 분리하지 않음=base 유지).
    - 마커는 분리되는 쪽(신규 번호)에만 붙인다. `--run`에서만 실제 리네임.
    - 반환: 부여 기록 리스트. 각 record는 {entry, old, new, dest_path, dry_run}.

    txt만 대상으로 한다(epub은 preview 불가).
    """
    by_bucket = defaultdict(list)
    for e in entries:
        if e.get("ext") != ".txt":
            continue
        if not _primary_bucket_eligible(e):
            continue
        core_title = e["core_title"]
        if len(core_title) <= 1:
            continue
        bucket_key = (
            core_title,
            e.get("ext"),
            e.get("volume_number"),
            e.get("start_number"),
            e.get("end_number"),
            e.get("is_side_story", False),
        )
        by_bucket[bucket_key].append(e)

    records = []
    for bucket_key, items in sorted(by_bucket.items(), key=lambda x: str(x[0])):
        core_title = bucket_key[0]
        if len(items) < 2:
            continue

        # 이미 마커가 있는 항목은 그 번호의 대표로 등록(번호→대표 entry).
        reps = {}
        unmarked = []
        # 정렬 우선순위: house 먼저(기존 원본 보존), 그다음 이름순.
        # 이렇게 해야 base(D1) 대표가 house가 되고, temp 신규분이 비교/리네임 대상이 된다.
        def _rep_sort_key(x):
            return (0 if x.get("source") == "house" else 1, x["name"])

        for e in sorted(items, key=_rep_sort_key):
            d = e.get("disambig", 1)
            if d > 1:
                reps.setdefault(d, e)
            else:
                unmarked.append(e)

        # base(D1) 대표는 무마커 중 첫 항목(house 우선). 나머지 무마커는 비교 대상.
        if unmarked:
            reps.setdefault(1, unmarked[0])

        # 비교 대상도 house 먼저 처리하되, base 대표는 건너뛴다. (temp가 앞서 리네임되는 일 방지)
        for e in unmarked:
            if e is reps.get(1):
                continue  # base 대표 자신
            # 기존 대표들과 비교해 same인 번호를 찾는다.
            matched = None
            for num in sorted(reps):
                if classify_pair(e, reps[num]) == "same":
                    matched = num
                    break
            if matched is not None:
                # same → 그 번호 사용(이미 base면 마커 없음). 분리 아님.
                if matched > 1 and e.get("disambig", 1) != matched:
                    records.append(_apply_disambig(e, matched, dry_run))
                continue
            # 모든 대표와 different인지 확인. 하나라도 ambiguous면 분리 보류.
            verdicts = [classify_pair(e, reps[num]) for num in sorted(reps)]
            if all(v == "different" for v in verdicts):
                if e.get("source") == "house":
                    records.append({
                        "entry": dict(e),
                        "core_title": e["core_title"],
                        "old_name": e["name"],
                        "new_name": e["name"],
                        "old_disambig": e.get("disambig", 1),
                        "new_disambig": None,
                        "dest_path": None,
                        "dry_run": dry_run,
                        "status": "house_conflict_logged",
                    })
                    continue
                new_num = max(reps) + 1
                reps[new_num] = e
                records.append(_apply_disambig(e, new_num, dry_run))
            # ambiguous가 섞이면 분리하지 않고 base에 남긴다(자동 확정 금지).

    return records


def _apply_disambig(entry, num, dry_run):
    old_name = entry["name"]
    new_name = add_disambig_marker(strip_disambig_marker(old_name), num)
    directory = os.path.dirname(entry["path"])
    dest_path = os.path.join(directory, new_name)
    record = {
        "entry": dict(entry),
        "core_title": entry["core_title"],
        "old_name": old_name,
        "new_name": new_name,
        "old_disambig": entry.get("disambig", 1),
        "new_disambig": num,
        "dest_path": dest_path,
        "dry_run": dry_run,
        "status": "assigned",
    }
    if entry.get("source") == "house":
        record["status"] = "house_conflict_logged"
        record["dest_path"] = None
        return record
    if not dry_run:
        if dest_path == entry["path"]:
            # 이미 같은 이름(이론상 도달하지 않음). 변경 없음으로 처리.
            record["status"] = "noop"
        elif os.path.exists(dest_path):
            # 목적 경로 충돌: 덮어쓰지 않고 실패로 기록한다.
            # (성공으로 위장하면 인덱스/버킷 판단이 틀어진다.)
            record["status"] = "skipped_collision"
        else:
            shutil.move(entry["path"], dest_path)
            # entry를 새 상태로 갱신해 이후 버킷팅에 반영.
            entry["name"] = new_name
            entry["path"] = dest_path
            entry["disambig"] = num
    else:
        # dry-run에서도 이후 버킷 분리가 보이도록 메모리상 disambig만 갱신.
        entry["disambig"] = num
    return record


def find_exact_duplicates(entries):
    size_groups = defaultdict(list)
    for entry in entries:
        size_groups[entry["size"]].append(entry)

    exact_groups = []
    for paths in size_groups.values():
        if len(paths) < 2:
            continue

        hash_groups = defaultdict(list)
        for entry in paths:
            file_hash = calculate_hash(entry["path"])
            if file_hash:
                entry["hash"] = file_hash
                hash_groups[file_hash].append(entry)

        for file_hash, hash_entries in hash_groups.items():
            if len(hash_entries) < 2:
                continue
            keep = choose_keep_exact(hash_entries)
            exact_groups.append({
                "hash": file_hash,
                "keep": keep,
                "duplicates": [e for e in hash_entries if e is not keep],
            })

    return exact_groups


def find_suspect_groups(entries, excluded_paths):
    groups = defaultdict(list)
    for entry in entries:
        if entry["path"] in excluded_paths:
            continue
        if not _primary_bucket_eligible(entry):
            continue
        core_title = entry["core_title"]
        if len(core_title) > 1:
            groups[core_title].append(entry)

    suspect_groups = []
    for core_title, group_entries in sorted(groups.items()):
        if len(group_entries) < 2:
            continue

        # 확장자별로 분리하여 txt와 epub 간 교차 비교를 방지
        ext_groups = defaultdict(list)
        for e in group_entries:
            ext_groups[e["ext"]].append(e)

        for ext, ext_entries in sorted(ext_groups.items()):
            if len(ext_entries) < 2:
                continue

            buckets = defaultdict(list)
            for e in ext_entries:
                vn = e.get("volume_number")
                sn = e.get("start_number")
                en = e.get("end_number")
                side = e.get("is_side_story", False)
                dis = e.get("disambig", 1)
                buckets[(dis, vn, sn, en, side)].append(e)

            for key, bucket_entries in sorted(buckets.items(), key=lambda x: str(x[0])):
                if len(bucket_entries) < 2:
                    continue

                # 작가 후보 토큰화: '현판, 고별' 같은 다중 작가 표기와 '고별' 단독 표기를
                # 동일 인물로 보고 작가 충돌 판정에서 빼기 위해, 작가 문자열을 쉼표/공백
                # 단위로 나눠 셋 비교 후 교집합이 있으면 같은 작가군으로 본다.
                def _author_tokens(value):
                    if not value:
                        return frozenset()
                    parts = re.split(r"[\s,/&·]+", value)
                    return frozenset(p.strip().lower() for p in parts if p and p.strip())

                author_groups = [_author_tokens(e["author"]) for e in bucket_entries if e["author"]]
                # 모두 빈 집합이면 작가 미상, 한쪽만 있으면 충돌 아님 → 기존 distinct_authors=False
                # 둘 이상의 작가가 있을 때 모든 쌍에서 교집합이 있으면 충돌이 아니라고 본다.
                if len(author_groups) >= 2:
                    distinct_pairs_exist = False
                    for i in range(len(author_groups)):
                        for j in range(i + 1, len(author_groups)):
                            if author_groups[i] and author_groups[j] and not (author_groups[i] & author_groups[j]):
                                distinct_pairs_exist = True
                                break
                        if distinct_pairs_exist:
                            break
                    distinct_authors = distinct_pairs_exist
                else:
                    distinct_authors = False

                authors = sorted({e["author"] for e in bucket_entries if e["author"]})
                ordered = sorted(
                    bucket_entries,
                    key=lambda e: (0 if e.get("source") == "house" else 1, e.get("rel_path", ""), e["name"]),
                )

                # 분류 전에는 전체 글자수 기반 compare/get_better를 호출하지 않는다.
                house_entries = [e for e in bucket_entries if e.get("source") == "house"]
                anchor = sorted(
                    house_entries,
                    key=lambda e: (e.get("rel_path", ""), e["name"]),
                )[0] if house_entries else ordered[0]

                # anchor와 본문 same인 것만 중복 후보. 그 외(ambiguous)는 보류 라우팅.
                # 작가 충돌 여부는 same/ambiguous 판정에 쓰지 않고(본문이 기준), ambiguous
                # 항목의 행선지(author_conflicts vs warning)에만 쓴다. different는 이미
                # assign_disambig가 다른 버킷으로 분리했으므로 여기 거의 오지 않는다.
                same_set = [anchor]
                ambiguous = []
                for e in ordered:
                    if e is anchor:
                        continue
                    route = _suspect_route(anchor, e)
                    if route == "move" and not _authors_conflict(anchor, e):
                        same_set.append(e)
                    elif route != "different":
                        # 항목 단위로 anchor와의 작가 충돌 여부를 기록(버킷 전체가 아니라).
                        e["_author_conflict_with_anchor"] = _authors_conflict(anchor, e)
                        if route == "move" and e["_author_conflict_with_anchor"]:
                            e["_primary_classification"] = "author_conflict_preview_same"
                        ambiguous.append(e)

                # keep은 same으로 확정된 집합(anchor 포함) 안에서만 고른다.
                keep = choose_keep(same_set)
                move_entries = [
                    e for e in same_set
                    if e["path"] != keep["path"]
                ]

                # ambiguous 행선지: 대표 외 후보를 작가 충돌이면 author_conflicts,
                # 아니면 warning 검토 큐로 이동한다.
                warning_entries = ambiguous

                considered = same_set + ambiguous
                if len(considered) < 2:
                    continue

                suspect_groups.append({
                    "core_title": core_title,
                    "keep": keep,
                    "entries": considered,
                    "move_entries": move_entries,
                    "same_house_entries": [],
                    "warning_entries": warning_entries,
                    "authors": authors,
                    "distinct_authors": distinct_authors,
                })

    return suspect_groups


def move_or_delete_exact_group(group, temp_dir, dry_run, delete_exact):
    records = []
    exact_dir = os.path.join(temp_dir, "trash_bin", "exact_duplicates")
    legacy_blocked = any(
        not entry.get("mutation_eligible", True)
        for entry in [group["keep"], *group["duplicates"]]
    )

    for entry in group["duplicates"]:
        action = "legacy_report_only" if legacy_blocked else ("delete" if delete_exact else "move")
        dest_path = (
            None
            if delete_exact or legacy_blocked
            else unique_path(exact_dir, entry["name"], "_dup")
        )
        record = {
            "action": action,
            "dry_run": dry_run,
            "hash": group["hash"],
            "keep": group["keep"],
            "entry": entry,
            "dest_path": dest_path,
            "size": entry["size"],
        }

        if not dry_run and not legacy_blocked:
            if delete_exact:
                os.remove(entry["path"])
            else:
                shutil.move(entry["path"], dest_path)

        records.append(record)

    return records


def move_suspect_group(group, temp_dir, dry_run):
    """의심 그룹을 라우팅한다.

    판정 기준은 본문 유사도다(작가 충돌은 same 판정에 쓰지 않음).
    - 본문 same으로 확정된 중복(필명 변경 포함): trash_bin/suspected_duplicates 로 이동.
    - ambiguous(애매, 0.35~0.85): 대표 1개만 남기고 나머지를 검토 큐로 격리한다.
      · 작가 충돌이면 trash_bin/author_conflicts(사람 판별, status='author_review'),
        아니면 trash_bin/warning(status='warning')로 보류 이동한다.
        작가 충돌 판단은 버킷 전체가 아니라 항목별 anchor와의 충돌(_author_conflict_with_anchor)로 한다.

    별개 작품(different)은 이미 assign_disambig가 다른 버킷으로 분리했으므로
    여기서는 same/ambiguous만 다룬다.
    """
    records = []
    if any(not entry.get("mutation_eligible", True) for entry in group.get("entries", [])):
        return records
    suspect_dir = os.path.join(temp_dir, "trash_bin", "suspected_duplicates")
    author_dir = os.path.join(temp_dir, "trash_bin", "author_conflicts")
    warning_dir = os.path.join(temp_dir, "trash_bin", "warning")

    def _route(entry, dest_dir, status):
        dest_path = unique_path(dest_dir, entry["name"], "_suspect")
        record = {
            "dry_run": dry_run,
            "core_title": group["core_title"],
            "keep": group["keep"],
            "entry": entry,
            "dest_path": dest_path,
            "size": entry["size"],
            "distinct_authors": group["distinct_authors"],
            "status": status,
        }
        if not dry_run:
            shutil.move(entry["path"], dest_path)
        records.append(record)

    # 본문 same으로 확정된 중복은 작가 충돌과 무관하게 suspected_duplicates로.
    for entry in group.get("move_entries", []):
        _route(entry, suspect_dir, "moved")
    # ambiguous: 출처와 무관하게 author_conflicts/warning으로 분기.
    for entry in group.get("warning_entries", []):
        classification = entry.get("_primary_classification")
        if classification == "metadata_only":
            _route(entry, warning_dir, "metadata_only")
            continue
        if entry.get("_author_conflict_with_anchor"):
            _route(entry, author_dir, "author_review")
        else:
            _route(entry, warning_dir, "warning")

    return records


def run_auditor_queue_report(
    index_path, house_dir, temp_dir, state_db_path=None, cache_write=True,
    progress_callback=None, rebaseline=False, verified_house_inventory=None,
    before_non_cache_mutation=None,
):
    """folderling 검토 큐용 read-only auditor 실행. 파일 이동은 호출 측에서만 한다."""
    import duplicate_auditor

    required = (
        duplicate_auditor.AUDITOR_VERSION == "1.4.17"
        and duplicate_auditor.MANAGED_REPRESENTATIVE_MODE == "normalized_sha_join"
        and duplicate_auditor.SUPPORTS_READ_ONLY_CACHE is True
    )
    if not required:
        raise RuntimeError("duplicate auditor feature contract mismatch; fail closed")

    argv = [
        "--index", os.fspath(index_path),
        "--house", os.fspath(house_dir),
        "--temp", os.fspath(temp_dir),
        # Keep the normal Folderling policy identical to the persisted warm
        # auditor cache.  Resource caps are not fingerprint semantics, but
        # max_file_bytes is deliberately part of the pair-cache key.
        "--max-file-bytes", "256MiB",
        "--max-read-bytes", (
            FOLDERLING_REBASELINE_READ_BUDGET
            if rebaseline else FOLDERLING_AUDIT_READ_BUDGET
        ),
    ]
    if rebaseline:
        argv.extend((
            "--max-deep-pairs-per-file",
            str(FOLDERLING_REBASELINE_DEEP_PAIRS_PER_FILE),
        ))
    if state_db_path:
        argv.extend(("--state-db", os.fspath(state_db_path)))
    args = duplicate_auditor.build_parser().parse_args(argv)
    args.progress = True
    args.cache_write = cache_write
    args.progress_callback = progress_callback
    args.verified_house_inventory = verified_house_inventory
    args.before_non_cache_mutation = before_non_cache_mutation
    return duplicate_auditor.run_audit(args)


def _auditor_rebaseline_reasons(report):
    """Return resource-only stop reasons eligible for one cold-cache retry."""
    if report is None or getattr(report, "completed", False):
        return ()
    reasons = frozenset(getattr(report, "stop_reasons", ()) or ())
    if reasons and reasons <= FOLDERLING_REBASELINE_STOP_REASONS:
        return tuple(sorted(reasons))
    return ()


def build_auditor_suspect_groups(
    report, entries, excluded_paths=(), blocked_relations=None
):
    """Build mutation groups from strong TXT/EPUB equivalence edges only.

    Weak/report-only classifications never enter this DSU.  Existing unassigned
    house files and conflicting coordinates/managed identities are also vetoed
    before an edge is added.
    """
    if report is None or not getattr(report, "completed", False):
        return []

    excluded = set(excluded_paths)
    by_source_rel = {
        (entry.get("source", "house"), normalize_nfc(entry.get("rel_path", ""))): entry
        for entry in entries
    }
    parent = {}
    incident = defaultdict(set)

    def coordinates_compatible(left, right):
        relation = classify_dedup_coordinate_relation(
            left.get("name", ""), right.get("name", ""),
            left_span_ambiguous=bool(left.get("span_ambiguous")),
            right_span_ambiguous=bool(right.get("span_ambiguous")),
        )
        if relation is not None:
            return True
        import decision_store

        return decision_store.coordinates_compatible(
            decision_store.coordinate_fields_from_name(left.get("name", "")),
            decision_store.coordinate_fields_from_name(right.get("name", "")),
        )

    def identity_veto(left, right):
        if not coordinates_compatible(left, right):
            return "coordinate_conflict"
        if left.get("assignment_state") == "managed" and right.get("assignment_state") == "managed":
            if left.get("work_bucket_id") != right.get("work_bucket_id"):
                return "distinct_work"
            if left.get("variant_id") != right.get("variant_id"):
                return "distinct_variant"
        return None

    def find(key):
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return True
        left_members = [key for key in parent if find(key) == left_root]
        right_members = [key for key in parent if find(key) == right_root]
        for left_key in left_members:
            for right_key in right_members:
                if identity_veto(by_source_rel[left_key], by_source_rel[right_key]):
                    return False
        parent[right_root] = left_root
        return True

    for result in getattr(report, "results", []):
        classification = result.get("classification")
        if classification not in AUDITOR_STRONG_CLASSES:
            continue
        if result.get("origin") != "auditor_aux":
            continue
        sides = []
        for label in ("left", "right"):
            side = result.get(label) or {}
            key = (side.get("source"), normalize_nfc(side.get("rel_path", "")))
            entry = by_source_rel.get(key)
            if not entry or entry.get("path") in excluded:
                sides = []
                break
            sides.append((key, entry))
            length = (result.get("evidence") or {}).get(f"{label}_normalized_length")
            if isinstance(length, int) and length > 0:
                entry["char_count"] = length
        if len(sides) != 2:
            continue
        # Strong body evidence may bridge nested episode spans, side-story
        # redistribution and 1-N episode/volume editions.  It must not bridge
        # explicit sibling volumes (1권 vs 2권) or disjoint episode ranges.
        veto = identity_veto(sides[0][1], sides[1][1])
        if veto:
            if blocked_relations is not None:
                blocked_relations.append({
                    "origin": "auditor_aux",
                    "classification": classification,
                    "left": sides[0][1],
                    "right": sides[1][1],
                    "reason": veto,
                    "mutation_eligible": False,
                })
            continue
        left_key, right_key = sides[0][0], sides[1][0]
        if not union(left_key, right_key):
            if blocked_relations is not None:
                blocked_relations.append({
                    "origin": "auditor_aux",
                    "classification": classification,
                    "left": sides[0][1],
                    "right": sides[1][1],
                    "reason": "component_coordinate_or_identity_conflict",
                    "mutation_eligible": False,
                })
            continue
        incident[left_key].add(classification)
        incident[right_key].add(classification)

    components = defaultdict(list)
    for key in parent:
        components[find(key)].append(key)

    groups = []
    for keys in components.values():
        members = [by_source_rel[key] for key in keys]
        if len(members) < 2:
            continue
        members = sorted(
            members,
            key=lambda entry: (entry.get("source", "house"), entry.get("rel_path", ""), entry["name"]),
        )
        protected = [entry for entry in members if entry.get("protected")]
        representatives = [entry for entry in members if entry.get("representative")]
        managed_house_representatives = [
            entry for entry in representatives
            if entry.get("source") == "house"
            and entry.get("assignment_state") == "managed"
        ]
        if len(managed_house_representatives) == 1:
            keep = managed_house_representatives[0]
        elif protected:
            keep = sorted(
                protected,
                key=lambda entry: (entry.get("source") != "house", entry.get("rel_path", "")),
            )[0]
        else:
            keep = choose_keep_exact(members)
        move_entries = []
        same_house_entries = []
        classifications = set()
        component_blocked = (
            len(managed_house_representatives) > 1
            or
            len(protected) > 1
            or not any(entry.get("source") == "house" for entry in members)
            or any(not entry.get("mutation_eligible", True) for entry in members)
        )
        for key in keys:
            entry = by_source_rel[key]
            classifications.update(incident[key])
            if entry is keep:
                continue
            if not component_blocked:
                if entry.get("source") == "temp":
                    move_entries.append(entry)
                elif entry.get("source") == "house" and not entry.get("protected"):
                    same_house_entries.append(entry)
        authors = sorted({entry["author"] for entry in members if entry.get("author")})
        groups.append({
            "origin": "auditor_aux",
            "core_title": keep.get("core_title") or "auditor_aux",
            "keep": keep,
            "entries": members,
            "move_entries": move_entries,
            "same_house_entries": same_house_entries,
            "warning_entries": [],
            "authors": authors,
            "distinct_authors": any(_authors_conflict(keep, entry) for entry in members if entry is not keep),
            "audit_classifications": sorted(classifications),
            "classification": (
                next(iter(classifications))
                if len(classifications) == 1 else "text_equivalent"
            ),
            "mutation_blocked": component_blocked,
        })
    return groups


def build_auditor_report_relations(report, entries, excluded_paths=()):
    """Return direct weak/report-only relations without mutation fields."""
    if report is None or not getattr(report, "completed", False):
        return []
    excluded = set(excluded_paths)
    by_source_rel = {
        (entry.get("source", "house"), normalize_nfc(entry.get("rel_path", ""))): entry
        for entry in entries
    }
    relations = []
    for result in getattr(report, "results", []):
        classification = result.get("classification")
        if classification not in AUDITOR_RELATION_CLASSES:
            continue
        if result.get("origin") != "auditor_aux":
            continue
        matched = []
        evidence = result.get("evidence") or {}
        for label in ("left", "right"):
            side = result.get(label) or {}
            key = (side.get("source"), normalize_nfc(side.get("rel_path", "")))
            entry = by_source_rel.get(key)
            if entry is None or entry.get("path") in excluded:
                matched = []
                break
            normalized_length = evidence.get(f"{label}_normalized_length")
            if isinstance(normalized_length, int) and normalized_length > 0:
                entry["char_count"] = normalized_length
            matched.append(entry)
        if len(matched) != 2:
            continue
        relations.append({
            "origin": "auditor_aux",
            "classification": classification,
            "left": matched[0],
            "right": matched[1],
            "evidence": evidence,
            "mutation_eligible": False,
        })
    return relations


def find_multi_representative_candidates(exact_groups, report, entries):
    """Return temp paths directly matching more than one managed representative."""
    by_source_rel = {
        (entry.get("source", "house"), normalize_nfc(entry.get("rel_path", ""))): entry
        for entry in entries
    }
    matches = defaultdict(set)

    def add(candidate, reference):
        if candidate.get("source") != "temp":
            return
        if reference.get("assignment_state") != "managed" or not reference.get("representative"):
            return
        identity = (reference.get("work_bucket_id"), reference.get("variant_id"))
        if None not in identity:
            matches[candidate["path"]].add(identity)

    for group in exact_groups:
        members = [group["keep"], *group["duplicates"]]
        representatives = [entry for entry in members if entry.get("representative")]
        for candidate in members:
            for reference in representatives:
                if candidate is not reference:
                    add(candidate, reference)

    if report is not None and getattr(report, "completed", False):
        for result in getattr(report, "results", []):
            if (
                result.get("origin") != "auditor_aux"
                or result.get("classification") not in AUDITOR_STRONG_CLASSES
            ):
                continue
            resolved = []
            for label in ("left", "right"):
                side = result.get(label) or {}
                resolved.append(by_source_rel.get((
                    side.get("source"), normalize_nfc(side.get("rel_path", ""))
                )))
            if all(resolved):
                add(resolved[0], resolved[1])
                add(resolved[1], resolved[0])
    return {path for path, identities in matches.items() if len(identities) > 1}


def mark_multi_representative_candidates(
    entries, candidate_paths, state_db_path, *, dry_run,
):
    if not candidate_paths:
        return
    file_ids = []
    for entry in entries:
        if entry.get("path") not in candidate_paths:
            continue
        entry["assignment_state"] = "decision_required"
        entry["mutation_eligible"] = False
        if entry.get("file_id"):
            file_ids.append(entry["file_id"])
    if dry_run or not file_ids:
        return
    import decision_store

    conn = decision_store.connect_state_db(state_db_path)
    try:
        with decision_store.transaction(conn):
            conn.executemany(
                """
                UPDATE files SET assignment_state = 'decision_required', assignment_origin = NULL
                WHERE file_id = ? AND source = 'temp' AND active = 1
                """,
                ((file_id,) for file_id in file_ids),
            )
    finally:
        conn.close()


def enrich_entries_from_state_db(entries, state_db_path, read_only=False):
    """Refresh decision/protection fields after auditor reconciliation."""
    if not state_db_path or not os.path.isfile(state_db_path):
        return
    import decision_store

    conn = (
        decision_store.connect_state_db_readonly(state_db_path)
        if read_only else decision_store.connect_state_db(state_db_path)
    )
    try:
        rows = conn.execute(
            """
            SELECT f.canonical_path, f.file_id, f.assignment_state, f.variant_id,
                   f.protected, v.work_bucket_id, f.coordinate_kind,
                   f.part_num, f.part_den, f.volume_num, f.volume_den,
                   f.coordinate_symbol, f.coordinate_sort_key,
                   f.episode_start, f.episode_end, f.coordinate_raw, f.span_ambiguous,
                   CASE WHEN r.file_id IS NULL THEN 0 ELSE 1 END AS representative
            FROM files AS f
            LEFT JOIN variants AS v ON v.variant_id = f.variant_id
            LEFT JOIN representatives AS r ON r.file_id = f.file_id
            WHERE f.active = 1
            """
        ).fetchall()
        # macOS commonly exposes an on-disk filename in NFD while SQLite keeps
        # the reconciled canonical path in NFC.  ``abspath`` alone therefore
        # misses the very temp row that the auditor just created, leaving its
        # file_id unset and turning a proven duplicate into report-only intake.
        by_path = {
            decision_store.canonicalize_path(row["canonical_path"]): row
            for row in rows
        }
        for entry in entries:
            row = by_path.get(decision_store.canonicalize_path(entry["path"]))
            if row is None:
                continue
            entry.update({
                "file_id": row["file_id"],
                "assignment_state": row["assignment_state"],
                "variant_id": row["variant_id"],
                "work_bucket_id": row["work_bucket_id"],
                "protected": bool(row["protected"]),
                "representative": bool(row["representative"]),
                "coordinate_kind": row["coordinate_kind"],
                "part_num": row["part_num"],
                "part_den": row["part_den"],
                "volume_num": row["volume_num"],
                "volume_den": row["volume_den"],
                "coordinate_symbol": row["coordinate_symbol"],
                "coordinate_sort_key": row["coordinate_sort_key"],
                "episode_start": row["episode_start"],
                "episode_end": row["episode_end"],
                "coordinate_raw": row["coordinate_raw"],
                "span_ambiguous": bool(row["span_ambiguous"]),
                "mutation_eligible": row["assignment_state"] not in {
                    "legacy_unresolved", "decision_required"
                },
            })
    finally:
        conn.close()


def _managed_exact_records(
    exact_groups,
    state_db_path,
    temp_dir,
    dry_run,
    actual_run_id=None,
    blocked_candidate_paths=(),
):
    import decision_store
    from dedup_mutations import (
        exact_quarantine,
        queue_exact_review_relationships_preserved,
    )

    records = []
    blocked_candidates = set(blocked_candidate_paths)
    conn = decision_store.connect_state_db(state_db_path)
    quarantine_dir = os.path.join(temp_dir, "trash_bin", "exact_quarantine")
    try:
        for group in exact_groups:
            keep = group["keep"]
            component_members = [keep, *group["duplicates"]]

            def pair_coordinates_compatible(left, right):
                left_core = analyze_name(left.get("name", "")).get("core_title")
                right_core = analyze_name(right.get("name", "")).get("core_title")
                if left_core and right_core and left_core != right_core:
                    return True
                relation = classify_dedup_coordinate_relation(
                    left.get("name", ""), right.get("name", ""),
                    left_span_ambiguous=bool(left.get("span_ambiguous")),
                    right_span_ambiguous=bool(right.get("span_ambiguous")),
                )
                return relation is not None or decision_store.coordinates_compatible(
                    decision_store.coordinate_fields_from_name(
                        left.get("name", "")
                    ),
                    decision_store.coordinate_fields_from_name(
                        right.get("name", "")
                    ),
                )

            component_coordinate_conflict = any(
                not pair_coordinates_compatible(left, right)
                for index, left in enumerate(component_members)
                for right in component_members[index + 1:]
            )
            for entry in group["duplicates"]:
                record = {
                    "action": "exact_quarantine",
                    "dry_run": dry_run,
                    "hash": group["hash"],
                    "keep": keep,
                    "entry": entry,
                    "dest_path": None,
                    "size": entry["size"],
                }
                keep_state = keep.get("assignment_state")
                entry_state = entry.get("assignment_state")
                queue_keep_eligible = bool(
                    keep.get("source") == "queue"
                    and entry.get("source") == "queue"
                    and keep.get("file_id")
                    and entry.get("file_id")
                    and queue_exact_review_relationships_preserved(
                        conn, entry["file_id"], keep["file_id"]
                    )
                )
                managed_relation_conflict = (
                    entry_state == "managed"
                    and (
                        keep_state != "managed"
                        or entry.get("variant_id") != keep.get("variant_id")
                    )
                )
                coordinate_conflict = (
                    component_coordinate_conflict
                    or not pair_coordinates_compatible(entry, keep)
                )
                if entry.get("path") in blocked_candidates:
                    record["action"] = "managed_report_only"
                    record["reason"] = "multi_representative_conflict"
                    records.append(record)
                    continue
                if coordinate_conflict:
                    record["action"] = "managed_report_only"
                    record["reason"] = "incompatible_canonical_coordinates"
                    records.append(record)
                    continue
                if (
                    keep.get("source") == "queue"
                    and entry.get("source") == "queue"
                    and not queue_keep_eligible
                ):
                    record["action"] = "managed_report_only"
                    record["reason"] = "queue_review_relationship_conflict"
                    records.append(record)
                    continue
                if (
                    keep.get("source") == "temp"
                    and entry.get("source") == "temp"
                    and keep.get("file_id")
                    and entry.get("file_id")
                    and keep_state == "unassigned"
                    and entry_state == "unassigned"
                    and not keep.get("protected")
                    and not keep.get("representative")
                    and not entry.get("protected")
                    and not entry.get("representative")
                    and keep.get("mutation_eligible", True)
                    and entry.get("mutation_eligible", True)
                    and not managed_relation_conflict
                ):
                    record["action"] = "managed_report_only"
                    record["reason"] = "await_same_run_house_keep"
                    records.append(record)
                    continue
                if (
                    not keep.get("file_id")
                    or (
                        keep.get("source") != "house"
                        and not queue_keep_eligible
                    )
                    or keep_state not in {
                        "managed", "unassigned", "legacy_unresolved", "decision_required"
                    }
                    or (keep_state == "managed" and not keep.get("representative"))
                    or not entry.get("file_id")
                    or entry.get("protected")
                    or entry.get("representative")
                    or (
                        not entry.get("mutation_eligible", True)
                        and entry_state not in {"legacy_unresolved", "decision_required"}
                    )
                    or (
                        entry.get("source") == "house"
                        and entry_state not in {
                            "managed", "unassigned", "legacy_unresolved", "decision_required"
                        }
                    )
                    or managed_relation_conflict
                ):
                    record["action"] = "managed_report_only"
                    records.append(record)
                    continue
                if dry_run:
                    records.append(record)
                    continue
                result = exact_quarantine(
                    conn,
                    source_file_id=entry["file_id"],
                    keep_file_id=keep["file_id"],
                    quarantine_dir=quarantine_dir,
                    run_id=actual_run_id,
                )
                record["dest_path"] = result["dest_path"]
                record["operation_id"] = result["operation_id"]
                records.append(record)
    finally:
        conn.close()
    return records


def cleanup_post_intake_exact_duplicates(
    state_db_path, temp_dir, actual_run_id, *, candidate_paths=None
):
    """Converge exact temp duplicates created by this run's intake ordering.

    The initial dedup snapshot may contain two identical temp files but no house
    keep.  One temp copy is therefore retained for intake and the other is held
    report-only.  Once the retained copy is durably ingested, this narrow pass
    finds only active temp rows matching that same run's committed house-ingest
    SHA and sends them through the normal manifest-bound exact quarantine.
    """

    import decision_store
    from dedup_mutations import exact_quarantine

    if candidate_paths is not None and not candidate_paths:
        return []
    conn = decision_store.connect_state_db(state_db_path)
    records = []
    quarantine_dir = os.path.join(temp_dir, "trash_bin", "exact_quarantine")
    allowed_paths = (
        {
            decision_store.canonicalize_path(path)
            for path in candidate_paths
        }
        if candidate_paths is not None else None
    )
    try:
        candidates = conn.execute(
            """
            SELECT source.file_id AS source_file_id,
                   source.canonical_path AS source_path,
                   keep.file_id AS keep_file_id,
                   keep.canonical_path AS keep_path,
                   origin.operation_id AS keep_origin_operation_id,
                   origin.source_sha256 AS raw_sha256
            FROM operations AS origin
            JOIN files AS keep
              ON keep.file_id = origin.file_id
             AND keep.active = 1
             AND keep.source = 'house'
             AND keep.canonical_path = origin.dest_path
            JOIN fingerprints AS source_fp
              ON source_fp.raw_sha256 = origin.source_sha256
             AND source_fp.size = origin.destination_size
            JOIN files AS source
              ON source.current_fingerprint_id = source_fp.fingerprint_id
             AND source.active = 1
             AND source.source = 'temp'
             AND source.file_id != keep.file_id
            WHERE origin.run_id = ?
              AND origin.action = 'house_ingest'
              AND origin.state = 'committed'
              AND origin.source_sha256 IS NOT NULL
              AND origin.destination_size IS NOT NULL
            ORDER BY source.canonical_path, keep.canonical_path,
                     origin.operation_id
            """,
            (actual_run_id,),
        ).fetchall()
        handled_sources = set()
        for candidate in candidates:
            source_file_id = candidate["source_file_id"]
            if (
                allowed_paths is not None
                and decision_store.canonicalize_path(candidate["source_path"])
                not in allowed_paths
            ):
                continue
            if source_file_id in handled_sources:
                continue
            result = exact_quarantine(
                conn,
                source_file_id=source_file_id,
                keep_file_id=candidate["keep_file_id"],
                quarantine_dir=quarantine_dir,
                run_id=actual_run_id,
            )
            handled_sources.add(source_file_id)
            records.append({
                **result,
                "source_path": candidate["source_path"],
                "keep_path": candidate["keep_path"],
                "keep_origin_operation_id": candidate[
                    "keep_origin_operation_id"
                ],
                "raw_sha256": candidate["raw_sha256"],
            })
    finally:
        conn.close()
    return records


def cleanup_relationship_preserving_queue_exact_duplicates(
    state_db_path, temp_dir, actual_run_id
):
    """Collapse an exact two-file queue group only when reviews are redundant.

    Managed temp scanning intentionally excludes ``trash_bin``.  Queue rows are
    nevertheless active state and included in the actual-run manifest, so a
    byte-identical pair could otherwise remain indefinitely as a strong review
    item.  Groups larger than two and pairs with asymmetric external review
    relationships stay fail-closed.
    """

    import decision_store
    from dedup_mutations import (
        exact_quarantine,
        queue_exact_review_relationships_preserved,
    )

    conn = decision_store.connect_state_db(state_db_path)
    records = []
    quarantine_dir = os.path.join(temp_dir, "trash_bin", "exact_quarantine")
    try:
        groups = conn.execute(
            """
            SELECT fp.raw_sha256, fp.size
            FROM files AS f
            JOIN fingerprints AS fp
              ON fp.fingerprint_id = f.current_fingerprint_id
            WHERE f.active = 1 AND f.source = 'queue'
              AND fp.raw_sha256 IS NOT NULL
            GROUP BY fp.raw_sha256, fp.size
            HAVING COUNT(*) = 2
            ORDER BY fp.raw_sha256, fp.size
            """
        ).fetchall()
        for group in groups:
            members = conn.execute(
                """
                SELECT f.file_id, f.canonical_path
                FROM files AS f
                JOIN fingerprints AS fp
                  ON fp.fingerprint_id = f.current_fingerprint_id
                WHERE f.active = 1 AND f.source = 'queue'
                  AND fp.raw_sha256 = ? AND fp.size = ?
                ORDER BY f.canonical_path, f.file_id
                """,
                (group["raw_sha256"], group["size"]),
            ).fetchall()
            if len(members) != 2:
                continue
            directions = (
                (members[1], members[0]),
                (members[0], members[1]),
            )
            selected = next((
                (source, keep)
                for source, keep in directions
                if queue_exact_review_relationships_preserved(
                    conn, source["file_id"], keep["file_id"]
                )
            ), None)
            if selected is None:
                continue
            source, keep = selected
            result = exact_quarantine(
                conn,
                source_file_id=source["file_id"],
                keep_file_id=keep["file_id"],
                quarantine_dir=quarantine_dir,
                run_id=actual_run_id,
            )
            records.append({
                **result,
                "source_path": source["canonical_path"],
                "keep_path": keep["canonical_path"],
                "raw_sha256": group["raw_sha256"],
            })
    finally:
        conn.close()
    return records


def cleanup_pending_queue_strong_reviews(
    state_db_path, house_dir, temp_dir, actual_run_id
):
    """Finalize current persisted strong reviews that the temp scan can miss.

    ``trash_bin`` is intentionally outside normal temp discovery.  A persisted
    queue review can nevertheless become a complete prefix or a current 95%
    ordered-body proof.  House/house reviews can also remain after a stronger
    rule is deployed while no temp input exists.  This pass considers only
    active house/queue endpoints with at least one house keep, honors active
    human distinct-edition decisions, and routes every mutation through the
    existing pinned proof, manifest, and journal APIs.
    """

    import decision_store
    from dedup_mutations import (
        ContainedUpgradeNotProven,
        OrderedBodyMatchNotProven,
        apply_contained_upgrade,
        apply_ordered_body_quarantine,
    )

    conn = decision_store.connect_state_db(state_db_path)
    records = []
    superseded_dir = os.path.join(temp_dir, "trash_bin", "superseded_versions")
    ordered_dir = os.path.join(temp_dir, "trash_bin", "ordered_body_duplicates")

    def entry(prefix, row):
        path = row[f"{prefix}_path"]
        assignment_state = row[f"{prefix}_assignment_state"]
        parsed = analyze_name(os.path.basename(path))
        stored_complete = row[f"{prefix}_complete"]
        return {
            "file_id": row[f"{prefix}_file_id"],
            "path": path,
            "name": os.path.basename(path),
            "source": row[f"{prefix}_source"],
            "assignment_state": assignment_state,
            "variant_id": row[f"{prefix}_variant_id"],
            "protected": bool(row[f"{prefix}_protected"]),
            "representative": bool(row[f"{prefix}_representative"]),
            "span_ambiguous": bool(row[f"{prefix}_span_ambiguous"]),
            "core_title": row[f"{prefix}_core_title"] or parsed["core_title"],
            "author": row[f"{prefix}_author"] or parsed["author"],
            "unit": row[f"{prefix}_unit"] or parsed.get("unit", "미상"),
            "normalized_length": row[f"{prefix}_normalized_length"],
            "char_count": row[f"{prefix}_normalized_length"] or 0,
            "current_fingerprint_id": row[f"{prefix}_current_fingerprint_id"],
            "complete": bool(
                parsed["complete"]
                if stored_complete is None else stored_complete
            ),
            "size": row[f"{prefix}_size"],
            "ext": os.path.splitext(path)[1].lower(),
            "mutation_eligible": assignment_state not in {
                "legacy_unresolved", "decision_required"
            },
        }

    def destination_for_queue(queue_entry, house_entry):
        clean_name = materialize_title_markup(
            normalize_filename(queue_entry["name"])
        )
        if not clean_name:
            return None, None
        destination = os.path.join(
            os.path.dirname(house_entry["path"]), clean_name
        )
        if os.path.lexists(destination):
            return None, None
        recent_path = os.path.join(
            house_dir, "_최근", os.path.basename(destination)
        )
        if os.path.lexists(recent_path):
            from folderling import recent_link_matches_destination

            if not recent_link_matches_destination(recent_path, destination):
                return None, None
        return destination, recent_path

    query = """
        SELECT r.review_id, r.classification,
               c.file_id AS candidate_file_id,
               c.canonical_path AS candidate_path,
               c.source AS candidate_source,
               c.assignment_state AS candidate_assignment_state,
               c.variant_id AS candidate_variant_id,
               c.protected AS candidate_protected,
               c.span_ambiguous AS candidate_span_ambiguous,
               c.current_fingerprint_id AS candidate_current_fingerprint_id,
               c.size AS candidate_size,
               CASE WHEN cr.file_id IS NULL THEN 0 ELSE 1 END
                   AS candidate_representative,
               cfp.normalized_length AS candidate_normalized_length,
               cfa.core_title AS candidate_core_title,
               cfa.author AS candidate_author,
               cfa.unit AS candidate_unit,
               cfa.complete AS candidate_complete,
               k.file_id AS reference_file_id,
               k.canonical_path AS reference_path,
               k.source AS reference_source,
               k.assignment_state AS reference_assignment_state,
               k.variant_id AS reference_variant_id,
               k.protected AS reference_protected,
               k.span_ambiguous AS reference_span_ambiguous,
               k.current_fingerprint_id AS reference_current_fingerprint_id,
               k.size AS reference_size,
               CASE WHEN kr.file_id IS NULL THEN 0 ELSE 1 END
                   AS reference_representative,
               kfp.normalized_length AS reference_normalized_length,
               kfa.core_title AS reference_core_title,
               kfa.author AS reference_author,
               kfa.unit AS reference_unit,
               kfa.complete AS reference_complete,
               r.left_fingerprint_id, r.right_fingerprint_id,
               r.evidence_json
        FROM review_items AS r
        JOIN files AS c ON c.file_id = r.candidate_file_id
        JOIN files AS k ON k.file_id = r.reference_file_id
        LEFT JOIN representatives AS cr ON cr.file_id = c.file_id
        LEFT JOIN representatives AS kr ON kr.file_id = k.file_id
        LEFT JOIN fingerprints AS cfp
          ON cfp.fingerprint_id = c.current_fingerprint_id
        LEFT JOIN fingerprints AS kfp
          ON kfp.fingerprint_id = k.current_fingerprint_id
        LEFT JOIN file_analysis AS cfa ON cfa.file_id = c.file_id
        LEFT JOIN file_analysis AS kfa ON kfa.file_id = k.file_id
        WHERE r.state IN ('pending', 'deferred')
          AND r.classification IN (
            'contained_exact', 'contained_version', 'ordered_body_match',
            'near_identical'
          )
          AND c.active = 1 AND k.active = 1
          AND c.source IN ('house', 'queue')
          AND k.source IN ('house', 'queue')
          AND (c.source = 'house' OR k.source = 'house')
        ORDER BY r.review_id
    """
    try:
        rows = conn.execute(query).fetchall()
        settled = set()
        for row in rows:
            pair_ids = frozenset((
                row["candidate_file_id"], row["reference_file_id"]
            ))
            if pair_ids & settled:
                continue
            if (
                row["left_fingerprint_id"]
                != row["candidate_current_fingerprint_id"]
                or row["right_fingerprint_id"]
                != row["reference_current_fingerprint_id"]
            ):
                continue
            if _current_review_evidence_precludes_action(
                row["classification"], row["evidence_json"]
            ):
                continue
            if decision_store.suppress_open_reviews_for_active_distinct_decision(
                conn,
                row["candidate_file_id"],
                row["reference_file_id"],
            ) is not None:
                continue

            left = entry("candidate", row)
            right = entry("reference", row)
            relation = {
                "classification": row["classification"],
                "left": left,
                "right": right,
                "evidence": {
                    "left_normalized_length": left["normalized_length"],
                    "right_normalized_length": right["normalized_length"],
                },
            }

            destination = recent_path = None
            if row["classification"] in {
                "contained_exact", "contained_version",
            }:
                direction = _contained_upgrade_direction(relation)
                if direction is None:
                    continue
                shorter, longer = direction
                if longer["source"] == "queue":
                    destination, recent_path = destination_for_queue(
                        longer, shorter
                    )
                    if destination is None:
                        continue
                try:
                    result = apply_contained_upgrade(
                        conn,
                        review_id=row["review_id"],
                        shorter_file_id=shorter["file_id"],
                        longer_file_id=longer["file_id"],
                        quarantine_dir=superseded_dir,
                        run_id=actual_run_id,
                        house_destination=destination,
                        classification=row["classification"],
                    )
                except ContainedUpgradeNotProven as exc:
                    _record_current_strong_proof_not_sufficient(
                        conn, row["review_id"], row["classification"], exc
                    )
                    continue
                record = {
                    "status": "superseded",
                    "classification": row["classification"],
                    "review_id": row["review_id"],
                    "source_file_id": shorter["file_id"],
                    "keep_file_id": longer["file_id"],
                    "source_path": shorter["path"],
                    "keep_path": result["ingested_path"],
                    "dest_path": result["quarantine_path"],
                    "operation_id": result["quarantine_operation_id"],
                    "ingest_operation_id": result["ingest_operation_id"],
                }
            else:
                direction = (
                    _ordered_body_direction(relation)
                    if row["classification"] == "ordered_body_match"
                    else _bidirectional_near_direction(relation)
                )
                if direction is None:
                    continue
                discard, keep, _coordinate_mode = direction
                if keep["source"] == "queue":
                    destination, recent_path = destination_for_queue(
                        keep, discard
                    )
                    if destination is None:
                        continue
                try:
                    result = apply_ordered_body_quarantine(
                        conn,
                        review_id=row["review_id"],
                        discard_file_id=discard["file_id"],
                        keep_file_id=keep["file_id"],
                        quarantine_dir=ordered_dir,
                        run_id=actual_run_id,
                        house_destination=destination,
                        classification=row["classification"],
                    )
                except (OrderedBodyMatchNotProven, NormalizationDeferred) as exc:
                    _record_current_strong_proof_not_sufficient(
                        conn, row["review_id"], row["classification"], exc
                    )
                    continue
                record = {
                    "status": "ordered_duplicate",
                    "classification": row["classification"],
                    "review_id": row["review_id"],
                    "source_file_id": discard["file_id"],
                    "keep_file_id": keep["file_id"],
                    "source_path": discard["path"],
                    "keep_path": result["ingested_path"],
                    "dest_path": result["quarantine_path"],
                    "operation_id": result["quarantine_operation_id"],
                    "ingest_operation_id": result["ingest_operation_id"],
                    "coverage_ppm": result["coverage_ppm"],
                    "max_unmatched_chars": result["max_unmatched_chars"],
                    "reverse_coverage_ppm": result["reverse_coverage_ppm"],
                    "reverse_max_unmatched_chars": result[
                        "reverse_max_unmatched_chars"
                    ],
                }
            if recent_path is not None and record["ingest_operation_id"] is not None:
                from folderling import create_recent_link

                create_recent_link(
                    record["keep_path"],
                    os.path.basename(record["keep_path"]),
                    os.path.dirname(recent_path),
                )
            records.append(record)
            settled.update(pair_ids)
    finally:
        conn.close()
    return records


def _review_id_for_pair(conn, candidate_file_id, reference_file_id):
    row = conn.execute(
        """
        SELECT review_id FROM review_items
        WHERE candidate_file_id = ? AND reference_file_id = ?
          AND state IN ('pending', 'deferred')
        ORDER BY review_id DESC LIMIT 1
        """,
        (candidate_file_id, reference_file_id),
    ).fetchone()
    return row[0] if row else None


def _review_id_for_unordered_pair(conn, left_file_id, right_file_id, classification):
    row = conn.execute(
        """
        SELECT review_id FROM review_items
        WHERE state IN ('pending', 'deferred') AND classification = ?
          AND ((candidate_file_id = ? AND reference_file_id = ?)
            OR (candidate_file_id = ? AND reference_file_id = ?))
        ORDER BY review_id DESC LIMIT 1
        """,
        (
            classification,
            left_file_id, right_file_id,
            right_file_id, left_file_id,
        ),
    ).fetchone()
    return row[0] if row else None


def _auditor_relation_queue_eligible(classification, left, right):
    """본문을 읽지 못한 서로 다른 제목을 유사도만으로 격리하지 않는다."""
    if classification != "decode_lossy":
        return True
    left_core = normalize_nfc(left.get("core_title") or "")
    right_core = normalize_nfc(right.get("core_title") or "")
    return bool(left_core and right_core and left_core == right_core)


def _contained_upgrade_direction(relation):
    """Return (shorter, longer) only for an automatic strict-version upgrade."""
    if relation.get("classification") not in {"contained_exact", "contained_version"}:
        return None
    evidence = relation.get("evidence") or {}
    left, right = relation["left"], relation["right"]
    left_length = evidence.get("left_normalized_length")
    right_length = evidence.get("right_normalized_length")
    if (
        not isinstance(left_length, int)
        or not isinstance(right_length, int)
        or left_length <= 0
        or right_length <= 0
        or left_length == right_length
    ):
        return None
    shorter, longer = (
        (left, right) if left_length < right_length else (right, left)
    )
    if {shorter.get("source"), longer.get("source")} == {"temp"}:
        return None
    if "house" not in {shorter.get("source"), longer.get("source")}:
        return None
    def automatic_endpoint(entry):
        return entry.get("mutation_eligible", True) or (
            entry.get("assignment_state") == "decision_required"
            and not entry.get("protected")
            and not entry.get("representative")
            and entry.get("variant_id") is None
        )

    if not automatic_endpoint(shorter) or not automatic_endpoint(longer):
        return None
    if os.path.splitext(shorter.get("name", ""))[1].lower() != ".txt" or os.path.splitext(
        longer.get("name", "")
    )[1].lower() != ".txt":
        return None
    coordinate_relation = classify_dedup_coordinate_relation(
        shorter["name"],
        longer["name"],
        left_span_ambiguous=bool(shorter.get("span_ambiguous")),
        right_span_ambiguous=bool(longer.get("span_ambiguous")),
    )
    special_coordinates = bool(
        coordinate_relation is not None
        and coordinate_relation.mode in DEDUP_SPECIAL_COORDINATE_MODES
    )
    if (
        coordinate_relation is None
        or coordinate_relation.preferred_side != "right"
    ):
        return None
    if not special_coordinates:
        if normalize_nfc(shorter.get("core_title") or "").casefold() != normalize_nfc(
            longer.get("core_title") or ""
        ).casefold():
            return None
        if not shorter.get("core_title") or shorter.get("unit") != longer.get("unit"):
            return None
    if _authors_conflict(shorter, longer):
        return None

    import decision_store

    def coordinates(entry):
        if "episode_start" in entry:
            return entry
        return decision_store.coordinate_fields_from_name(entry["name"])

    if not special_coordinates:
        short_coordinates = coordinates(shorter)
        long_coordinates = coordinates(longer)
        if (
            short_coordinates.get("span_ambiguous")
            or long_coordinates.get("span_ambiguous")
            or short_coordinates.get("episode_start") is None
            or short_coordinates.get("episode_end") is None
            or long_coordinates.get("episode_start") is None
            or long_coordinates.get("episode_end") is None
            or short_coordinates["episode_start"] != long_coordinates["episode_start"]
            or long_coordinates["episode_end"] <= short_coordinates["episode_end"]
        ):
            return None

    if shorter.get("protected") and not shorter.get("representative"):
        return None
    if shorter.get("assignment_state") == "managed" and not shorter.get(
        "representative"
    ):
        if not (
            longer.get("representative")
            and shorter.get("variant_id") == longer.get("variant_id")
        ):
            return None
    if (
        shorter.get("assignment_state") == "managed"
        and longer.get("assignment_state") == "managed"
        and shorter.get("variant_id") != longer.get("variant_id")
    ):
        return None
    return shorter, longer


def _ordered_body_direction(relation):
    """Return (discard, keep, coordinate mode) for a proven 1.4.1 relation."""
    if relation.get("classification") != "ordered_body_match":
        return None
    left, right = relation["left"], relation["right"]
    if {left.get("source"), right.get("source")} == {"temp"}:
        return None
    if "house" not in {left.get("source"), right.get("source")}:
        return None
    if os.path.splitext(left.get("name", ""))[1].lower() != ".txt" or os.path.splitext(
        right.get("name", "")
    )[1].lower() != ".txt":
        return None
    exact_core = bool(
        left.get("core_title")
        and normalize_nfc(left.get("core_title") or "").casefold()
        == normalize_nfc(right.get("core_title") or "").casefold()
    )
    if _authors_conflict(left, right):
        return None

    classifier = (
        classify_dedup_coordinate_relation
        if exact_core else classify_loose_title_upgrade_relation
    )
    coordinate_relation = classifier(
        left["name"], right["name"],
        left_span_ambiguous=bool(left.get("span_ambiguous")),
        right_span_ambiguous=bool(right.get("span_ambiguous")),
    )
    if coordinate_relation is None:
        return None
    if coordinate_relation.preferred_side == "left":
        keep, discard = left, right
    elif coordinate_relation.preferred_side == "right":
        keep, discard = right, left
    else:
        keep = choose_keep([left, right])
        discard = right if keep is left else left

    def automatic_endpoint(entry):
        return entry.get("mutation_eligible", True) or (
            entry.get("assignment_state") == "decision_required"
            and not entry.get("protected")
            and not entry.get("representative")
            and entry.get("variant_id") is None
        )

    if not automatic_endpoint(keep) or not (
        automatic_endpoint(discard)
        or _legacy_marker_discard_allowed(discard, keep)
    ):
        return None

    if discard.get("protected") and not discard.get("representative"):
        return None
    if (
        discard.get("assignment_state") == "managed"
        and not discard.get("representative")
        and not (
            keep.get("representative")
            and discard.get("variant_id") == keep.get("variant_id")
        )
    ):
        return None
    if (
        discard.get("assignment_state") == "managed"
        and keep.get("assignment_state") == "managed"
        and discard.get("variant_id") != keep.get("variant_id")
    ):
        return None
    return discard, keep, coordinate_relation.mode


def _bidirectional_near_direction(relation):
    """Return a narrowly safe direction for a bidirectional near duplicate.

    Queue -> house retains the established house endpoint.  House -> house is
    admitted only for a clean filename paired with a longer distribution-date
    filename, equal declared coordinates, near-identical cores, and no edition
    markers or managed relationships.  The mutation boundary independently
    replays both directions and raises the house/house threshold to 99%.
    """
    if relation.get("classification") != "near_identical":
        return None
    left, right = relation["left"], relation["right"]
    if any(
        os.path.splitext(entry.get("name", ""))[1].lower() != ".txt"
        for entry in (left, right)
    ):
        return None
    if any(
        entry.get("variant_id") is not None
        or entry.get("protected")
        or entry.get("representative")
        or entry.get("assignment_state") not in {
            "unassigned", "decision_required",
        }
        or has_legacy_marker(entry.get("name", ""))
        for entry in (left, right)
    ):
        return None

    sources = {left.get("source"), right.get("source")}
    if sources == {"house", "queue"}:
        discard = left if left.get("source") == "queue" else right
        keep = right if discard is left else left
        discard_core = normalize_nfc(discard.get("core_title") or "").casefold()
        keep_core = normalize_nfc(keep.get("core_title") or "").casefold()
        if not discard_core or discard_core != keep_core:
            return None
    elif left.get("source") == right.get("source") == "house":
        left_name = left.get("name", "")
        right_name = right.get("name", "")
        left_distribution = bool(_DISTRIBUTION_SUFFIX_RE.search(left_name))
        right_distribution = bool(_DISTRIBUTION_SUFFIX_RE.search(right_name))
        if left_distribution == right_distribution:
            return None
        discard, keep = (
            (left, right) if left_distribution else (right, left)
        )
        if _EXPLICIT_EDITION_MARKER_RE.search(left_name) or (
            _EXPLICIT_EDITION_MARKER_RE.search(right_name)
        ):
            return None
        if not left.get("complete") or not right.get("complete"):
            return None
        left_core = normalize_nfc(left.get("core_title") or "").casefold()
        right_core = normalize_nfc(right.get("core_title") or "").casefold()
        if not left_core or not right_core or SequenceMatcher(
            None, left_core, right_core, autojunk=False
        ).ratio() < 0.90:
            return None
        lengths = (left.get("normalized_length"), right.get("normalized_length"))
        if all(isinstance(value, int) and value > 0 for value in lengths):
            if abs(lengths[0] - lengths[1]) / max(lengths) > 0.01:
                return None
    else:
        return None

    coordinate = classify_dedup_coordinate_relation(
        discard["name"], keep["name"],
        left_span_ambiguous=bool(discard.get("span_ambiguous")),
        right_span_ambiguous=bool(keep.get("span_ambiguous")),
    )
    if coordinate is not None:
        if coordinate.preferred_side is not None:
            return None
        if sources == {"house"} and coordinate.mode != "same_coordinates":
            return None
        mode = coordinate.mode
    else:
        profiles = (
            episode_profile(
                discard["name"],
                span_ambiguous=bool(discard.get("span_ambiguous")),
            ),
            episode_profile(
                keep["name"],
                span_ambiguous=bool(keep.get("span_ambiguous")),
            ),
        )
        # A single omitted span is tolerable because house is always kept.
        # Two incompatible known spans or two unknown editions stay manual.
        if (profiles[0] is None) == (profiles[1] is None):
            return None
        if sources == {"house"}:
            return None
        mode = "one_sided_unknown_coordinates"
    return discard, keep, mode


def count_actionable_pending_strong_reviews(state_db_path):
    """Count persisted strong reviews that Folderling can settle automatically.

    The normal intake walk intentionally excludes ``trash_bin`` and therefore
    cannot make a queue/house review visible to the service readiness check.
    House/house reviews can likewise remain after a rule upgrade even when
    ``txt_temp`` is empty.  Reuse the same direction guards as the mutation
    phase, require current fingerprints and on-disk endpoints, and honor an
    active human distinct-edition decision.  This is a read-only readiness
    probe; the mutation phase revalidates all content and snapshots again.
    """

    import decision_store

    conn = decision_store.connect_state_db_readonly(state_db_path)
    try:
        rows = conn.execute(
            """
            SELECT r.classification,
                   c.file_id AS candidate_file_id,
                   c.canonical_path AS candidate_path,
                   c.source AS candidate_source,
                   c.assignment_state AS candidate_assignment_state,
                   c.variant_id AS candidate_variant_id,
                   c.protected AS candidate_protected,
                   c.span_ambiguous AS candidate_span_ambiguous,
                   c.size AS candidate_size,
                   c.current_fingerprint_id AS candidate_fingerprint_id,
                   CASE WHEN cr.file_id IS NULL THEN 0 ELSE 1 END
                       AS candidate_representative,
                   cfp.normalized_length AS candidate_normalized_length,
                   cfa.core_title AS candidate_core_title,
                   cfa.author AS candidate_author,
                   cfa.unit AS candidate_unit,
                   cfa.complete AS candidate_complete,
                   k.file_id AS reference_file_id,
                   k.canonical_path AS reference_path,
                   k.source AS reference_source,
                   k.assignment_state AS reference_assignment_state,
                   k.variant_id AS reference_variant_id,
                   k.protected AS reference_protected,
                   k.span_ambiguous AS reference_span_ambiguous,
                   k.size AS reference_size,
                   k.current_fingerprint_id AS reference_fingerprint_id,
                   CASE WHEN kr.file_id IS NULL THEN 0 ELSE 1 END
                       AS reference_representative,
                   kfp.normalized_length AS reference_normalized_length,
                   kfa.core_title AS reference_core_title,
                   kfa.author AS reference_author,
                   kfa.unit AS reference_unit,
                   kfa.complete AS reference_complete,
                   r.left_fingerprint_id, r.right_fingerprint_id,
                   r.evidence_json
            FROM review_items AS r
            JOIN files AS c ON c.file_id = r.candidate_file_id
            JOIN files AS k ON k.file_id = r.reference_file_id
            LEFT JOIN representatives AS cr ON cr.file_id = c.file_id
            LEFT JOIN representatives AS kr ON kr.file_id = k.file_id
            LEFT JOIN fingerprints AS cfp
              ON cfp.fingerprint_id = c.current_fingerprint_id
            LEFT JOIN fingerprints AS kfp
              ON kfp.fingerprint_id = k.current_fingerprint_id
            LEFT JOIN file_analysis AS cfa ON cfa.file_id = c.file_id
            LEFT JOIN file_analysis AS kfa ON kfa.file_id = k.file_id
            WHERE r.state IN ('pending', 'deferred')
              AND r.classification IN (
                'contained_exact', 'contained_version', 'ordered_body_match',
                'near_identical'
              )
              AND c.active = 1 AND k.active = 1
              AND c.source IN ('house', 'queue')
              AND k.source IN ('house', 'queue')
              AND (c.source = 'house' OR k.source = 'house')
            ORDER BY r.review_id
            """
        ).fetchall()

        def entry(prefix, row):
            state = row[f"{prefix}_assignment_state"]
            path = row[f"{prefix}_path"]
            parsed = analyze_name(os.path.basename(path))
            stored_complete = row[f"{prefix}_complete"]
            return {
                "file_id": row[f"{prefix}_file_id"],
                "path": path,
                "name": os.path.basename(path),
                "source": row[f"{prefix}_source"],
                "assignment_state": state,
                "variant_id": row[f"{prefix}_variant_id"],
                "protected": bool(row[f"{prefix}_protected"]),
                "representative": bool(row[f"{prefix}_representative"]),
                "span_ambiguous": bool(row[f"{prefix}_span_ambiguous"]),
                "size": int(row[f"{prefix}_size"] or 0),
                "ext": os.path.splitext(path)[1].lower(),
                "core_title": (
                    row[f"{prefix}_core_title"] or parsed["core_title"]
                ),
                "author": row[f"{prefix}_author"] or parsed["author"],
                "unit": row[f"{prefix}_unit"] or parsed.get("unit", "미상"),
                "complete": bool(
                    parsed["complete"]
                    if stored_complete is None else stored_complete
                ),
                "char_count": row[f"{prefix}_normalized_length"] or 0,
                "mutation_eligible": state not in {
                    "legacy_unresolved", "decision_required"
                },
            }

        count = 0
        for row in rows:
            if (
                row["left_fingerprint_id"]
                != row["candidate_fingerprint_id"]
                or row["right_fingerprint_id"]
                != row["reference_fingerprint_id"]
                or not os.path.isfile(row["candidate_path"])
                or not os.path.isfile(row["reference_path"])
            ):
                continue
            if _current_review_evidence_precludes_action(
                row["classification"], row["evidence_json"]
            ):
                continue
            if decision_store.active_distinct_decision_for_pair(
                conn,
                row["candidate_file_id"],
                row["reference_file_id"],
            ) is not None:
                continue
            left = entry("candidate", row)
            right = entry("reference", row)
            relation = {
                "classification": row["classification"],
                "left": left,
                "right": right,
                "evidence": {
                    "left_normalized_length": row[
                        "candidate_normalized_length"
                    ],
                    "right_normalized_length": row[
                        "reference_normalized_length"
                    ],
                },
            }
            direction = (
                _contained_upgrade_direction(relation)
                if row["classification"] in {
                    "contained_exact", "contained_version",
                }
                else _ordered_body_direction(relation)
                if row["classification"] == "ordered_body_match"
                else _bidirectional_near_direction(relation)
            )
            if direction is not None:
                count += 1
        return count
    finally:
        conn.close()


def _pending_active_distinct_decision_pairs(conn):
    """Return one current open-review row for each human-vetoed file pair."""

    import decision_store

    rows = conn.execute(
        """
        SELECT r.candidate_file_id, r.reference_file_id,
               c.canonical_path AS candidate_path,
               k.canonical_path AS reference_path
        FROM review_items AS r
        JOIN files AS c ON c.file_id = r.candidate_file_id
        JOIN files AS k ON k.file_id = r.reference_file_id
        WHERE r.state IN ('pending', 'deferred')
          AND c.active = 1 AND k.active = 1
        ORDER BY r.review_id
        """
    ).fetchall()
    pairs = []
    seen = set()
    for row in rows:
        pair_key = frozenset(
            (row["candidate_file_id"], row["reference_file_id"])
        )
        if pair_key in seen:
            continue
        seen.add(pair_key)
        decision = decision_store.active_distinct_decision_for_pair(
            conn, row["candidate_file_id"], row["reference_file_id"]
        )
        if decision is not None:
            pairs.append((row, decision))
    return pairs


def count_pending_active_distinct_decision_reviews(state_db_path):
    """Count open machine-review pairs already vetoed by a human decision."""

    import decision_store

    conn = decision_store.connect_state_db_readonly(state_db_path)
    try:
        return len(_pending_active_distinct_decision_pairs(conn))
    finally:
        conn.close()


def cleanup_pending_active_distinct_decision_reviews(state_db_path):
    """Close stale machine reviews even when the auditor pair cache is warm.

    Pair-cache hits need not pass through ``DuplicateAuditor._store_review_item``.
    This independent convergence pass therefore makes the active human variant
    decision authoritative without relying on a cold audit or new evidence.
    """

    import decision_store

    conn = decision_store.connect_state_db(state_db_path)
    records = []
    try:
        with decision_store.transaction(conn):
            for row, decision in _pending_active_distinct_decision_pairs(conn):
                open_reviews = conn.execute(
                    """
                    SELECT review_id, classification FROM review_items
                    WHERE state IN ('pending', 'deferred')
                      AND ((candidate_file_id = ? AND reference_file_id = ?)
                        OR (candidate_file_id = ? AND reference_file_id = ?))
                    ORDER BY review_id
                    """,
                    (
                        row["candidate_file_id"], row["reference_file_id"],
                        row["reference_file_id"], row["candidate_file_id"],
                    ),
                ).fetchall()
                decision_id = (
                    decision_store.suppress_open_reviews_for_active_distinct_decision(
                        conn,
                        row["candidate_file_id"],
                        row["reference_file_id"],
                    )
                )
                if decision_id is None:
                    continue
                records.append({
                    "decision_id": decision_id,
                    "verdict": decision["verdict"],
                    "candidate_file_id": row["candidate_file_id"],
                    "reference_file_id": row["reference_file_id"],
                    "candidate_path": row["candidate_path"],
                    "reference_path": row["reference_path"],
                    "review_ids": [item["review_id"] for item in open_reviews],
                    "classifications": sorted({
                        item["classification"] for item in open_reviews
                    }),
                })
        return records
    finally:
        conn.close()


def _managed_auditor_queue_records(
    auditor_groups,
    auditor_relations,
    state_db_path,
    temp_dir,
    dry_run,
    excluded_paths,
    actual_run_id=None,
    house_dir=None,
):
    import decision_store
    from dedup_mutations import (
        ContainedUpgradeNotProven,
        HUMAN_REVIEW_CLASSES,
        OrderedBodyMatchNotProven,
        apply_contained_upgrade,
        apply_ordered_body_quarantine,
        apply_strong_equivalent_quarantine,
        house_review_move,
        queue_candidate,
    )

    records = []
    queued_paths = set(excluded_paths)
    conn = decision_store.connect_state_db(state_db_path)

    def queue_temp(entry, reference, classification, destination, status):
        if classification not in HUMAN_REVIEW_CLASSES:
            return
        if not _auditor_relation_queue_eligible(classification, entry, reference):
            return
        if classification not in AUDITOR_STRONG_CLASSES and not decision_store.coordinates_compatible(
            decision_store.coordinate_fields_from_name(entry["name"]),
            decision_store.coordinate_fields_from_name(reference["name"]),
        ):
            return
        if entry["path"] in queued_paths:
            return
        if not entry.get("file_id") or not reference.get("file_id"):
            return
        review_id = _review_id_for_pair(conn, entry["file_id"], reference["file_id"])
        if review_id is None:
            return
        record = {
            "dry_run": dry_run,
            "core_title": reference.get("core_title") or entry.get("core_title"),
            "keep": reference,
            "entry": entry,
            "dest_path": None,
            "size": entry["size"],
            "distinct_authors": _authors_conflict(reference, entry),
            "status": status,
            "classification": classification,
            "review_id": review_id,
            "final_quarantine": classification in AUDITOR_STRONG_CLASSES,
        }
        if not dry_run:
            if classification in AUDITOR_STRONG_CLASSES:
                result = apply_strong_equivalent_quarantine(
                    conn,
                    review_id=review_id,
                    discard_file_id=entry["file_id"],
                    keep_file_id=reference["file_id"],
                    classification=classification,
                    quarantine_dir=strong_dir,
                    run_id=actual_run_id,
                )
                record["dest_path"] = result["dest_path"]
            else:
                result = queue_candidate(
                    conn,
                    candidate_file_id=entry["file_id"],
                    reference_file_id=reference["file_id"],
                    classification=classification,
                    queue_dir=destination,
                    run_id=actual_run_id,
                    review_id=review_id,
                    allow_unassigned_reference=True,
                )
                record["dest_path"] = result["dest_path"]
            record["operation_id"] = result["operation_id"]
        records.append(record)
        queued_paths.add(entry["path"])

    def queue_house(entry, reference, classification, destination, status):
        if classification not in HUMAN_REVIEW_CLASSES:
            return
        if not _auditor_relation_queue_eligible(classification, entry, reference):
            return
        if classification not in AUDITOR_STRONG_CLASSES and not decision_store.coordinates_compatible(
            decision_store.coordinate_fields_from_name(entry["name"]),
            decision_store.coordinate_fields_from_name(reference["name"]),
        ):
            return
        if entry["path"] in queued_paths:
            return
        if not entry.get("file_id") or not reference.get("file_id"):
            return
        review_id = _review_id_for_unordered_pair(
            conn, entry["file_id"], reference["file_id"], classification
        )
        if review_id is None:
            return
        record = {
            "dry_run": dry_run,
            "core_title": reference.get("core_title") or entry.get("core_title"),
            "keep": reference,
            "entry": entry,
            "dest_path": None,
            "size": entry["size"],
            "distinct_authors": _authors_conflict(reference, entry),
            "status": status,
            "classification": classification,
            "review_id": review_id,
            "final_quarantine": classification in AUDITOR_STRONG_CLASSES,
        }
        if not dry_run:
            if classification in AUDITOR_STRONG_CLASSES:
                result = apply_strong_equivalent_quarantine(
                    conn,
                    review_id=review_id,
                    discard_file_id=entry["file_id"],
                    keep_file_id=reference["file_id"],
                    classification=classification,
                    quarantine_dir=strong_dir,
                    run_id=actual_run_id,
                )
                record["dest_path"] = result["dest_path"]
            else:
                result = house_review_move(
                    conn,
                    review_id=review_id,
                    move_file_id=entry["file_id"],
                    keep_file_id=reference["file_id"],
                    classification=classification,
                    queue_dir=destination,
                    run_id=actual_run_id,
                )
                record["dest_path"] = result["destination"]
            record["operation_id"] = result["operation_id"]
        records.append(record)
        queued_paths.add(entry["path"])

    try:
        suspected_dir = os.path.join(temp_dir, "trash_bin", "suspected_duplicates")
        strong_dir = os.path.join(
            temp_dir, "trash_bin", "strong_equivalent_duplicates"
        )
        warning_dir = os.path.join(temp_dir, "trash_bin", "warning")
        superseded_dir = os.path.join(temp_dir, "trash_bin", "superseded_versions")
        ordered_dir = os.path.join(temp_dir, "trash_bin", "ordered_body_duplicates")
        for group in auditor_groups:
            reference = group["keep"]
            classification = group.get("classification", "text_equivalent")
            for entry in group.get("move_entries", []):
                queue_temp(entry, reference, classification, suspected_dir, "moved")
            for entry in group.get("same_house_entries", []):
                queue_house(entry, reference, classification, suspected_dir, "moved")

        # A complete normalized prefix plus a strictly wider episode span is a
        # normal latest-version replacement, not a human-review exception.
        # Process these directed edges before the generic weak-relation queue so
        # a new long version can replace an established managed representative.
        contained_relations = []
        for relation in auditor_relations:
            direction = _contained_upgrade_direction(relation)
            if direction is None:
                continue
            shorter, longer = direction
            contained_relations.append((
                -int(longer.get("char_count") or 0),
                int(shorter.get("char_count") or 0),
                relation,
                shorter,
                longer,
            ))
        for _, _, relation, shorter, longer in sorted(
            contained_relations,
            key=lambda item: (item[0], item[1], item[3]["path"], item[4]["path"]),
        ):
            if shorter["path"] in queued_paths or longer["path"] in queued_paths:
                continue
            if not shorter.get("file_id") or not longer.get("file_id"):
                continue
            review_id = _review_id_for_unordered_pair(
                conn,
                shorter["file_id"],
                longer["file_id"],
                relation["classification"],
            )
            if review_id is None:
                continue

            dry_containment_evidence = None
            if dry_run:
                from mutation_io import (
                    contained_anchor_proof_sufficient,
                    inspect_contained_text,
                )

                try:
                    proof = inspect_contained_text(shorter["path"], longer["path"])
                except (OSError, RuntimeError, UnicodeError):
                    continue
                if proof.short_normalized_length >= proof.long_normalized_length:
                    continue
                if relation["classification"] == "contained_exact" and (
                    proof.long_prefix_sha256 != proof.short_normalized_sha256
                ):
                    continue
                if relation["classification"] == "contained_version" and not (
                    contained_anchor_proof_sufficient(proof)
                ):
                    continue
                dry_containment_evidence = {
                    "short_normalized_sha256": proof.short_normalized_sha256,
                    "long_normalized_sha256": proof.long_normalized_sha256,
                    "short_normalized_length": proof.short_normalized_length,
                    "long_normalized_length": proof.long_normalized_length,
                    "ordered_anchor_count": proof.ordered_anchor_count,
                    "anchor_chars": proof.anchor_chars,
                    "anchor_offset_span": proof.anchor_offset_span,
                }

            original_long_path = longer["path"]
            destination = None
            recent_path = None
            if longer.get("source") == "temp":
                if shorter.get("source") != "house" or not house_dir:
                    continue
                clean_name = materialize_title_markup(
                    normalize_filename(longer["name"])
                )
                if not clean_name:
                    continue
                destination = os.path.join(
                    os.path.dirname(shorter["path"]), clean_name
                )
                if os.path.lexists(destination):
                    continue
                recent_path = os.path.join(house_dir, "_최근", os.path.basename(destination))
                if os.path.lexists(recent_path):
                    from folderling import recent_link_matches_destination

                    if not recent_link_matches_destination(
                        recent_path, destination
                    ):
                        continue

            record = {
                "dry_run": dry_run,
                "core_title": longer.get("core_title") or shorter.get("core_title"),
                "keep": longer,
                "entry": shorter,
                "dest_path": (
                    os.path.join(superseded_dir, os.path.basename(shorter["path"]))
                    if dry_run else None
                ),
                "size": shorter["size"],
                "distinct_authors": False,
                "status": "superseded",
                "classification": relation["classification"],
                "review_id": review_id,
                "house_destination": destination,
                "recent_link": recent_path,
                "containment_evidence": dry_containment_evidence,
            }
            if not dry_run:
                if recent_path is not None:
                    from folderling import ensure_recent_link_slot

                    ensure_recent_link_slot(
                        os.path.basename(destination),
                        os.path.dirname(recent_path),
                        destination,
                    )
                try:
                    result = apply_contained_upgrade(
                        conn,
                        review_id=review_id,
                        shorter_file_id=shorter["file_id"],
                        longer_file_id=longer["file_id"],
                        quarantine_dir=superseded_dir,
                        run_id=actual_run_id,
                        house_destination=destination,
                        classification=relation["classification"],
                    )
                except ContainedUpgradeNotProven as exc:
                    _record_current_strong_proof_not_sufficient(
                        conn, review_id, relation["classification"], exc
                    )
                    continue
                record["dest_path"] = result["quarantine_path"]
                record["operation_id"] = result["quarantine_operation_id"]
                record["ingest_operation_id"] = result["ingest_operation_id"]
                record["house_destination"] = result["ingested_path"]
                record["containment_evidence"] = {
                    key: result[key] for key in (
                        "short_normalized_sha256",
                        "long_normalized_sha256",
                        "short_normalized_length",
                        "long_normalized_length",
                        "ordered_anchor_count",
                        "anchor_chars",
                        "anchor_offset_span",
                    )
                }
                if recent_path is not None:
                    from folderling import create_recent_link

                    create_recent_link(
                        result["ingested_path"],
                        os.path.basename(result["ingested_path"]),
                        os.path.dirname(recent_path),
                    )
                    longer["path"] = result["ingested_path"]
                    longer["name"] = os.path.basename(result["ingested_path"])
                    longer["rel_path"] = normalize_nfc(
                        os.path.relpath(result["ingested_path"], house_dir)
                    )
                    longer["source"] = "house"
            records.append(record)
            queued_paths.add(shorter["path"])
            if original_long_path != longer["path"]:
                # Auditor relations share entry dictionaries.  Intake mutates
                # this path to the new house destination, while other edges
                # may retain either the old temp path or the shared new path.
                # Both identities describe the same already-settled file and
                # must be excluded from the rest of this snapshot graph.
                queued_paths.add(original_long_path)
                queued_paths.add(longer["path"])

        ordered_relations = []
        for relation in auditor_relations:
            direction = _ordered_body_direction(relation)
            if direction is None:
                continue
            discard, keep, coordinate_mode = direction
            ordered_relations.append((
                discard["path"], keep["path"], relation,
                discard, keep, coordinate_mode,
            ))
        for _, _, relation, discard, keep, coordinate_mode in sorted(
            ordered_relations, key=lambda item: (item[0], item[1])
        ):
            if discard["path"] in queued_paths or keep["path"] in queued_paths:
                continue
            if not discard.get("file_id") or not keep.get("file_id"):
                continue
            review_id = _review_id_for_unordered_pair(
                conn,
                discard["file_id"],
                keep["file_id"],
                relation["classification"],
            )
            if review_id is None:
                continue

            original_keep_path = keep["path"]
            destination = None
            recent_path = None
            if keep.get("source") == "temp":
                if discard.get("source") != "house" or not house_dir:
                    continue
                clean_name = materialize_title_markup(
                    normalize_filename(keep["name"])
                )
                if not clean_name:
                    continue
                destination = os.path.join(
                    os.path.dirname(discard["path"]), clean_name
                )
                if os.path.lexists(destination):
                    continue
                recent_path = os.path.join(
                    house_dir, "_최근", os.path.basename(destination)
                )
                if os.path.lexists(recent_path):
                    from folderling import recent_link_matches_destination

                    if not recent_link_matches_destination(
                        recent_path, destination
                    ):
                        continue

            record = {
                "dry_run": dry_run,
                "core_title": keep.get("core_title") or discard.get("core_title"),
                "keep": keep,
                "entry": discard,
                "dest_path": (
                    os.path.join(ordered_dir, os.path.basename(discard["path"]))
                    if dry_run else None
                ),
                "size": discard["size"],
                "distinct_authors": False,
                "status": "ordered_duplicate",
                "classification": relation["classification"],
                "review_id": review_id,
                "house_destination": destination,
                "recent_link": recent_path,
                "coordinate_mode": coordinate_mode,
                "ordered_body_evidence": relation.get("evidence") or {},
            }
            if not dry_run:
                if recent_path is not None:
                    from folderling import ensure_recent_link_slot

                    ensure_recent_link_slot(
                        os.path.basename(destination),
                        os.path.dirname(recent_path),
                        destination,
                    )
                try:
                    result = apply_ordered_body_quarantine(
                        conn,
                        review_id=review_id,
                        discard_file_id=discard["file_id"],
                        keep_file_id=keep["file_id"],
                        quarantine_dir=ordered_dir,
                        run_id=actual_run_id,
                        house_destination=destination,
                        classification=relation["classification"],
                    )
                except (OrderedBodyMatchNotProven, NormalizationDeferred) as exc:
                    _record_current_strong_proof_not_sufficient(
                        conn, review_id, relation["classification"], exc
                    )
                    continue
                record["dest_path"] = result["quarantine_path"]
                record["operation_id"] = result["quarantine_operation_id"]
                record["ingest_operation_id"] = result["ingest_operation_id"]
                record["house_destination"] = result["ingested_path"]
                record["ordered_body_evidence"] = {
                    key: result[key] for key in (
                        "source_normalized_sha256",
                        "target_normalized_sha256",
                        "source_normalized_length",
                        "target_normalized_length",
                        "coverage_ppm",
                        "matched_chars",
                        "source_chars",
                        "max_unmatched_chars",
                        "coordinate_mode",
                    )
                }
                if recent_path is not None:
                    from folderling import create_recent_link

                    create_recent_link(
                        result["ingested_path"],
                        os.path.basename(result["ingested_path"]),
                        os.path.dirname(recent_path),
                    )
                    keep["path"] = result["ingested_path"]
                    keep["name"] = os.path.basename(result["ingested_path"])
                    keep["rel_path"] = normalize_nfc(
                        os.path.relpath(result["ingested_path"], house_dir)
                    )
                    keep["source"] = "house"
            records.append(record)
            queued_paths.add(discard["path"])
            if original_keep_path != keep["path"]:
                # See the contained-upgrade case above: exclude both path
                # representations of a keep that this run just ingested.
                queued_paths.add(original_keep_path)
                queued_paths.add(keep["path"])

        by_temp = defaultdict(list)
        for relation in auditor_relations:
            classification = relation["classification"]
            if classification not in HUMAN_REVIEW_CLASSES:
                continue
            left, right = relation["left"], relation["right"]
            if {left.get("source"), right.get("source")} != {"house", "temp"}:
                continue
            if not _auditor_relation_queue_eligible(classification, left, right):
                continue
            temp_entry = left if left.get("source") == "temp" else right
            house_entry = right if temp_entry is left else left
            if not temp_entry.get("mutation_eligible", True):
                continue
            if classification not in AUDITOR_STRONG_CLASSES and not decision_store.coordinates_compatible(
                decision_store.coordinate_fields_from_name(temp_entry["name"]),
                decision_store.coordinate_fields_from_name(house_entry["name"]),
            ):
                continue
            by_temp[temp_entry["path"]].append((relation, temp_entry, house_entry))

        house_incidence = Counter(
            house_entry["path"]
            for relations in by_temp.values()
            for _, _, house_entry in relations
        )
        for temp_path in sorted(by_temp):
            if temp_path in queued_paths:
                continue
            relations = by_temp[temp_path]
            temp_entry = relations[0][1]
            houses = []
            seen_house = set()
            for _, _, house_entry in relations:
                if house_entry["path"] not in seen_house:
                    houses.append(house_entry)
                    seen_house.add(house_entry["path"])

            strong_relations = [
                item for item in relations
                if item[0]["classification"] in AUDITOR_STRONG_CLASSES
            ]
            protected_or_managed = [
                entry for entry in houses
                if entry.get("protected") or entry.get("representative")
                or entry.get("assignment_state") != "unassigned"
                or house_incidence[entry["path"]] > 1
            ]
            if strong_relations:
                # Exact normalized TXT/EPUB content is already fully proven;
                # preserve an established house copy instead of allowing a
                # cosmetic incoming filename to replace it.
                keep = choose_keep_exact(houses)
            elif protected_or_managed:
                keep = choose_keep(protected_or_managed)
            else:
                keep = choose_keep([temp_entry, *houses])

            if keep is temp_entry:
                for relation, _, house_entry in relations:
                    if house_entry["path"] in queued_paths:
                        continue
                    queue_house(
                        house_entry,
                        temp_entry,
                        relation["classification"],
                        warning_dir,
                        "warning",
                    )
            else:
                selected = next(
                    relation for relation, _, house_entry in relations
                    if house_entry["path"] == keep["path"]
                )
                is_strong = selected["classification"] in AUDITOR_STRONG_CLASSES
                queue_temp(
                    temp_entry,
                    keep,
                    selected["classification"],
                    suspected_dir if is_strong else warning_dir,
                    "moved" if is_strong else "warning",
                )
    finally:
        conn.close()
    return records


def _managed_epub_analysis_hold_records(
    auditor_report,
    entries,
    state_db_path,
    temp_dir,
    dry_run,
    actual_run_id=None,
):
    """Hold only incoming EPUBs whose exact audit error was persisted.

    House EPUB errors remain report stop reasons and never reach this path.
    Missing entry/file/error evidence fails closed instead of silently allowing
    an ambiguous archive into the library.
    """

    errors = list(
        getattr(auditor_report, "stats", {}).get("epub_analysis_errors", ())
        or ()
    )
    if not errors:
        return []
    configuration = getattr(auditor_report, "configuration", {}) or {}
    max_file_bytes = configuration.get("max_file_bytes")
    max_uncompressed_bytes = configuration.get("max_epub_uncompressed_bytes")
    if not isinstance(max_file_bytes, int) or max_file_bytes <= 0:
        raise RuntimeError("auditor EPUB file limit evidence is missing")
    if max_uncompressed_bytes is not None and (
        not isinstance(max_uncompressed_bytes, int)
        or max_uncompressed_bytes <= 0
    ):
        raise RuntimeError("auditor EPUB expansion limit evidence is missing")
    by_key = {
        (entry.get("source", "house"), normalize_nfc(entry.get("rel_path", ""))): entry
        for entry in entries
    }
    records = []
    conn = None
    if not dry_run:
        import decision_store

        conn = decision_store.connect_state_db(state_db_path)
    try:
        for error_record in errors:
            source = str(error_record.get("source") or "")
            rel_path = normalize_nfc(error_record.get("rel_path", ""))
            analysis_error = str(error_record.get("error") or "")
            entry = by_key.get((source, rel_path))
            if source != "temp":
                raise RuntimeError(
                    "house EPUB analysis error escaped the auditor stop gate"
                )
            if entry is None or not entry.get("file_id") or not analysis_error:
                raise RuntimeError(
                    "incoming EPUB analysis error evidence is incomplete"
                )
            destination = os.path.join(
                temp_dir,
                "trash_bin",
                "warning",
                "epub_analysis_errors",
                os.path.basename(entry["path"]),
            )
            record = {
                "dry_run": dry_run,
                "core_title": entry.get("core_title"),
                "keep": {},
                "entry": entry,
                "dest_path": destination if dry_run else None,
                "size": entry["size"],
                "distinct_authors": False,
                "status": "warning",
                "classification": "epub_analysis_error",
                "analysis_error": analysis_error,
                "final_quarantine": False,
            }
            if not dry_run:
                from dedup_mutations import hold_epub_analysis_error

                result = hold_epub_analysis_error(
                    conn,
                    source_file_id=entry["file_id"],
                    temp_root=temp_dir,
                    run_id=actual_run_id,
                    analysis_error=analysis_error,
                    max_file_bytes=max_file_bytes,
                    max_uncompressed_bytes=max_uncompressed_bytes,
                )
                record["dest_path"] = result["dest_path"]
                record["operation_id"] = result["operation_id"]
            records.append(record)
    finally:
        if conn is not None:
            conn.close()
    return records


def write_review_log(
    temp_dir, exact_records, suspect_groups, suspect_move_records, summary,
    disambig_records=None, blocked_strong_relations=None,
):
    log_dir = os.path.join(temp_dir, "dedup_logs")
    os.makedirs(log_dir, exist_ok=True)
    generated_at = datetime.now().astimezone()
    report_stem = f"dedup_{generated_at.strftime('%Y%m%d_%H%M%S_%f')}"
    structured_path = os.path.join(log_dir, f"{report_stem}.json")
    summary["report_path"] = structured_path
    # 구버전 호출부 호환: 이제 두 경로 모두 유일한 JSON 원본을 가리킨다.
    summary["structured_report_path"] = structured_path
    structured_payload = {
        "schema_version": 1,
        "kind": "folderling_dedup",
        "generated_at": generated_at.isoformat(),
        "summary": dict(summary),
        "exact_records": exact_records,
        "suspect_groups": suspect_groups,
        "suspect_move_records": suspect_move_records,
        "disambig_records": disambig_records or [],
        "blocked_strong_relations": blocked_strong_relations or [],
    }
    temporary_path = structured_path + ".tmp"
    try:
        with open(temporary_path, "w", encoding="utf-8") as stream:
            json.dump(
                structured_payload,
                stream,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                default=str,
            )
        os.replace(temporary_path, structured_path)
    except OSError as exc:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        summary["report_path"] = None
        summary["structured_report_path"] = None
        print(f"⚠️ 구조화 dedup 보고서 저장 실패: {exc}", file=sys.stderr)
        return None
    return structured_path


def _emit_dedup_record_events(event_callback, exact_records, suspect_move_records):
    if event_callback is None:
        return
    total_records = len(exact_records) + len(suspect_move_records)
    completed = 0

    def entry_fields(entry):
        entry = entry or {}
        return {
            "source_path": entry.get("path"),
            "source_name": entry.get("name"),
            "source_kind": entry.get("source"),
            "core_title": entry.get("core_title"),
            "author": entry.get("author"),
            "size": entry.get("size"),
            "file_id": entry.get("file_id"),
        }

    for record in exact_records:
        completed += 1
        action = str(record.get("action") or "exact")
        if action in {"exact_quarantine", "move", "delete"}:
            status = "exact_duplicate"
        else:
            status = "skipped"
        keep = record.get("keep") or {}
        event_callback({
            "phase": "file_result",
            "stage": "dedup",
            "status": status,
            "reason": action,
            "duplicate_basis": "exact_fingerprint",
            "destination_path": record.get("dest_path"),
            "existing_paths": [keep.get("path")] if keep.get("path") else [],
            "operation_id": record.get("operation_id"),
            "dry_run": bool(record.get("dry_run")),
            "completed": completed,
            "total": total_records,
            **entry_fields(record.get("entry")),
        })

    status_labels = {
        "moved": "suspected_duplicate",
        "superseded": "superseded_version",
        "ordered_duplicate": "ordered_body_duplicate",
        "warning": "warning",
        "author_review": "author_conflict",
        "metadata_only": "metadata_only",
    }
    for record in suspect_move_records:
        completed += 1
        keep = record.get("keep") or {}
        raw_status = str(record.get("status") or "review")
        event_callback({
            "phase": "file_result",
            "stage": "dedup",
            "status": status_labels.get(raw_status, raw_status),
            "reason": record.get("classification") or raw_status,
            "duplicate_basis": record.get("classification"),
            "destination_path": record.get("dest_path"),
            "existing_paths": [keep.get("path")] if keep.get("path") else [],
            "operation_id": record.get("operation_id"),
            "review_id": record.get("review_id"),
            "dry_run": bool(record.get("dry_run")),
            "completed": completed,
            "total": total_records,
            **entry_fields(record.get("entry")),
        })


def _clean_duplicates_impl(
    house_dir,
    temp_dir,
    dry_run=True,
    index_path=None,
    rescan=False,
    move_suspects=False,
    delete_exact=True,
    include_temp=False,
    audit_suspects=False,
    auditor_report=None,
    update_index_after_run=True,
    state_db_path=DEFAULT_STATE_DB,
    require_state_db=False,
    actual_run_id=None,
    pure_plan=False,
    event_callback=None,
    verified_index_entries=None,
    verified_house_inventory=None,
    before_non_cache_mutation=None,
):
    performance_metrics = {}
    auditor_rebaseline_retry = False
    auditor_initial_stop_reasons = []
    script_dir = str(PROJECT_ROOT)
    index_path = index_path or os.path.join(script_dir, "file_index.json")

    scan_label = "house + temp" if include_temp else "house만"
    print(f"🧹 중복/검토 큐 정리 시작 (모드: {'미리보기/Dry-run' if dry_run else '실제 실행'}, 스캔: {scan_label})")
    print(f"  - 대상 폴더 (house): {house_dir}")
    print(f"  - 임시 폴더 (temp) : {temp_dir}")
    print(f"  - 인덱스          : {index_path}")
    print(f"  - 지원 확장자     : {', '.join(sorted(SUPPORTED_EXTENSIONS))}")

    managed_mode = bool(require_state_db)
    if not dry_run and managed_mode:
        import decision_store

        authorization_conn = decision_store.connect_state_db(state_db_path)
        try:
            decision_store.assert_active_actual_run(
                authorization_conn, actual_run_id, house_dir=house_dir, temp_dir=temp_dir
            )
        finally:
            authorization_conn.close()

    # 1.2.0 auditor bridge는 약한 간선을 mutation component로 합칠 수 있다.
    # Phase D의 strong/weak graph 분리가 끝날 때까지 actual에서는 무조건 fail closed 한다.
    if not dry_run and audit_suspects and not managed_mode:
        raise RuntimeError(
            "unsafe legacy auditor bridge는 actual mutation에 사용할 수 없습니다. "
            "1.2.1 Phase D 완료 전에는 --dry-run만 허용합니다."
        )

    if not os.path.exists(house_dir):
        print("❌ 대상 폴더(house_dir)가 존재하지 않습니다.")
        return None

    stage_started_at = time.perf_counter()
    entries = load_index_entries(
        house_dir,
        index_path,
        rescan=rescan,
        state_db_path=state_db_path if managed_mode and not pure_plan else None,
        allow_write=not pure_plan,
        verified_entries=verified_index_entries,
    )
    performance_metrics["index_load_seconds"] = round(
        time.perf_counter() - stage_started_at, 6
    )
    print(f"🔍 house 인덱스 로드 완료: {len(entries)}개")

    if include_temp:
        temp_entries = scan_temp_files(temp_dir)
        print(f"🔍 temp 루트 스캔 완료: {len(temp_entries)}개")
        entries = entries + temp_entries

    print(f"🔍 통합 대상: 총 {len(entries)}개의 관리 대상 파일")

    if audit_suspects:
        print("🔎 cross/near-core 본문 감사 시작 (완료된 결과만 검토 큐에 반영)")
        stage_started_at = time.perf_counter()
        auditor_report_supplied = auditor_report is not None
        auditor_progress_callback = (
            lambda update: event_callback({
                "phase": "auditor_progress", "status": "running", **update
            })
            if event_callback is not None else None
        )
        auditor_report = auditor_report or run_auditor_queue_report(
            index_path,
            house_dir,
            temp_dir,
            state_db_path=state_db_path if managed_mode else None,
            cache_write=not pure_plan,
            progress_callback=auditor_progress_callback,
            verified_house_inventory=verified_house_inventory,
            before_non_cache_mutation=before_non_cache_mutation,
        )
        rebaseline_reasons = _auditor_rebaseline_reasons(auditor_report)
        if (
            rebaseline_reasons
            and not dry_run
            and managed_mode
            and not pure_plan
            and not auditor_report_supplied
        ):
            auditor_rebaseline_retry = True
            auditor_initial_stop_reasons = list(rebaseline_reasons)
            initial_read_bytes = int(
                getattr(auditor_report, "stats", {}).get("actual_read_bytes", 0)
            )
            print(
                "♻️ cold-cache 전체 재기준 자동 재시도: "
                f"{list(rebaseline_reasons)} "
                f"(read {FOLDERLING_REBASELINE_READ_BUDGET}, "
                "파일당 정밀 후보 "
                f"{FOLDERLING_REBASELINE_DEEP_PAIRS_PER_FILE})"
            )
            if event_callback is not None:
                event_callback({
                    "phase": "auditor_rebaseline",
                    "status": "running",
                    "initial_stop_reasons": list(rebaseline_reasons),
                    "max_read_bytes": FOLDERLING_REBASELINE_READ_BUDGET,
                    "max_deep_pairs_per_file": (
                        FOLDERLING_REBASELINE_DEEP_PAIRS_PER_FILE
                    ),
                })
            auditor_report = run_auditor_queue_report(
                index_path,
                house_dir,
                temp_dir,
                state_db_path=state_db_path,
                cache_write=True,
                progress_callback=auditor_progress_callback,
                rebaseline=True,
                verified_house_inventory=verified_house_inventory,
                before_non_cache_mutation=before_non_cache_mutation,
            )
            if isinstance(getattr(auditor_report, "stats", None), dict):
                retry_read_bytes = int(
                    auditor_report.stats.get("actual_read_bytes", 0)
                )
                auditor_report.stats.update({
                    "folderling_rebaseline_retry": True,
                    "folderling_initial_stop_reasons": list(rebaseline_reasons),
                    "folderling_initial_read_bytes": initial_read_bytes,
                    "folderling_total_read_bytes": (
                        initial_read_bytes + retry_read_bytes
                    ),
                })
            if event_callback is not None:
                event_callback({
                    "phase": "auditor_rebaseline_result",
                    "status": (
                        "succeeded"
                        if getattr(auditor_report, "completed", False)
                        else "failed"
                    ),
                    "completed": bool(
                        getattr(auditor_report, "completed", False)
                    ),
                    "stop_reasons": list(
                        getattr(auditor_report, "stop_reasons", ()) or ()
                    ),
                })
        if not getattr(auditor_report, "completed", False):
            reasons = getattr(auditor_report, "stop_reasons", [])
            raise RuntimeError(f"auditor가 불완전 종료되어 폴더링을 중단합니다: {reasons}")
        print(
            f"🔎 auditor 완료: {len(getattr(auditor_report, 'results', []))}쌍 "
            f"(coverage_limited={getattr(auditor_report, 'coverage_limited', False)})"
        )
        if managed_mode:
            enrich_entries_from_state_db(entries, state_db_path, read_only=pure_plan)
        performance_metrics["auditor_seconds"] = round(
            time.perf_counter() - stage_started_at, 6
        )

    # scanner/auditor reconcile이 DB 상태를 바꾼 뒤, 첫 mutation 직전에 다시 검사한다.
    if not dry_run and managed_mode:
        import decision_store

        readiness_conn = decision_store.connect_state_db(state_db_path)
        try:
            decision_store.assert_active_actual_run(
                readiness_conn,
                actual_run_id,
                house_dir=house_dir,
                temp_dir=temp_dir,
                full_evidence=True,
            )
            issues = decision_store.doctor_issues(
                readiness_conn, allowed_active_run_id=actual_run_id
            )
        finally:
            readiness_conn.close()
        if issues:
            raise RuntimeError(
                f"reconcile 후 doctor issue로 actual mutation을 중단합니다: "
                f"{len(issues)} ({issues[0]['kind']})"
            )

    epub_analysis_hold_records = []
    if managed_mode and audit_suspects and move_suspects:
        epub_analysis_hold_records = _managed_epub_analysis_hold_records(
            auditor_report,
            entries,
            state_db_path,
            temp_dir,
            dry_run,
            actual_run_id=actual_run_id,
        )
    epub_analysis_hold_paths = {
        record["entry"]["path"] for record in epub_analysis_hold_records
    }

    stage_started_at = time.perf_counter()
    exact_groups = find_exact_duplicates([
        entry for entry in entries
        if entry["path"] not in epub_analysis_hold_paths
    ])
    performance_metrics["exact_grouping_seconds"] = round(
        time.perf_counter() - stage_started_at, 6
    )
    multi_representative_paths = set()
    if managed_mode:
        multi_representative_paths = find_multi_representative_candidates(
            exact_groups, auditor_report, entries
        )
        mark_multi_representative_candidates(
            entries, multi_representative_paths, state_db_path, dry_run=dry_run
        )
    exact_records = []
    excluded_paths = set(epub_analysis_hold_paths)
    if managed_mode:
        exact_records = _managed_exact_records(
            exact_groups,
            state_db_path,
            temp_dir,
            dry_run,
            actual_run_id,
            blocked_candidate_paths=multi_representative_paths,
        )
        excluded_paths.update(
            record["entry"]["path"] for record in exact_records
            if record.get("action") != "managed_report_only"
        )
    else:
        for group in exact_groups:
            records = move_or_delete_exact_group(group, temp_dir, dry_run, delete_exact)
            exact_records.extend(records)
            excluded_paths.update(record["entry"]["path"] for record in records)

    # 버킷을 만들기 전에 본문 유사도로 별개 작품에 〔Dn〕 마커를 부여한다.
    # (번호 확정이 버킷 생성보다 먼저여야 신규 무마커가 기존 D2/D3와 비교된다.)
    # NOTE: assign_disambig는 remaining(exact 격리 제외분)으로 돌면서 entry dict의
    # ["disambig"]/["name"]/["path"]를 in-place로 갱신한다. 바로 아래 find_suspect_groups는
    # 같은 entries 리스트의 같은 dict 객체를 재사용하므로, 그 갱신된 disambig로 버킷이
    # 나뉜다(dry-run 포함). 즉 "두 단계가 같은 dict를 공유한다"는 전제에 의존한다.
    # remaining을 새 dict로 복사하거나 entries를 따로 만들면 이 연결이 끊기니 주의.
    remaining = [
        e for e in entries
        if e["path"] not in excluded_paths and e.get("mutation_eligible", True)
    ]
    disambig_records = []
    if move_suspects and not managed_mode:
        disambig_records = assign_disambig(remaining, dry_run)
        assigned = [r for r in disambig_records if r.get("status") == "assigned"]
        collisions = [r for r in disambig_records if r.get("status") == "skipped_collision"]
        if assigned:
            print(f"🔖 분리 마커 부여: {len(assigned)}개 (본문 유사도 기반 별개 작품)")
        if collisions:
            print(f"⚠️ 분리 마커 충돌로 건너뜀: {len(collisions)}개 (목적 파일명이 이미 존재)")

    suspect_groups = [] if managed_mode else find_suspect_groups(entries, excluded_paths)
    suspect_move_records = list(epub_analysis_hold_records)
    if move_suspects and not managed_mode:
        for group in suspect_groups:
            suspect_move_records.extend(move_suspect_group(group, temp_dir, dry_run))

    auditor_groups = []
    auditor_relations = []
    blocked_strong_relations = []
    if audit_suspects:
        already_queued = excluded_paths | {
            record["entry"]["path"] for record in suspect_move_records
        }
        auditor_groups = build_auditor_suspect_groups(
            auditor_report, entries, excluded_paths=already_queued,
            blocked_relations=blocked_strong_relations,
        )
        auditor_relations = build_auditor_report_relations(
            auditor_report, entries, excluded_paths=already_queued,
        )
        if managed_mode and move_suspects:
            suspect_move_records.extend(_managed_auditor_queue_records(
                auditor_groups,
                auditor_relations,
                state_db_path,
                temp_dir,
                dry_run,
                already_queued,
                actual_run_id,
                house_dir,
            ))
        elif not managed_mode:
            for group in auditor_groups:
                suspect_move_records.extend(move_suspect_group(group, temp_dir, dry_run))
        suspect_groups.extend(auditor_groups)
        if managed_mode:
            for record in suspect_move_records:
                if (
                    record.get("status") != "warning"
                    or not record.get("classification")
                    or not record.get("keep")
                ):
                    continue
                suspect_groups.append({
                    "origin": "auditor_aux",
                    "core_title": record["core_title"],
                    "keep": record["keep"],
                    "entries": [record["keep"], record["entry"]],
                    "move_entries": [],
                    "same_house_entries": [],
                    "warning_entries": [record["entry"]],
                    "authors": [],
                    "distinct_authors": record["distinct_authors"],
                    "audit_classifications": [record["classification"]],
                    "mutation_blocked": False,
                })

    # status='moved'만 중복 큐 이동으로 집계. 'warning' 보류는 따로 센다.
    # 본문 same 확정 중복(suspected_duplicates로 이동).
    moved_records = [r for r in suspect_move_records if r.get("status") == "moved"]
    strong_quarantine_records = [
        r for r in moved_records if r.get("final_quarantine")
    ]
    review_moved_records = [
        r for r in moved_records if not r.get("final_quarantine")
    ]
    # 애매 + 작가 충돌(author_conflicts로 이동, 사람 판별).
    author_review_records = [r for r in suspect_move_records if r.get("status") == "author_review"]
    # 애매 보류: 출처와 무관하게 warning 검토 큐로 이동.
    warning_records = [r for r in suspect_move_records if r.get("status") == "warning"]
    superseded_records = [
        r for r in suspect_move_records if r.get("status") == "superseded"
    ]
    ordered_duplicate_records = [
        r for r in suspect_move_records if r.get("status") == "ordered_duplicate"
    ]
    metadata_only_records = [
        r for r in suspect_move_records
        if r.get("status") == "metadata_only"
    ]
    author_conflict_count = len(author_review_records)
    same_author_count = len(review_moved_records)
    review_queue_move_count = (
        len(review_moved_records) + len(author_review_records) +
        len([r for r in warning_records if r.get("status") == "warning"]) +
        len([r for r in metadata_only_records if r.get("status") == "metadata_only"])
    )

    # disambig는 실제 부여 성공분만 집계(충돌 건너뜀 제외).
    disambig_assigned = [r for r in disambig_records if r.get("status") == "assigned"]

    legacy_report_only_records = [
        record for record in exact_records if record.get("action") == "legacy_report_only"
    ]
    managed_report_only_records = [
        record for record in exact_records if record.get("action") == "managed_report_only"
    ]
    blocked_intake_paths = sorted({
        record["entry"]["path"]
        for record in managed_report_only_records
        if record.get("entry", {}).get("source") == "temp"
    })
    post_intake_exact_candidate_paths = sorted({
        record["entry"]["path"]
        for record in managed_report_only_records
        if (
            record.get("reason") == "await_same_run_house_keep"
            and record.get("entry", {}).get("source") == "temp"
        )
    })
    exact_mutation_records = [
        record for record in exact_records
        if record.get("action") not in {"legacy_report_only", "managed_report_only"}
    ]
    summary = {
        "dry_run": dry_run,
        "include_temp": include_temp,
        "managed_mode": managed_mode,
        "audit_suspects": audit_suspects,
        "index_input_mode": (
            "verified_snapshot_entries"
            if verified_index_entries is not None else "file_index_json"
        ),
        "performance_metrics": performance_metrics,
        "auditor_metrics": (
            {
                key: getattr(auditor_report, "stats", {}).get(key, 0)
                for key in (
                    "candidate_pairs", "managed_representative_pairs",
                    "result_pairs", "actual_read_bytes",
                    "fingerprint_cache_hits", "fingerprint_cache_misses",
                    "pair_cache_hits", "pair_cache_misses",
                    "house_inventory_source",
                    "house_inventory_reused_files",
                    "house_inventory_load_seconds",
                    "fingerprint_identity_stat_skips",
                    "folderling_initial_read_bytes",
                    "folderling_total_read_bytes",
                )
            }
            if auditor_report is not None else {}
        ),
        "auditor_rebaseline_retry": auditor_rebaseline_retry,
        "auditor_initial_stop_reasons": auditor_initial_stop_reasons,
        "unsafe_legacy_bridge": bool(audit_suspects and not managed_mode),
        "auditor_group_count": len(auditor_groups),
        "auditor_report_relation_count": len(auditor_relations),
        "blocked_strong_relation_count": len(blocked_strong_relations),
        "delete_exact": delete_exact,
        "exact_count": len(exact_records),
        "exact_mutation_count": len(exact_mutation_records),
        "legacy_report_only_count": len(legacy_report_only_records),
        "managed_report_only_count": len(managed_report_only_records),
        "managed_temp_report_only_count": len(blocked_intake_paths),
        "blocked_intake_paths": blocked_intake_paths,
        "post_intake_exact_candidate_paths": (
            post_intake_exact_candidate_paths
        ),
        "multi_representative_conflict_count": len(multi_representative_paths),
        "suspect_group_count": len(suspect_groups),
        "suspect_move_count": len(moved_records),
        "strong_equivalent_quarantine_count": len(strong_quarantine_records),
        "review_queue_move_count": review_queue_move_count,
        "warning_count": len(warning_records),
        "epub_analysis_hold_count": len(epub_analysis_hold_records),
        "contained_upgrade_count": len(superseded_records),
        "ordered_body_quarantine_count": len(ordered_duplicate_records),
        "metadata_only_count": len(metadata_only_records),
        "author_conflict_count": author_conflict_count,
        "same_author_count": same_author_count,
        "disambig_count": len(disambig_assigned),
        "disambig_skipped_count": len(disambig_records) - len(disambig_assigned),
        "pure_plan": pure_plan,
        "write_surfaces": [] if pure_plan else ["fingerprint_cache", "index_if_rescan", "review_log"],
    }
    _emit_dedup_record_events(
        event_callback, exact_records, suspect_move_records
    )
    log_path = None
    if not pure_plan:
        log_path = write_review_log(
            temp_dir, exact_records, suspect_groups, suspect_move_records, summary,
            disambig_records, blocked_strong_relations,
        )
    if log_path and not summary.get("report_path"):
        summary["report_path"] = log_path

    # 실제 파일 상태가 바뀐 경우에만 인덱스를 재생성한다.
    moved_suspects = [
        r for r in suspect_move_records
        if r.get("status") in (
            "moved", "warning", "author_review", "metadata_only", "superseded",
            "ordered_duplicate",
        )
    ]
    changed = bool(exact_mutation_records or moved_suspects or disambig_assigned)
    if not dry_run and changed and update_index_after_run:
        file_list_path = os.path.join(os.path.dirname(index_path), "file_list.json")
        generate_file_list(
            [house_dir],
            file_list_path,
            index_path,
            state_db_path=state_db_path if managed_mode else None,
        )
        # 단독 dedup 실행에서도 확장 검색 인덱스가 stale되지 않도록 동기화.
        # 단, 운영 기본 인덱스(script_dir/file_index.json)일 때만. 임시/테스트
        # 인덱스 경로로 확장 인덱스를 덮어쓰지 않게 가드한다.
        canonical_index = os.path.join(script_dir, "file_index.json")
        if os.path.abspath(index_path) == os.path.abspath(canonical_index):
            _sync_extension_index(index_path)

    print(f"→ 완전 중복 처리 대상: {len(exact_records)}개")
    print(f"→ 제목 기반 검토 큐 그룹: {len(suspect_groups)}개")
    if audit_suspects:
        print(f"→ auditor 연결 검토 큐 그룹: {len(auditor_groups)}개")
        print(f"→ auditor report-only 직접 관계: {len(auditor_relations)}개")
        print(f"→ 좌표/identity 차단 strong 관계: {len(blocked_strong_relations)}개")
    if move_suspects:
        if strong_quarantine_records:
            print(
                "→ 강한 동일 본문 최종 격리: "
                f"{len(strong_quarantine_records)}개 (복구 가능)"
            )
        if review_moved_records:
            print(
                f"→ 중복 의심 검토 큐 이동: {len(review_moved_records)}개"
            )
        if author_review_records:
            print(f"→ 작가 충돌 큐(author_conflicts): {len(author_review_records)}개 (사람 판별)")
        if warning_records:
            print(f"→ 애매(warning) 검토 큐 이동: {len(warning_records)}개")
        if superseded_records:
            print(
                f"→ 완전 포함 최신판 자동 교체: {len(superseded_records)}개 "
                "(짧은 판본은 superseded_versions에 격리)"
            )
        if ordered_duplicate_records:
            print(
                f"→ 순서형 본문 95% 자동 격리: {len(ordered_duplicate_records)}개 "
                "(ordered_body_duplicates에서 복구 가능)"
            )
        if metadata_only_records:
            print(f"→ 본문 증거 없음(metadata_only): {len(metadata_only_records)}개")
    if log_path:
        print(f"🧾 구조화 검토 보고서 생성 완료: {log_path}")
    elif pure_plan:
        print("📝 pure-plan: DB/index/report write 없음")
    else:
        print("⚠️ 구조화 검토 보고서를 저장하지 못했습니다.")
    print(
        "✨ 중복 정리 완료. (강한 동일성은 복구 가능한 최종 quarantine, "
        "나머지 검토 큐는 자동 삭제 아님)\n"
    )
    return summary


def clean_duplicates(
    house_dir,
    temp_dir,
    dry_run=True,
    index_path=None,
    rescan=False,
    move_suspects=False,
    delete_exact=True,
    include_temp=False,
    audit_suspects=False,
    auditor_report=None,
    update_index_after_run=True,
    state_db_path=DEFAULT_STATE_DB,
    require_state_db=False,
    authorized_run_id=None,
    pure_plan=False,
    event_callback=None,
    verified_index_entries=None,
    verified_house_inventory=None,
    before_non_cache_mutation=None,
):
    """Public entry point; actual managed runs require a DB-backed active capability."""
    if not dry_run and not require_state_db:
        raise RuntimeError(
            "1.2.2 Phase A safety gate: legacy actual execution is disabled; "
            "an active managed state DB run is required"
        )
    kwargs = dict(
        house_dir=house_dir,
        temp_dir=temp_dir,
        dry_run=dry_run,
        index_path=index_path,
        rescan=rescan,
        move_suspects=move_suspects,
        delete_exact=delete_exact,
        include_temp=include_temp,
        audit_suspects=audit_suspects,
        auditor_report=auditor_report,
        update_index_after_run=update_index_after_run,
        state_db_path=state_db_path,
        require_state_db=require_state_db,
        pure_plan=pure_plan,
        event_callback=event_callback,
        verified_index_entries=verified_index_entries,
        verified_house_inventory=verified_house_inventory,
        before_non_cache_mutation=before_non_cache_mutation,
    )
    if dry_run or not require_state_db:
        return _clean_duplicates_impl(**kwargs, actual_run_id=None)

    import decision_store
    from mutation_io import mutation_lock_for_roots

    owns_run = authorized_run_id is None
    with mutation_lock_for_roots(house_dir, temp_dir, "deduplicator-command"):
        try:
            if owns_run:
                try:
                    authorized_run_id, _manifest_path = decision_store.prepare_actual_run(
                        state_db_path, house_dir, temp_dir
                    )
                except RuntimeError as exc:
                    raise RuntimeError(
                        f"1.2.1 state DB/journal 검증 실패로 actual mutation을 중단합니다: {exc}"
                    ) from exc
            else:
                conn = decision_store.connect_state_db(state_db_path)
                try:
                    decision_store.assert_active_actual_run(
                        conn, authorized_run_id, house_dir=house_dir, temp_dir=temp_dir
                    )
                finally:
                    conn.close()
            result = _clean_duplicates_impl(**kwargs, actual_run_id=authorized_run_id)
        except Exception as exc:
            if owns_run and authorized_run_id:
                conn = decision_store.connect_state_db(state_db_path)
                try:
                    decision_store.finish_actual_run(
                        conn, authorized_run_id, success=False, error=str(exc)
                    )
                finally:
                    conn.close()
            raise
        if owns_run:
            conn = decision_store.connect_state_db(state_db_path)
            try:
                decision_store.finish_actual_run(conn, authorized_run_id, success=True)
            finally:
                conn.close()
    return result


if __name__ == "__main__":
    options = parse_args(sys.argv)
    clean_duplicates(**options)
