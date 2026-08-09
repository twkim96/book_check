// extension/normalizer.js
// 확장 런타임이 사용하는 제목 분석 함수 모음. Python normalizer.py와 핵심 의도(같은
// core_title을 만들고, 5자리까지 회차 허용, 외전/완결 표식 처리 등)를 동기화한다.
// service worker(background.js)와 popup이 공유하기 좋게 ES module로 export한다.

export const SUPPORTED_EXTENSIONS = [".txt", ".epub", ".pdf"];

export const NORMALIZER_VERSION = "1.3.3";

export const PASS_MARKER = "〔P〕";

// 마커는 "확장자 바로 앞"(접미사)에 붙인다(Python normalizer와 동치).
// 예: 제목〔P〕.txt / 제목〔D2〕.txt / 제목〔P〕〔D2〕.txt
const PASS_SUFFIX_RE = /〔P〕(?=(?:〔D\d+〕)?(?:\.[^.]+)?$)/;
const DISAMBIG_SUFFIX_RE = /〔D(\d+)〕(?=(?:\.[^.]+)?$)/;

export function readDisambigMarker(value) {
  const match = normalizeNfc(value).match(DISAMBIG_SUFFIX_RE);
  return match ? parseInt(match[1], 10) : 1;
}

export function stripDisambigMarker(value) {
  return normalizeNfc(value).replace(DISAMBIG_SUFFIX_RE, "");
}

export const NOISE_KEYWORDS = [
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
];

export const PREFIX_NOISE_WORDS = [
  "추천",
  "강추",
  "외전추가",
  "웹툰화",
  "모음집",
  "요청",
  "재업",
  "글밈",
  "꾸롶",
];

export function safeDecode(value) {
  try {
    return decodeURIComponent(value);
  } catch (e) {
    return value;
  }
}

export function normalizeNfc(value) {
  return String(value || "").normalize("NFC");
}

// Python normalizer 1.3.0과 동일한 로컬 제목 보호 문법. 게시글 사이트가 이
// 표기를 만들지는 않지만, 양쪽 normalizer의 계약을 완전히 같게 유지하기 위해
// 확장에서도 해석한다. 번들 index에는 materialize된 파일명/core_title만 들어온다.
const TITLE_LITERAL_RE = /\[\[([^\[\]\r\n]+)\]\]/g;
const STRUCTURE_HINT_RE = /\{\{([^{}\r\n]+)\}\}/g;

function _matches(value, pattern) {
  return [...normalizeNfc(value).matchAll(pattern)];
}

export function titleLiteralSyntaxError(value) {
  const normalized = normalizeNfc(value);
  if (_matches(normalized, TITLE_LITERAL_RE).some((match) => !match[1].trim())) {
    return "제목 보호 표시 안에는 보존할 제목을 입력하세요";
  }
  const remainder = normalized.replace(TITLE_LITERAL_RE, "");
  return remainder.includes("[[") || remainder.includes("]]" )
    ? "제목 보호 표시는 [[보존할 제목]]처럼 짝을 맞춰 입력하세요"
    : null;
}

export function structureHintSyntaxError(value) {
  const normalized = normalizeNfc(value);
  if (_matches(normalized, STRUCTURE_HINT_RE).some((match) => !match[1].trim())) {
    return "구조 힌트 안에는 분석할 내용을 입력하세요";
  }
  const remainder = normalized.replace(STRUCTURE_HINT_RE, "");
  return remainder.includes("{{") || remainder.includes("}}")
    ? "구조 힌트는 {{분석할 내용}}처럼 짝을 맞춰 입력하세요"
    : null;
}

export function extractTitleLiteralTokens(value) {
  return _matches(value, TITLE_LITERAL_RE).map((match) => match[1].trim()).filter(Boolean);
}

export function extractStructureHintTokens(value) {
  return _matches(value, STRUCTURE_HINT_RE).map((match) => match[1].trim()).filter(Boolean);
}

export function materializeTitleLiterals(value) {
  return normalizeNfc(value).replace(TITLE_LITERAL_RE, (_match, token) => token.trim());
}

export function materializeStructureHints(value) {
  return normalizeNfc(value).replace(STRUCTURE_HINT_RE, (_match, token) => token.trim());
}

export function materializeTitleMarkup(value) {
  return materializeStructureHints(materializeTitleLiterals(value));
}

function _stripStructureHints(value) {
  return normalizeNfc(value).replace(STRUCTURE_HINT_RE, " ");
}

function _literalPlaceholder(index) {
  return `QZXPROTECTEDTITLE${"A".repeat(index + 1)}QZX`;
}

