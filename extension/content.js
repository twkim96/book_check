// ============================================================
// [수정됨] 여러 사이트 지원을 위한 셀렉터 목록
// EnterJoy: .item-subject, 게시글 상세 제목
// Tcafe21: .td_subject a, .list-subject a 등
// Chating Wiki: 자료함 카드와 게시판 행의 제목 strong
const SELECTORS = [
  ".item-subject",
  "#at-main > div.view-wrap > section > article > h1",
  ".td_subject a",
  ".list-subject a",
  ".wr-subject a",
  ".group-material-copy > strong",
  ".cw-board-item__title > strong"
];
const TARGET_SELECTOR = SELECTORS.join(", ");
const CONTEXT_TARGET_SELECTORS = [
  ...SELECTORS,
  ".at-content > .view-wrap > h1",
  ".highlighted-code .source li > div"
];
const CONTEXT_TARGET_SELECTOR = CONTEXT_TARGET_SELECTORS.join(", ");
const TOOLTIP_RESULT_LIMIT = 20;
const searchCache = new Map();
const webStatsCache = new Map();
let searchCacheVersion = 0;
let sharedTooltip = null;
let activeTooltipOwner = null;
let selectionSearchModal = null;
let lastContextSearchQuery = "";
// ============================================================

if (chrome.storage && chrome.storage.onChanged) {
  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (
      areaName === "local" &&
      (changes.localFileEntries || changes.localFileList || changes.localFileIndexMeta)
    ) {
      searchCache.clear();
      searchCacheVersion += 1;
    }
  });
}

/**
 * 제목 요소에서 아이콘, 댓글 수 등을 제외하고 순수 텍스트만 추출합니다.
 */
function getCleanTitle(titleEl) {
  // 1. 요소를 복제하여 원본 훼손 방지
  const clone = titleEl.cloneNode(true);

  // 2. 검색에 방해되는 요소들 제거 (아이콘, 댓글 수, 스크린 리더용 텍스트 등)
  const noiseSelectors = [
    ".wr-icon", ".sound_only", ".count", ".cnt_cmt", ".comment-badge", // 특정 사이트 클래스
    ".dl-check-btn", ".google-search-btn" // 이미 붙은 확장 아이콘
  ];

  noiseSelectors.forEach((sel) => {
    clone.querySelectorAll(sel).forEach((el) => el.remove());
  });

  // 3. 텍스트만 추출하고 불필요한 공백/줄바꿈 정리
  return normalizeSiteTitleText(titleEl, clone.textContent)
    .replace(/\n/g, " ") // 줄바꿈을 공백으로
    .replace(/\s+/g, " ") // 연속된 공백을 하나로
    .trim();
}

function normalizeSiteTitleText(titleEl, value) {
  const text = String(value || "");
  if (
    titleEl &&
    typeof titleEl.matches === "function" &&
    titleEl.matches(".group-material-copy > strong")
  ) {
    return text.replace(/\+/g, " ");
  }
  return text;
}

