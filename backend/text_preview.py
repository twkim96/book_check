"""txt 본문 도입부 판독 유틸.

중복 판정의 두 신호 중 "같은 작품인가"(앞부분 유사도)와 "어느 게 더 완전한가"
(공백 제거 글자수)를 위해 txt 본문을 안전하게 읽는다.

- 대상은 `.txt`만. epub은 ZIP 바이너리라 앞부분을 그대로 읽으면 압축 헤더가 나오므로
  호출 측에서 확장자 가드를 둔다(여기서도 방어적으로 검사).
- BOM이 있는 UTF-8/UTF-16을 우선 판별한 뒤 UTF-8 → CP949(EUC-KR) 순으로 폴백한다.
- 글자수는 인코딩과 무관하게 동일해야 하므로, 디코드 후 공백/개행을 제거한 문자 수를 센다.
"""
from __future__ import annotations

import os
import re
import codecs
import hashlib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path


_WHITESPACE_RE = re.compile(r"\s+")

# 인코딩 자동판별 시도 순서. 한국어 txt는 UTF-8 아니면 CP949(=EUC-KR 상위호환)가 대부분.
_ENCODING_CANDIDATES = ("utf-8", "cp949")

# 글자수 누적 시 한 번에 읽는 바이트 크기.
_CHUNK_BYTES = 1024 * 1024

DEFAULT_ANCHOR_CHARS = 2048
MIN_STRONG_TEXT_CHARS = 512
DEFAULT_MAX_FILE_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_READ_BYTES = 20 * 1024 * 1024 * 1024
DEFAULT_NFC_CARRY_CHARS = 1024 * 1024


class TextAnalysisError(Exception):
    pass


class BodyBudgetExceeded(TextAnalysisError):
    pass


class NormalizationDeferred(TextAnalysisError):
    pass


@dataclass
class ReadBudget:
    max_bytes: int = DEFAULT_MAX_READ_BYTES
    read_bytes: int = 0

    def reserve_pass(self, expected_bytes):
        if expected_bytes < 0 or self.read_bytes + expected_bytes > self.max_bytes:
            raise BodyBudgetExceeded(
                f"read budget exceeded: {self.read_bytes}+{expected_bytes}>{self.max_bytes}"
            )

    def consume(self, byte_count):
        self.read_bytes += byte_count
        if self.read_bytes > self.max_bytes:
            raise BodyBudgetExceeded(
                f"read budget exceeded: {self.read_bytes}>{self.max_bytes}"
            )


@dataclass(frozen=True)
class TextAnalysis:
    path: str
    size: int
    mtime_ns: int
    encoding: str | None
    lossy: bool
    error: str | None
    raw_sha256: str | None
    normalized_sha256: str | None
    normalized_length: int
    front_anchor: str
    tail_anchor: str
    status: str
    read_bytes: int


@dataclass
class BatchScanResult:
    occurrences: dict = field(default_factory=dict)
    prefix_digests: dict = field(default_factory=dict)
    read_bytes: int = 0


@dataclass(frozen=True)
class EdgePreview:
    path: str
    size: int
    mtime_ns: int
    encoding: str | None
    front: str
    tail: str
    uncertain: bool
    error: str | None
    read_bytes: int


_EDGE_CACHE = {}


def clear_edge_preview_cache():
    _EDGE_CACHE.clear()


def _normalize_edge(text):
    return "".join(char for char in unicodedata.normalize("NFC", text.lstrip("\ufeff")) if not char.isspace())


def _encoding_candidates_for_sample(raw):
    """BOM이 명시한 인코딩은 다른 후보로 오인하지 않고 그대로 사용한다."""
    if raw.startswith(codecs.BOM_UTF8):
        return ("utf-8-sig",)
    if raw.startswith(codecs.BOM_UTF16_LE):
        return ("utf-16-le",)
    if raw.startswith(codecs.BOM_UTF16_BE):
        return ("utf-16-be",)
    return _ENCODING_CANDIDATES


def _decode_front(raw):
    candidates = _encoding_candidates_for_sample(raw)
    errors = []
    for encoding in candidates:
        try:
            decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
            return decoder.decode(raw, final=False), encoding, None
        except UnicodeDecodeError as exc:
            errors.append(f"{encoding}: {exc}")
    return "", None, "; ".join(errors)


