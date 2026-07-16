import os
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote


CHOSUNG_LIST = [
    "ㄱ", "ㄲ", "ㄴ", "ㄷ", "ㄸ", "ㄹ", "ㅁ", "ㅂ", "ㅃ",
    "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅉ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ"
]

EXCLUDED_DIR_NAMES = {
    "_최근",
    "warning",
    "trash_bin",
    "trash bin",
    "dedup_logs",
    "dedup logs",
    "review_actions",
    "pass",
    "__pycache__",
}

SUPPORTED_EXTENSIONS = {".txt", ".epub", ".pdf"}

SOURCE_SITE_TAG_RE = re.compile(
    r"\s*[\(（]\s*[^()（）]*(?:z-library\.sk|1lib\.sk|z-lib\.sk)[^()（）]*\s*[\)）]",
    re.IGNORECASE,
)

# normalizer 규칙 버전. 핵심 추출 로직(core_title/author/max_number/...)이 바뀌면
# bump 한다. file_index.json에 함께 기록되어 stale index 감지에 사용된다.
NORMALIZER_VERSION = "1.2.1"

# pass 폴더를 거쳐 강제 입고된 파일에 부여하는 마커.
# 짧고 전각 괄호를 사용하여 일반 도서 제목과 충돌하지 않도록 한다.
PASS_MARKER = "〔P〕"

# 마커는 "확장자 바로 앞"(접미사)에 붙인다. 앞에 붙이면 자모 폴더 분류와
# 파일 탐색기 정렬이 제목 대신 마커 기준으로 꼬이기 때문이다.
# 예: 제목〔P〕.txt / 제목〔D2〕.txt / 제목〔P〕〔D2〕.txt
#
# 확장자 앞 위치에서 마커를 찾는 정규식(지원 확장자 또는 무확장자 모두 허용).
# pass 마커: 제목 ... 〔P〕 (〔Dn〕) (.ext)?
_PASS_SUFFIX_RE = re.compile(r"〔P〕(?=(?:〔D\d+〕)?(?:\.[^.]+)?$)")
# disambig 마커: 제목 ... 〔Dn〕 (.ext)?  (pass 마커 뒤에 올 수 있음)
_DISAMBIG_SUFFIX_RE = re.compile(r"〔D(\d+)〕(?=(?:\.[^.]+)?$)")

COMPLETION_MARKERS = {
    "완",
    "완결",
    "完",
    "完結",
    "종",
    "終",
}

NOISE_KEYWORDS = [
    "에필로그",
    "에필",
    "후기",
    "포함",
    "미포함",
    "수정",
    "개정판",
    "개정",
    "공금",
    "텍본",
    "전체",
    "본편",
    "19금",
    "19禁",
    "19N",
    "19n",
]

# 게시글 제목 앞에 붙는 접두 노이즈(추천/강추/재업 등). JS normalizer와 동기화.
PREFIX_NOISE_WORDS = [
    "추천",
    "강추",
    "외전추가",
    "웹툰화",
    "모음집",
    "요청",
    "재업",
]

AUTHOR_NOISE_KEYWORDS = {
    # 완결/상태/조각 표기
    "완",
    "완결",
    "외전",
    "후기",
    "공금",
    "퓨전",
    "게임",
    "연재중",
    "연재",
    "재업",
    "본편",
    "외전포함",
    # 파일 포맷/플랫폼
    "txt",
    "text",
    "텍본",
    "이북",
    "이펍",
    "ebook",
    "epub",
    "pdf",
    "ios",
    "iphone",
    "아이폰",
    "android",
    "안드",
    "안드로이드",
    "pc",
    # 버전/개정
    "최신",
    "수정",
    "수정본",
    "수정판",
    "개정",
    "개정판",
    # 권/시즌 구분
    "상",
    "중",
    "하",
    "시즌",
    # 출판/배포 식별
    "library",
    "라이브러리",
    "j사",
    "카",
    # 회차/누락 메모 흔적
    "누락",
    "중복",
    "후일담",
    "예정",
}

# 한 글자/짧은 알파벳 토큰은 거의 항상 플랫폼/버전 약어이지 작가명이 아니다.
AUTHOR_MIN_LATIN_TOKEN_LEN = 4


def normalize_nfc(value):
    return unicodedata.normalize("NFC", value or "")