function escapeHtml(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function formatResultMeta(item) {
  const searchBy = item.searchLabel ? `${item.searchLabel} 검색` : "검색";
  const parts = [`${searchBy}: ${item.matchLabel || "검색 결과"}`];
  if (item.author) parts.push(`작가 ${item.author}`);
  if (item.maxNumber) parts.push(`${item.maxNumber}편`);
  if (item.complete) parts.push("완결");
  return parts.map(escapeHtml).join(" · ");
}

function formatQueryInfo(response) {
  if (!response || !response.query || !response.query.extracted) return "";
  if (response.query.original === response.query.extracted) return "";
  return `<div class='query-info'>추출 제목: ${escapeHtml(response.query.extracted)}</div>`;
}

function getSharedTooltip() {
  if (!sharedTooltip) {
    sharedTooltip = document.createElement("div");
    sharedTooltip.className = "dl-check-tooltip";
    if (typeof sharedTooltip.showPopover === "function") {
      sharedTooltip.setAttribute("popover", "manual");
    }
    document.documentElement.appendChild(sharedTooltip);
  }
  return sharedTooltip;
}

function positionTooltip(btn, tooltip) {
  const rect = btn.getBoundingClientRect();
  tooltip.style.left = rect.left + "px";
  tooltip.style.top = rect.bottom + 5 + "px";
}

function showSharedTooltip(btn, tooltip) {
  positionTooltip(btn, tooltip);
  tooltip.style.display = "block";
  if (typeof tooltip.showPopover !== "function") return;
  try {
    if (!tooltip.matches(":popover-open")) tooltip.showPopover();
  } catch (_error) {
    tooltip.removeAttribute("popover");
  }
}

function hideSharedTooltip(tooltip) {
  if (!tooltip) return;
  if (typeof tooltip.hidePopover === "function") {
    try {
      if (tooltip.matches(":popover-open")) tooltip.hidePopover();
    } catch (_error) {
      // The fixed max-z-index fallback is still hidden below.
    }
  }
  tooltip.style.display = "none";
}

function getSearchCacheKey(query) {
  return String(query || "").normalize("NFC").trim();
}

function requestSearch(query) {
  const key = getSearchCacheKey(query);
  if (!key) return Promise.resolve(null);

  const cached = searchCache.get(key);
  if (cached && cached.version === searchCacheVersion && cached.response) {
    return Promise.resolve(cached.response);
  }
  if (cached && cached.version === searchCacheVersion && cached.promise) {
    return cached.promise;
  }

  const version = searchCacheVersion;
  const promise = new Promise((resolve) => {
    chrome.runtime.sendMessage(
      { action: "searchFile", query: key, limit: TOOLTIP_RESULT_LIMIT },
      (response) => {
        if (chrome.runtime.lastError) {
          searchCache.delete(key);
          resolve(null);
          return;
        }

        searchCache.set(key, { response, version });
        resolve(response);
      }
    );
  });

  searchCache.set(key, { promise, version });
  return promise;
}

function requestWebStats(query) {
  const key = getSearchCacheKey(query);
  if (!key) return Promise.resolve(null);

  const cached = webStatsCache.get(key);
  if (cached && cached.response) {
    return Promise.resolve(cached.response);
  }
  if (cached && cached.promise) {
    return cached.promise;
  }

  const promise = new Promise((resolve) => {
    chrome.runtime.sendMessage(
      { action: "lookupWebStats", query: key },
      (response) => {
        if (chrome.runtime.lastError) {
          webStatsCache.delete(key);
          resolve(null);
          return;
        }

        webStatsCache.set(key, { response });
        resolve(response);
      }
    );
  });

  webStatsCache.set(key, { promise });
  return promise;
}

// coverage(응답 단위)로 3색 돋보기를 정한다.
//  covered  → 녹색(duplicate): 단위 같고 보유 편수 ≥ 대상, 받을 필요 적음
//  outdated → 파랑(outdated): 보유 부족/편수 불명/단위 다름, 갱신 후보(확인)
//  none     → 회색(safe): 미보유
function coverageOf(response) {
  if (!response) return "none";
  if (response.coverage) return response.coverage;
  // 구버전 응답 폴백: 결과 유무로만.
  return response.results && response.results.length > 0 ? "outdated" : "none";
}

// outdated 사유별 사람 친화 문구. 거짓 비교("999권 < 100화")를 피하려고
// 단위 상이/편수 불명/느슨한 매칭은 편수 비교 문구를 쓰지 않는다.
function outdatedReasonText(response) {
  const owned = response.ownedMaxNumber || 0;
  const q = response.queryMaxNumber || 0;
  switch (response.coverageReason) {
    case "owned_less":
      return owned && q ? `보유 ${owned} < 대상 ${q}` : "보유 부족";
    case "unit_mismatch":
      return "단위 상이 (확인 필요)";
    case "unit_unknown":
      return "단위 불명 (확인 필요)";
    case "count_unknown":
      return "편수 불명 (확인 필요)";
    case "loose_match":
      return "유사 후보 (확인 필요)";
    default:
      return "확인 권장";
  }
}

function updateButtonState(btn, response) {
  const coverage = coverageOf(response);
  btn.classList.remove("pending", "safe", "duplicate", "outdated");
  if (coverage === "covered") {
    btn.classList.add("duplicate"); // 녹색
    btn.title = "보유 충분 (중복 의심)";
  } else if (coverage === "outdated") {
    btn.classList.add("outdated"); // 파랑
    btn.title = `갱신 후보 (${outdatedReasonText(response)})`;
  } else {
    btn.classList.add("safe"); // 회색
    btn.title = "미보유";
  }
}

function renderTooltip(tooltip, response) {
  if (!response) {
    tooltip.innerHTML = "<div class='warning'>검색 응답 없음</div>";
    return;
  }

  if (!response.results || response.results.length === 0) {
    tooltip.innerHTML =
      `${formatQueryInfo(response)}<div class='safe'>✅ 발견된 파일 없음 (다운 가능)</div>`;
    return;
  }

  let html = formatQueryInfo(response);
  html += coverageBanner(response);
  html += "<ul>";

  response.results.forEach((item) => {
    const sourceClass = item.source && item.source.includes("PC")
      ? "source-pc"
      : "source-web";
    html += `
      <li>
        <span class='badge ${sourceClass}'>${escapeHtml(item.source)}</span>
        <span class='filename'>${escapeHtml(item.name)}</span>
        <span class='match-meta'>${formatResultMeta(item)}</span>
      </li>
    `;
  });

  html += "</ul>";
  tooltip.innerHTML = html;
}

function closeSelectionSearchModal() {
  if (!selectionSearchModal) return;
  if (
    selectionSearchModal.root.open &&
    typeof selectionSearchModal.root.close === "function"
  ) {
    selectionSearchModal.root.close();
  }
  selectionSearchModal.root.style.display = "none";
}

function getSelectionSearchModal() {
  if (selectionSearchModal) return selectionSearchModal;

  const root = document.createElement("dialog");
  root.className = "dl-selection-search-modal";
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-modal", "true");
  root.setAttribute("aria-label", "선택한 제목 중복 확인");
  root.innerHTML = `
    <div class="dl-selection-search-panel">
      <div class="dl-selection-search-header">
        <strong>선택한 제목 중복 확인</strong>
        <button type="button" class="dl-selection-search-close" aria-label="닫기">×</button>
      </div>
      <div class="dl-selection-search-query"></div>
      <div class="dl-selection-web-stats">
        <div class="dl-check-tooltip"></div>
      </div>
      <div class="dl-selection-search-results">
        <div class="dl-check-tooltip"></div>
      </div>
    </div>
  `;

  const panel = root.querySelector(".dl-selection-search-panel");
  const closeBtn = root.querySelector(".dl-selection-search-close");
  const queryEl = root.querySelector(".dl-selection-search-query");
  const webStatsWrap = root.querySelector(".dl-selection-web-stats");
  const webStatsEl = webStatsWrap.querySelector(".dl-check-tooltip");
  const resultsEl = root.querySelector(".dl-selection-search-results .dl-check-tooltip");

  closeBtn.addEventListener("click", closeSelectionSearchModal);
  root.addEventListener("click", (event) => {
    if (event.target === root) closeSelectionSearchModal();
  });
  root.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeSelectionSearchModal();
  });
  panel.addEventListener("click", (event) => event.stopPropagation());
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && root.style.display !== "none") {
      closeSelectionSearchModal();
    }
  });

  document.body.appendChild(root);
  selectionSearchModal = {
    root,
    closeBtn,
    queryEl,
    webStatsWrap,
    webStatsEl,
    resultsEl,
  };
  return selectionSearchModal;
}