def _decode_tail(raw, encoding, whole_file=False):
    if whole_file:
        try:
            return raw.decode(encoding), False, None
        except UnicodeDecodeError as exc:
            return "", True, str(exc)
    # 역방향 read는 멀티바이트 문자 중간에서 시작할 수 있으므로 최대 4바이트만 정렬 탐색.
    successes = []
    for offset in range(min(4, len(raw))):
        try:
            successes.append((offset, raw[offset:].decode(encoding)))
        except UnicodeDecodeError:
            continue
    if not successes:
        return "", True, "tail decode boundary not found"
    # 가장 적게 버린 유일한 정렬을 사용한다. 같은 offset의 해는 하나뿐이다.
    offset, text = min(successes, key=lambda item: item[0])
    return text, False, None


def read_text_edges(path, preview_chars=DEFAULT_ANCHOR_CHARS, raw_bytes=64 * 1024):
    """primary dedup 전용 bounded 앞/뒤 preview. 전체 본문을 읽지 않는다."""
    if not _is_txt(path):
        return EdgePreview(str(path), 0, 0, None, "", "", True, "not_txt", 0)
    try:
        resolved, size, mtime_ns = _stat_key(path)
    except OSError as exc:
        return EdgePreview(str(path), 0, 0, None, "", "", True, str(exc), 0)
    key = (resolved, size, mtime_ns, preview_chars, raw_bytes)
    if key in _EDGE_CACHE:
        return _EDGE_CACHE[key]

    read_bytes = 0
    try:
        with open(path, "rb") as stream:
            front_raw = stream.read(raw_bytes)
            read_bytes += len(front_raw)
            front_text, encoding, error = _decode_front(front_raw)
            if encoding is None:
                result = EdgePreview(resolved, size, mtime_ns, None, "", "", True, error, read_bytes)
                _EDGE_CACHE[key] = result
                return result
            if size <= raw_bytes:
                tail_raw = front_raw
                whole_file = True
            else:
                stream.seek(max(0, size - raw_bytes))
                tail_raw = stream.read(raw_bytes)
                read_bytes += len(tail_raw)
                whole_file = False
        tail_text, uncertain, tail_error = _decode_tail(tail_raw, encoding, whole_file=whole_file)
        front = _normalize_edge(front_text)[:preview_chars]
        tail = _normalize_edge(tail_text)[-preview_chars:]
        current = os.stat(path, follow_symlinks=False)
        if current.st_size != size or current.st_mtime_ns != mtime_ns:
            uncertain = True
            tail_error = "stale during edge read"
        result = EdgePreview(
            resolved, size, mtime_ns, encoding, front, tail, uncertain,
            tail_error, read_bytes,
        )
    except (OSError, UnicodeError) as exc:
        result = EdgePreview(resolved, size, mtime_ns, None, "", "", True, str(exc), read_bytes)
    _EDGE_CACHE[key] = result
    return result


def _stat_key(path):
    stat = os.stat(path, follow_symlinks=False)
    return (str(Path(path).resolve()), stat.st_size, stat.st_mtime_ns)


class TextAnalysisCache:
    def __init__(self):
        self._items = {}

    def get(self, path):
        try:
            return self._items.get(_stat_key(path))
        except OSError:
            return None

    def put(self, analysis):
        self._items[(analysis.path, analysis.size, analysis.mtime_ns)] = analysis

    def analyze(self, path, **kwargs):
        cached = self.get(path)
        if cached is not None:
            return cached
        analysis = analyze_text_file(path, **kwargs)
        self.put(analysis)
        return analysis


def _read_chunks(path, budget, chunk_bytes):
    size = os.path.getsize(path)
    budget.reserve_pass(size)
    with open(path, "rb") as stream:
        while True:
            raw = stream.read(chunk_bytes)
            if not raw:
                break
            budget.consume(len(raw))
            yield raw


def _validate_encoding(path, encoding, budget, chunk_bytes):
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    for raw in _read_chunks(path, budget, chunk_bytes):
        decoder.decode(raw)
    decoder.decode(b"", final=True)


def detect_text_encoding(path, budget=None, chunk_bytes=_CHUNK_BYTES):
    """전체 파일 strict decode로 BOM UTF-8/UTF-16, UTF-8, CP949를 판별한다."""
    budget = budget or ReadBudget()
    with open(path, "rb") as stream:
        bom = stream.read(3)
    candidates = _encoding_candidates_for_sample(bom)
    errors = []
    for encoding in candidates:
        try:
            _validate_encoding(path, encoding, budget, chunk_bytes)
            return encoding, None
        except UnicodeDecodeError as exc:
            errors.append(f"{encoding}: {exc}")
    return None, "; ".join(errors) or "no strict decoder matched"