function _maskTitleLiterals(value) {
  const mapping = new Map();
  const tokens = [];
  const masked = normalizeNfc(value).replace(TITLE_LITERAL_RE, (_match, rawToken) => {
    const token = rawToken.trim();
    const placeholder = _literalPlaceholder(tokens.length);
    tokens.push(token);
    mapping.set(placeholder, token);
    return placeholder;
  });
  return { masked, mapping, tokens };
}

function _restoreTitleLiterals(value, mapping) {
  let restored = value;
  for (const [placeholder, token] of mapping.entries()) {
    restored = restored.split(placeholder).join(token);
  }
  return restored;
}

function splitSupportedExtension(value) {
  const match = value.match(/(\.[^.\\/]+)$/);
  if (match && SUPPORTED_EXTENSIONS.includes(match[1].toLowerCase())) {
    return [value.slice(0, -match[1].length), match[1]];
  }
  return [value, ""];
}

// Python title_cleanup_rules.py와 동일한 1.2.7 닫힌 규칙만 적용한다.
// 실제 파일명은 바꾸지 않고 모든 분석 함수가 보는 가상 파일명만 정리한다.
export function applyTitleCleanupRules(value) {
  let candidate = normalizeNfc(value);
  candidate = candidate.replace(/%25(28|29|40|5b|5d)/gi, "%$1");
  candidate = candidate.split("＠").join("@");
  candidate = candidate.replace(/_dup_\d+(?=\.[^.]+$)/i, "");
  candidate = candidate.replace(/^NWN\s+/, "");
  candidate = candidate.replace(
    /^\s*(?:19\s*[\)）]|[\(（]\s*19\s*[\)）]|[\[［]\s*19\s*[\]］])\s*/,
    "",
  );
  candidate = candidate.replace(/^\s*(?:무협)\s*[\)）]\s*/, "");

  let [base, ext] = splitSupportedExtension(candidate);
  base = base.replace(
    /\s*(?<![\[［])[ⓒ©]\s*(.{1,50}?)(?=\s+0*\d+\s*[-~]\s*\d+)/s,
    (_match, author) => ` [ⓒ${author.trim()}]`,
  );
  base = base.replace(/\s*[ⓩⓖ].{0,50}?(?=\s+0*\d+\s*[-~]\s*\d+)/s, " ");
  base = base.replace(/\s*\$공금\$직\s*$/, "");
  if (ext.toLowerCase() === ".epub") {
    base = base.replace(/\s+RS\s*[\(（][^()（）]{1,50}[\)）]\s*$/, "");
  }
  base = base.replace(/\s+(?:UTF\s*8\s+BOM|noPic\s+ver)\s*$/, "");
  base = base.replace(
    /\s+(\d+)\s+(\d+)\s*\[\s*(?:완결|완|完)\s*\]\s*-\s*(.+?)\s*$/,
    (_match, start, end, author) => ` ${start}-${end} 완 [${author.trim()}]`,
  );
  base = base.replace(
    /\s+(\d+)\s+(\d+)\s+@.+$/,
    (_match, start, end) => ` ${start}-${end}`,
  );
  if ([".epub", ".pdf"].includes(ext.toLowerCase())) {
    base = base.replace(
      /^(.*?\S)\s+(\d{1,3})\s+(\([^()]{2,40}\))$/,
      (_match, title, volume, author) => `${title} ${volume}권 ${author}`,
    );
  }
  base = base.replace(/(^|[^A-Za-z0-9])ep\s*(?=0*\d+\s*[-~]\s*\d+)/i, "$1");
  base = base.replace(/\s+총\s*(\d+)\s*화/, (_match, end) => ` 1-${end}화`);
  base = base.replace(
    /([^0-9외\s])(?:완|完)[⓳⑲](?=\s*(?:\[[^\]]+\])?\s*$)/,
    "$1 完",
  );
  base = base.replace(
    /([A-Za-z가-힣\u3400-\u9fff\uf900-\ufaff])(완|完)(\d+\s*[-~]\s*\d+)/,
    "$1 $3 $2",
  );
  return `${base}${ext}`.replace(/\s+/g, " ").trim();
}

// 분석 단계 전용 전각→반각 매핑 (Python `_normalize_for_analysis`와 동기화).
const _ANALYSIS_PUNCT_MAP = {
  "－": "-",
  "〜": "~",
  "～": "~",
  "，": ",",
  "．": ".",
  "：": ":",
  "（": "(", "）": ")",
  "［": "[", "］": "]",
  "｛": "{", "｝": "}",
};

