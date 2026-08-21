import {
  SUPPORTED_EXTENSIONS,
  analyzeTitle,
  extractCoreTitle,
  isSupportedFileName,
  normalizeNfc,
  normalizeSearchText,
  removeExtension,
  safeDecode,
  computeCoverage,
} from "./normalizer.js";

const CONTEXT_SEARCH_MENU_ID = "search-context-title";
const SELECTED_TEXT_COMMAND_ID = "search-selected-text";

function createContextSearchMenu() {
  chrome.contextMenus.remove(CONTEXT_SEARCH_MENU_ID, () => {
    void chrome.runtime.lastError;
    chrome.contextMenus.create({
      id: CONTEXT_SEARCH_MENU_ID,
      title: "이 제목으로 중복 확인",
      contexts: ["page", "selection", "link"],
      documentUrlPatterns: [
        "*://enterjoy.day/*",
        "*://*.enterjoy.day/*",
        "*://tcafe21.com/*",
        "*://*.tcafe21.com/*",
        "*://pastebin.com/*",
        "*://*.pastebin.com/*",
        "*://chating.wiki/*",
        "*://*.chating.wiki/*"
      ],
    }, () => {
      void chrome.runtime.lastError;
    });
  });
}

chrome.runtime.onInstalled.addListener(createContextSearchMenu);
chrome.runtime.onStartup.addListener(createContextSearchMenu);
createContextSearchMenu();

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== CONTEXT_SEARCH_MENU_ID || !tab || tab.id === undefined) return;

  chrome.tabs.sendMessage(tab.id, {
    action: "showContextSearch",
    query: String(info.selectionText || "").trim(),
  }, () => {
    void chrome.runtime.lastError;
  });
});

function showSelectedTextSearch(tab) {
  if (!tab || tab.id === undefined) return;
  chrome.tabs.sendMessage(tab.id, {
    action: "showShortcutSelectionSearch",
  }, () => {
    void chrome.runtime.lastError;
  });
}

chrome.commands.onCommand.addListener((command, tab) => {
  if (command !== SELECTED_TEXT_COMMAND_ID) return;
  if (tab && tab.id !== undefined) {
    showSelectedTextSearch(tab);
    return;
  }

  chrome.tabs.query({ active: true, lastFocusedWindow: true }, (tabs) => {
    showSelectedTextSearch(tabs[0]);
  });
});

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "searchFile") {
    handleSearch(request, sendResponse);
    return true;
  }
  if (request.action === "lookupWebStats") {
    handleWebStats(request, sendResponse);
    return true;
  }
});

const FUZZY_THRESHOLD = 0.82;
const MAX_RESULTS = 50;
const DEFAULT_RESULT_LIMIT = 20;
const CACHE_TTL = 30000;
const QUERY_CACHE_TTL = 30000;
const WEB_STATS_CACHE_TTL = 6 * 60 * 60 * 1000;
const WEB_STATS_FETCH_TIMEOUT = 3500;
const KAKAO_FETCH_TIMEOUT = 12000;
const KAKAO_BFF_ORIGIN = "https://bff-page.kakao.com";
const BUNDLED_INDEX_FILE = "file_index.json";

export function toSearchEntry(raw, source, fallbackPath) {
  if (typeof raw === "string") {
    const analysis = analyzeTitle(raw);
    return {
      source,
      name: raw,
      path: fallbackPath || "로컬 드라이브",
      coreTitle: analysis.coreTitle,
      author: null,
      maxNumber: analysis.maxNumber,
      effectiveMax: analysis.effectiveMax,
      unit: analysis.unit,
      startNumber: analysis.startNumber,
      endNumber: analysis.endNumber,
      spanAmbiguous: analysis.spanAmbiguous,
      complete: analysis.complete,
    };
  }

  const name = normalizeNfc(raw.name || raw.filename || "");
  const needsAnalysis =
    (!raw.core_title && !raw.coreTitle) ||
    (raw.max_number === undefined && raw.maxNumber === undefined) ||
    (raw.effective_max === undefined && raw.effectiveMax === undefined) ||
    (raw.unit === undefined && !raw.unit) ||
    raw.complete === undefined;
  const analysis = needsAnalysis ? analyzeTitle(name) : null;
  const spanAmbiguous = raw.span_ambiguous === true || raw.spanAmbiguous === true ||
    (analysis ? analysis.spanAmbiguous : false);
  const effectiveMax = spanAmbiguous ? 0 : _firstDefined(
    raw.effective_max,
    raw.effectiveMax,
    // 구버전 index에 ambiguous flag와 effective_max가 모두 없을 때만 max_number 호환 폴백.
    raw.span_ambiguous === undefined && raw.spanAmbiguous === undefined
      ? _firstDefined(raw.max_number, raw.maxNumber)
      : undefined,
    analysis ? analysis.effectiveMax : undefined,
    0,
  );
  return {
    source,
    name,
    path: raw.rel_path || raw.path || fallbackPath || "로컬 드라이브",
    coreTitle: normalizeSearchText(raw.core_title || raw.coreTitle || (analysis ? analysis.coreTitle : "")),
    author: raw.author || null,
    maxNumber: raw.max_number !== undefined ? raw.max_number : (raw.maxNumber !== undefined ? raw.maxNumber : (analysis ? analysis.maxNumber : 0)),
    effectiveMax,
    unit: spanAmbiguous ? "미상" : (raw.unit || (analysis ? analysis.unit : "미상")),
    startNumber: spanAmbiguous ? null : _firstDefined(
      raw.start_number, raw.startNumber, analysis ? analysis.startNumber : undefined, null,
    ),
    endNumber: spanAmbiguous ? null : _firstDefined(
      raw.end_number, raw.endNumber, analysis ? analysis.endNumber : undefined, null,
    ),
    spanAmbiguous,
    complete: raw.complete !== undefined ? raw.complete : (analysis ? analysis.complete : false),
  };
}

