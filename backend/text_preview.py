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
import stat
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
LOSSLESS_LEGACY_STATUS = "lossless_legacy_text"
# Strict decoding remains the normal path.  This bounded fallback exists only
# for otherwise-readable legacy novels with a very small number of malformed
# bytes.  The digest uses surrogate escapes under a separate domain, so no bad
# byte is replaced or silently made equal to clean Unicode text.
LOSSLESS_LEGACY_DIGEST_DOMAIN = b"file-check-lossless-legacy-text-v1\0"
DEFAULT_MAX_LEGACY_FILE_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_LEGACY_ESCAPE_BYTES = 4_096
DEFAULT_MAX_LEGACY_DAMAGE_PPM = 1_000  # 0.1%
ORDERED_BODY_MATCH_THRESHOLD_PPM = 950_000
ORDERED_BODY_MIN_SOURCE_CHARS = 100_000
ORDERED_BODY_MAX_GAP_PPM = 20_000
ORDERED_BODY_MAX_LINE_OCCURRENCES = 64
ORDERED_BODY_MAX_MATCH_NODES = 500_000


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


@dataclass(frozen=True)
class NormalizedLineSequence:
    path: str
    size: int
    mtime_ns: int
    dev: int
    ino: int
    ctime_ns: int
    lines: tuple[str, ...]
    weights: tuple[int, ...]
    total_chars: int
    read_bytes: int


@dataclass(frozen=True)
class OrderedBodyCoverage:
    source_chars: int
    target_chars: int
    source_lines: int
    target_lines: int
    matched_chars: int
    matched_lines: int
    coverage_ppm: int
    max_unmatched_chars: int
    repetitive_source_chars: int


def ordered_body_coverage_sufficient(proof: OrderedBodyCoverage) -> bool:
    max_gap = max(
        2_048,
        proof.source_chars * ORDERED_BODY_MAX_GAP_PPM // 1_000_000,
    )
    return bool(
        proof.source_chars >= ORDERED_BODY_MIN_SOURCE_CHARS
        and proof.coverage_ppm >= ORDERED_BODY_MATCH_THRESHOLD_PPM
        and proof.max_unmatched_chars <= max_gap
    )


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


def lossless_legacy_text_fingerprint_bytes(
    raw,
    *,
    max_file_bytes=DEFAULT_MAX_LEGACY_FILE_BYTES,
    max_escape_bytes=DEFAULT_MAX_LEGACY_ESCAPE_BYTES,
    max_damage_ppm=DEFAULT_MAX_LEGACY_DAMAGE_PPM,
):
    """Return a collision-separated, reversible fingerprint for lightly damaged text.

    This is intentionally *not* a repair decoder.  Invalid UTF-8/CP949 bytes are
    represented by Python's reserved surrogate-escape code points, and an odd
    trailing UTF-16 byte is represented the same way.  Encoding the decoded
    value back with the selected codec must reproduce every original byte.
    Files with too much undecodable/control data are rejected as unreadable.
    """
    raw = bytes(raw)
    size = len(raw)
    if size == 0 or size > int(max_file_bytes):
        return None

    candidates = []

    def add_surrogate_candidate(label, payload, encoding, bom=b""):
        try:
            decoded = payload.decode(encoding, errors="surrogateescape")
            if bom + decoded.encode(encoding, errors="surrogateescape") != raw:
                return
        except (UnicodeDecodeError, UnicodeEncodeError, LookupError):
            return
        escaped = sum(0xDC80 <= ord(char) <= 0xDCFF for char in decoded)
        candidates.append((escaped, label, decoded))

    if raw.startswith(codecs.BOM_UTF8):
        add_surrogate_candidate(
            "utf-8-sig", raw[len(codecs.BOM_UTF8):], "utf-8", codecs.BOM_UTF8
        )
    elif raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        if raw.startswith(codecs.BOM_UTF16_LE):
            bom, encoding, label = codecs.BOM_UTF16_LE, "utf-16-le", "utf-16-le"
        else:
            bom, encoding, label = codecs.BOM_UTF16_BE, "utf-16-be", "utf-16-be"
        payload = raw[len(bom):]
        trailing = payload[-1:] if len(payload) % 2 else b""
        even_payload = payload[:-1] if trailing else payload
        try:
            decoded = even_payload.decode(encoding, errors="strict")
            if bom + decoded.encode(encoding, errors="strict") + trailing != raw:
                decoded = None
        except (UnicodeDecodeError, UnicodeEncodeError):
            decoded = None
        if decoded is not None and trailing:
            decoded += chr(0xDC00 + trailing[0])
            candidates.append((1, label, decoded))
    else:
        add_surrogate_candidate("utf-8", raw, "utf-8")
        add_surrogate_candidate("cp949", raw, "cp949")

    if not candidates:
        return None
    escaped, encoding, decoded = min(
        candidates,
        key=lambda item: (item[0], 0 if item[1] == "utf-8" else 1, item[1]),
    )
    controls = sum(
        1
        for char in decoded
        if (ord(char) < 32 and not char.isspace()) or 0x7F <= ord(char) <= 0x9F
    )
    damage = escaped + controls
    damage_ppm = damage * 1_000_000 // max(1, size)
    if (
        escaped == 0
        or escaped > int(max_escape_bytes)
        or damage_ppm > int(max_damage_ppm)
    ):
        return None

    normalized = unicodedata.normalize("NFC", decoded).lstrip("\ufeff")
    normalized = _WHITESPACE_RE.sub("", normalized)
    if not normalized:
        return None
    digest = hashlib.sha256()
    digest.update(LOSSLESS_LEGACY_DIGEST_DOMAIN)
    digest.update(normalized.encode("utf-8", errors="surrogatepass"))
    return {
        "normalized_sha256": digest.hexdigest(),
        "normalized_length": len(normalized),
        "encoding": encoding,
        "escaped_bytes": escaped,
        "control_chars": controls,
        "damage_ppm": damage_ppm,
    }