function _normalizeForAnalysis(value) {
  let s = normalizeNfc(value);
  for (const [from, to] of Object.entries(_ANALYSIS_PUNCT_MAP)) {
    if (s.includes(from)) s = s.split(from).join(to);
  }
  return s;
}

export function removeExtension(value) {
  let normalized = _normalizeForAnalysis(applyTitleCleanupRules(value));
  // 확장자 먼저 분리
  let ext = "";
  const match = normalized.match(/(\.[^.\\/]+)$/);
  if (match && SUPPORTED_EXTENSIONS.includes(match[1].toLowerCase())) {
    ext = match[1];
    normalized = normalized.slice(0, -ext.length);
  }
  // 확장자 앞 접미사 마커(〔P〕/〔Dn〕) 제거
  normalized = normalized.replace(/〔P〕/g, "").replace(/〔D\d+〕/g, "");
  return normalized;
}

export function getExtension(value) {
  const match = normalizeNfc(value).toLowerCase().match(/(\.[^.\\/]+)$/);
  return match ? match[1] : "";
}

export function isSupportedFileName(value) {
  return SUPPORTED_EXTENSIONS.includes(getExtension(value));
}

export function hasPassMarker(value) {
  return PASS_SUFFIX_RE.test(normalizeNfc(value));
}

export function stripPassMarker(value) {
  return normalizeNfc(value).replace(PASS_SUFFIX_RE, "");
}

export function normalizeSearchText(value) {
  return safeDecode(normalizeNfc(value))
    .toLowerCase()
    // 한글/영숫자 + CJK 한자(표의문자) 보존(Python _compact_search와 동치).
    .replace(/[^a-z0-9가-힣\u3400-\u9fff\uf900-\ufaff]/g, "");
}

function replaceAllText(value, search, replacement) {
  return value.split(search).join(replacement);
}

function stripCompletionMarkers(value) {
  return value
    .replace(/(^|[^가-힣A-Za-z])(?:완결|完結|완|完|終|종)(?=$|[^가-힣A-Za-z])/g, "$1 ")
    .replace(/\d+\s*(?:완결|完結|완|完|終)/g, " ");
}

const LEADING_POST_STATUS_RE = /^\s*[\[\({【]?\s*(?:(?:(?:신규|신작|갱신|업데이트|업뎃|재업(?:로드)?|수정(?:본)?|교체|추가)\s*)*(?:(?:신작\s*)?(?:완결|完結|완|完)|19\s*(?:禁|금|N|n)\s*(?:완결|完結|완|完)?)|(?:(?:갱신|업데이트|업뎃|재업(?:로드)?|수정(?:본)?|교체|추가)\s*)+|(?:(?:신규|신작)\s*(?=[\)\]\}】〉》])))\s*[\)\]\}】〉》:：,.\\\-_/]+\s*/i;
const LEADING_SOURCE_AUTHOR_TITLE_RE = /^\s*(?:꾸롶|CSS|판)\s+(?=\[[^\[\]]{1,50}\]\s+.{2,}?\d+\s*(?:[~\-]|(?:화|회|권|장|편|부)))/i;
const ATTACHED_TITLE_PAREN_RE = /(?<=[A-Za-z0-9가-힣\u3400-\u9fff\uf900-\ufaff])\(([^()\[\]]{1,30})\)(?=\s*[A-Za-z0-9가-힣\u3400-\u9fff\uf900-\ufaff])/g;
const TITLE_PAREN_OPEN = "\uE000";
const TITLE_PAREN_CLOSE = "\uE001";

function stripLeadingPostStatus(value) {
  return String(value || "").replace(LEADING_POST_STATUS_RE, "");
}