function _firstDefined(...vals) {
  for (const v of vals) {
    if (v !== undefined && v !== null) return v;
  }
  return undefined;
}

function toPreparedSearchEntry(raw, source, fallbackPath) {
  const entry = toSearchEntry(raw, source, fallbackPath);
  return {
    ...entry,
    nameKey: normalizeSearchText(removeExtension(entry.name)),
    fullKey: normalizeSearchText(entry.name),
    coreKey: entry.coreTitle || extractCoreTitle(entry.name),
  };
}

function canFuzzy(queryValue, targetValue) {
  if (!queryValue || !targetValue) return false;
  if (Math.min(queryValue.length, targetValue.length) <= 3) return false;

  const ratio = Math.min(queryValue.length, targetValue.length) /
    Math.max(queryValue.length, targetValue.length);
  return ratio >= 0.65;
}

function containsEnough(queryValue, targetValue) {
  if (!queryValue || !targetValue) return false;
  if (queryValue === targetValue) return true;

  const shorter = queryValue.length <= targetValue.length ? queryValue : targetValue;
  const longer = queryValue.length > targetValue.length ? queryValue : targetValue;
  if (!longer.includes(shorter)) return false;

  const ratio = shorter.length / longer.length;
  return shorter.length >= 8 && ratio >= 0.72;
}

function makeResult(entry, matchType, score, queryVariant) {
  const labels = {
    exact: "정확 일치",
    core: "제목 일치",
    contains: "포함",
    similar: "유사 후보",
  };
  const ranks = {
    exact: 0,
    core: 1,
    contains: 2,
    similar: 3,
  };

  return {
    source: entry.source,
    name: entry.name,
    path: entry.path,
    score,
    rank: ranks[matchType],
    matchType,
    matchLabel: labels[matchType],
    searchLabel: queryVariant && queryVariant.label ? queryVariant.label : "검색",
    searchText: queryVariant && queryVariant.display ? queryVariant.display : "",
    coreTitle: entry.coreTitle,
    author: entry.author,
    maxNumber: entry.maxNumber,
    effectiveMax: entry.effectiveMax,
    unit: entry.unit,
    complete: entry.complete,
  };
}

function searchDirectEntry(entry, query, queryVariant) {
  if (!entry.name || !query.nameKey) return null;

  const targetNameKey = entry.nameKey;
  const targetFullKey = entry.fullKey;
  const targetCore = entry.coreKey;

  if (targetNameKey === query.nameKey || targetFullKey === query.nameKey) {
    return makeResult(entry, "exact", 1, queryVariant);
  }

  if (targetCore && query.coreTitle && targetCore === query.coreTitle) {
    return makeResult(entry, "core", 0.96, queryVariant);
  }

  return null;
}

function searchSecondaryEntry(entry, query, queryVariant) {
  if (!entry.name || !query.nameKey) return null;

  const targetNameKey = entry.nameKey;
  const targetFullKey = entry.fullKey;
  const targetCore = entry.coreKey;

  const included =
    containsEnough(query.nameKey, targetNameKey) ||
    (targetFullKey !== targetNameKey && containsEnough(query.nameKey, targetFullKey)) ||
    (targetCore && query.coreTitle && containsEnough(query.coreTitle, targetCore));
  if (included) {
    return makeResult(entry, "contains", 0.82, queryVariant);
  }

  let simScore = 0;
  if (canFuzzy(query.coreTitle, targetCore)) {
    simScore = Math.max(simScore, getSimilarity(query.coreTitle, targetCore));
  }
  if (canFuzzy(query.nameKey, targetNameKey)) {
    simScore = Math.max(simScore, getSimilarity(query.nameKey, targetNameKey));
  }

  if (simScore >= FUZZY_THRESHOLD) {
    return makeResult(entry, "similar", simScore, queryVariant);
  }

  return null;
}

function searchEntry(entry, query, queryVariant) {
  return searchDirectEntry(entry, query, queryVariant) ||
    searchSecondaryEntry(entry, query, queryVariant);
}

function addResult(resultMap, result) {
  if (!result) return;
  const key = `${result.source}|${result.path}|${result.name}`;
  const existing = resultMap.get(key);
  if (!existing) {
    resultMap.set(key, result);
    return;
  }
  if (result.rank < existing.rank || (result.rank === existing.rank && result.score > existing.score)) {
    resultMap.set(key, result);
  }
}

function buildQueryVariants(rawQuery) {
  const analysis = analyzeTitle(rawQuery);
  const variants = [];

  if (analysis.nameKey) {
    variants.push({
      label: "원제목",
      display: analysis.name,
      query: {
        nameKey: analysis.nameKey,
        coreTitle: "",
      },
    });
  }

  if (analysis.coreTitle) {
    variants.push({
      label: "추출 제목",
      display: analysis.readableTitle || analysis.coreTitle,
      query: {
        nameKey: analysis.coreTitle,
        coreTitle: analysis.coreTitle,
      },
    });
  }

  return {
    original: analysis.name,
    extracted: analysis.readableTitle || analysis.coreTitle,
    effectiveMax: analysis.effectiveMax,
    unit: analysis.unit,
    variants,
  };
}

let cache = {
  searchIndex: null,
  timestamp: 0,
  loadingPromise: null,
  queryResults: new Map(),
};