def lossless_legacy_text_fingerprint(
    path,
    *,
    budget=None,
    max_file_bytes=DEFAULT_MAX_LEGACY_FILE_BYTES,
):
    """Read one bounded file and compute the reversible legacy-text fingerprint."""
    budget = budget or ReadBudget()
    size = os.path.getsize(path)
    if size > int(max_file_bytes):
        return None
    budget.reserve_pass(size)
    raw = bytearray()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(_CHUNK_BYTES)
            if not chunk:
                break
            budget.consume(len(chunk))
            raw.extend(chunk)
    result = lossless_legacy_text_fingerprint_bytes(
        raw, max_file_bytes=max_file_bytes
    )
    if result is not None:
        result["raw_sha256"] = hashlib.sha256(raw).hexdigest()
    return result


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
            legacy = lossless_legacy_text_fingerprint(
                path,
                budget=budget,
                max_file_bytes=min(max_file_bytes, DEFAULT_MAX_LEGACY_FILE_BYTES),
            )
            if legacy is not None and legacy["normalized_length"] >= min_strong_chars:
                current = os.stat(path, follow_symlinks=False)
                status = LOSSLESS_LEGACY_STATUS
                if current.st_size != size or current.st_mtime_ns != mtime_ns:
                    status = "stale"
                detail = (
                    "strict decode failed; reversible byte escape used: "
                    f"encoding={legacy['encoding']}, "
                    f"escaped_bytes={legacy['escaped_bytes']}, "
                    f"control_chars={legacy['control_chars']}, "
                    f"damage_ppm={legacy['damage_ppm']}"
                )
                return TextAnalysis(
                    resolved, size, mtime_ns,
                    f"lossless:{legacy['encoding']}", False, detail,
                    legacy["raw_sha256"], legacy["normalized_sha256"],
                    legacy["normalized_length"], "", "", status,
                    budget.read_bytes - before,
                )
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


def read_normalized_line_sequence(
    path,
    encoding,
    *,
    budget=None,
    max_file_bytes=DEFAULT_MAX_FILE_BYTES,
):
    """Read one current TXT into exact, whitespace-free NFC line tokens.

    Physical lines are retained as order units, while their whitespace is
    removed so line indentation and CRLF/LF differences do not affect proof.
    The caller supplies the already fingerprinted strict encoding.  A complete
    identity check prevents a changed file from contributing cached evidence.
    """
    budget = budget or ReadBudget()
    resolved = str(Path(path).resolve())
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise TextAnalysisError(f"ordered body source is not regular: {path}")
    if before.st_size > max_file_bytes:
        raise NormalizationDeferred(
            f"ordered body source exceeds {max_file_bytes} bytes"
        )
    budget.reserve_pass(before.st_size)
    read_before = budget.read_bytes
    lines = []
    weights = []
    first = True
    try:
        with open(path, "r", encoding=encoding, errors="strict", newline=None) as stream:
            for raw_line in stream:
                if first:
                    raw_line = raw_line.lstrip("\ufeff")
                    first = False
                token = _WHITESPACE_RE.sub(
                    "", unicodedata.normalize("NFC", raw_line)
                )
                if not token:
                    continue
                lines.append(token)
                weights.append(len(token))
    finally:
        # TextIO may buffer ahead, so charge the complete reserved pass even
        # when strict decoding fails partway through.
        budget.consume(before.st_size)
    after = os.stat(path, follow_symlinks=False)
    identity_before = (
        before.st_dev, before.st_ino, before.st_ctime_ns,
        before.st_size, before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev, after.st_ino, after.st_ctime_ns,
        after.st_size, after.st_mtime_ns,
    )
    if identity_before != identity_after:
        raise TextAnalysisError(f"ordered body source changed during read: {path}")
    return NormalizedLineSequence(
        path=resolved,
        size=after.st_size,
        mtime_ns=after.st_mtime_ns,
        dev=after.st_dev,
        ino=after.st_ino,
        ctime_ns=after.st_ctime_ns,
        lines=tuple(lines),
        weights=tuple(weights),
        total_chars=sum(weights),
        read_bytes=budget.read_bytes - read_before,
    )