export function extractReadableTitle(value) {
  let base = _stripStructureHints(safeDecode(removeExtension(value)));
  const literalState = _maskTitleLiterals(base);
  base = literalState.masked;
  const statusPrefix = base.match(LEADING_POST_STATUS_RE);
  if (statusPrefix) {
    base = base.slice(statusPrefix[0].length);
    base = base.replace(
      ATTACHED_TITLE_PAREN_RE,
      (_match, content) => `${TITLE_PAREN_OPEN}${content}${TITLE_PAREN_CLOSE}`,
    );
  }
  base = base.replace(LEADING_SOURCE_AUTHOR_TITLE_RE, "");
  const colonParts = base.split(/[:：]/);
  if (colonParts.length > 1) {
    const lastPart = colonParts[colonParts.length - 1].trim();
    if (normalizeSearchText(lastPart).length >= 4) {
      base = lastPart;
    }
  }
  base = base.replace(/\[.*?\]|\(.*?\)|【.*?】|\{.*?\}/g, " ");
  // 게시글 접두 태그 제거 (예: "19禁완)", "19금)", "완결)" 등)
  base = base.replace(/^\s*(?:19\s*(?:禁|금|N|n)\s*)?(?:(?:완결|완|完)\s*)?[\)\]\}〉》:：,.\-_/\\]+\s*/i, " ");
  base = base.replace(/@[^\s]+/g, " ");
  base = base.replace(/^[^a-zA-Z0-9가-힣\u3400-\u9fff\uf900-\ufaff]+/g, " ");

  // Python normalizer의 cut-off 전략과 동기화: 첫 번째 메타데이터(편수/완결/외전/본편)
  // 위치에서 잘라 그 뒤의 작가명/판번호/꼬리표가 core_title에 끼지 않게 한다.
  // NOTE: 단일 단위 매칭에서 `회`는 제외한다. `2회차`처럼 의미상 회차 표기를 회차 표식으로
  // 잘못 인식해 제목을 과하게 잘라버리는 부작용이 있다.
  const cutPattern = new RegExp(
    [
      "\\d+(?:\\.\\d+)+\\s*권",
      "\\d+\\s*권\\s*[~\\-]\\s*\\d+\\s*권",
      "\\d+\\s*(?:화|권|부|회|장|편)\\s*[~\\-]\\s*\\d+\\s*(?:화|권|부|회|장|편)?",
      "\\d+\\s*[~\\-]\\s*\\d+",
      "(?<![\\d./／\\\\])\\d+\\s+\\d+\\s*(?:화|권|부|장|편)",
      "(?<![\\d./／\\\\])\\d+\\s+\\d+\\s*(?:완결|完結|완|完|終)",
      "[~\\-]\\s*\\d+(?!\\d|\\s*회차)",
      "\\d+\\s*(?:화|권|부|장|편)(?=$|[^가-힣A-Za-z\\u3400-\\u9fff\\uf900-\\ufaff]|(?:완결|完結|완|完|終|종|외전|外傳|外伝|번외|특외|부외|후일담|에필로그|에필|본편|포함|미포함|공금|텍본|합(?:본|완)?|작(?=\\s|$)|누락|중복|수정|추가|부족|까지))",
      "(^|[^가-힣A-Za-z])(?:완결|完結|완|完|終|종)(?=$|[^가-힣A-Za-z])",
      "\\d+\\s*(?:완결|完結|완|完|終)",
      "본편|本編|외전|外傳|外伝|(^|[^가-힣A-Za-z])外(?=$|[^가-힣A-Za-z])",
    ].join("|"),
  );
  // 컷이 제목 선두('7부 리그...')에 걸려 제목을 통째로 날리지 않도록, 컷 앞에 실제
  // 제목 글자가 있는 첫 매치에서 자른다(Python과 동기화). 전역 매칭으로 순회.
  const cutGlobal = new RegExp(cutPattern.source, "g");
  let cm;
  while ((cm = cutGlobal.exec(base)) !== null) {
    let cutAt = cm.index;
    const descendingPlainRange = cm[0].match(/^(\d+)\s*([~\-])\s*(\d+)$/);
    if (descendingPlainRange) {
      const start = Number.parseInt(descendingPlainRange[1], 10);
      const end = Number.parseInt(descendingPlainRange[3], 10);
      if (start > end) {
        if (start >= 1000) {
          cutAt += cm[0].indexOf(descendingPlainRange[2]);
          if (normalizeSearchText(base.slice(0, cutAt))) {
            base = base.slice(0, cutAt);
            break;
          }
        }
        if (/\d+\s*[~\-]\s*\d+/.test(base.slice(cutGlobal.lastIndex))) {
          continue;
        }
      }
    }
    if (cm[1] !== undefined && cm[1] !== "") {
      cutAt += cm[1].length;
    }
    if (normalizeSearchText(base.slice(0, cutAt))) {
      base = base.slice(0, cutAt);
      break;
    }
    if (cm.index === cutGlobal.lastIndex) cutGlobal.lastIndex++; // 빈 매치 방지
  }

  NOISE_KEYWORDS.forEach((keyword) => {
    base = replaceAllText(base, keyword, " ");
  });
  // 선두 상태 태그가 있으면 제목에 붙은 괄호를 임시 보호한다. 그 안이 ``19N`` 같은
  // 등급어뿐이었다면 위 노이즈 제거 뒤 빈 보호 괄호만 남으므로 표시/웹 검색 전에 없앤다.
  // 내용이 남은 ``(근친)`` 같은 실제 제목 괄호는 그대로 보존한다.
  base = base.replace(new RegExp(`${TITLE_PAREN_OPEN}\\s*${TITLE_PAREN_CLOSE}`, "g"), " ");
  PREFIX_NOISE_WORDS.forEach((keyword) => {
    base = base.replace(new RegExp(`^\\s*${keyword}\\s*`, "i"), " ");
  });
  base = base.replace(/^[^a-zA-Z0-9가-힣\u3400-\u9fff\uf900-\ufaff]+/g, " ");
  const readable = base
    .replace(/\s+/g, " ")
    .trim()
    .split(TITLE_PAREN_OPEN).join("(")
    .split(TITLE_PAREN_CLOSE).join(")");
  return _restoreTitleLiterals(readable, literalState.mapping);
}

