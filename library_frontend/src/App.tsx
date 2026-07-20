import { useEffect, useMemo, useRef, useState } from "react";
import { NavLink, Navigate, Route, Routes, useNavigate, useSearchParams } from "react-router-dom";

import { ApiError, api, postJson } from "./api";
import type {
  DashboardData,
  JobRecord,
  PlatformStatus,
  TitleCase,
  TitleListing,
  TitlePlan,
  TitlePreview,
  VolumeCase,
  VolumeClassification,
  VolumeListing,
  VolumePreview
} from "./types";

type Draft = {
  value: string;
  selected: boolean;
  loading: boolean;
  preview?: TitlePreview;
  error?: string;
};

const platformLabels = { series: "시리즈", kakao: "카카오", novelpia: "노벨피아" };

function formatNumber(value: number): string {
  return new Intl.NumberFormat("ko-KR").format(value);
}

function formatBytes(value: number): string {
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function statusLabel(status: PlatformStatus): string {
  return {
    ok: "확인",
    not_found: "없음",
    error: "오류",
    skipped: "제외",
    missing: "미수집"
  }[status];
}

function StatusBadge({ status }: { status: PlatformStatus }) {
  return <span className={`status status-${status}`}>{statusLabel(status)}</span>;
}

function Shell() {
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">書</span>
          <div>
            <strong>도서 관리</strong>
            <small>file_check 1.2.9</small>
          </div>
        </div>
        <nav>
          <NavLink to="/" end>대시보드</NavLink>
          <NavLink to="/review/titles">제목 교정</NavLink>
          <NavLink to="/review/volumes">분권 묶기</NavLink>
          <NavLink to="/jobs">작업 이력</NavLink>
        </nav>
        <div className="sidebar-note">로컬 전용 · 실제 변경 전 계획 확인</div>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/review/titles" element={<TitleReview />} />
          <Route path="/review/volumes" element={<VolumeReview />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

export function App() {
  return <Shell />;
}

function PageHeader({ eyebrow, title, description, action }: {
  eyebrow: string;
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action}
    </header>
  );
}

function Dashboard() {
  const [data, setData] = useState<DashboardData>();
  const [error, setError] = useState("");
  const load = () => {
    setError("");
    api<DashboardData>("/api/dashboard").then(setData).catch((reason) => setError(reason.message));
  };
  useEffect(load, []);
  if (error) return <ErrorPanel message={error} retry={load} />;
  if (!data) return <Loading />;
  const cards = [
    ["보유 도서", data.database.supported_house_files, "활성 지원 파일"],
    ["미확인 작품", data.database.titles_without_ok_metadata, "core_title 기준"],
    ["Folderling 대기", data.filesystem.folderling_pending, "txt_temp"],
    ["Warning", data.filesystem.warning_files, "사람 판단 필요"]
  ] as const;
  return (
    <>
      <PageHeader
        eyebrow="LIBRARY OVERVIEW"
        title="오늘의 도서 상태"
        description="명령이나 로그를 열기 전에 현재 DB와 입고 대기 상태를 확인합니다."
        action={<button className="button secondary" onClick={load}>새로고침</button>}
      />
      <section className="health-strip">
        <span className={data.database.doctor_ok ? "health-ok" : "health-bad"} />
        <strong>{data.database.doctor_ok ? "DB와 파일 상태 정상" : `Doctor 문제 ${data.database.doctor_issue_count}건`}</strong>
        <span>무결성 {data.database.integrity}</span>
        <span>index {formatNumber(data.filesystem.index.files)}개</span>
        <span>normalizer {data.filesystem.index.normalizer_version ?? "-"}</span>
      </section>
      <section className="stat-grid">
        {cards.map(([label, value, note]) => (
          <article className="stat-card" key={label}>
            <span>{label}</span>
            <strong>{formatNumber(value)}</strong>
            <small>{note}</small>
          </article>
        ))}
      </section>
      <section className="panel">
        <div className="panel-title">
          <div><span className="eyebrow">RECENT JOBS</span><h2>최근 작업</h2></div>
          <NavLink className="text-link" to="/jobs">전체 보기</NavLink>
        </div>
        <JobList jobs={data.jobs} empty="아직 도서 관리 서버에서 실행한 작업이 없습니다." />
      </section>
    </>
  );
}

function TitleReview() {
  const [urlParams] = useSearchParams();
  const initialSearch = urlParams.get("search") ?? "";
  const [listing, setListing] = useState<TitleListing>();
  const [search, setSearch] = useState(initialSearch);
  const [submittedSearch, setSubmittedSearch] = useState(initialSearch);
  const [status, setStatus] = useState("all");
  const [sort, setSort] = useState("name");
  const [direction, setDirection] = useState("asc");
  const [cursor, setCursor] = useState<string | null>(null);
  const [cursorHistory, setCursorHistory] = useState<(string | null)[]>([]);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [error, setError] = useState("");
  const [plan, setPlan] = useState<TitlePlan>();
  const [planning, setPlanning] = useState(false);
  const navigate = useNavigate();

  const load = () => {
    const params = new URLSearchParams({
      search: submittedSearch,
      status,
      sort,
      direction,
      limit: "50"
    });
    if (cursor) params.set("cursor", cursor);
    setError("");
    setListing(undefined);
    api<TitleListing>(`/api/review/titles?${params}`).then(setListing).catch((reason) => setError(reason.message));
  };
  useEffect(load, [submittedSearch, status, sort, direction, cursor]);

  const selectedChanges = useMemo(() => {
    if (!listing) return [];
    return listing.items.flatMap((item) => {
      const draft = drafts[item.file_id];
      if (!draft?.selected || !draft.preview?.runnable) return [];
      return [{ file_id: item.file_id, source_revision: item.source_revision, new_body: draft.value }];
    });
  }, [listing, drafts]);

  const createPlan = async () => {
    setPlanning(true);
    setError("");
    try {
      setPlan(await postJson<TitlePlan>("/api/review/titles/plan", { changes: selectedChanges }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "계획을 만들지 못했습니다.");
    } finally {
      setPlanning(false);
    }
  };

  const applyPlan = async () => {
    if (!plan) return;
    setPlanning(true);
    try {
      const job = await postJson<JobRecord>("/api/review/titles/apply", {
        changes: selectedChanges,
        confirm_count: plan.item_count,
        confirm_plan_sha256: plan.plan_sha256
      });
      setPlan(undefined);
      navigate(`/jobs?focus=${job.job_id}`);
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === "confirmation_stale") {
        setError("파일이나 DB 상태가 바뀌었습니다. 계획을 다시 확인하세요.");
      } else {
        setError(reason instanceof Error ? reason.message : "실행하지 못했습니다.");
      }
    } finally {
      setPlanning(false);
    }
  };

  const resetPage = () => {
    setCursor(null);
    setCursorHistory([]);
    setDrafts({});
  };

  return (
    <>
      <PageHeader
        eyebrow="TITLE CORRECTION"
        title="실제 파일명 교정"
        description="플랫폼에서 확인되지 않은 활성 파일을 표시합니다. 같은 작품의 여러 파일도 따로 보이며, 입력한 파일만 txt_temp에 재입고됩니다."
        action={
          <button className="button primary" disabled={!selectedChanges.length || planning} onClick={createPlan}>
            선택 {selectedChanges.length}개 계획 확인
          </button>
        }
      />
      <section className="toolbar">
        <form onSubmit={(event) => { event.preventDefault(); setSubmittedSearch(search); resetPage(); }} className="search-form">
          <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="파일명, core_title 검색" />
          <button className="button secondary">검색</button>
        </form>
        <select value={status} onChange={(event) => { setStatus(event.target.value); resetPage(); }}>
          <option value="all">모든 미확인 상태</option>
          <option value="all_not_found">세 플랫폼 모두 없음</option>
          <option value="error">오류 포함</option>
          <option value="missing">미수집 포함</option>
        </select>
        <select value={sort} onChange={(event) => { setSort(event.target.value); resetPage(); }}>
          <option value="name">파일명순</option>
          <option value="core">core_title순</option>
          <option value="path">경로순</option>
        </select>
        <button className="button ghost" onClick={() => { setDirection(direction === "asc" ? "desc" : "asc"); resetPage(); }}>
          {direction === "asc" ? "오름차순" : "내림차순"}
        </button>
      </section>
      {error && <div className="inline-error">{error}</div>}
      {!listing ? <Loading /> : (
        <section className="table-panel">
          <div className="table-summary">
            <span>검토 파일 <strong>{formatNumber(listing.total)}</strong>개</span>
            <span>현재 페이지에서 입력한 항목만 변경됩니다.</span>
          </div>
          <div className="table-scroll">
            <table>
              <thead><tr>
                <th className="select-col">선택</th>
                <th>현재 파일명</th>
                <th>현재 core_title</th>
                <th>플랫폼 상태</th>
                <th>새 파일명</th>
                <th>변경 후 core_title</th>
              </tr></thead>
              <tbody>
                {listing.items.map((item) => (
                  <TitleRow key={item.file_id} item={item} draft={drafts[item.file_id]}
                    onChange={(draft) => setDrafts((current) => ({ ...current, [item.file_id]: draft }))} />
                ))}
              </tbody>
            </table>
          </div>
          <div className="pagination">
            <button className="button secondary" disabled={!cursorHistory.length} onClick={() => {
              const history = [...cursorHistory];
              setCursor(history.pop() ?? null);
              setCursorHistory(history);
              setDrafts({});
            }}>이전</button>
            <button className="button secondary" disabled={!listing.next_cursor} onClick={() => {
              setCursorHistory((history) => [...history, cursor]);
              setCursor(listing.next_cursor);
              setDrafts({});
            }}>다음</button>
          </div>
        </section>
      )}
      {plan && <PlanDialog plan={plan} busy={planning} onClose={() => setPlan(undefined)} onApply={applyPlan} />}
    </>
  );
}

function TitleRow({ item, draft, onChange }: { item: TitleCase; draft?: Draft; onChange: (draft: Draft) => void }) {
  const timer = useRef<number | undefined>(undefined);
  const requestVersion = useRef(0);
  const value = draft?.value ?? "";
  const requestPreview = (nextValue: string) => {
    window.clearTimeout(timer.current);
    const version = ++requestVersion.current;
    const base: Draft = { value: nextValue, selected: draft?.selected ?? true, loading: Boolean(nextValue.trim()) };
    onChange(base);
    if (!nextValue.trim()) return;
    timer.current = window.setTimeout(async () => {
      try {
        const preview = await postJson<TitlePreview>("/api/review/titles/preview", {
          file_id: item.file_id,
          source_revision: item.source_revision,
          new_body: nextValue
        });
        if (version !== requestVersion.current) return;
        onChange({ ...base, loading: false, selected: preview.runnable, preview });
      } catch (reason) {
        if (version !== requestVersion.current) return;
        onChange({ ...base, loading: false, selected: false, error: reason instanceof Error ? reason.message : "미리보기 실패" });
      }
    }, 350);
  };
  useEffect(() => () => {
    requestVersion.current += 1;
    window.clearTimeout(timer.current);
  }, []);
  const blocked = !item.editable || Boolean(draft?.preview && !draft.preview.runnable);
  return (
    <tr className={value ? "row-dirty" : ""}>
      <td className="select-col">
        <input type="checkbox" aria-label={`${item.current_name} 선택`}
          checked={Boolean(draft?.selected && draft.preview?.runnable)} disabled={blocked || !draft?.preview}
          onChange={(event) => onChange({ ...(draft ?? { value, loading: false }), selected: event.target.checked })} />
      </td>
      <td>
        <strong className="filename">{item.current_name}</strong>
        <small className="path" title={item.canonical_path}>{item.author ? `작가 ${item.author} · ` : ""}{item.effective_max ? `${item.effective_max}${item.unit}` : "범위 미상"}</small>
        {!item.editable && <small className="blocked">보호된 연결 파일 — 이번 버전에서는 수정 불가</small>}
      </td>
      <td><code className="core">{item.core_title}</code></td>
      <td><div className="platforms">
        {(Object.keys(platformLabels) as Array<keyof typeof platformLabels>).map((platform) => (
          <span key={platform}><small>{platformLabels[platform]}</small><StatusBadge status={item.platforms[platform]} /></span>
        ))}
      </div></td>
      <td>
        <div className="filename-input">
          <input value={value} disabled={!item.editable} onChange={(event) => requestPreview(event.target.value)} placeholder="확장자 제외 새 파일명 · 보존할 제목은 [[19금]]" />
          <span>{item.extension}</span>
        </div>
        <small className="muted">등급·상태처럼 보이지만 실제 제목인 말은 <code>[[단어]]</code>로 보호합니다.</small>
        {draft?.loading && <small className="muted">분석 중…</small>}
        {draft?.error && <small className="blocked">{draft.error}</small>}
        {draft?.preview?.blocked_reasons.map((reason) => <small className="blocked" key={reason}>{reason}</small>)}
      </td>
      <td>
        {draft?.preview ? <>
          <code className={draft.preview.runnable ? "core core-new" : "core core-bad"}>{draft.preview.after_core_title || "-"}</code>
          <small className="path">검색어: {draft.preview.after_query_title || "-"}</small>
          <small className="path">{draft.preview.after_author ? `작가 ${draft.preview.after_author} · ` : ""}{draft.preview.after_effective_max ? `${draft.preview.after_effective_max}${draft.preview.after_unit}` : "범위 미상"}{draft.preview.after_complete ? " · 완결" : ""}</small>
          {draft.preview.title_literal_tokens.length > 0 && <small className="safe-note">제목 보호: {draft.preview.title_literal_tokens.join(", ")} · 최종 파일명에서는 [[ ]] 제거</small>}
          {draft.preview.target_exists && <small className="collision">기존 core 존재{draft.preview.target_has_ok ? " · 플랫폼 정보 있음" : ""}</small>}
        </> : <span className="muted">입력 대기</span>}
      </td>
    </tr>
  );
}

function PlanDialog({ plan, busy, onClose, onApply }: { plan: TitlePlan; busy: boolean; onClose: () => void; onApply: () => void }) {
  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="plan-title">
        <span className="eyebrow">FINAL CONFIRMATION</span>
        <h2 id="plan-title">{plan.item_count}개 파일을 txt_temp로 보냅니다</h2>
        <p>실제 파일명이 변경되고 기존 DB 행은 비활성 이력으로 남습니다. <code>[[ ]]</code> 표시는 temp에서만 보호값을 운반하고 다음 Folderling 입고 때 제거됩니다.</p>
        <div className="plan-summary">
          <span><small>대상</small><strong>{plan.item_count}</strong></span>
          <span><small>차단</small><strong>{plan.blocked_count}</strong></span>
          <span className="sha"><small>Plan SHA-256</small><code>{plan.plan_sha256}</code></span>
        </div>
        <div className="plan-list">
          {plan.items.map((item) => <div key={item.file_id}><span>{item.current_name}</span><b>→</b><strong>{item.materialized_candidate_name}</strong></div>)}
        </div>
        <footer>
          <button className="button secondary" disabled={busy} onClick={onClose}>취소</button>
          <button className="button danger" disabled={busy || !plan.runnable} onClick={onApply}>{busy ? "등록 중…" : "확인하고 실행"}</button>
        </footer>
      </section>
    </div>
  );
}

const volumeClassLabels: Record<VolumeClassification, string> = {
  auto_ready: "자동 가능",
  review_required: "검토 필요",
  already_grouped: "이미 한 폴더",
  excluded: "제외"
};

const volumeBlockerLabels: Record<string, string> = {
  non_title_core: "제목으로 보기 어려운 core",
  mixed_coordinate_kind: "권과 부 등 서로 다른 본편 좌표 체계 혼합",
  duplicate_coordinate: "같은 권 좌표 중복",
  missing_coordinate: "중간 권 누락",
  author_conflict: "작가 충돌",
  work_conflict: "기존 work 충돌",
  disambig_conflict: "서로 다른 작품 표시 충돌",
  ambiguous_coordinate: "권 좌표 불명확",
  source_outside_house: "house 밖 파일",
  source_revision_stale: "파일 또는 DB 상태 변경",
  at_least_two_files_required: "두 파일 이상 필요",
  unknown_selected_file: "현재 그룹에 없는 파일 선택",
  target_filename_collision: "목적 폴더 파일명 충돌",
  target_folder_invalid: "목적 폴더 경로가 안전하지 않음",
  source_missing_or_not_regular: "원본 파일 누락 또는 링크",
  source_identity_stale: "원본 파일 상태 변경",
  source_folder_contains_unselected_files: "기존 폴더에 선택하지 않은 파일 또는 부속 파일 존재",
  no_files_to_move: "이미 결과 폴더에 정리됨"
};

function VolumeClassBadge({ value }: { value: VolumeClassification }) {
  return <span className={`volume-class class-${value}`}>{volumeClassLabels[value]}</span>;
}

const volumeCoordinateLabels: Record<string, string> = {
  volume: "권",
  part: "부",
  symbol: "외전/부속"
};

function VolumeReview() {
  const [listing, setListing] = useState<VolumeListing>();
  const [search, setSearch] = useState("");
  const [submittedSearch, setSubmittedSearch] = useState("");
  const [classification, setClassification] = useState("all");
  const [sort, setSort] = useState("classification");
  const [direction, setDirection] = useState("asc");
  const [cursor, setCursor] = useState<string | null>(null);
  const [cursorHistory, setCursorHistory] = useState<(string | null)[]>([]);
  const [activeCase, setActiveCase] = useState<VolumeCase>();
  const [error, setError] = useState("");

  const resetPage = () => {
    setCursor(null);
    setCursorHistory([]);
  };
  const load = () => {
    const params = new URLSearchParams({
      search: submittedSearch,
      classification,
      sort,
      direction,
      limit: "30"
    });
    if (cursor) params.set("cursor", cursor);
    setError("");
    setListing(undefined);
    api<VolumeListing>(`/api/review/volumes?${params}`)
      .then(setListing)
      .catch((reason) => setError(reason.message));
  };
  useEffect(load, [submittedSearch, classification, sort, direction, cursor]);

  return (
    <>
      <PageHeader
        eyebrow="VOLUME GROUPING · 1.2.9"
        title="분권·다권본 묶기"
        description="권·부·상중하 좌표와 기존 폴더를 분석합니다. 선택한 파일은 staging 검증과 journal 기록 후 한 작품 폴더로 이동합니다."
        action={<span className="readonly-pill">STAGING + JOURNAL</span>}
      />
      <section className="toolbar">
        <form onSubmit={(event) => { event.preventDefault(); setSubmittedSearch(search); resetPage(); }} className="search-form">
          <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="작품명, core_title, 폴더 검색" />
          <button className="button secondary">검색</button>
        </form>
        <select value={classification} onChange={(event) => { setClassification(event.target.value); resetPage(); }}>
          <option value="all">모든 분류</option>
          <option value="review_required">검토 필요</option>
          <option value="auto_ready">자동 가능</option>
          <option value="already_grouped">이미 한 폴더</option>
          <option value="excluded">제외</option>
        </select>
        <select value={sort} onChange={(event) => { setSort(event.target.value); resetPage(); }}>
          <option value="classification">분류순</option>
          <option value="title">작품명순</option>
          <option value="files">파일수순</option>
          <option value="parents">폴더수순</option>
        </select>
        <button className="button ghost" onClick={() => { setDirection(direction === "asc" ? "desc" : "asc"); resetPage(); }}>
          {direction === "asc" ? "오름차순" : "내림차순"}
        </button>
      </section>
      {error && <div className="inline-error">{error}</div>}
      {!listing ? <Loading /> : <>
        <section className="volume-summary-grid">
          {(Object.keys(volumeClassLabels) as VolumeClassification[]).map((key) => (
            <button key={key} className={classification === key ? "active" : ""} onClick={() => { setClassification(key); resetPage(); }}>
              <span>{volumeClassLabels[key]}</span><strong>{formatNumber(listing.summary[key])}</strong>
            </button>
          ))}
        </section>
        <section className="table-panel">
          <div className="table-summary">
            <span>분권 그룹 <strong>{formatNumber(listing.total)}</strong>개</span>
            <span>후보 분석은 DB와 파일을 변경하지 않습니다.</span>
          </div>
          <div className="table-scroll volume-scroll">
            <table className="volume-table">
              <thead><tr>
                <th>분류</th><th>제안 작품</th><th>권 범위</th><th>구성</th><th>확인할 점</th><th>계획</th>
              </tr></thead>
              <tbody>{listing.items.map((item) => (
                <tr key={item.case_id}>
                  <td><VolumeClassBadge value={item.classification} /></td>
                  <td>
                    <strong className="filename">{item.display_title}</strong>
                    <code className="core">{item.core_title}</code>
                    {item.authors.length > 0 && <small className="path">작가 {item.authors.join(", ")}</small>}
                  </td>
                  <td><strong>{item.coordinate_range.join(" → ")}</strong><small className="path">{item.coordinate_kinds.join(" + ")}</small></td>
                  <td><strong>{item.file_count}개 파일</strong><small className="path">{item.parent_count}개 위치</small></td>
                  <td>
                    {item.blocked_reasons.length === 0 ? <span className="safe-note">충돌 없음</span> : item.blocked_reasons.map((reason) => (
                      <small className="blocked" key={reason}>{volumeBlockerLabels[reason] ?? reason}</small>
                    ))}
                    {item.duplicate_coordinates.length > 0 && <small className="collision">중복 좌표 {item.duplicate_coordinates.join(", ")}</small>}
                    {item.missing_coordinates.length > 0 && <small className="collision">누락 {item.missing_coordinates.slice(0, 8).join(", ")}{item.missing_coordinates.length > 8 ? "…" : ""}</small>}
                  </td>
                  <td><button className="button secondary" onClick={() => setActiveCase(item)}>구성 보기</button></td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          <div className="pagination">
            <button className="button secondary" disabled={!cursorHistory.length} onClick={() => {
              const history = [...cursorHistory];
              setCursor(history.pop() ?? null);
              setCursorHistory(history);
            }}>이전</button>
            <button className="button secondary" disabled={!listing.next_cursor} onClick={() => {
              setCursorHistory((history) => [...history, cursor]);
              setCursor(listing.next_cursor);
            }}>다음</button>
          </div>
        </section>
      </>}
      {activeCase && <VolumePreviewDialog value={activeCase} onClose={() => setActiveCase(undefined)} />}
    </>
  );
}

function VolumePreviewDialog({ value, onClose }: { value: VolumeCase; onClose: () => void }) {
  const [selected, setSelected] = useState(() => new Set(value.items.map((item) => item.file_id)));
  const [folderName, setFolderName] = useState(value.target_folder_name);
  const [allowDuplicateCoordinates, setAllowDuplicateCoordinates] = useState(false);
  const [preview, setPreview] = useState<VolumePreview>();
  const [busy, setBusy] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState("");
  const navigate = useNavigate();

  const refresh = async () => {
    setBusy(true);
    setError("");
    try {
      setPreview(await postJson<VolumePreview>("/api/review/volumes/preview", {
        case_id: value.case_id,
        source_revision: value.source_revision,
        selected_file_ids: [...selected],
        target_folder_name: folderName,
        allow_duplicate_coordinates: allowDuplicateCoordinates
      }));
      setConfirmed(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "분권 계획을 만들지 못했습니다.");
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => { void refresh(); }, []);

  const apply = async () => {
    if (!preview?.apply_available || !confirmed) return;
    setBusy(true);
    setError("");
    try {
      const job = await postJson<JobRecord>("/api/review/volumes/apply", {
        case_id: value.case_id,
        source_revision: value.source_revision,
        selected_file_ids: preview.selected_file_ids,
        target_folder_name: preview.target_folder_name,
        allow_duplicate_coordinates: preview.allow_duplicate_coordinates,
        confirm_count: preview.item_count,
        confirm_plan_sha256: preview.plan_sha256
      });
      onClose();
      navigate(`/jobs?focus=${job.job_id}`);
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === "confirmation_stale") {
        setError("파일이나 DB 상태가 바뀌었습니다. 미리보기를 다시 확인하세요.");
      } else {
        setError(reason instanceof Error ? reason.message : "분권 묶기를 시작하지 못했습니다.");
      }
    } finally {
      setBusy(false);
    }
  };

  return <div className="modal-backdrop" role="presentation">
    <section className="modal volume-modal" role="dialog" aria-modal="true" aria-labelledby="volume-plan-title">
      <span className="eyebrow">STAGED GROUP PLAN</span>
      <h2 id="volume-plan-title">{value.display_title}</h2>
      <p>포함할 파일과 결과 폴더를 검토합니다. 실행하면 staging 복사 검증 후 원본과 DB를 함께 이동합니다.</p>
      <label className="field-label">결과 폴더명
        <input value={folderName} onChange={(event) => { setFolderName(event.target.value); setPreview(undefined); setConfirmed(false); }} />
      </label>
      <div className="volume-file-table-wrap">
        <table className="volume-file-table">
          <thead><tr><th>선택</th><th>원본 제목</th><th>좌표·분류</th><th>작가</th><th>파일</th><th>확인할 점</th><th>교정</th></tr></thead>
          <tbody>{value.items.map((item) => <tr key={item.file_id}>
            <td><input aria-label={`${item.name} 선택`} type="checkbox" checked={selected.has(item.file_id)} onChange={(event) => {
              setSelected((current) => {
                const next = new Set(current);
                if (event.target.checked) next.add(item.file_id); else next.delete(item.file_id);
                return next;
              });
              setPreview(undefined);
              setConfirmed(false);
            }} /></td>
            <td><strong title={item.canonical_path}>{item.name}</strong><small>{item.parent}</small></td>
            <td><b>{item.coordinate}</b><small>{volumeCoordinateLabels[item.coordinate_kind] ?? item.coordinate_kind}{item.complete ? " · 완결" : ""}</small></td>
            <td>{item.author ?? <span className="muted-value">미상</span>}</td>
            <td><b>{item.extension.replace(".", "").toUpperCase()}</b><small>{formatBytes(item.size)}</small></td>
            <td>{item.issues.length === 0 ? <span className="safe-note">없음</span> : item.issues.map((reason) => (
              <small className="blocked" key={reason}>{volumeBlockerLabels[reason] ?? reason}{reason === "duplicate_coordinate" ? ` (${item.same_coordinate_count}개)` : ""}</small>
            ))}</td>
            <td><NavLink to={`/review/titles?search=${encodeURIComponent(item.name)}`} onClick={onClose}>제목 교정</NavLink></td>
          </tr>)}</tbody>
        </table>
      </div>
      {value.duplicate_coordinates.length > 0 && <label className="volume-override">
        <input type="checkbox" checked={allowDuplicateCoordinates} onChange={(event) => {
          setAllowDuplicateCoordinates(event.target.checked);
          setPreview(undefined);
          setConfirmed(false);
        }} />
        <span><strong>같은 좌표 파일도 서로 다른 판본으로 함께 보관</strong><small>중복 격리에서 동일 파일로 확정되지 않은 파일만 대상으로, 삭제하지 않고 같은 작품 폴더의 별도 variant로 연결합니다.</small></span>
      </label>}
      {error && <div className="inline-error">{error}</div>}
      {preview && <div className="volume-tree">
        <div><strong>{preview.item_count}개 중 {preview.moved_count}개 이동</strong><code>{preview.plan_sha256}</code></div>
        {preview.tree.map((path) => <span key={path}>{path}</span>)}
        {preview.blocked_reasons.map((reason) => <small className="blocked" key={reason}>{volumeBlockerLabels[reason] ?? reason}</small>)}
      </div>}
      {preview?.apply_available && <label className="volume-confirm">
        <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
        파일 {preview.item_count}개와 결과 폴더를 확인했습니다
      </label>}
      <footer>
        <button className="button secondary" disabled={busy} onClick={onClose}>닫기</button>
        <button className="button primary" disabled={busy} onClick={refresh}>{busy ? "계산 중…" : "미리보기 갱신"}</button>
        <button className="button danger" disabled={busy || !confirmed || !preview?.apply_available} onClick={apply}>
          {busy ? "등록 중…" : `${preview?.item_count ?? 0}개 묶기`}
        </button>
      </footer>
    </section>
  </div>;
}

function Jobs() {
  const [jobs, setJobs] = useState<JobRecord[]>();
  const [error, setError] = useState("");
  const load = () => api<JobRecord[]>("/api/jobs").then(setJobs).catch((reason) => setError(reason.message));
  useEffect(() => {
    load();
    const id = window.setInterval(load, 1500);
    return () => window.clearInterval(id);
  }, []);
  return (
    <>
      <PageHeader eyebrow="JOB HISTORY" title="작업 이력" description="페이지를 닫거나 서버를 다시 열어도 실행 결과와 복구 근거가 남습니다." action={<button className="button secondary" onClick={load}>새로고침</button>} />
      {error && <div className="inline-error">{error}</div>}
      <section className="panel"><JobList jobs={jobs ?? []} empty="실행 이력이 없습니다." detailed /></section>
    </>
  );
}

function JobList({ jobs, empty, detailed = false }: { jobs: JobRecord[]; empty: string; detailed?: boolean }) {
  if (!jobs.length) return <div className="empty">{empty}</div>;
  return <div className="job-list">{jobs.map((job) => {
    const percent = job.progress.total ? Math.round(job.progress.current / job.progress.total * 100) : 0;
    return <article className="job" key={job.job_id}>
      <div className={`job-state state-${job.state}`}>{job.state}</div>
      <div className="job-main">
        <strong>{job.job_type === "title_requeue" ? "제목 교정 재입고" : job.job_type === "volume_group_merge" ? "분권 묶기" : job.job_type}</strong>
        <span>{job.message}</span>
        {job.progress.total > 0 && <div className="progress"><i style={{ width: `${percent}%` }} /><small>{job.progress.current}/{job.progress.total}</small></div>}
        {job.error && <div className="job-error">{job.error.message}</div>}
        {detailed && job.result && <div className="job-result">완료 결과가 저장되었습니다 · 작업 ID {job.job_id}</div>}
      </div>
      <time>{new Date(job.updated_at).toLocaleString("ko-KR")}</time>
    </article>;
  })}</div>;
}

function Loading() {
  return <div className="loading"><span />데이터를 확인하고 있습니다.</div>;
}

function ErrorPanel({ message, retry }: { message: string; retry: () => void }) {
  return <div className="error-panel"><h2>화면을 불러오지 못했습니다</h2><p>{message}</p><button className="button secondary" onClick={retry}>다시 시도</button></div>;
}