export function parseLocalIndexPayload(payload) {
  if (Array.isArray(payload)) {
    return {
      meta: { version: 1 },
      entries: payload
        .filter((name) => typeof name === "string" && name.trim())
        .map((name) => ({ name, path: "로컬 드라이브" })),
    };
  }

  if (payload && payload.version === 2 && Array.isArray(payload.entries)) {
    return {
      meta: {
        version: 2,
        normalizer_version: payload.normalizer_version || null,
        generated_at: payload.generated_at || null,
      },
      entries: payload.entries
        .filter((entry) => entry && typeof entry.name === "string" && entry.name.trim())
        .map((entry) => ({
          type: entry.type || "file",
          name: entry.name,
          rel_path: entry.rel_path || "",
          ext: entry.ext || "",
          size: entry.size !== undefined ? entry.size : null,
          core_title: entry.core_title || "",
          author: entry.author || null,
          max_number: entry.max_number !== undefined ? entry.max_number : 0,
          effective_max: entry.effective_max !== undefined ? entry.effective_max : null,
          unit: entry.unit || null,
          start_number: entry.start_number !== undefined ? entry.start_number : null,
          end_number: entry.end_number !== undefined ? entry.end_number : null,
          span_ambiguous: entry.span_ambiguous === undefined ? undefined : Boolean(entry.span_ambiguous),
          complete: Boolean(entry.complete),
        })),
    };
  }

  throw new Error("Unsupported index format");
}

function getIndexMetaKey(meta) {
  if (!meta) return "";
  return [
    meta.version === undefined ? "" : String(meta.version),
    meta.normalizer_version || "",
    meta.generated_at || "",
  ].join("|");
}

function storageGet(keys) {
  return new Promise((resolve) => {
    chrome.storage.local.get(keys, (data) => resolve(data || {}));
  });
}

function storageSet(values) {
  return new Promise((resolve) => {
    chrome.storage.local.set(values, () => {
      resolve(!chrome.runtime.lastError);
    });
  });
}

function downloadsSearch(query) {
  return new Promise((resolve) => {
    chrome.downloads.search(query, (items) => {
      if (chrome.runtime.lastError) {
        resolve([]);
        return;
      }
      resolve(items || []);
    });
  });
}

async function loadBundledIndexIntoStorage(storedData) {
  try {
    const response = await fetch(chrome.runtime.getURL(BUNDLED_INDEX_FILE), { cache: "no-store" });
    if (!response.ok) return storedData;

    const parsed = parseLocalIndexPayload(await response.json());
    const storedMeta = storedData.localFileIndexMeta || null;
    const sameIndex =
      getIndexMetaKey(parsed.meta) === getIndexMetaKey(storedMeta) &&
      Array.isArray(storedData.localFileEntries) &&
      storedData.localFileEntries.length === parsed.entries.length;

    if (sameIndex) return storedData;

    const updatedData = {
      localFileEntries: parsed.entries,
      localFileList: parsed.entries.map((entry) => entry.name),
      localFileIndexMeta: parsed.meta,
    };

    const saved = await storageSet(updatedData);
    return saved ? updatedData : storedData;
  } catch (error) {
    return storedData;
  }
}

function addToValueMap(map, key, entry) {
  if (!key) return;
  const entries = map.get(key);
  if (entries) entries.push(entry);
  else map.set(key, [entry]);
}

function getUniqueTrigrams(value) {
  if (!value || value.length < 3) return [];
  const seen = new Set();
  for (let i = 0; i <= value.length - 3; i++) {
    seen.add(value.slice(i, i + 3));
  }
  return [...seen];
}

function addToTrigramMap(map, key, entry) {
  getUniqueTrigrams(key).forEach((trigram) => {
    const entries = map.get(trigram);
    if (entries) entries.push(entry);
    else map.set(trigram, [entry]);
  });
}

function createSearchIndex(searchEntries) {
  const index = {
    entries: searchEntries,
    byNameKey: new Map(),
    byFullKey: new Map(),
    byCoreKey: new Map(),
    nameKeyTrigrams: new Map(),
    fullKeyTrigrams: new Map(),
    coreKeyTrigrams: new Map(),
  };

  searchEntries.forEach((entry) => {
    addToValueMap(index.byNameKey, entry.nameKey, entry);
    addToValueMap(index.byFullKey, entry.fullKey, entry);
    addToValueMap(index.byCoreKey, entry.coreKey, entry);
    addToTrigramMap(index.nameKeyTrigrams, entry.nameKey, entry);
    addToTrigramMap(index.fullKeyTrigrams, entry.fullKey, entry);
    addToTrigramMap(index.coreKeyTrigrams, entry.coreKey, entry);
  });

  return index;
}

function buildSearchIndex(downloads, localEntries) {
  const searchEntries = [];

  downloads.forEach((item) => {
    const fileName = String(item.filename || "").split(/[\\/]/).pop();
    if (!isSupportedFileName(fileName)) return;
    searchEntries.push(toPreparedSearchEntry(
      { name: fileName, path: item.filename },
      "🌐 다운로드 기록",
      item.filename
    ));
  });

  localEntries.forEach((rawEntry) => {
    searchEntries.push(toPreparedSearchEntry(rawEntry, "💻 내 PC", "로컬 드라이브"));
  });

  return createSearchIndex(searchEntries);
}

function resetSearchCache() {
  cache.searchIndex = null;
  cache.timestamp = 0;
  cache.loadingPromise = null;
  cache.queryResults.clear();
}

if (chrome.storage && chrome.storage.onChanged) {
  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (
      areaName === "local" &&
      (changes.localFileEntries || changes.localFileList || changes.localFileIndexMeta)
    ) {
      resetSearchCache();
    }
  });
}