export function extractCoreTitle(value) {
  return normalizeSearchText(extractReadableTitle(value));
}

export function hasCompletionMarker(value) {
  const base = _maskTitleLiterals(materializeStructureHints(removeExtension(value))).masked;
  return /(^|[^가-힣A-Za-z])(?:완결|完結|완|完|終|종)(?=$|[^가-힣A-Za-z])/.test(base) ||
    /\d+\s*(?:완결|完結|완|完|終)/.test(base);
}

export function extractMaxNumber(value) {
  const base = stripLeadingPostStatus(
    _maskTitleLiterals(materializeStructureHints(removeExtension(value))).masked,
  )
    .replace(/\[.*?\]|\(.*?\)|【.*?】|\{.*?\}/g, " ");
  const numbers = (base.match(/\d+/g) || []).map(Number).filter((n) => !Number.isNaN(n));
  // 6자리 이상은 날짜/판번호로 보고 회차에서 제외, 5자리(10000~99999)까지는 장기연재 허용
  const filtered = numbers.filter((n) => n < 100000);
  return filtered.length ? Math.max(...filtered) : 0;
}

// === effective_max / unit (Python normalizer.py와 동치) ===
// 본편 분량을 키워드 비의존 '갈래(run)' 구조로 추정한다.
const _NUMBER_MAX = 100000;

function _analysisBase(value) {
  // 괄호/@태그 제거한 분석용 본문. removeExtension은 전각→반각 + 〔Dn〕 제거 포함.
  return stripLeadingPostStatus(
    _maskTitleLiterals(materializeStructureHints(removeExtension(value))).masked,
  )
    .replace(/\[.*?\]|\(.*?\)|【.*?】|\{.*?\}/g, " ")
    .replace(/@[^\s]+/g, " ");
}

function _episodeAnalysisBase(value) {
  return stripLeadingPostStatus(
    _maskTitleLiterals(materializeStructureHints(removeExtension(value))).masked,
  ).replace(/@[^\s]+/g, " ");
}

function _spanUnit(rawUnit, explicitRange = false) {
  if (rawUnit === "권") return "권";
  if (rawUnit === "부") return "미상";
  return rawUnit || explicitRange ? "화" : "미상";
}

function _spanRole(base, startAt, endAt, rawUnit, start, end) {
  if (start >= _NUMBER_MAX || end >= _NUMBER_MAX) return "date";
  const prefix = base.slice(Math.max(0, startAt - 12), startAt);
  const suffix = base.slice(endAt, endAt + 20);
  const sideWord = "(?:외전|번외|특외|부외|후일담|에필로그|에필|(?:^|[^가-힣A-Za-z])외(?![가-힣A-Za-z])|外傳|外伝|(?:^|[^가-힣A-Za-z])外(?![가-힣A-Za-z]))";
  if (new RegExp(`${sideWord}[^0-9]{0,8}$`).test(prefix) ||
      new RegExp(`^\\s*[\\(\\[【{]?\\s*${sideWord}(?!\\s*(?:포함|\\d))`).test(suffix)) return "side";
  if (/(?:시즌|누락|중복|수정|추가|부족|완결|完結|완|完)\s*$/i.test(prefix) ||
      /^(?=.{0,30}(?:누락|중복|수정|추가|임플란트|예정))/s.test(suffix)) return "metadata";
  if (rawUnit === "부") return "part";
  if (rawUnit === "권") return "volume";
  return "main";
}

function _overlaps(start, end, occupied) {
  return occupied.some(([otherStart, otherEnd]) => start < otherEnd && otherStart < end);
}