# 분석 단계에서만 적용하는 전각→반각 매핑.
# 표시 이름은 그대로 두되, core_title/author/max_number 추출에서 전각 구두점이
# 비교/컷 패턴을 빠져나가지 않도록 사전 치환한다.
_ANALYSIS_PUNCT_TRANSLATION = {
    ord("－"): "-",   # FULLWIDTH HYPHEN-MINUS
    ord("〜"): "~",   # WAVE DASH
    ord("～"): "~",   # FULLWIDTH TILDE
    ord("，"): ",",
    ord("．"): ".",
    ord("："): ":",
    ord("（"): "(", ord("）"): ")",
    ord("［"): "[", ord("］"): "]",
    ord("｛"): "{", ord("｝"): "}",
}


def _normalize_for_analysis(value):
    return normalize_nfc(value).translate(_ANALYSIS_PUNCT_TRANSLATION)


def normalize_filename(name):
    """파일명 또는 폴더명 표시 형태를 정리한다."""
    name = normalize_nfc(unquote(name.lstrip("\ufeff")))
    name = name.replace("+", " ").replace("_", " ").strip()
    name = SOURCE_SITE_TAG_RE.sub(" ", name)

    base, ext = os.path.splitext(name)
    match = re.match(r"^\[(.*?)\]\s*(.+)$", base)
    if match:
        bracket_content, rest = match.groups()
        base = f"{rest} [{bracket_content}]"

    base = re.sub(r"\s+", " ", base).strip()
    return base + ext


def should_exclude_dir(name):
    normalized = normalize_nfc(name).strip()
    return (
        not normalized
        or normalized.startswith(".")
        or normalized.lower() in EXCLUDED_DIR_NAMES
    )


def should_exclude_file(name):
    normalized = normalize_nfc(name).strip()
    return not normalized or normalized.startswith(".") or normalized == "Icon\r"


def is_supported_file(name):
    _, ext = os.path.splitext(normalize_nfc(name))
    return ext.lower() in SUPPORTED_EXTENSIONS


def _split_name_ext(name):
    """(확장자 제외 본문, 확장자) 분리. 지원 확장자만 분리하고 그 외는 ext="".
    마커는 확장자 앞에 붙으므로, 마커 처리 시 확장자를 떼어 두고 다룬다."""
    base, ext = os.path.splitext(name)
    if ext.lower() in SUPPORTED_EXTENSIONS:
        return base, ext
    return name, ""


def has_pass_marker(name):
    """pass 마커가 (확장자 앞) 접미사로 붙어 있는지."""
    return bool(_PASS_SUFFIX_RE.search(normalize_nfc(name or "")))


def add_pass_marker(name):
    """pass 마커를 확장자 앞에 붙인다. 이미 있으면 그대로. 〔Dn〕보다 앞에 둔다."""
    normalized = normalize_nfc(name or "")
    if has_pass_marker(normalized):
        return normalized
    base, ext = _split_name_ext(normalized)
    # 기존 disambig가 있으면 그 앞에 pass를 끼운다(제목〔P〕〔Dn〕.ext 순서 유지).
    dmatch = _DISAMBIG_SUFFIX_RE.search(base)
    if dmatch:
        head = base[:dmatch.start()]
        dis = base[dmatch.start():]
        return f"{head}{PASS_MARKER}{dis}{ext}"
    return f"{base}{PASS_MARKER}{ext}"


def strip_pass_marker(name):
    """pass 마커를 떼어낸 이름을 반환한다."""
    return _PASS_SUFFIX_RE.sub("", normalize_nfc(name or ""))


def read_disambig_marker(name):
    """`〔Dn〕` 마커 번호를 반환한다. 마커가 없으면 1(=base/D1)."""
    match = _DISAMBIG_SUFFIX_RE.search(normalize_nfc(name or ""))
    return int(match.group(1)) if match else 1


def strip_disambig_marker(name):
    """`〔Dn〕` 마커를 떼어낸 이름을 반환한다(표시/검색/core_title용)."""
    return _DISAMBIG_SUFFIX_RE.sub("", normalize_nfc(name or ""), count=1)