function getSearchData() {
  const now = Date.now();
  if (cache.searchIndex && (now - cache.timestamp < CACHE_TTL)) {
    return Promise.resolve({ searchIndex: cache.searchIndex });
  }
  if (cache.loadingPromise) {
    return cache.loadingPromise;
  }

  cache.loadingPromise = (async () => {
    const [downloadItems, storedData] = await Promise.all([
      downloadsSearch({ state: "complete", exists: true }),
      storageGet(["localFileEntries", "localFileList", "localFileIndexMeta"]),
    ]);
    const data = await loadBundledIndexIntoStorage(storedData);
    const localEntries = Array.isArray(data.localFileEntries) && data.localFileEntries.length > 0
      ? data.localFileEntries
      : (data.localFileList || []);
    cache.searchIndex = buildSearchIndex(downloadItems, localEntries);
    cache.timestamp = Date.now();
    cache.loadingPromise = null;
    cache.queryResults.clear();
    return { searchIndex: cache.searchIndex };
  })();

  return cache.loadingPromise;
}

function parseResultLimit(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) return DEFAULT_RESULT_LIMIT;
  return Math.min(Math.floor(parsed), MAX_RESULTS);
}

function makeResponse(queryInfo, results, limit, coverageInfo) {
  return {
    query: {
      original: queryInfo.original,
      extracted: queryInfo.extracted,
    },
    coverage: coverageInfo ? coverageInfo.coverage : "none",
    coverageReason: coverageInfo ? coverageInfo.reason : "none",
    ownedMaxNumber: coverageInfo ? coverageInfo.ownedMaxNumber : 0,
    queryMaxNumber: coverageInfo ? coverageInfo.queryMaxNumber : 0,
    ownedUnit: coverageInfo ? coverageInfo.ownedUnit : null,
    queryUnit: coverageInfo ? coverageInfo.queryUnit : null,
    results: results.slice(0, limit),
  };
}

// computeCoverage는 normalizer.js에서 import한다(순수 함수, 테스트 용이).

function addEntriesToSet(candidateSet, entries) {
  if (!entries) return;
  entries.forEach((entry) => candidateSet.add(entry));
}

function addSubstringMapCandidates(candidateSet, map, queryValue) {
  if (!queryValue || queryValue.length < 8) return;
  const minLength = Math.max(8, Math.ceil(queryValue.length * 0.72));
  const seen = new Set();

  for (let length = minLength; length <= queryValue.length; length++) {
    for (let start = 0; start <= queryValue.length - length; start++) {
      const part = queryValue.slice(start, start + length);
      if (seen.has(part)) continue;
      seen.add(part);
      addEntriesToSet(candidateSet, map.get(part));
    }
  }
}

function addTrigramCandidates(candidateSet, map, queryValue) {
  getUniqueTrigrams(queryValue).forEach((trigram) => {
    addEntriesToSet(candidateSet, map.get(trigram));
  });
}

function collectDirectCandidates(searchIndex, variant) {
  const candidateSet = new Set();
  const query = variant.query;

  addEntriesToSet(candidateSet, searchIndex.byNameKey.get(query.nameKey));
  addEntriesToSet(candidateSet, searchIndex.byFullKey.get(query.nameKey));
  if (query.coreTitle) {
    addEntriesToSet(candidateSet, searchIndex.byCoreKey.get(query.coreTitle));
  }

  return candidateSet;
}

function collectSecondaryCandidates(searchIndex, variant) {
  const candidateSet = new Set();
  const query = variant.query;

  addSubstringMapCandidates(candidateSet, searchIndex.byNameKey, query.nameKey);
  addSubstringMapCandidates(candidateSet, searchIndex.byFullKey, query.nameKey);
  addTrigramCandidates(candidateSet, searchIndex.nameKeyTrigrams, query.nameKey);
  addTrigramCandidates(candidateSet, searchIndex.fullKeyTrigrams, query.nameKey);

  if (query.coreTitle) {
    addSubstringMapCandidates(candidateSet, searchIndex.byCoreKey, query.coreTitle);
    addTrigramCandidates(candidateSet, searchIndex.coreKeyTrigrams, query.coreTitle);
  }

  return candidateSet;
}

function runIndexedSearch(searchIndex, queryInfo) {
  const resultMap = new Map();
  const variants = queryInfo.variants;

  // 후보집합을 직접 순회한다(전체 entries 완주 제거). 후보집합은 trigram/substring
  // 인덱스로 좁혀진 부분집합이라 비용이 인덱스 크기가 아니라 후보 수에 비례한다.
  // direct를 먼저 전부 처리해 더 높은 rank(낮은 숫자)를 선점하게 한 뒤 secondary를 본다.
  variants.forEach((variant) => {
    for (const entry of collectDirectCandidates(searchIndex, variant)) {
      addResult(resultMap, searchDirectEntry(entry, variant.query, variant));
    }
  });

  variants.forEach((variant) => {
    for (const entry of collectSecondaryCandidates(searchIndex, variant)) {
      addResult(resultMap, searchSecondaryEntry(entry, variant.query, variant));
    }
  });

  return [...resultMap.values()];
}