export function extractEpisodeSpans(value) {
  const base = _episodeAnalysisBase(value);
  const found = [];
  const occupied = [];

  function add(match, start, end, rawUnit, explicitRange, forcedRole = null) {
    let role = forcedRole || _spanRole(
      base, match.index, match.index + match[0].length, rawUnit, start, end,
    );
    if (start >= _NUMBER_MAX || end >= _NUMBER_MAX) role = "date";
    found.push({
      index: match.index,
      span: {
        start: Math.max(1, start),
        end,
        unit: _spanUnit(rawUnit, explicitRange),
        explicitRange,
        sourceText: match[0],
        role,
      },
    });
    occupied.push([match.index, match.index + match[0].length]);
  }

  let match;
  const rangeRe = /(?<!\d)(\d+)\s*(화|회|권|장|편|부)?\s*[~\-]\s*(\d+)\s*(화|회|권|장|편|부)?/g;
  while ((match = rangeRe.exec(base)) !== null) {
    if (match.index > 0 && base[match.index - 1] === "%") continue;
    const start = parseInt(match[1], 10);
    const end = parseInt(match[3], 10);
    if (end < start) continue;
    add(match, start, end, match[4] || match[2] || null, true);
  }

  const spaceRangeRe = /(?<!\d)(\d+)\s+(\d+)\s*(화|회|권|장|편)(?!차)/g;
  while ((match = spaceRangeRe.exec(base)) !== null) {
    const matchEnd = match.index + match[0].length;
    if (_overlaps(match.index, matchEnd, occupied)) continue;
    const start = parseInt(match[1], 10);
    const end = parseInt(match[2], 10);
    if (end < 10 || end < start) continue;
    if (/\d/.test(base.slice(matchEnd).replace(/(?:판|현)\d{6}|제\d+판/gi, ""))) continue;
    add(match, start, end, match[3], true);
  }

  // 구분자가 유실된 완료본 범위: `1 125 완`, `001 200 완결`.
  const spaceCompletionRangeRe = /(?<!\d)(\d+)\s+(\d+)\s*(?:완결|完結|완|完|終)/g;
  while ((match = spaceCompletionRangeRe.exec(base)) !== null) {
    const matchEnd = match.index + match[0].length;
    if (_overlaps(match.index, matchEnd, occupied)) continue;
    const start = parseInt(match[1], 10);
    const end = parseInt(match[2], 10);
    if (end < 10 || end < start) continue;
    const prefix = base.slice(Math.max(0, match.index - 12), match.index);
    const sidePrefix = /(?:외전|번외|특외|부외|후일담|에필로그|에필|外傳|外伝|(?:^|[^가-힣A-Za-z])외)[^0-9]{0,8}$/;
    add(match, start, end, null, true, sidePrefix.test(prefix) ? "side" : "main");
  }

  const totalCountRe = /(?<![가-힣A-Za-z0-9])총\s*(\d+)\s*(화|회|장|편)/g;
  while ((match = totalCountRe.exec(base)) !== null) {
    const matchEnd = match.index + match[0].length;
    if (_overlaps(match.index, matchEnd, occupied)) continue;
    const end = parseInt(match[1], 10);
    if (end >= 1 && end < _NUMBER_MAX) add(match, 1, end, match[2], false, "main");
  }

  const unitRe = /(?<![\d.])(\d+)\s*(화|회|권|장|편|부)(?!\s*차|[가-힣A-Za-z])/g;
  while ((match = unitRe.exec(base)) !== null) {
    const matchEnd = match.index + match[0].length;
    if (_overlaps(match.index, matchEnd, occupied)) continue;
    const number = parseInt(match[1], 10);
    add(match, number, number, match[2], false);
  }

  const completionRe = /(?<!\d)(\d+)\s*(?:완결|完結|완|完|終)(?![가-힣A-Za-z])/g;
  while ((match = completionRe.exec(base)) !== null) {
    const matchEnd = match.index + match[0].length;
    if (_overlaps(match.index, matchEnd, occupied)) continue;
    const end = parseInt(match[1], 10);
    if (end >= 10 && !(match.index > 0 && base[match.index - 1] === "%")) {
      const prefix = base.slice(Math.max(0, match.index - 12), match.index);
      const forcedRole = /본편\s*$/.test(prefix) && found.some(({ span }) => span.role === "main")
        ? "metadata"
        : null;
      add(match, 1, end, null, false, forcedRole);
    }
  }

  const hyphenTailRe = /[~\-]\s*(\d+)\s*(?=(?:완결|完結|완|完|終|종|$))/g;
  while ((match = hyphenTailRe.exec(base)) !== null) {
    const matchEnd = match.index + match[0].length;
    if (_overlaps(match.index, matchEnd, occupied)) continue;
    const end = parseInt(match[1], 10);
    if (end >= 10) add(match, 1, end, null, false);
  }

  if (!found.some(({ span }) => ["main", "side", "volume"].includes(span.role))) {
    const bare = [...base.matchAll(/(?<!\d)(\d+)(?!\d)/g)]
      .filter((m) => parseInt(m[1], 10) < _NUMBER_MAX);
    if (bare.length === 1 && !base.slice(bare[0].index + bare[0][0].length).trim()) {
      add(bare[0], 1, parseInt(bare[0][1], 10), null, false);
    }
  }

  return found.sort((a, b) => a.index - b.index).map(({ span }) => span);
}