function showSelectionSearch(query, { includeWebStats = false } = {}) {
  const cleanQuery = String(query || "").replace(/\s+/g, " ").trim();
  if (!cleanQuery) return;

  const modal = getSelectionSearchModal();
  modal.queryEl.textContent = cleanQuery;
  modal.resultsEl.innerHTML = "<div class='loading'>파일 찾는 중...</div>";
  modal.webStatsWrap.style.display = includeWebStats ? "block" : "none";
  modal.webStatsEl.innerHTML = includeWebStats
    ? "<div class='loading'>조회수·추천 찾는 중...</div>"
    : "";
  modal.root.style.display = "flex";
  if (!modal.root.open && typeof modal.root.showModal === "function") {
    try {
      modal.root.showModal();
    } catch (_error) {
      // Older/embedded pages can reject top-layer promotion; fixed max z-index remains.
    }
  }
  modal.closeBtn.focus();

  requestSearch(cleanQuery).then((response) => {
    if (modal.root.style.display !== "none" && modal.queryEl.textContent === cleanQuery) {
      renderTooltip(modal.resultsEl, response);
    }
  });

  if (includeWebStats) {
    requestWebStats(cleanQuery).then((response) => {
      if (
        modal.root.style.display !== "none" &&
        modal.webStatsWrap.style.display !== "none" &&
        modal.queryEl.textContent === cleanQuery
      ) {
        renderWebStatsTooltip(modal.webStatsEl, response);
      }
    });
  }
}

function getSelectedPageText() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed) return "";
  return String(selection).replace(/\s+/g, " ").trim();
}

function getContextSearchQuery(event) {
  const rawTarget = event.target;
  const targetEl = rawTarget instanceof Element
    ? rawTarget
    : (rawTarget && rawTarget.parentElement ? rawTarget.parentElement : null);
  const selection = window.getSelection();
  const selectionTouchesTarget = selection && !selection.isCollapsed && targetEl &&
    selection.containsNode(targetEl, true);
  const selectedText = selectionTouchesTarget
    ? String(selection).replace(/\s+/g, " ").trim()
    : "";
  const titleEl = targetEl ? targetEl.closest(CONTEXT_TARGET_SELECTOR) : null;
  return selectedText || (titleEl ? getCleanTitle(titleEl) : "");
}