def _safe_nfc_split(value):
    """NFC 문자열에서 미래 청크와 결합 가능한 마지막 starter부터 carry로 남긴다."""
    last_starter = None
    for index in range(len(value) - 1, -1, -1):
        if unicodedata.combining(value[index]) == 0:
            last_starter = index
            break
    if last_starter is None:
        return "", value
    return value[:last_starter], value[last_starter:]


def iter_normalized_text(
    path,
    encoding,
    budget=None,
    chunk_bytes=_CHUNK_BYTES,
    carry_limit=DEFAULT_NFC_CARRY_CHARS,
):
    """whole-string NFC와 같은 결과를 내는 공백 제거 정규화 스트림."""
    budget = budget or ReadBudget()
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    carry = ""
    first_output = True

    def clean(text):
        nonlocal first_output
        if not text:
            return ""
        if first_output:
            text = text.lstrip("\ufeff")
            first_output = False
        return _WHITESPACE_RE.sub("", text)

    for raw in _read_chunks(path, budget, chunk_bytes):
        decoded = decoder.decode(raw)
        normalized = unicodedata.normalize("NFC", carry + decoded)
        emit, carry = _safe_nfc_split(normalized)
        if len(carry) > carry_limit:
            raise NormalizationDeferred(f"NFC carry exceeded {carry_limit} characters")
        cleaned = clean(emit)
        if cleaned:
            yield cleaned

    decoded = decoder.decode(b"", final=True)
    final_text = unicodedata.normalize("NFC", carry + decoded)
    cleaned = clean(final_text)
    if cleaned:
        yield cleaned


def normalized_text_fingerprint(
    path,
    encoding,
    budget=None,
    chunk_bytes=_CHUNK_BYTES,
    carry_limit=DEFAULT_NFC_CARRY_CHARS,
    anchor_chars=DEFAULT_ANCHOR_CHARS,
):
    """정규화 SHA/길이/앞뒤 앵커와 원본 byte SHA를 streaming으로 계산한다."""
    budget = budget or ReadBudget()
    before = budget.read_bytes
    hasher = hashlib.sha256()
    raw_hasher = hashlib.sha256()
    front_parts = []
    front_length = 0
    tail = ""
    normalized_length = 0

    # 원본 hash는 정규화 pass와 별도 raw pass를 만들지 않도록 같은 물리 read에서 계산해야
    # 하지만 iter_normalized_text가 바이트를 감춘다. 인코딩 확정 뒤 이 pass에서 raw hash와
    # 정규화를 함께 수행한다.
    size = os.path.getsize(path)
    budget.reserve_pass(size)
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    carry = ""
    first_output = True

    def consume_text(text):
        nonlocal front_length, tail, normalized_length, first_output
        if not text:
            return
        if first_output:
            text = text.lstrip("\ufeff")
            first_output = False
        text = _WHITESPACE_RE.sub("", text)
        if not text:
            return
        encoded = text.encode("utf-8")
        hasher.update(encoded)
        normalized_length += len(text)
        if front_length < anchor_chars:
            piece = text[:anchor_chars - front_length]
            front_parts.append(piece)
            front_length += len(piece)
        tail = (tail + text)[-anchor_chars:]

    with open(path, "rb") as stream:
        while True:
            raw = stream.read(chunk_bytes)
            if not raw:
                break
            budget.consume(len(raw))
            raw_hasher.update(raw)
            decoded = decoder.decode(raw)
            normalized = unicodedata.normalize("NFC", carry + decoded)
            emit, carry = _safe_nfc_split(normalized)
            if len(carry) > carry_limit:
                raise NormalizationDeferred(f"NFC carry exceeded {carry_limit} characters")
            consume_text(emit)
    consume_text(unicodedata.normalize("NFC", carry + decoder.decode(b"", final=True)))
    return {
        "raw_sha256": raw_hasher.hexdigest(),
        "normalized_sha256": hasher.hexdigest(),
        "normalized_length": normalized_length,
        "front_anchor": "".join(front_parts),
        "tail_anchor": tail,
        "read_bytes": budget.read_bytes - before,
    }