def add_disambig_marker(name, n):
    """`〔Dn〕` 마커를 확장자 앞(+pass 마커 뒤)에 붙인다. n<=1이면 마커 없이 반환.
    기존 disambig 마커가 있으면 교체한다."""
    normalized = strip_disambig_marker(normalize_nfc(name or ""))
    if n is None or n <= 1:
        return normalized
    base, ext = _split_name_ext(normalized)
    # pass 마커가 있으면 그 뒤(확장자 앞)에 둔다. 없으면 그냥 확장자 앞.
    pmatch = _PASS_SUFFIX_RE.search(base)
    if pmatch:
        return f"{base[:pmatch.end()]}〔D{n}〕{base[pmatch.end():]}{ext}"
    return f"{base}〔D{n}〕{ext}"


# trash_bin에서 임시로 붙는 충돌 회피 꼬리표(_suspect_N / _dup_N / _pass_N).
# 사용자가 검토 큐 파일을 trash_bin 밖으로 옮겼을 때, 어디서든 안전하게 떼어낼 수 있다.
_TRASH_SUFFIX_RE = re.compile(r"_(?:suspect|dup|pass)_\d+(?=\.[^.]+$)")


def strip_trash_suffix(filename):
    """`_suspect_N` 등 휴지통 꼬리표를 제거한 파일명을 반환한다."""
    return _TRASH_SUFFIX_RE.sub("", normalize_nfc(filename or ""))


