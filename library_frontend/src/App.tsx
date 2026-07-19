import { useEffect, useMemo, useRef, useState } from "react";
import { NavLink, Navigate, Route, Routes, useNavigate } from "react-router-dom";

import { ApiError, api, postJson } from "./api";
import type {
  DashboardData,
  JobRecord,
  PlatformStatus,
  TitleCase,
  TitleListing,
  TitlePlan,
  TitlePreview
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
            <small>file_check 1.2.8</small>
          </div>
        </div>
        <nav>
          <NavLink to="/" end>대시보드</NavLink>
          <NavLink to="/review/titles">제목 교정</NavLink>
          <span className="nav-disabled">분권 묶기 <small>1.2.9</small></span>
          <NavLink to="/jobs">작업 이력</NavLink>
        </nav>
        <div className="sidebar-note">로컬 전용 · 실제 변경 전 계획 확인</div>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/review/titles" element={<TitleReview />} />
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
  const [listing, setListing] = useState<TitleListing>();
  const [search, setSearch] = useState("");
  const [submittedSearch, setSubmittedSearch] = useState("");
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
          <input value={value} disabled={!item.editable} onChange={(event) => requestPreview(event.target.value)} placeholder="확장자 제외 새 파일명" />
          <span>{item.extension}</span>
        </div>
        {draft?.loading && <small className="muted">분석 중…</small>}
        {draft?.error && <small className="blocked">{draft.error}</small>}
        {draft?.preview?.blocked_reasons.map((reason) => <small className="blocked" key={reason}>{reason}</small>)}
      </td>
      <td>
        {draft?.preview ? <>
          <code className={draft.preview.runnable ? "core core-new" : "core core-bad"}>{draft.preview.after_core_title || "-"}</code>
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
        <p>실제 파일명이 변경되고 기존 DB 행은 비활성 이력으로 남습니다. 다음 Folderling에서 전체 중복처리를 다시 수행합니다.</p>
        <div className="plan-summary">
          <span><small>대상</small><strong>{plan.item_count}</strong></span>
          <span><small>차단</small><strong>{plan.blocked_count}</strong></span>
          <span className="sha"><small>Plan SHA-256</small><code>{plan.plan_sha256}</code></span>
        </div>
        <div className="plan-list">
          {plan.items.map((item) => <div key={item.file_id}><span>{item.current_name}</span><b>→</b><strong>{item.candidate_name}</strong></div>)}
        </div>
        <footer>
          <button className="button secondary" disabled={busy} onClick={onClose}>취소</button>
          <button className="button danger" disabled={busy || !plan.runnable} onClick={onApply}>{busy ? "등록 중…" : "확인하고 실행"}</button>
        </footer>
      </section>
    </div>
  );
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
        <strong>{job.job_type === "title_requeue" ? "제목 교정 재입고" : job.job_type}</strong>
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