def analyze_text_file(
    path,
    budget=None,
    max_file_bytes=DEFAULT_MAX_FILE_BYTES,
    chunk_bytes=_CHUNK_BYTES,
    carry_limit=DEFAULT_NFC_CARRY_CHARS,
    anchor_chars=DEFAULT_ANCHOR_CHARS,
    min_strong_chars=MIN_STRONG_TEXT_CHARS,
):
    budget = budget or ReadBudget()
    before = budget.read_bytes
    resolved, size, mtime_ns = _stat_key(path)
    if not _is_txt(path):
        return TextAnalysis(resolved, size, mtime_ns, None, False, "not_txt", None, None, 0, "", "", "metadata_only", 0)
    if size > max_file_bytes:
        return TextAnalysis(resolved, size, mtime_ns, None, False, "oversize", None, None, 0, "", "", "oversize_deferred", 0)

    try:
        with open(path, "rb") as stream:
            bom = stream.read(3)
        candidates = _encoding_candidates_for_sample(bom)
        values = None
        encoding = None
        decode_errors = []
        for candidate in candidates:
            try:
                values = normalized_text_fingerprint(
                    path,
                    candidate,
                    budget=budget,
                    chunk_bytes=chunk_bytes,
                    carry_limit=carry_limit,
                    anchor_chars=anchor_chars,
                )
                encoding = candidate
                break
            except UnicodeDecodeError as exc:
                decode_errors.append(f"{candidate}: {exc}")
        if encoding is None or values is None:
            return TextAnalysis(
                resolved, size, mtime_ns, None, True, "; ".join(decode_errors), None, None, 0, "", "",
                "decode_lossy", budget.read_bytes - before,
            )
        length = values["normalized_length"]
        status = "empty_text" if length == 0 else ("insufficient_text" if length < min_strong_chars else "ok")
        current = os.stat(path, follow_symlinks=False)
        if current.st_size != size or current.st_mtime_ns != mtime_ns:
            status = "stale"
        return TextAnalysis(
            resolved, size, mtime_ns, encoding, False, None,
            values["raw_sha256"], values["normalized_sha256"], length,
            values["front_anchor"], values["tail_anchor"], status,
            budget.read_bytes - before,
        )
    except BodyBudgetExceeded:
        raise
    except NormalizationDeferred as exc:
        return TextAnalysis(
            resolved, size, mtime_ns, None, False, str(exc), None, None, 0, "", "",
            "normalization_deferred", budget.read_bytes - before,
        )
    except (OSError, UnicodeError) as exc:
        return TextAnalysis(
            resolved, size, mtime_ns, None, True, str(exc), None, None, 0, "", "",
            "decode_lossy", budget.read_bytes - before,
        )


def extract_position_anchors(
    path,
    analysis,
    positions,
    anchor_chars=DEFAULT_ANCHOR_CHARS,
    budget=None,
    chunk_bytes=_CHUNK_BYTES,
    carry_limit=DEFAULT_NFC_CARRY_CHARS,
):
    """정규화 문자 offset별 앵커를 한 pass에서 추출한다."""
    if not analysis.encoding:
        return {}
    budget = budget or ReadBudget()
    wanted = {label: max(0, int(position)) for label, position in positions.items()}
    result = {label: "" for label in wanted}
    offset = 0
    for chunk in iter_normalized_text(
        path, analysis.encoding, budget=budget, chunk_bytes=chunk_bytes, carry_limit=carry_limit,
    ):
        chunk_end = offset + len(chunk)
        for label, start in wanted.items():
            if len(result[label]) >= anchor_chars or start >= chunk_end or start + anchor_chars <= offset:
                continue
            local_start = max(0, start - offset)
            local_end = min(len(chunk), start + anchor_chars - offset)
            if local_end > local_start:
                result[label] += chunk[local_start:local_end]
        offset = chunk_end
    return result