async function handleSearch(request, sendResponse) {
  const rawQuery = request.query || "";
  const limit = parseResultLimit(request.limit);
  const queryInfo = buildQueryVariants(rawQuery);
  const queryKey = `${normalizeSearchText(rawQuery)}|${queryInfo.extracted || ""}`;

  const { searchIndex } = await getSearchData();
  const cached = cache.queryResults.get(queryKey);
  if (cached && Date.now() - cached.timestamp < QUERY_CACHE_TTL) {
    // 캐시된 coverageInfo(전체 결과 기준)를 재사용. 없으면(구캐시) 보수적으로 재계산.
    const coverageInfo = cached.coverageInfo || computeCoverage(cached.results, queryInfo);
    sendResponse(makeResponse(queryInfo, cached.results, limit, coverageInfo));
    return;
  }

  const results = runIndexedSearch(searchIndex, queryInfo);
  results.sort((a, b) => a.rank - b.rank || b.score - a.score || a.name.localeCompare(b.name));
  // coverage는 전체 결과 기준으로 계산해야 한다(슬라이스 이후면 51번째 이후의 더 완전한
  // 보유본을 놓쳐 파랑으로 오판할 수 있다). 표시만 MAX_RESULTS로 자른다.
  const coverageInfo = computeCoverage(results, queryInfo);
  const limitedResults = results.slice(0, MAX_RESULTS);
  cache.queryResults.set(queryKey, {
    timestamp: Date.now(),
    results: limitedResults,
    coverageInfo,
  });
  sendResponse(makeResponse(queryInfo, limitedResults, limit, coverageInfo));
}

const webStatsCache = new Map();

function stripTags(value) {
  return decodeHtml(String(value || "")
    .replace(/<script\b[\s\S]*?<\/script>/gi, " ")
    .replace(/<style\b[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " "))
    .replace(/\s+/g, " ")
    .trim();
}

function decodeHtml(value) {
  return String(value || "")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#039;/g, "'")
    .replace(/&#x([0-9a-f]+);/gi, (match, hex) => String.fromCodePoint(parseInt(hex, 16)))
    .replace(/&#(\d+);/g, (match, code) => String.fromCodePoint(parseInt(code, 10)));
}

function extractSimpleLookupTitle(value) {
  let base = safeDecode(removeExtension(normalizeNfc(value)));
  base = base.replace(/\[.*?\]|\(.*?\)|【.*?】|\{.*?\}/g, " ");
  base = base.replace(/^\s*(?:19\s*(?:禁|금|N|n)\s*)?(?:(?:완결|완|完)\s*)?[\)\]\}〉》:：,.\-_/\\]+\s*/i, " ");

  const cutPattern = new RegExp(
    [
      "\\d+\\s*권\\s*[~\\-]\\s*\\d+\\s*권",
      "\\d+\\s*(?:화|권|부|회|장|편)\\s*[~\\-]\\s*\\d+\\s*(?:화|권|부|회|장|편)?",
      "\\d+\\s*[~\\-]\\s*\\d+",
      "\\d+\\s*(?:화|권|부|장|편)",
      "(^|[^가-힣A-Za-z])(?:완결|完結|완|完|終|종)(?=$|[^가-힣A-Za-z])",
      "\\d+\\s*(?:완결|完結|완|完|終)",
      "본편|本編|외전|外傳|外伝|(^|[^가-힣A-Za-z])外(?=$|[^가-힣A-Za-z])",
    ].join("|"),
  );
  const cutMatch = base.match(cutPattern);
  if (cutMatch && cutMatch.index !== undefined) {
    let cutAt = cutMatch.index;
    if (cutMatch[1] !== undefined && cutMatch[1] !== "") cutAt += cutMatch[1].length;
    base = base.slice(0, cutAt);
  }

  base = base
    .replace(/@[^\s]+/g, " ")
    .replace(/\b(?:txt|epub|pdf)\b/gi, " ")
    .replace(/^[^a-zA-Z0-9가-힣]+/g, " ")
    .replace(/[^a-zA-Z0-9가-힣]+$/g, " ")
    .replace(/\s+/g, " ")
    .trim();

  return normalizeSearchText(base).length >= 2 ? base : "";
}

function buildWebStatsQueryInfo(rawQuery) {
  const analysis = analyzeTitle(rawQuery);
  const extracted = (analysis.readableTitle || "").trim();
  const fallback = extractSimpleLookupTitle(analysis.name);
  const title = extracted || fallback || analysis.name.trim();

  return {
    original: analysis.name,
    title,
    extracted,
    fallbackUsed: !extracted && Boolean(fallback),
  };
}

async function handleWebStats(request, sendResponse) {
  const queryInfo = buildWebStatsQueryInfo(request.query || "");
  const queryKey = normalizeSearchText(queryInfo.title);

  if (!queryKey) {
    sendResponse({
      query: queryInfo,
      results: [
        makeWebStatResult("네이버", "skipped", { message: "검색 제목 없음" }),
        makeWebStatResult("카카오", "skipped", { message: "검색 제목 없음" }),
        makeWebStatResult("노벨피아", "skipped", { message: "검색 제목 없음" }),
      ],
    });
    return;
  }

  const results = await getWebStatsResults(queryKey, queryInfo.title);
  sendResponse({
    query: queryInfo,
    results,
  });
}

async function getWebStatsResults(queryKey, title) {
  const now = Date.now();
  const cached = webStatsCache.get(queryKey);

  if (cached && cached.results && now - cached.timestamp < WEB_STATS_CACHE_TTL) {
    return cached.results;
  }
  if (cached && cached.promise && now - cached.timestamp < WEB_STATS_CACHE_TTL) {
    return cached.promise;
  }

  const promise = runWebStatsLookups(title);
  webStatsCache.set(queryKey, { timestamp: now, promise });

  try {
    const results = await promise;
    webStatsCache.set(queryKey, { timestamp: Date.now(), results });
    return results;
  } catch (error) {
    webStatsCache.delete(queryKey);
    return [
      makeWebStatResult("네이버", "error", { message: "조회 실패" }),
      makeWebStatResult("카카오", "error", { message: "조회 실패" }),
      makeWebStatResult("노벨피아", "error", { message: "조회 실패" }),
    ];
  }
}