document.addEventListener("contextmenu", (event) => {
  lastContextSearchQuery = getContextSearchQuery(event);

  if (!event.metaKey || !lastContextSearchQuery) return;

  event.preventDefault();
  event.stopPropagation();
  showSelectionSearch(lastContextSearchQuery, { includeWebStats: true });
}, true);

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "showShortcutSelectionSearch") {
    const selectedText = getSelectedPageText();
    if (!selectedText) {
      sendResponse({ ok: false });
      return;
    }

    showSelectionSearch(selectedText);
    sendResponse({ ok: true });
    return;
  }

  if (request.action !== "showContextSearch") return;

  const query = String(request.query || lastContextSearchQuery || "")
    .replace(/\s+/g, " ")
    .trim();
  if (!query) {
    sendResponse({ ok: false });
    return;
  }

  showSelectionSearch(query);
  sendResponse({ ok: true });
});

// coverage 상태를 색+텍스트로 함께 표기(색각 비의존).
function coverageBanner(response) {
  const coverage = coverageOf(response);
  const owned = response.ownedMaxNumber || 0;
  const q = response.queryMaxNumber || 0;
  const count = response.results ? response.results.length : 0;
  if (coverage === "covered") {
    const cmp = owned && q ? ` (보유 ${owned} ≥ 대상 ${q})` : "";
    return `<div class='cov cov-covered'>${escapeHtml(`🟢 보유 충분 · 중복 의심${cmp} · ${count}건`)}</div>`;
  }
  if (coverage === "outdated") {
    return `<div class='cov cov-outdated'>${escapeHtml(`🔵 갱신 후보 (${outdatedReasonText(response)}) · ${count}건`)}</div>`;
  }
  return `<div class='cov cov-none'>⚪ 미보유</div>`;
}

function formatWebStatStatus(item) {
  if (!item) return "조회 실패";
  if (item.status === "ok") {
    const metrics = Array.isArray(item.metrics) ? item.metrics : [];
    if (metrics.length === 0) return "정보 있음";
    return metrics
      .map((metric) => `${escapeHtml(metric.label)} ${escapeHtml(metric.value)}`)
      .join(" · ");
  }
  if (item.message) return escapeHtml(item.message);

  const labels = {
    not_found: "결과 없음",
    timeout: "시간 초과",
    error: "조회 실패",
    skipped: "건너뜀",
  };
  return labels[item.status] || "조회 실패";
}

function renderWebStatsTooltip(tooltip, response) {
  if (!response) {
    tooltip.innerHTML = "<div class='warning'>웹 정보 응답 없음</div>";
    return;
  }

  const queryTitle = response.query && response.query.title ? response.query.title : "";
  let html = `<div class='web-stats-title'>웹 정보: ${escapeHtml(queryTitle || "검색 제목 없음")}</div>`;
  const results = Array.isArray(response.results) ? response.results : [];

  if (results.length === 0) {
    html += "<div class='web-stat-row status-not-found'>결과 없음</div>";
    tooltip.innerHTML = html;
    return;
  }

  results.forEach((item) => {
    const statusClass = item.status ? `status-${item.status}` : "status-error";
    html += `
      <div class='web-stat-row ${statusClass}'>
        <span class='web-stat-source'>${escapeHtml(item.platform || "")}</span>
        <span class='web-stat-status'>${formatWebStatStatus(item)}</span>
      </div>
    `;
  });

  tooltip.innerHTML = html;
}

