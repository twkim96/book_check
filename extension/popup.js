document.addEventListener("DOMContentLoaded", () => {
  chrome.storage.local.get(["localFileEntries", "localFileList", "localFileIndexMeta"], (result) => {
    const count = Array.isArray(result.localFileEntries) && result.localFileEntries.length > 0
      ? result.localFileEntries.length
      : (result.localFileList ? result.localFileList.length : 0);
    document.getElementById("currentCount").innerText = count;

    const meta = result.localFileIndexMeta;
    const metaEl = document.getElementById("indexMeta");
    if (metaEl && meta) {
      const parts = [];
      if (meta.normalizer_version) parts.push(`normalizer ${meta.normalizer_version}`);
      if (meta.version !== undefined) parts.push(`format v${meta.version}`);
      if (meta.generated_at) parts.push(meta.generated_at);
      metaEl.innerText = parts.join(" · ");
    }
  });
});

document.getElementById("fileInput").addEventListener("change", function (event) {
  const file = event.target.files[0];
  const statusDiv = document.getElementById("status");

  if (!file) return;

  const reader = new FileReader();

  reader.onload = function (loadEvent) {
    try {
      const parsed = JSON.parse(loadEvent.target.result);
      const index = parseUploadPayload(parsed);

      chrome.storage.local.set({
        localFileEntries: index.entries,
        localFileList: index.entries.map((entry) => entry.name),
        localFileIndexMeta: index.meta,
      }, () => {
        if (chrome.runtime.lastError) {
          statusDiv.style.display = "block";
          statusDiv.className = "error";
          statusDiv.innerText = `❌ 저장 실패: ${chrome.runtime.lastError.message}`;
          console.error(chrome.runtime.lastError);
          return;
        }

        statusDiv.style.display = "block";
        statusDiv.className = "success";
        const metaParts = [];
        if (index.meta.normalizer_version) metaParts.push(`normalizer ${index.meta.normalizer_version}`);
        if (index.meta.version !== undefined) metaParts.push(`format v${index.meta.version}`);
        if (index.meta.generated_at) metaParts.push(index.meta.generated_at);
        const metaSuffix = metaParts.length ? ` (${metaParts.join(" · ")})` : "";
        statusDiv.innerText = `✅ 성공! ${index.entries.length}개의 항목이 등록되었습니다.${metaSuffix}`;

        document.getElementById("currentCount").innerText = index.entries.length;
        const metaEl = document.getElementById("indexMeta");
        if (metaEl) metaEl.innerText = metaParts.join(" · ");
      });
    } catch (err) {
      statusDiv.style.display = "block";
      statusDiv.className = "error";
      statusDiv.innerText = "❌ 올바른 file_list.json 또는 file_index.json이 아닙니다.";
      console.error(err);
    }
  };

  reader.readAsText(file);
});

function parseUploadPayload(payload) {
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
          complete: Boolean(entry.complete),
        })),
    };
  }

  throw new Error("Unsupported index format");
}

const searchInput = document.getElementById("searchInput");
const searchBtn = document.getElementById("searchBtn");
const searchResults = document.getElementById("searchResults");

function escapeHtml(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function formatMeta(item) {
  const searchBy = item.searchLabel ? `${item.searchLabel} 검색` : "검색";
  const parts = [`${searchBy}: ${item.matchLabel || "검색 결과"}`];
  if (item.author) parts.push(`작가 ${item.author}`);
  // 색 판정 근거(effectiveMax+unit)와 표시를 맞춘다. effectiveMax 없으면 maxNumber 폴백.
  const eff = item.effectiveMax !== undefined && item.effectiveMax !== null ? item.effectiveMax : item.maxNumber;
  if (eff) {
    const unit = item.unit && item.unit !== "미상" ? item.unit : "편";
    parts.push(`${eff}${unit}`);
  }
  if (item.complete) parts.push("완결");
  return parts.map(escapeHtml).join(" · ");
}

// content.js와 동일한 coverage 사유 문구(색각 비의존).
function popupCoverageBanner(response) {
  const coverage = response && response.coverage ? response.coverage : (response && response.results && response.results.length ? "outdated" : "none");
  const owned = response.ownedMaxNumber || 0;
  const q = response.queryMaxNumber || 0;
  const count = response.results ? response.results.length : 0;
  if (coverage === "covered") {
    const cmp = owned && q ? ` (보유 ${owned} ≥ 대상 ${q})` : "";
    return `<div class='cov cov-covered'>${escapeHtml(`🟢 보유 충분 · 중복 의심${cmp} · ${count}건`)}</div>`;
  }
  if (coverage === "outdated") {
    return `<div class='cov cov-outdated'>${escapeHtml(`🔵 갱신 후보 (${popupReasonText(response)}) · ${count}건`)}</div>`;
  }
  return `<div class='cov cov-none'>⚪ 미보유</div>`;
}

function popupReasonText(response) {
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

function formatQueryInfo(response) {
  if (!response || !response.query || !response.query.extracted) return "";
  if (response.query.original === response.query.extracted) return "";
  return `<div class="query-info">추출 제목: ${escapeHtml(response.query.extracted)}</div>`;
}

function performSearch() {
  const query = searchInput.value.trim();
  if (!query) {
    searchResults.innerHTML = "";
    return;
  }

  searchResults.innerHTML = "<div style='padding: 10px; text-align: center;'>검색 중...</div>";

  chrome.runtime.sendMessage({ action: "searchFile", query }, (response) => {
    if (!response || !response.results || response.results.length === 0) {
      searchResults.innerHTML = `${formatQueryInfo(response)}<div class='no-results'>✅ 발견된 중복 파일 없음</div>`;
      return;
    }

    let html = formatQueryInfo(response);
    html += popupCoverageBanner(response);
    response.results.forEach((item) => {
      const sourceClass = item.source.includes("PC") ? "source-pc" : "source-web";
      html += `
        <div class="result-item">
          <div class="result-source ${sourceClass}">${escapeHtml(item.source)}</div>
          <div class="result-name">${escapeHtml(item.name)}</div>
          <div class="result-meta">${formatMeta(item)}</div>
        </div>
      `;
    });
    searchResults.innerHTML = html;
  });
}

searchInput.addEventListener("keypress", (event) => {
  if (event.key === "Enter") performSearch();
});

searchBtn.addEventListener("click", performSearch);