function makeWebStatResult(platform, status, details = {}) {
  return {
    platform,
    status,
    message: details.message || "",
    url: details.url || "",
    title: details.title || "",
    metrics: Array.isArray(details.metrics) ? details.metrics.filter((item) => item && item.value) : [],
  };
}

async function runWebStatsLookups(title) {
  return Promise.all([
    runPlatformLookup("네이버", (signal) => lookupNaverStats(title, signal)),
    runPlatformLookup("카카오", (signal) => lookupKakaoStats(title, signal), KAKAO_FETCH_TIMEOUT),
    runPlatformLookup("노벨피아", (signal) => lookupNovelpiaStats(title, signal)),
  ]);
}

async function runPlatformLookup(platform, lookup, timeoutMs = WEB_STATS_FETCH_TIMEOUT) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const result = await lookup(controller.signal);
    return {
      ...result,
      platform,
    };
  } catch (error) {
    const status = error && error.name === "AbortError" ? "timeout" : "error";
    return makeWebStatResult(platform, status, {
      message: status === "timeout" ? "시간 초과" : "조회 실패",
    });
  } finally {
    clearTimeout(timeoutId);
  }
}

async function fetchText(url, signal, accept = "text/html,application/xhtml+xml,*/*") {
  const response = await fetch(url, {
    cache: "no-store",
    credentials: "include",
    signal,
    headers: {
      Accept: accept,
    },
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }

  return response.text();
}

async function fetchJson(url, signal) {
  const text = await fetchText(url, signal, "application/json,text/plain,*/*");
  return JSON.parse(text);
}

function titleLooksSame(requestedTitle, candidateTitle) {
  const requestedKey = normalizeSearchText(requestedTitle);
  const candidateKey = normalizeSearchText(candidateTitle);
  return requestedKey && candidateKey &&
    (requestedKey === candidateKey || containsEnough(requestedKey, candidateKey));
}

function formatMetric(label, value) {
  const cleaned = String(value === null || value === undefined ? "" : value).trim();
  return cleaned ? { label, value: cleaned } : null;
}

function parseNumberValue(value) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value !== "string") return null;
  const cleaned = value.replace(/,/g, "").match(/\d+(?:\.\d+)?/);
  if (!cleaned) return null;
  const parsed = Number(cleaned[0]);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatCompactCount(value) {
  const raw = String(value === null || value === undefined ? "" : value).trim();
  if (!raw) return "";
  if (/[만천]/.test(raw)) return raw;

  const numberValue = parseNumberValue(value);
  if (numberValue === null) return raw;
  if (numberValue >= 10000) {
    const compact = Math.round((numberValue / 10000) * 10) / 10;
    return `${compact.toLocaleString("ko-KR", { maximumFractionDigits: 1 })}만`;
  }
  return numberValue.toLocaleString("ko-KR");
}

function formatPlainCount(value) {
  const raw = String(value === null || value === undefined ? "" : value).trim();
  if (!raw) return "";
  const numberValue = parseNumberValue(value);
  return numberValue === null ? raw : numberValue.toLocaleString("ko-KR");
}

function formatRating(value) {
  const numberValue = parseNumberValue(value);
  if (numberValue === null) return "";
  return (Math.round(numberValue * 10) / 10).toLocaleString("ko-KR", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 1,
  });
}

function getFirstValue(record, keys) {
  if (!record || typeof record !== "object") return "";
  for (const key of keys) {
    if (record[key] !== undefined && record[key] !== null && String(record[key]).trim() !== "") {
      return record[key];
    }
  }
  return "";
}

async function lookupNaverStats(title, signal) {
  const searchUrl = `https://series.naver.com/search/search.series?t=all&fs=novel&q=${encodeURIComponent(title)}`;
  const searchHtml = await fetchText(searchUrl, signal);
  const candidates = parseNaverSearchCandidates(searchHtml);
  if (!candidates.length) {
    return makeWebStatResult("네이버", "not_found", { message: "결과 없음" });
  }

  const candidate = candidates.find((item) => titleLooksSame(title, item.title)) || candidates[0];
  const detailUrl = `https://series.naver.com/novel/detail.series?productNo=${encodeURIComponent(candidate.productNo)}`;
  const detailHtml = await fetchText(detailUrl, signal);
  const detailTitle = parseNaverDetailTitle(detailHtml) || candidate.title;
  if (detailTitle && !titleLooksSame(title, detailTitle)) {
    return makeWebStatResult("네이버", "not_found", { message: "결과 없음" });
  }

  const metrics = parseNaverMetrics(detailHtml, candidate.score);
  return makeWebStatResult("네이버", "ok", {
    url: detailUrl,
    title: detailTitle,
    metrics,
  });
}