def batch_scan_normalized(
    path,
    analysis,
    queries,
    prefix_lengths=(),
    budget=None,
    chunk_bytes=_CHUNK_BYTES,
    carry_limit=DEFAULT_NFC_CARRY_CHARS,
):
    """한 긴 파일 pass에서 여러 anchor occurrence와 prefix digest checkpoint를 계산한다."""
    budget = budget or ReadBudget()
    before = budget.read_bytes
    clean_queries = {key: value for key, value in queries.items() if value}
    occurrences = {key: [] for key in clean_queries}
    checkpoints = sorted({int(length) for length in prefix_lengths if int(length) >= 0})
    prefix_digests = {}
    hasher = hashlib.sha256()
    hashed_chars = 0
    max_query = max((len(value) for value in clean_queries.values()), default=1)
    search_tail = ""
    seen_positions = {key: set() for key in clean_queries}

    for chunk in iter_normalized_text(
        path, analysis.encoding, budget=budget, chunk_bytes=chunk_bytes, carry_limit=carry_limit,
    ):
        # Prefix checkpoints are by normalized character count; update UTF-8 hash at exact char boundaries.
        chunk_start = hashed_chars
        chunk_end = chunk_start + len(chunk)
        cursor = 0
        while checkpoints and checkpoints[0] <= chunk_end:
            target = checkpoints.pop(0)
            take = max(0, target - (chunk_start + cursor))
            hasher.update(chunk[cursor:cursor + take].encode("utf-8"))
            cursor += take
            prefix_digests[target] = hasher.copy().hexdigest()
        if cursor < len(chunk):
            hasher.update(chunk[cursor:].encode("utf-8"))
        hashed_chars = chunk_end

        window = search_tail + chunk
        base_position = chunk_start - len(search_tail)
        for key, needle in clean_queries.items():
            start = 0
            while len(occurrences[key]) < 2:
                index = window.find(needle, start)
                if index < 0:
                    break
                absolute = base_position + index
                if absolute not in seen_positions[key]:
                    seen_positions[key].add(absolute)
                    occurrences[key].append(absolute)
                start = index + 1
        search_tail = window[-(max_query - 1):] if max_query > 1 else ""

    return BatchScanResult(
        occurrences=occurrences,
        prefix_digests=prefix_digests,
        read_bytes=budget.read_bytes - before,
    )


def _is_txt(path):
    return os.path.splitext(path)[1].lower() == ".txt"


def _decode_best_effort(raw):
    """바이트열을 후보 인코딩으로 차례로 디코드 시도, 모두 실패하면 replace."""
    for encoding in _encoding_candidates_for_sample(raw):
        try:
            return raw.decode(encoding).lstrip("\ufeff")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _strip_ws(text):
    return _WHITESPACE_RE.sub("", text)


def read_text_preview(path, limit=300):
    """txt 본문 앞부분에서 공백/개행을 제거한 앞 `limit`자를 반환한다.

    같은 작품 판정(유사도)용 `preview_key`. 읽기 실패/비txt면 빈 문자열.
    """
    if not _is_txt(path):
        return ""

    # limit자를 확보하려면 공백 제거를 감안해 넉넉히 읽는다. 한글 UTF-8 기준 글자당
    # 최대 3바이트 + 공백 여유를 보고 limit * 8바이트 + 여유분을 읽는다.
    want_bytes = max(limit * 8, 4096)
    try:
        with open(path, "rb") as f:
            raw = f.read(want_bytes)
    except OSError:
        return ""

    text = _decode_best_effort(raw)
    stripped = _strip_ws(text)
    return stripped[:limit]


def count_text_chars(path):
    """txt 본문에서 공백/개행을 제거한 총 글자수를 반환한다(인코딩 독립).

    최신/완전판 판정용. 큰 파일을 위해 스트리밍으로 누적한다. 비txt/실패면 -1.
    """
    if not _is_txt(path):
        return -1

    total = 0
    try:
        with open(path, "rb") as f:
            # 멀티바이트 문자가 청크 경계에서 잘리는 것을 피하려고 incremental decoder 사용.
            # 후보 인코딩을 먼저 정한 뒤 그 디코더로 끝까지 읽는다.
            head = f.read(_CHUNK_BYTES)
            encoding = _pick_encoding(head)
            import codecs

            decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
            chunk = head
            first_chunk = True
            while chunk:
                text = decoder.decode(chunk)
                if first_chunk:
                    text = text.lstrip("\ufeff")
                    first_chunk = False
                total += len(_strip_ws(text))
                chunk = f.read(_CHUNK_BYTES)
            text = decoder.decode(b"", final=True)
            total += len(_strip_ws(text))
    except OSError:
        return -1

    return total


def _pick_encoding(sample):
    """샘플 바이트로 디코딩 가능한 첫 후보 인코딩을 고른다. 없으면 utf-8(replace)."""
    for encoding in _encoding_candidates_for_sample(sample):
        try:
            sample.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            # 멀티바이트 경계에서 잘렸을 수 있으니, 끝 몇 바이트를 떼고 재시도.
            for trim in (1, 2, 3):
                if trim >= len(sample):
                    break
                try:
                    sample[:-trim].decode(encoding)
                    return encoding
                except UnicodeDecodeError:
                    continue
    return "utf-8"


def preview_similarity(a, b):
    """두 preview 문자열의 유사도(0.0~1.0). difflib 기반."""
    if not a or not b:
        return 0.0
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio()