function addCheckButtons() {
  const titles = document.querySelectorAll(TARGET_SELECTOR);

  titles.forEach((titleEl) => {
    // 이미 버튼이 달려있으면 중복 생성 방지
    if (titleEl.querySelector(".dl-check-btn")) return;

    // 제목 텍스트가 비어있으면 패스
    const queryText = getCleanTitle(titleEl);
    if (!queryText) return;

    // 1. 돋보기 아이콘 생성 (중복 확인)
    const btn = document.createElement("span");
    btn.className = "dl-check-btn pending";
    btn.title = "중복 파일 확인";
    btn.innerHTML = `
      <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="3" fill="none" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="11" cy="11" r="8"></circle>
        <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
      </svg>
    `;

    // 돋보기 버튼 스타일 보정 (레이아웃 관련 최소 스타일만 유지)
    btn.style.zIndex = "100";
    btn.style.position = "relative";

    // 1-2. 구글 검색 아이콘 생성
    const googleBtn = document.createElement("span");
    googleBtn.innerText = " 🌐";
    googleBtn.className = "google-search-btn";
    googleBtn.title = "구글 평점 검색";

    // 구글 버튼 스타일 보정 (레이아웃 관련 최소 스타일만 유지)
    googleBtn.style.zIndex = "100";
    googleBtn.style.position = "relative";

    // 2. 버튼 클릭 시 게시글로 이동하는 것을 막아야 함
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
    });

    googleBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const currentQuery = getCleanTitle(titleEl);
      if (currentQuery) {
        // 팝업 차단 방지를 위해 동기적으로 빈 탭을 먼저 엽니다.
        const newTab = window.open("about:blank", "_blank");

        // background.js 에 제목 정제를 비동기로 요청합니다.
        chrome.runtime.sendMessage(
          { action: "searchFile", query: currentQuery },
          (response) => {
            if (response && response.query && response.query.extracted) {
              const extracted = response.query.extracted;
              newTab.location.href = `https://www.google.com/search?q=${encodeURIComponent(extracted)}`;
            } else {
              // 실패할 경우 원본 제목으로 이동합니다.
              newTab.location.href = `https://www.google.com/search?q=${encodeURIComponent(currentQuery)}`;
            }
          }
        );
      }
    });

    // 제목 요소(a태그 등) 안에 추가
    titleEl.appendChild(btn);
    titleEl.appendChild(googleBtn);

    let latestQuery = queryText;
    let latestResponse = null;
    let latestPromise = null;
    let latestVersion = searchCacheVersion;
    let tooltipVisible = false;
    let webStatsVisible = false;

    function ensureSearch() {
      const currentQuery = getCleanTitle(titleEl);
      if (!currentQuery) return Promise.resolve(null);
      if (latestResponse && latestQuery === currentQuery && latestVersion === searchCacheVersion) {
        return Promise.resolve(latestResponse);
      }
      if (latestPromise && latestQuery === currentQuery && latestVersion === searchCacheVersion) {
        return latestPromise;
      }

      latestQuery = currentQuery;
      latestVersion = searchCacheVersion;
      const requestVersion = latestVersion;
      latestPromise = requestSearch(currentQuery).then((response) => {
        if (latestQuery === currentQuery && latestVersion === requestVersion) {
          latestResponse = response;
          latestPromise = null;
          updateButtonState(btn, response);
        }
        return response;
      });
      return latestPromise;
    }

    // 3. 즉시 중복 확인 수행. 이 결과를 hover 툴팁에서도 재사용한다.
    ensureSearch();

    googleBtn.addEventListener("mouseenter", () => {
      const tooltip = getSharedTooltip();
      activeTooltipOwner = googleBtn;
      webStatsVisible = true;
      showSharedTooltip(googleBtn, tooltip);
      tooltip.innerHTML = "<div class='loading'>웹 정보 찾는 중...</div>";

      const currentQuery = getCleanTitle(titleEl);
      requestWebStats(currentQuery).then((response) => {
        if (webStatsVisible && activeTooltipOwner === googleBtn) {
          renderWebStatsTooltip(tooltip, response);
        }
      });
    });

    googleBtn.addEventListener("mouseleave", () => {
      webStatsVisible = false;
      if (activeTooltipOwner === googleBtn && sharedTooltip) {
        hideSharedTooltip(sharedTooltip);
        activeTooltipOwner = null;
      }
    });

    // 4. 마우스 진입 이벤트
    btn.addEventListener("mouseenter", () => {
      const tooltip = getSharedTooltip();
      activeTooltipOwner = btn;
      tooltipVisible = true;
      showSharedTooltip(btn, tooltip);

      const currentQuery = getCleanTitle(titleEl);
      if (latestResponse && latestQuery === currentQuery && latestVersion === searchCacheVersion) {
        renderTooltip(tooltip, latestResponse);
        return;
      }

      tooltip.innerHTML = "<div class='loading'>파일 찾는 중...</div>";
      ensureSearch().then((response) => {
        if (tooltipVisible && activeTooltipOwner === btn) {
          renderTooltip(tooltip, response);
        }
      });
    });

    // 5. 마우스 이탈 이벤트
    btn.addEventListener("mouseleave", () => {
      tooltipVisible = false;
      if (activeTooltipOwner === btn && sharedTooltip) {
        hideSharedTooltip(sharedTooltip);
        activeTooltipOwner = null;
      }
    });
  });
}

// 초기 실행
addCheckButtons();

// 동적으로 로딩되는 사이트(무한 스크롤 등)를 위해 주기적 실행
setInterval(addCheckButtons, 2000);