function parseNaverSearchCandidates(html) {
  const candidates = [];
  const seen = new Set();
  const itemRegex = /<li\b[\s\S]*?<\/li>/gi;
  let itemMatch;

  while ((itemMatch = itemRegex.exec(html)) !== null) {
    const itemHtml = itemMatch[0];
    const productMatch = itemHtml.match(/\/novel\/detail\.series\?productNo=(\d+)/i);
    if (!productMatch || seen.has(productMatch[1])) continue;

    const titleMatch = itemHtml.match(/<a[^>]+class="[^"]*N=a:nov\.title[^"]*"[^>]*>([\s\S]*?)<\/a>/i) ||
      itemHtml.match(/<h3[^>]*>[\s\S]*?<a[^>]+href="[^"]*\/novel\/detail\.series\?productNo=\d+[^"]*"[^>]*>([\s\S]*?)<\/a>[\s\S]*?<\/h3>/i) ||
      itemHtml.match(/<strong[^>]*>([\s\S]*?)<\/strong>/i) ||
      itemHtml.match(/<img[^>]+alt="([^"]+)"/i) ||
      itemHtml.match(/<a[^>]+href="[^"]*\/novel\/detail\.series\?productNo=\d+[^"]*"[^>]*>([\s\S]*?)<\/a>/i);
    const scoreMatch = itemHtml.match(/class="[^"]*score_num[^"]*"[^>]*>\s*([0-9.]+)/i);

    seen.add(productMatch[1]);
    candidates.push({
      productNo: productMatch[1],
      title: titleMatch ? stripTags(titleMatch[1]) : "",
      score: scoreMatch ? scoreMatch[1] : "",
    });
  }

  if (candidates.length) return candidates;

  const anchorRegex = /<a[^>]+href="[^"]*\/novel\/detail\.series\?productNo=(\d+)[^"]*"[^>]*>([\s\S]*?)<\/a>/gi;
  let anchorMatch;
  while ((anchorMatch = anchorRegex.exec(html)) !== null) {
    if (seen.has(anchorMatch[1])) continue;
    seen.add(anchorMatch[1]);
    candidates.push({
      productNo: anchorMatch[1],
      title: stripTags(anchorMatch[2]),
      score: "",
    });
  }

  return candidates;
}