export function selectMainEpisodeSpan(spans) {
  const main = spans.filter((span) => span.role === "main");
  if (main.length === 1) return main[0];
  if (main.length > 1) return _mergeContiguousMain(main);
  const fallback = spans.filter((span) => span.role === "side" || span.role === "volume");
  return fallback.length === 1 ? fallback[0] : null;
}

function _mergeContiguousMain(main) {
  if (!main.length) return null;
  for (let i = 1; i < main.length; i++) {
    if (main[i - 1].unit !== main[i].unit || main[i].start !== main[i - 1].end + 1) return null;
  }
  return {
    start: main[0].start,
    end: main[main.length - 1].end,
    unit: main[0].unit,
    explicitRange: true,
    sourceText: main.map((span) => span.sourceText).join(" + "),
    role: "main",
  };
}

function _episodeSelection(value) {
  const spans = extractEpisodeSpans(value);
  const main = spans.filter((span) => span.role === "main");
  if (main.length === 1) return { span: main[0], ambiguous: false };
  if (main.length > 1) {
    const merged = _mergeContiguousMain(main);
    return merged ? { span: merged, ambiguous: false } : { span: null, ambiguous: true };
  }
  const fallback = spans.filter((span) => span.role === "side" || span.role === "volume");
  if (fallback.length === 1) return { span: fallback[0], ambiguous: false };
  if (fallback.length > 1) return { span: null, ambiguous: true };
  return { span: null, ambiguous: false };
}

export function extractEpisodeSpan(value) {
  return _episodeSelection(value).span;
}

export function extractStartNumber(value) {
  const { span, ambiguous } = _episodeSelection(value);
  return ambiguous || !span ? null : span.start;
}

export function extractEndNumber(value) {
  const { span, ambiguous } = _episodeSelection(value);
  return ambiguous || !span ? null : span.end;
}

export function extractEffectiveMax(value) {
  const { span, ambiguous } = _episodeSelection(value);
  return ambiguous || !span ? 0 : span.end;
}

export function extractUnit(value) {
  const { span, ambiguous } = _episodeSelection(value);
  return ambiguous || !span ? "미상" : span.unit;
}

export function unitsComparable(unitA, unitB) {
  if (unitA === "미상" || unitB === "미상") return true;
  return unitA === unitB;
}

export function extractVolumeNumber(value) {
  let base = stripLeadingPostStatus(
    _maskTitleLiterals(materializeStructureHints(removeExtension(value))).masked,
  );
  base = base.replace(/\[.*?\]|\(.*?\)|【.*?】|\{.*?\}/g, " ").replace(/@[^\s]+/g, " ");

  const complexPart = /\d+\s*[~\-,]\s*\d+\s*부/.test(base) ||
    /\d+\s*부\s*[~\-,]?\s*\d+\s*부/.test(base);
  const partWorks = base.match(/(?<!\d)(\d+)\s*부작/);
  const partMatches = [...base.matchAll(/(?<![\d~\-,])(\d+)\s*부(?!작)/g)];
  let part = null;
  if (!complexPart && partWorks) part = parseInt(partWorks[1], 10);
  else if (!complexPart && partMatches.length === 1) part = parseInt(partMatches[0][1], 10);

  const number = "\\d+(?:\\.\\d+)?";
  let masked = base.replace(new RegExp(`${number}\\s*권\\s*[~\\-]\\s*${number}\\s*권`, "g"), " ");
  masked = masked.replace(new RegExp(`${number}\\s*[~\\-]\\s*${number}\\s*권`, "g"), " ");
  const volumeMatch = masked.match(new RegExp(`(?<![\\d.])(${number})\\s*권`));
  let volume = null;
  if (volumeMatch) {
    const raw = volumeMatch[1];
    if (raw.includes(".")) {
      const [whole, rawFraction] = raw.split(".");
      const fraction = rawFraction.replace(/0+$/, "");
      volume = fraction ? `${parseInt(whole, 10)}.${fraction}` : parseInt(whole, 10);
    } else {
      volume = parseInt(raw, 10);
    }
  }
  return part === null && volume === null ? null : [part, volume];
}