def get_chosung(char):
    if "가" <= char <= "힣":
        code = ord(char) - 0xAC00
        return CHOSUNG_LIST[code // 588]
    if char.isdigit():
        return "숫자"
    if "a" <= char.lower() <= "z":
        return "영어"
    return "기타"


def _split_supported_extension(filename):
    normalized = _normalize_for_analysis(filename)
    base, ext = os.path.splitext(normalized)
    if ext.lower() in SUPPORTED_EXTENSIONS:
        return base, ext
    return normalized, ""


def _without_extension(filename):
    base, _ = _split_supported_extension(filename)
    return unquote(base)


def _bracket_tokens(text):
    patterns = [
        r"\[(.*?)\]",
        r"\((.*?)\)",
        r"【(.*?)】",
        r"\{(.*?)\}",
    ]
    tokens = []
    for pattern in patterns:
        tokens.extend(token.strip() for token in re.findall(pattern, text))
    return [token for token in tokens if token]


def _compact_token(text):
    return re.sub(r"[\s,./_\-]+", "", normalize_nfc(text)).lower()


def _compact_search(text):
    """JS normalizeSearchText와 동일: 소문자화 후 영숫자/한글/CJK 한자만 남긴다."""
    return re.sub(r"[^a-z0-9가-힣\u3400-\u9fff\uf900-\ufaff]", "", normalize_nfc(text).lower())


def _is_completion_token(text):
    token = _compact_token(text)
    return token in {_compact_token(marker) for marker in COMPLETION_MARKERS}


def _is_author_noise_token(text):
    token = _compact_token(text)
    if not token:
        return True
    if any(keyword in token for keyword in AUTHOR_NOISE_KEYWORDS):
        return True

    # 라틴 글자만으로 이루어진 짧은 토큰(NF, IOS, PC 등)은 작가가 아니라 약어/태그로 본다.
    if re.fullmatch(r"[a-z]+", token) and len(token) < AUTHOR_MIN_LATIN_TOKEN_LEN:
        return True

    # 토큰 안 숫자 비중이 절반 이상이면 회차/날짜/메모로 본다 (예: "984화중복", "1.14일후일담예정").
    digit_count = sum(1 for ch in token if ch.isdigit())
    if digit_count and digit_count * 2 >= len(token):
        return True

    return False


def extract_author(filename):
    """괄호나 @ 뒤에 붙은 작가 후보를 보존한다. 확정값이 아니라 검색 보조값이다.

    @로 시작하는 태그는 보통 작가지만 사이트/위치/상태 태그(`@홈5`, `@연재중`,
    `@NF Library`)가 섞여 있다. 후보가 노이즈로 판정되면 괄호 토큰 폴백으로 이어간다.
    """
    base = _without_extension(filename)

    at_match = re.search(r"@([^\s\[\](){}【】]+)", base)
    if at_match:
        candidate = at_match.group(1).strip()
        if not _is_author_noise_token(candidate):
            return candidate

    for token in reversed(_bracket_tokens(base)):
        if _is_completion_token(token) or _is_author_noise_token(token):
            continue
        if re.search(r"[가-힣A-Za-z]", token):
            return token

    return None


def has_completion_marker(filename):
    base = _without_extension(filename)
    if any(_is_completion_token(token) for token in _bracket_tokens(base)):
        return True

    return bool(
        re.search(r"(?<![가-힣A-Za-z])(?:완결|完結|완|完|終|종)(?![가-힣A-Za-z])", base)
        or re.search(r"\d+\s*(?:완결|完結|완|完|終)", base)
    )


def extract_max_number(filename):
    name = _without_extension(filename)
    name = re.sub(r"\[.*?\]|\(.*?\)|【.*?】|\{.*?\}", " ", name)

    numbers = []
    for token in re.findall(r"\d+", name):
        try:
            value = int(token)
        except ValueError:
            continue
        # 6자리 이상은 날짜/판번호(예: 250919)로 보고 편수에서 제외한다.
        # 5자리(10000~99999)까지는 장기 연재 웹소설을 위해 허용.
        if value >= 100000:
            continue
        numbers.append(value)

    return max(numbers) if numbers else 0


# 회차/권 비교에서 날짜·판번호로 보고 제외하는 임계(6자리 이상).
_NUMBER_MAX = 100000

# 범위 표기 뒤에 붙을 수 있는 단위. '부'는 시리즈 분할(부작)이라 본편 회차가 아니다.
_RANGE_WITH_UNIT_RE = re.compile(
    r"(?<!\d)(\d+)\s*(화|회|권|장|편|부)?\s*[~\-]\s*"
    r"(\d+)\s*(화|회|권|장|편|부)?"
)
_SPACE_RANGE_RE = re.compile(r"(?<!\d)(\d+)\s+(\d+)\s*(화|회|권|장|편)(?!차)")
_UNIT_NUMBER_RE = re.compile(
    r"(?<!\d)(\d+)\s*(화|회|권|장|편|부)(?!\s*차|[가-힣A-Za-z])"
)
_COMPLETION_NUMBER_RE = re.compile(r"(?<!\d)(\d+)\s*(?:완결|完結|완|完|終)(?![가-힣A-Za-z])")
_HYPHEN_TAIL_RE = re.compile(r"[~\-]\s*(\d+)\s*(?=(?:완결|完結|완|完|終|종|$))")
_SIDE_WORD = r"(?:외전|번외|특외|부외|후일담|에필로그|에필|(?<![가-힣A-Za-z])외(?![가-힣A-Za-z])|外傳|外伝|(?<![가-힣A-Za-z])外(?![가-힣A-Za-z]))"
_SIDE_PREFIX_RE = re.compile(_SIDE_WORD + r"[^0-9]{0,8}$")
_SIDE_SUFFIX_RE = re.compile(
    r"^\s*[\(\[【{]?\s*" + _SIDE_WORD + r"(?!\s*(?:포함|\d))"
)
_META_NUMBER_PREFIX_RE = re.compile(
    r"(?:시즌|누락|중복|수정|추가|부족|완결|完結|완|完)\s*$", re.IGNORECASE
)
_META_NUMBER_SUFFIX_RE = re.compile(
    r"^(?=.{0,30}(?:누락|중복|수정|추가|임플란트|예정))", re.DOTALL
)
_META_SUFFIX_RE = re.compile(
    r"^\s*(?:(?:완결|完結|완|完|終|종|외전|외포|포함|미포함|본편|"
    r"판\d{6}|현\d{6}|@[^^\s]+|[\[\](){}【】]|[,:;|★·\-_/\\]|\w+)\s*)*$",
    re.IGNORECASE,
)
_KNOWN_NUMERIC_META_RE = re.compile(r"(?:판|현)\d{6}|제\d+판", re.IGNORECASE)


@dataclass(frozen=True)
class EpisodeSpan:
    start: int
    end: int
    unit: str
    explicit_range: bool
    source_text: str
    role: str


def _analysis_base(filename):
    """괄호/@태그를 제거한 분석용 본문 문자열."""
    base = _without_extension(filename)
    base = re.sub(r"\[.*?\]|\(.*?\)|【.*?】|\{.*?\}", " ", base)
    base = re.sub(r"@[^\s]+", " ", base)
    return base


def _episode_analysis_base(filename):
    """span role 판정을 위해 외전/본편 괄호 표기는 보존하고 작가 @태그만 제거한다."""
    return re.sub(r"@[^\s]+", " ", _without_extension(filename))


def _span_unit(raw_unit, explicit_range=False):
    if raw_unit == "권":
        return "권"
    if raw_unit == "부":
        return "미상"
    return "화" if raw_unit or explicit_range else "미상"


def _span_role(base, start_at, end_at, raw_unit, start, end):
    if start >= _NUMBER_MAX or end >= _NUMBER_MAX:
        return "date"
    prefix = base[max(0, start_at - 12):start_at]
    suffix = base[end_at:end_at + 20]
    if _SIDE_PREFIX_RE.search(prefix) or _SIDE_SUFFIX_RE.search(suffix):
        return "side"
    if _META_NUMBER_PREFIX_RE.search(prefix) or _META_NUMBER_SUFFIX_RE.search(suffix):
        return "metadata"
    if raw_unit == "부":
        return "part"
    if raw_unit == "권":
        return "volume"
    return "main"


def _overlaps(span, occupied):
    return any(span[0] < end and start < span[1] for start, end in occupied)


def extract_episode_spans(filename):
    """회차/권수 후보를 위치 순으로 수집한다.

    판정에서 제외할 날짜·부 좌표와 외전 자체 범위도 role로 보존한다. 이 함수는 후보를
    임의로 하나로 합치지 않는다.
    """
    base = _episode_analysis_base(filename)
    found = []
    occupied = []

    def add(match, start, end, raw_unit, explicit, role=None):
        if start >= _NUMBER_MAX or end >= _NUMBER_MAX:
            role = "date"
        role = role or _span_role(base, match.start(), match.end(), raw_unit, start, end)
        found.append((match.start(), EpisodeSpan(
            start=max(1, start),
            end=end,
            unit=_span_unit(raw_unit, explicit),
            explicit_range=explicit,
            source_text=match.group(0),
            role=role,
        )))
        occupied.append((match.start(), match.end()))

    for match in _RANGE_WITH_UNIT_RE.finditer(base):
        if match.start() > 0 and base[match.start() - 1] == "%":
            continue
        start = int(match.group(1))
        end = int(match.group(3))
        raw_unit = match.group(4) or match.group(2)
        if end < start:
            continue
        add(match, start, end, raw_unit, True)

    # 공백 구분 범위는 명시 단위가 있고 끝값이 충분히 크며 제목 뒤 메타 영역에 있을 때만.
    for match in _SPACE_RANGE_RE.finditer(base):
        if _overlaps((match.start(), match.end()), occupied):
            continue
        start, end = int(match.group(1)), int(match.group(2))
        if end < 10 or end < start:
            continue
        # 숫자 쌍 뒤는 완료/작가/상태 메타 영역이어야 한다. 평문 작가가 붙는 운영 파일도 허용.
        suffix = base[match.end():]
        if re.search(r"\d", _KNOWN_NUMERIC_META_RE.sub("", suffix)):
            continue
        if not _META_SUFFIX_RE.match(suffix):
            continue
        add(match, start, end, match.group(3), True)

    for match in _UNIT_NUMBER_RE.finditer(base):
        if _overlaps((match.start(), match.end()), occupied):
            continue
        number = int(match.group(1))
        raw_unit = match.group(2)
        role = _span_role(base, match.start(), match.end(), raw_unit, number, number)
        add(match, number, number, raw_unit, False, role=role)

    for match in _COMPLETION_NUMBER_RE.finditer(base):
        if _overlaps((match.start(), match.end()), occupied):
            continue
        end = int(match.group(1))
        if end >= 10 and not (match.start() > 0 and base[match.start() - 1] == "%"):
            prefix = base[max(0, match.start() - 12):match.start()]
            forced_role = "metadata" if re.search(r"본편\s*$", prefix) and any(
                span.role == "main" for _, span in found
            ) else None
            add(match, 1, end, None, False, role=forced_role)

    for match in _HYPHEN_TAIL_RE.finditer(base):
        if _overlaps((match.start(), match.end()), occupied):
            continue
        end = int(match.group(1))
        if end >= 10:
            add(match, 1, end, None, False)

    # 단위 없는 숫자는 제목 끝의 유일한 bare 숫자일 때만 회차 후보로 쓴다.
    if not any(span.role in {"main", "side", "volume"} for _, span in found):
        bare_matches = list(re.finditer(r"(?<!\d)(\d+)(?!\d)", base))
        eligible = [m for m in bare_matches if int(m.group(1)) < _NUMBER_MAX]
        if len(eligible) == 1:
            match = eligible[0]
            if not base[match.end():].strip():
                add(match, 1, int(match.group(1)), None, False)

    return [span for _, span in sorted(found, key=lambda item: item[0])]


def _merge_contiguous_main(main):
    if len(main) < 2:
        return main[0] if main else None
    if any(left.unit != right.unit or right.start != left.end + 1 for left, right in zip(main, main[1:])):
        return None
    return EpisodeSpan(
        start=main[0].start,
        end=main[-1].end,
        unit=main[0].unit,
        explicit_range=True,
        source_text=" + ".join(span.source_text for span in main),
        role="main",
    )


def _select_episode_span(filename):
    spans = extract_episode_spans(filename)
    main = [span for span in spans if span.role == "main"]
    if len(main) == 1:
        return main[0], False
    if len(main) > 1:
        merged = _merge_contiguous_main(main)
        return (merged, False) if merged else (None, True)

    # 외전 단독본이나 단일 권 파일은 그 파일 자체의 유일한 내용 좌표를 보존한다.
    fallback = [span for span in spans if span.role in {"side", "volume"}]
    if len(fallback) == 1:
        return fallback[0], False
    if len(fallback) > 1:
        return None, True
    return None, False


def select_main_episode_span(spans):
    """이미 수집한 span 목록에서 권위 span을 고른다. 모호하면 None."""
    main = [span for span in spans if span.role == "main"]
    if len(main) == 1:
        return main[0]
    if len(main) > 1:
        return _merge_contiguous_main(main)
    fallback = [span for span in spans if span.role in {"side", "volume"}]
    return fallback[0] if len(fallback) == 1 else None


def extract_episode_span(filename):
    return _select_episode_span(filename)[0]


def is_span_ambiguous(filename):
    return _select_episode_span(filename)[1]


def extract_effective_max(filename):
    """본편(메인) 분량을 키워드 비의존 '갈래(run)' 구조로 추정한다.

    - 연속 범위(`1~500`, `1-19권`)가 있으면 그 끝값들 중 최댓값을 본편으로 본다.
      단 `1-2부`처럼 '부'가 붙은 범위는 시리즈 분할이라 본편 회차가 아니므로 제외.
    - 범위가 없으면 단위(화/회/권/장/편)가 붙은 숫자의 최댓값.
    - 그것도 없으면 맨 숫자(bare) 최댓값(예: 단독 `555`).
    - 6자리 이상(날짜/판번호)은 제외.

    예: `1~100 외전 200` → 100 (연속 범위가 본편, 큰 bare 200은 부수로 무시).
    """
    span, ambiguous = _select_episode_span(filename)
    return 0 if ambiguous or span is None else span.end


def extract_unit(filename):
    """편수 비교 가능 여부를 가르는 단위. '권' / '화' / '미상'.

    - '권' 표기가 있으면 권 단위(출판본).
    - 범위나 화/회/장/편 표기가 있으면 화 단위(웹연재본 기본).
    - 숫자만 동떨어져 있으면 '미상'(비교는 허용하되 확신은 낮음).
    """
    span, ambiguous = _select_episode_span(filename)
    return "미상" if ambiguous or span is None else span.unit


def units_comparable(unit_a, unit_b):
    """두 단위가 편수 우열 비교 가능한지. 미상은 어느 쪽과도 비교 허용,
    권 vs 화처럼 둘 다 알려졌는데 다르면 비교 불가."""
    if unit_a == "미상" or unit_b == "미상":
        return True
    return unit_a == unit_b


def extract_volume_number(filename):
    """권별/부별 분리 파일의 시리즈 좌표를 반환한다.

    반환값은 ``(part, volume)`` 튜플 또는 None.
    - ``part``: '1부', '2부' 같은 시리즈 번호. 없으면 None.
    - ``volume``: 단일 'N권'. 권 범위(`1-6권`)는 회차/권 범위 표기로 보고 None.

    예:
    - '가상야담 개정판 1부 첫달밤 09권'  → (1, 9)
    - '가상야담 개정판 2부 새달밤 22권'  → (2, 22)
    - '바이발할 연대기 1부 1-6권 완'      → (1, None)
    - '어떤 책 3권'                        → (None, 3)
    - '어떤 책 1-3권 모음'                → None  (권 범위만 있을 때)
    - 권/부 표기 자체가 없으면          → None
    """
    base = _without_extension(filename)
    base = re.sub(r"\[.*?\]|\(.*?\)|【.*?】|\{.*?\}", " ", base)
    base = re.sub(r"@[^\s]+", " ", base)

    # `N부작`은 'N부로 이루어진 시리즈 중 N번째' 의미로 part 번호로 사용한다.
    # 예: 베르나르 베르베르 '개미 1부작/2부작/3부작'은 각각 별개 책.
    complex_part = bool(
        re.search(r"\d+\s*[~\-,]\s*\d+\s*부", base)
        or re.search(r"\d+\s*부\s*[~\-,]?\s*\d+\s*부", base)
    )
    match_part_works = re.search(r"(?<!\d)(\d+)\s*부작", base)
    part_matches = re.findall(r"(?<![\d~\-,])(\d+)\s*부(?!작)", base)
    if complex_part:
        part = None
    elif match_part_works:
        part = int(match_part_works.group(1))
    elif len(part_matches) == 1:
        part = int(part_matches[0])
    else:
        part = None

    # 권 범위 표기는 단일 권 매칭에서 제외한다.
    masked = re.sub(r"\d+\s*권\s*[~\-]\s*\d+\s*권", " ", base)
    masked = re.sub(r"\d+\s*[~\-]\s*\d+\s*권", " ", masked)
    match_vol = re.search(r"(\d+)\s*권", masked)
    volume = int(match_vol.group(1)) if match_vol else None

    if part is None and volume is None:
        return None
    return (part, volume)


def extract_start_number(filename):
    """파일명에서 시작 화수/권수를 추출한다.

    - "1~100" / "1-400" 같은 명시적 범위는 시작값 사용 (1 이하면 1로 보정).
    - "101화"처럼 시작이 명시된 단일 표기는 그 값을 사용.
    - "201완", "412 완" 처럼 시작 표기 없이 끝값만 있는 경우는 1로 가정한다.
      (같은 작품을 다른 표기끼리 같은 버킷에 묶기 위해 보수적으로 처리)
    - 숫자가 아예 없으면 1.
    """
    span, ambiguous = _select_episode_span(filename)
    return None if ambiguous or span is None else span.start


def extract_end_number(filename):
    span, ambiguous = _select_episode_span(filename)
    return None if ambiguous or span is None else span.end


def is_side_story(filename):
    """파일명이 외전 단독본인지 판단한다.

    '본편'이 함께 표기되어 있거나, '1-N' 같은 메인 범위 표식이 있으면
    본편+외전 합본으로 보고 외전 단독으로 분류하지 않는다.

    단, '외전 1-N'처럼 외전 표식 바로 뒤에 자체 회차 범위가 붙은 경우는
    외전 자체의 회차이지 본편 범위가 아니므로 외전 단독으로 본다.
    """
    name = _without_extension(filename)
    if not re.search(r"외전|外", name):
        return False

    if re.search(r"본편|本編", name):
        return False

    if any(span.role == "main" for span in extract_episode_spans(filename)):
        return False

    return True


def extract_readable_title(filename):
    """편수/완결/작가 표기를 제거하되 사람이 읽을 제목 형태는 보존한다.

    Chrome 확장의 ``extractReadableTitle``과 같은 플랫폼 검색어를 만들기 위한
    함수다.  중복 묶음 key가 필요하면 :func:`extract_core_title`을 사용한다.
    """
    # 분리 마커 〔Dn〕는 검색/매칭에서 제거해 base와 같은 코어로 인식되게 한다.
    base = _without_extension(strip_disambig_marker(filename))

    # 콜론 분리: "메인: 부제"에서 부제 쪽 검색어 길이가 충분하면 부제를 코어로 본다.
    # (JS extractReadableTitle과 동기화)
    colon_parts = re.split(r"[:：]", base)
    if len(colon_parts) > 1:
        last_part = colon_parts[-1].strip()
        if len(_compact_search(last_part)) >= 4:
            base = last_part

    base = re.sub(r"\[.*?\]|\(.*?\)|【.*?】|\{.*?\}", " ", base)
    # 게시글 접두 태그 제거 (예: "19禁완)", "19금)", "완결)" 등). JS와 동기화.
    base = re.sub(
        r"^\s*(?:19\s*(?:禁|금|N|n)\s*)?(?:(?:완결|완|完)\s*)?[\)\]\}〉》:：,.\-_/\\]+\s*",
        " ",
        base,
        flags=re.IGNORECASE,
    )
    base = re.sub(r"@[^\s]+", " ", base)
    base = re.sub(r"^[^a-zA-Z0-9가-힣\u3400-\u9fff\uf900-\ufaff]+", " ", base)

    # 제목 뒤에 붙는 메타데이터(편수/완결/외전/본편 등)의 첫 등장 위치에서 잘라낸다.
    # 메타데이터 뒤에 붙는 작가명, 판번호, 군더더기 토큰까지 함께 떨어뜨려
    # 같은 제목의 다른 표기를 동일 코어로 묶을 수 있게 한다.
    # NOTE: 단일 단위 매칭에서 `회`는 제외한다. `2회차`, `3회차` 등 의미상 회차 표기에
    # 들어가 제목을 과하게 잘라버리는 부작용이 있다. 진짜 회차 표기는 `1-N화` / `N권`
    # 패턴이 보통이다.
    cut_patterns = [
        r"\d+\s*권\s*[~\-]\s*\d+\s*권",
        r"\d+\s*(?:화|권|부|회|장|편)\s*[~\-]\s*\d+\s*(?:화|권|부|회|장|편)?",
        r"\d+\s*[~\-]\s*\d+",
        # 범위 구분자가 공백으로 치환된 변형: '1 325화' → '1'부터 컷(같은 작품의 다른 표기).
        r"\d+\s+\d+\s*(?:화|권|부|장|편)",
        # 하이픈+숫자만 떨어져 남은 회차 꼬리: '… -379' (작가 태그 제거 후 흔함).
        r"[~\-]\s*\d+",
        r"\d+\s*(?:화|권|부|장|편)",
        r"(?<![가-힣A-Za-z])(?:완결|完結|완|完|終|종)(?![가-힣A-Za-z])",
        r"\d+\s*(?:완결|完結|완|完|終)",
        r"본편|本編|외전|外傳|外伝|(?<![가-힣A-Za-z])外(?![가-힣A-Za-z])",
    ]
    cut_re = re.compile("|".join(cut_patterns))
    # 컷은 "제목 뒤" 메타데이터를 떼기 위한 것이다. 매치가 제목 선두('7부 리그...')에
    # 걸려 제목을 통째로 날리지 않도록, 컷 앞에 실제 제목 글자가 있는 첫 매치에서 자른다.
    for m in cut_re.finditer(base):
        if _compact_search(base[:m.start()]):
            base = base[:m.start()]
            break

    for keyword in NOISE_KEYWORDS:
        base = base.replace(keyword, " ")

    # 접두 노이즈(추천/강추/재업 등)는 제목 맨 앞에서만 제거. JS와 동기화.
    for keyword in PREFIX_NOISE_WORDS:
        base = re.sub(rf"^\s*{re.escape(keyword)}\s*", " ", base)

    base = re.sub(r"^[^a-zA-Z0-9가-힣\u3400-\u9fff\uf900-\ufaff]+", " ", base)
    return re.sub(r"\s+", " ", base).strip()


def extract_core_title(filename):
    """원문형 제목을 소문자 영숫자/한글/CJK만 남긴 안정적인 묶음 key로 만든다."""
    return _compact_search(extract_readable_title(filename))


def analyze_name(name):
    normalized_name = normalize_nfc(name)
    disambig = read_disambig_marker(normalized_name)
    # P/D는 legacy/표시 메타데이터일 뿐 제목 문자가 아니다. 모든 분석 전에 함께 제거한다.
    core_name = strip_pass_marker(strip_disambig_marker(normalized_name))
    _, ext = _split_supported_extension(core_name)
    episode_span, span_ambiguous = _select_episode_span(core_name)
    return {
        "name": normalized_name,
        "ext": ext.lower(),
        "disambig": disambig,
        "core_title": extract_core_title(core_name),
        "author": extract_author(core_name),
        "max_number": extract_max_number(core_name),
        "effective_max": 0 if span_ambiguous or episode_span is None else episode_span.end,
        "unit": "미상" if span_ambiguous or episode_span is None else episode_span.unit,
        "complete": has_completion_marker(core_name),
        "volume_number": extract_volume_number(core_name),
        "start_number": None if span_ambiguous or episode_span is None else episode_span.start,
        "end_number": None if span_ambiguous or episode_span is None else episode_span.end,
        "span_ambiguous": span_ambiguous,
        "is_side_story": is_side_story(core_name),
    }