function parseNaverDetailTitle(html) {
  const metaTitleMatch = html.match(/<meta[^>]+property=["']og:title["'][^>]+content=["']([^"']+)["']/i) ||
    html.match(/<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:title["']/i);
  if (metaTitleMatch) return stripTags(metaTitleMatch[1]);

  const documentTitleMatch = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
  if (documentTitleMatch) return stripTags(documentTitleMatch[1]).replace(/\s*:\s*네이버시리즈\s*$/, "").trim();

  const h2Matches = [...html.matchAll(/<h2[^>]*>([\s\S]*?)<\/h2>/gi)]
    .map((match) => stripTags(match[1]))
    .filter((title) => title && !/local navigation/i.test(title));
  return h2Matches[0] || "";
}

function parseNaverMetrics(html, fallbackScore) {
  const scoreMatch = html.match(/class="[^"]*score_area[^"]*"[\s\S]*?<em[^>]*>\s*([0-9.]+)\s*<\/em>/i);
  const interestMatch = html.match(/class="[^"]*btn_download[^"]*"[\s\S]*?<span[^>]*>([\s\S]*?)<\/span>/i);

  return [
    formatMetric("관심", interestMatch ? stripTags(interestMatch[1]) : ""),
    formatMetric("별점", formatRating(scoreMatch ? scoreMatch[1] : fallbackScore)),
  ].filter(Boolean);
}

async function lookupKakaoStats(title, signal) {
  const apiCandidates = await lookupKakaoApiCandidates(title, signal);
  for (const candidate of apiCandidates.slice(0, 3)) {
    if (!candidate.contentId || !titleLooksSame(title, candidate.title)) continue;

    const detailUrl = `https://page.kakao.com/content/${encodeURIComponent(candidate.contentId)}`;
    const detail = await lookupKakaoOverview(candidate.contentId, signal);
    if (detail.title && titleLooksSame(title, detail.title)) {
      return makeWebStatResult("카카오", "ok", {
        url: detailUrl,
        title: detail.title,
        metrics: detail.metrics.length ? detail.metrics : candidate.metrics,
      });
    }
  }
  return makeWebStatResult("카카오", "not_found", { message: "결과 없음" });
}

async function lookupKakaoApiCandidates(title, signal) {
  const params = new URLSearchParams({
    keyword: title,
    category_uid: "0",
    is_complete: "false",
    sort_type: "ACCURACY",
    page: "0",
    size: "5",
  });
  const data = await fetchJson(`${KAKAO_BFF_ORIGIN}/api/gateway/api/v2/search/series?${params.toString()}`, signal);
  return parseKakaoSearchResponse(data);
}

export function parseKakaoSearchResponse(data) {
  const list = data && data.result && Array.isArray(data.result.list) ? data.result.list : [];

  return list
    .map((item) => {
      const contentId = getFirstValue(item, ["series_id", "seriesId", "id"]);
      const serviceProperty = item.service_property || item.serviceProperty || {};
      return {
        contentId,
        title: getFirstValue(item, ["title", "name"]),
        metrics: [
          formatMetric("열람자", formatCompactCount(getFirstValue(serviceProperty, ["view_count", "viewCount"]))),
        ].filter(Boolean),
      };
    })
    .filter((item) => item.contentId && item.title);
}

async function lookupKakaoOverview(contentId, signal) {
  const params = new URLSearchParams({ series_id: String(contentId) });
  const data = await fetchJson(
    `${KAKAO_BFF_ORIGIN}/api/gateway/api/v1/content/overview?${params.toString()}`,
    signal,
  );
  return parseKakaoOverview(data);
}

export function parseKakaoOverview(data) {
  const content = data && data.result && data.result.content;
  if (!content) return { title: "", metrics: [] };

  const serviceProperty = content.service_property || content.serviceProperty || {};
  const viewCount = getFirstValue(serviceProperty, ["viewCount", "view_count", "readCount", "read_count"]);
  const ratingAverage = getFirstValue(serviceProperty, ["ratingAverage", "ratingAvg", "rating"]);
  const ratingCount = parseNumberValue(getFirstValue(serviceProperty, ["ratingCount", "rating_count"]));
  const ratingSum = parseNumberValue(getFirstValue(serviceProperty, ["ratingSum", "rating_sum"]));
  const computedRating = ratingAverage || (ratingCount && ratingSum ? ratingSum / ratingCount : "");

  return {
    title: getFirstValue(content, ["title", "name", "seoTitle"]),
    metrics: [
      formatMetric("열람자", formatCompactCount(viewCount)),
      formatMetric("별점", formatRating(computedRating)),
    ].filter(Boolean),
  };
}

async function lookupNovelpiaStats(title, signal) {
  const params = new URLSearchParams({
    cmd: "novel_search",
    page: "1",
    rows: "30",
    search_type: "novel_name",
    search_val: title,
    novel_type: "",
    start_count_book: "",
    end_count_book: "",
    novel_age: "",
    start_days: "",
    sort_col: "last_viewdate",
    novel_genre: "",
    block_out: "0",
    block_stop: "0",
    is_contest: "0",
    is_complete: "",
    is_challenge: "",
    list_display: "list",
  });
  const data = await fetchJson(`https://novelpia.com/proc/novel?${params.toString()}`, signal);
  const candidates = collectObjectArrays(data)
    .flat()
    .filter((item) => getNovelpiaTitle(item));
  const candidate = candidates.find((item) => titleLooksSame(title, getNovelpiaTitle(item)));

  if (!candidate) {
    return makeWebStatResult("노벨피아", "not_found", { message: "결과 없음" });
  }

  const novelNo = getFirstValue(candidate, ["novel_no", "novelNo", "novel_id", "novelId", "id"]);
  const detailUrl = novelNo ? `https://novelpia.com/novel/${encodeURIComponent(novelNo)}` : "";
  const apiMetrics = parseNovelpiaMetrics(candidate);

  if (apiMetrics.length || !detailUrl) {
    return makeWebStatResult("노벨피아", "ok", {
      url: detailUrl,
      title: getNovelpiaTitle(candidate),
      metrics: apiMetrics,
    });
  }

  const detailHtml = await fetchText(detailUrl, signal);
  const detail = parseNovelpiaDetail(detailHtml);
  return makeWebStatResult("노벨피아", "ok", {
    url: detailUrl,
    title: detail.title || getNovelpiaTitle(candidate),
    metrics: detail.metrics,
  });
}

function collectObjectArrays(root) {
  const arrays = [];
  const stack = [root];
  const seen = new Set();

  while (stack.length) {
    const node = stack.pop();
    if (!node || typeof node !== "object" || seen.has(node)) continue;
    seen.add(node);

    if (Array.isArray(node)) {
      if (node.some((item) => item && typeof item === "object" && !Array.isArray(item))) {
        arrays.push(node);
      }
      node.forEach((item) => stack.push(item));
      continue;
    }

    Object.values(node).forEach((value) => {
      if (value && typeof value === "object") stack.push(value);
    });
  }

  return arrays;
}

function getNovelpiaTitle(record) {
  return getFirstValue(record, ["novel_name", "novelName", "title", "name", "subject"]);
}

function parseNovelpiaMetrics(record) {
  return [
    formatMetric("조회", formatPlainCount(getFirstValue(record, ["count_view", "view_count", "viewCount", "hit", "hits"]))),
    formatMetric("추천", formatPlainCount(getFirstValue(record, ["count_good", "good_count", "goodCount", "recommend", "recommend_count"]))),
  ].filter(Boolean);
}

function parseNovelpiaDetail(html) {
  const plain = stripTags(html);
  const titleMatch = html.match(/class=["'][^"']*epnew-novel-title[^"']*["'][^>]*>([\s\S]*?)<\/div>/i) ||
    html.match(/class=["'][^"']*share-nov-tit[^"']*["'][^>]*>([\s\S]*?)<\/span>/i) ||
    html.match(/<meta[^>]+property=["']og:title["'][^>]+content=["']([^"']+)["']/i) ||
    html.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
  const viewMatch = plain.match(/조회\s*([0-9,.]+(?:\s*[만천])?)/);
  const recommendMatch = plain.match(/추천\s*([0-9,.]+(?:\s*[만천])?)/);

  return {
    title: titleMatch ? stripTags(titleMatch[1]).replace(/^노벨피아\s*-\s*웹소설로 꿈꾸는 세상!\s*-\s*/, "").trim() : "",
    metrics: [
      formatMetric("조회", formatPlainCount(viewMatch ? viewMatch[1] : "")),
      formatMetric("추천", formatPlainCount(recommendMatch ? recommendMatch[1] : "")),
    ].filter(Boolean),
  };
}

function getSimilarity(s1, s2) {
  const longer = s1.length > s2.length ? s1 : s2;
  const shorter = s1.length > s2.length ? s2 : s1;
  const longerLength = longer.length;

  if (longerLength === 0) return 1.0;
  return (longerLength - editDistance(longer, shorter)) / parseFloat(longerLength);
}

function editDistance(s1, s2) {
  s1 = s1.toLowerCase();
  s2 = s2.toLowerCase();

  const costs = [];
  for (let i = 0; i <= s1.length; i++) {
    let lastValue = i;
    for (let j = 0; j <= s2.length; j++) {
      if (i === 0) costs[j] = j;
      else if (j > 0) {
        let newValue = costs[j - 1];
        if (s1.charAt(i - 1) !== s2.charAt(j - 1)) {
          newValue = Math.min(Math.min(newValue, lastValue), costs[j]) + 1;
        }
        costs[j - 1] = lastValue;
        lastValue = newValue;
      }
    }
    if (i > 0) costs[s2.length] = lastValue;
  }
  return costs[s2.length];
}