// === 돋보기 coverage 판정 (서버 중복 시스템과 동일한 보수성) ===
// 편수 비교에 쓰는 "강한 매칭"(서버의 같은 버킷에 해당). 느슨한 매칭은 제외.
export const STRONG_MATCH_TYPES = ["exact", "core"];

// results: makeResult 형태 배열({matchType, effectiveMax, unit, ...})
// query: { effectiveMax, unit }
// 반환: { coverage: 'covered'|'outdated'|'none', ownedMaxNumber, queryMaxNumber, ownedUnit, queryUnit }
export function computeCoverage(results, query) {
  const queryMaxNumber = (query && query.effectiveMax) || 0;
  const queryUnit = (query && query.unit) || "미상";

  if (!results || !results.length) {
    return { coverage: "none", reason: "none", ownedMaxNumber: 0, queryMaxNumber, ownedUnit: null, queryUnit };
  }

  const strongSet = new Set(STRONG_MATCH_TYPES);
  const strong = results.filter((r) => strongSet.has(r.matchType));
  if (!strong.length) {
    // 느슨한 매칭만 있으면 같은 작품 보장이 없으므로 보수적으로 파랑(확인 권장).
    return { coverage: "outdated", reason: "loose_match", ownedMaxNumber: 0, queryMaxNumber, ownedUnit: null, queryUnit };
  }

  let rep = strong[0];
  for (const r of strong) {
    if ((r.effectiveMax || 0) > (rep.effectiveMax || 0)) rep = r;
  }
  const ownedMaxNumber = rep.effectiveMax || 0;
  const ownedUnit = rep.unit || "미상";

  // 녹색은 "확실할 때만". 단위가 양쪽 모두 확정(미상 아님)이고 동일해야 한다.
  // 미상이 끼면 비교 신뢰가 낮으므로 covered로 보지 않는다(애매하면 파랑).
  const unitKnown = ownedUnit !== "미상" && queryUnit !== "미상";
  const unitSame = ownedUnit === queryUnit;
  const bothKnownCounts = ownedMaxNumber > 0 && queryMaxNumber > 0;

  let reason;
  if (!bothKnownCounts) {
    reason = "count_unknown"; // 편수 불명
  } else if (!unitKnown) {
    reason = "unit_unknown"; // 단위 미상(한쪽이라도)
  } else if (!unitSame) {
    reason = "unit_mismatch"; // 권 vs 화 등
  } else if (ownedMaxNumber >= queryMaxNumber) {
    reason = "covered"; // 단위 동일 + 보유 ≥ 대상
  } else {
    reason = "owned_less"; // 보유 < 대상
  }

  return {
    coverage: reason === "covered" ? "covered" : "outdated",
    reason,
    ownedMaxNumber,
    queryMaxNumber,
    ownedUnit,
    queryUnit,
  };
}

export function isSideStory(value) {
  const base = _maskTitleLiterals(materializeStructureHints(removeExtension(value))).masked;
  if (!/외전|外/.test(base)) return false;
  if (/본편|本編/.test(base)) return false;

  if (extractEpisodeSpans(value).some((span) => span.role === "main")) return false;

  return true;
}

export function analyzeTitle(value) {
  const name = normalizeNfc(value);
  const selection = _episodeSelection(name);
  const volumeNumber = extractVolumeNumber(name);
  return {
    name,
    nameKey: normalizeSearchText(removeExtension(name)),
    coreTitle: extractCoreTitle(name),
    readableTitle: extractReadableTitle(name),
    maxNumber: extractMaxNumber(name),
    effectiveMax: selection.ambiguous || !selection.span ? 0 : selection.span.end,
    unit: volumeNumber && volumeNumber[1] !== null
      ? "권"
      : selection.ambiguous || !selection.span ? "미상" : selection.span.unit,
    startNumber: selection.ambiguous || !selection.span ? null : selection.span.start,
    endNumber: selection.ambiguous || !selection.span ? null : selection.span.end,
    spanAmbiguous: selection.ambiguous,
    volumeNumber,
    disambig: readDisambigMarker(name),
    complete: hasCompletionMarker(name),
    isSideStory: isSideStory(name),
    titleLiteralTokens: extractTitleLiteralTokens(name),
    structureHintTokens: extractStructureHintTokens(name),
  };
}