def ordered_body_coverage(source: NormalizedLineSequence, target: NormalizedLineSequence):
    """Return a weighted ordered-line LCS from ``source`` into ``target``.

    Hunt-Szymanski style matching keeps normal cases near O(matches log N).
    Lines occurring more than 64 times on either side remain in the denominator
    but are not accepted as identity evidence; this both bounds repeated-text
    cost and prevents separators or boilerplate from inflating the score.
    """
    if not source.lines or not target.lines or source.total_chars <= 0:
        return OrderedBodyCoverage(
            source.total_chars, target.total_chars,
            len(source.lines), len(target.lines),
            0, 0, 0, source.total_chars, 0,
        )

    target_positions = {}
    for index, token in enumerate(target.lines, start=1):
        target_positions.setdefault(token, []).append(index)

    source_occurrences = {}
    for token in source.lines:
        source_occurrences[token] = source_occurrences.get(token, 0) + 1
    repetitive_tokens = {
        token for token, count in source_occurrences.items()
        if count > ORDERED_BODY_MAX_LINE_OCCURRENCES
        or len(target_positions.get(token, ())) > ORDERED_BODY_MAX_LINE_OCCURRENCES
    }
    estimated_nodes = sum(
        count * len(target_positions.get(token, ()))
        for token, count in source_occurrences.items()
        if token not in repetitive_tokens
    )
    if estimated_nodes > ORDERED_BODY_MAX_MATCH_NODES:
        raise NormalizationDeferred(
            "ordered body match graph exceeds safe node budget: "
            f"{estimated_nodes}>{ORDERED_BODY_MAX_MATCH_NODES}"
        )

    # Each node is (matched chars, matched lines, source index, previous node).
    nodes = []
    tree = [-1] * (len(target.lines) + 1)

    def better(left_id, right_id):
        if left_id < 0:
            return right_id
        if right_id < 0:
            return left_id
        left = nodes[left_id]
        right = nodes[right_id]
        if (right[0], right[1]) > (left[0], left[1]):
            return right_id
        return left_id

    def query(position):
        best = -1
        while position > 0:
            best = better(best, tree[position])
            position -= position & -position
        return best

    def update(position, node_id):
        while position < len(tree):
            tree[position] = better(tree[position], node_id)
            position += position & -position

    repetitive_source_chars = 0
    for source_index, (token, weight) in enumerate(
        zip(source.lines, source.weights)
    ):
        positions = target_positions.get(token, ())
        if token in repetitive_tokens:
            repetitive_source_chars += weight
            continue
        # Descending target positions prevent one source line from extending
        # another match created for that same line.
        for target_position in reversed(positions):
            previous_id = query(target_position - 1)
            previous_chars = nodes[previous_id][0] if previous_id >= 0 else 0
            previous_lines = nodes[previous_id][1] if previous_id >= 0 else 0
            node_id = len(nodes)
            nodes.append((
                previous_chars + weight,
                previous_lines + 1,
                source_index,
                previous_id,
            ))
            update(target_position, node_id)

    best_id = query(len(target.lines))
    if best_id < 0:
        matched_chars = matched_lines = 0
        matched_source_indices = set()
    else:
        matched_chars, matched_lines = nodes[best_id][:2]
        matched_source_indices = set()
        current = best_id
        while current >= 0:
            matched_source_indices.add(nodes[current][2])
            current = nodes[current][3]

    max_unmatched = current_unmatched = 0
    for index, weight in enumerate(source.weights):
        if index in matched_source_indices:
            max_unmatched = max(max_unmatched, current_unmatched)
            current_unmatched = 0
        else:
            current_unmatched += weight
    max_unmatched = max(max_unmatched, current_unmatched)
    coverage_ppm = matched_chars * 1_000_000 // source.total_chars
    return OrderedBodyCoverage(
        source_chars=source.total_chars,
        target_chars=target.total_chars,
        source_lines=len(source.lines),
        target_lines=len(target.lines),
        matched_chars=matched_chars,
        matched_lines=matched_lines,
        coverage_ppm=coverage_ppm,
        max_unmatched_chars=max_unmatched,
        repetitive_source_chars=repetitive_source_chars,
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
