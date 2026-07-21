import { Fragment, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { NavLink, Navigate, Route, Routes, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { ApiError, api, postJson } from "./api";
import type {
  CatalogItem,
  CatalogListing,
  DashboardData,
  JobEvent,
  JobRecord,
  PlatformStatus,
  ServiceDescriptor,
  TitleCase,
  TitleListing,
  TitlePlan,
  TitlePreview,
  ReviewQueueListing,
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
            <small>file_check 1.3.0</small>
          </div>
        </div>
        <nav>
          <NavLink to="/" end>대시보드</NavLink>
          <NavLink to="/services">서비스</NavLink>
          <NavLink to="/catalog">카탈로그</NavLink>
          <NavLink to="/review/titles">제목 교정</NavLink>
          <NavLink to="/review/volumes">분권 묶기</NavLink>
          <NavLink to="/review/queue">검토 큐</NavLink>
          <NavLink to="/jobs">작업 이력</NavLink>
        </nav>
        <div className="sidebar-note">로컬 전용 · 실제 변경 전 계획 확인</div>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/services" element={<Services />} />
          <Route path="/services/:serviceId" element={<ServiceDetail />} />
          <Route path="/catalog" element={<Catalog />} />
          <Route path="/review/titles" element={<TitleReview />} />
          <Route path="/review/volumes" element={<VolumeReview />} />
          <Route path="/review/queue" element={<ReviewQueue />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/jobs/:jobId" element={<JobDetail />} />
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
  const [services, setServices] = useState<ServiceDescriptor[]>();
  const [error, setError] = useState("");
  const [serviceError, setServiceError] = useState("");
  const load = () => {
    setError("");
    setServiceError("");
    api<DashboardData>("/api/dashboard")
      .then(setData)
      .catch((reason) => setError(reason.message));
    api<ServiceDescriptor[]>("/api/services")
      .then(setServices)
      .catch((reason) => setServiceError(reason.message));
  };
  useEffect(load, []);
  if (error && !data) return <ErrorPanel message={error} retry={load} />;
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
        <strong>{data.database.doctor_ok ? "DB 운영 상태 정상" : `Doctor 문제 ${data.database.doctor_issue_count}건`}</strong>
        <span>{data.database.integrity === "deferred" ? "전체 무결성은 실행 전에 재검증" : `무결성 ${data.database.integrity}`}</span>
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
      <section className="panel next-actions-panel">
        <div className="panel-title"><div><span className="eyebrow">NEXT ACTIONS</span><h2>확인할 일</h2></div><span>{data.next_actions.length}건</span></div>
        {data.next_actions.length ? <div className="next-action-grid">{data.next_actions.map((item) => <NavLink className={`next-action next-action-${item.severity}`} to={item.href} key={item.code}>
          <strong>{item.label}</strong><span>{item.detail}</span><b>열기 →</b>
        </NavLink>)}</div> : <div className="all-clear">현재 바로 확인할 작업이 없습니다.</div>}
      </section>
      <section className="panel service-quick-panel">
        <div className="panel-title">
          <div><span className="eyebrow">QUICK SERVICES</span><h2>원버튼 실행</h2></div>
          <NavLink className="text-link" to="/services">서비스 상세</NavLink>
        </div>
        <div className="quick-service-grid">
          {services?.filter((item) => item.quick_action).map((item) => (
            <article className="quick-service" key={item.id}>
              <div>
                <strong>{item.label}</strong>
                <small>{item.target_label} {formatNumber(item.target_count)}개</small>
              </div>
              <ServiceRunButton service={item} source="dashboard" />
              {!item.ready && <p>{item.blocked_reason}</p>}
            </article>
          ))}
          {!services && !serviceError && <div className="all-clear">서비스 실행 조건 확인 중…</div>}
          {serviceError && <div className="all-clear">서비스 상태를 불러오지 못했습니다: {serviceError}</div>}
        </div>
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

function catalogMetric(item: CatalogItem, platform: "series" | "kakao" | "novelpia"): string {
  const value = item.platforms[platform];
  if (value.status !== "ok") return statusLabel(value.status);
  const metrics = platform === "series"
    ? [["다운", value.download_count], ["평점", value.rating]]
    : platform === "kakao"
      ? [["조회", value.view_count], ["평점", value.rating]]
      : [["조회", value.view_count], ["좋아요", value.recommend_count]];
  return metrics
    .filter((entry) => entry[1] !== null && entry[1] !== undefined)
    .map(([label, metric]) => `${label} ${typeof metric === "number" ? formatNumber(metric) : metric}`)
    .join(" · ") || "확인";
}

function Catalog() {
  const [params, setParams] = useSearchParams();
  const [listing, setListing] = useState<CatalogListing>();
  const [error, setError] = useState("");
  const [draft, setDraft] = useState(params.get("search") ?? "");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const search = params.get("search") ?? "";
  const status = params.get("status") ?? "all";
  const cursor = params.get("cursor") ?? "";
  const load = () => {
    const query = new URLSearchParams({ search, status, limit: "50" });
    if (cursor) query.set("cursor", cursor);
    api<CatalogListing>(`/api/catalog?${query}`).then((value) => { setListing(value); setError(""); }).catch((reason) => setError(reason.message));
  };
  useEffect(load, [search, status, cursor]);
  const update = (next: Record<string, string>) => {
    const value = new URLSearchParams(params);
    Object.entries(next).forEach(([key, item]) => item ? value.set(key, item) : value.delete(key));
    value.delete("cursor");
    setParams(value);
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    update({ search: draft.trim() });
  };
  if (error && !listing) return <ErrorPanel message={error} retry={load} />;
  return <>
    <PageHeader eyebrow="READ-ONLY CATALOG" title="보유 도서 카탈로그" description="현재 house 파일·core title·플랫폼 수집 상태를 한 화면에서 찾습니다. 1.3.0에서는 조회만 가능합니다." action={<span className="readonly-pill">READ ONLY</span>} />
    {error && <div className="inline-error">{error}</div>}
    <div className="toolbar catalog-toolbar">
      <form className="search-form" onSubmit={submit}><input value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="원본 제목·core title·작가 검색" /><button className="button secondary">검색</button></form>
      <select value={status} onChange={(event) => update({ status: event.target.value })}>
        <option value="all">전체 플랫폼 상태</option>
        <option value="found">하나 이상 확인</option>
        <option value="missing">확인된 플랫폼 없음</option>
        <option value="not_found">모두 검색 결과 없음</option>
        <option value="error">오류 포함</option>
      </select>
    </div>
    <section className="table-panel catalog-panel">
      <div className="table-summary"><span>현재 조건 <strong>{formatNumber(listing?.total ?? 0)}</strong>작품</span><span>행을 열면 실제 보유 파일과 마지막 수집 시각을 확인합니다.</span></div>
      {!listing ? <Loading /> : listing.items.length ? <div className="catalog-table-wrap"><table className="catalog-table">
        <thead><tr><th>작품·core title</th><th>보유</th><th>시리즈</th><th>카카오</th><th>노벨피아</th><th>상세</th></tr></thead>
        <tbody>{listing.items.map((item) => {
          const isOpen = expanded.has(item.title_key);
          return <Fragment key={item.title_key}>
            <tr>
              <td><strong>{item.display_title}</strong><code className="core">{item.title_key}</code>{item.author && <small>{item.author}</small>}</td>
              <td><b>{item.file_count}개</b><small>{item.effective_max ? `~${formatNumber(item.effective_max)}${item.unit}${item.complete ? " 완" : ""}` : "범위 미상"}</small></td>
              {(["series", "kakao", "novelpia"] as const).map((platform) => <td key={platform}><StatusBadge status={item.platforms[platform].status} /><span>{catalogMetric(item, platform)}</span>{item.platforms[platform].remote_url && <a href={item.platforms[platform].remote_url ?? undefined} target="_blank" rel="noreferrer">원문 ↗</a>}</td>)}
              <td><button className="button ghost" onClick={() => setExpanded((current) => { const next = new Set(current); if (next.has(item.title_key)) next.delete(item.title_key); else next.add(item.title_key); return next; })}>{isOpen ? "접기" : "파일 보기"}</button></td>
            </tr>
            {isOpen && <tr className="catalog-detail-row"><td colSpan={6}><div className="catalog-detail-grid">
              <section><strong>보유 파일 {item.files.length}개</strong>{item.files.map((file) => <div className="catalog-file" key={file.file_id}><b>{file.name}</b><small title={file.path}>{file.path}</small></div>)}</section>
              <section><strong>플랫폼 수집 근거</strong>{(["series", "kakao", "novelpia"] as const).map((platform) => { const value = item.platforms[platform]; return <div className="catalog-platform-detail" key={platform}><b>{platformLabels[platform]} · {statusLabel(value.status)}</b><small>{value.remote_title ?? "원문 제목 없음"}</small><small>마지막 시도 {value.last_attempt_at ? new Date(value.last_attempt_at).toLocaleString("ko-KR") : "없음"}</small>{value.error_message && <small className="blocked">{value.error_message}</small>}</div>; })}</section>
            </div></td></tr>}
          </Fragment>;
        })}</tbody>
      </table></div> : <div className="empty">조건에 맞는 보유 작품이 없습니다.</div>}
      {listing && <div className="pagination"><button className="button secondary" disabled={!cursor} onClick={() => { const value = new URLSearchParams(params); const previous = Math.max(0, Number(cursor || 0) - listing.limit); if (previous) value.set("cursor", String(previous)); else value.delete("cursor"); setParams(value); }}>이전</button><button className="button secondary" disabled={!listing.next_cursor} onClick={() => { const value = new URLSearchParams(params); if (listing.next_cursor) value.set("cursor", listing.next_cursor); setParams(value); }}>다음</button></div>}
    </section>
  </>;
}

function ReviewQueue() {
  const [params, setParams] = useSearchParams();
  const [listing, setListing] = useState<ReviewQueueListing>();
  const [error, setError] = useState("");
  const [draft, setDraft] = useState(params.get("search") ?? "");
  const search = params.get("search") ?? "";
  const category = params.get("category") ?? "all";
  const physical = params.get("physical") ?? "all";
  const load = () => {
    const query = new URLSearchParams({ search, category, physical, limit: "100" });
    api<ReviewQueueListing>(`/api/review/queue?${query}`).then((value) => { setListing(value); setError(""); }).catch((reason) => setError(reason.message));
  };
  useEffect(load, [search, category, physical]);
  const update = (next: Record<string, string>) => {
    const value = new URLSearchParams(params);
    Object.entries(next).forEach(([key, item]) => item ? value.set(key, item) : value.delete(key));
    setParams(value);
  };
  return <>
    <PageHeader eyebrow="REVIEW EVIDENCE" title="검토 큐·격리 현황" description="DB review와 temp/trash_bin의 관리 파일을 함께 봅니다. 복원·중복 아님·영구 삭제는 1.3.2의 확인형 작업으로 추가됩니다." action={<span className="readonly-pill">READ ONLY</span>} />
    {error && <div className="inline-error">{error}</div>}
    <div className="toolbar">
      <form className="search-form" onSubmit={(event) => { event.preventDefault(); update({ search: draft.trim() }); }}><input value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="파일명·후보 경로 검색" /><button className="button secondary">검색</button></form>
      <select value={category} onChange={(event) => update({ category: event.target.value })}>
        <option value="all">전체 검토 큐</option><option value="database">DB review</option><option value="warning">warning</option><option value="author_conflicts">작가 충돌</option><option value="suspected_duplicates">중복 의심</option><option value="exact_quarantine">정확 중복 격리</option><option value="exact_duplicates">legacy 정확 중복</option>
      </select>
      <select value={physical} onChange={(event) => update({ physical: event.target.value })}>
        <option value="all">전체 보관 상태</option><option value="relation_only">관계 검토 · 미격리</option><option value="quarantined">실제 격리됨</option><option value="queue_missing">격리 경로 확인 필요</option>
      </select>
    </div>
    {listing && <section className="review-queue-summary">
      <button className={physical === "relation_only" ? "active" : ""} onClick={() => update({ physical: "relation_only" })}><span>관계 검토 · 미격리</span><strong>{formatNumber(listing.summary.relation_only)}</strong></button>
      <button className={physical === "quarantined" ? "active" : ""} onClick={() => update({ physical: "quarantined" })}><span>실제 격리됨</span><strong>{formatNumber(listing.summary.quarantined)}</strong></button>
      <button className={physical === "queue_missing" ? "active" : ""} onClick={() => update({ physical: "queue_missing" })}><span>격리 경로 확인</span><strong>{formatNumber(listing.summary.queue_missing)}</strong></button>
    </section>}
    <section className="table-panel review-queue-panel">
      <div className="table-summary"><span>현재 조건 <strong>{formatNumber(listing?.total_visible ?? 0)}</strong>건 · 최대 {formatNumber(listing?.items.length ?? 0)}건 표시</span><span>파일 조작 없음</span></div>
      {!listing ? <Loading /> : listing.items.length ? <div className="review-queue-list">{listing.items.map((item, index) => <article key={`${item.kind}-${item.review_id ?? item.path}-${index}`}>
        <span className={`queue-kind queue-kind-${item.physical_state}`}>{item.physical_state === "relation_only" ? "관계 검토 · 미격리" : item.physical_state === "quarantined" ? "격리됨" : "격리 경로 확인"}</span>
        <div><strong>{item.name ?? fileBasename(item.candidate_path ?? item.path ?? item.queue_path ?? "검토 항목")}</strong><small>{item.category} · {item.state}</small></div>
        <div className="queue-paths">{item.candidate_path && <small>후보: {item.candidate_path}</small>}{item.reference_path && <small>비교: {item.reference_path}</small>}{item.path && <small>격리: {item.path}</small>}{item.queue_path && <small>큐: {item.queue_path}</small>}</div>
        {item.size !== undefined && <b>{formatBytes(item.size)}</b>}
      </article>)}</div> : <div className="empty">조건에 맞는 검토 항목이 없습니다.</div>}
    </section>
  </>;
}

function ServiceRunButton({ service, source }: {
  service: ServiceDescriptor;
  source: "dashboard" | "service_detail";
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const navigate = useNavigate();
  const run = async () => {
    setBusy(true);
    setError("");
    try {
      const job = await postJson<JobRecord>(`/api/services/${service.id}/start`, { source });
      navigate(`/jobs/${job.job_id}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "서비스를 시작하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  };
  return <div className="service-run-control">
    <button
      className="button primary"
      disabled={busy || !service.ready}
      title={service.blocked_reason ?? undefined}
      onClick={run}
    >{busy ? "등록 중…" : "실행"}</button>
    {error && <small className="control-error">{error}</small>}
  </div>;
}

function Services() {
  const [items, setItems] = useState<ServiceDescriptor[]>();
  const [error, setError] = useState("");
  const load = () => {
    setError("");
    api<ServiceDescriptor[]>("/api/services").then(setItems).catch((reason) => setError(reason.message));
  };
  useEffect(load, []);
  if (error) return <ErrorPanel message={error} retry={load} />;
  if (!items) return <Loading />;
  const categories = [...new Set(items.map((item) => item.category))];
  return <>
    <PageHeader
      eyebrow="OPERATION SERVICES · 1.3.0"
      title="서비스"
      description="기존 컨트롤서버와 같은 도메인 로직을 실행합니다. 대상과 쓰기 범위, 사전 검사와 최근 결과를 확인할 수 있습니다."
      action={<button className="button secondary" onClick={load}>상태 새로고침</button>}
    />
    {categories.map((category) => <section className="service-section" key={category}>
      <div className="service-section-title"><span>{category}</span><small>{items.filter((item) => item.category === category).length}개 서비스</small></div>
      <div className="service-grid">
        {items.filter((item) => item.category === category).map((item) => <article className="service-card" key={item.id}>
          <div className="service-card-head">
            <span className={item.ready ? "service-ready" : "service-blocked"}>{item.ready ? "실행 가능" : "대기"}</span>
            <strong>{item.label}</strong>
          </div>
          <p>{item.summary}</p>
          <div className="service-count"><strong>{formatNumber(item.target_count)}</strong><span>{item.target_label}</span></div>
          {!item.ready && <div className="service-reason">{item.blocked_reason}</div>}
          {item.latest_job && <NavLink className="service-latest" to={`/jobs/${item.latest_job.job_id}`}>최근 실행 · {item.latest_job.state}</NavLink>}
          <footer>
            <NavLink className="button secondary" to={`/services/${item.id}`}>자세히</NavLink>
            <ServiceRunButton service={item} source="service_detail" />
          </footer>
        </article>)}
      </div>
    </section>)}
  </>;
}

function ServiceDetail() {
  const { serviceId = "" } = useParams();
  const [service, setService] = useState<ServiceDescriptor>();
  const [error, setError] = useState("");
  const load = () => {
    setError("");
    api<ServiceDescriptor>(`/api/services/${serviceId}`).then(setService).catch((reason) => setError(reason.message));
  };
  useEffect(load, [serviceId]);
  if (error) return <ErrorPanel message={error} retry={load} />;
  if (!service) return <Loading />;
  return <>
    <PageHeader
      eyebrow={`${service.category.toUpperCase()} SERVICE`}
      title={service.label}
      description={service.summary}
      action={<ServiceRunButton service={service} source="service_detail" />}
    />
    <section className="service-detail-grid">
      <article className="panel service-status-panel">
        <span className={service.ready ? "service-ready" : "service-blocked"}>{service.ready ? "실행 가능" : "현재 실행 불가"}</span>
        <strong>{service.target_label} {formatNumber(service.target_count)}개</strong>
        <p>{service.ready ? "운영 기본값으로 즉시 실행할 수 있습니다." : service.blocked_reason}</p>
      </article>
      <article className="panel service-scope-panel">
        <h2>읽는 범위</h2>
        <ul>{service.read_scope.map((value) => <li key={value}>{value}</li>)}</ul>
      </article>
      <article className="panel service-scope-panel">
        <h2>변경하는 범위</h2>
        <ul>{service.write_scope.map((value) => <li key={value}>{value}</li>)}</ul>
      </article>
    </section>
    <section className="panel service-defaults">
      <div className="panel-title"><div><span className="eyebrow">SAFE DEFAULTS</span><h2>이번 버전의 고정 실행 조건</h2></div></div>
      <ul>{service.defaults.map((value) => <li key={value}>{value}</li>)}</ul>
    </section>
    <section className="panel">
      <div className="panel-title"><div><span className="eyebrow">LATEST OUTPUT</span><h2>최근 실행</h2></div><NavLink className="text-link" to="/jobs">전체 이력</NavLink></div>
      {service.latest_job ? <JobList jobs={[service.latest_job]} empty="" detailed /> : <div className="empty">이 서비스의 실행 이력이 없습니다.</div>}
    </section>
  </>;
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
      navigate(`/jobs/${job.job_id}`);
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
  approved_duplicate_coordinate: "같은 작품으로 승인된 동일 권 판본",
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
                    {item.unapproved_duplicate_coordinates.length > 0 && <small className="collision">중복 좌표 {item.unapproved_duplicate_coordinates.join(", ")}</small>}
                    {item.approved_duplicate_coordinates.length > 0 && <small className="safe-note">승인된 동일 권 판본 {item.approved_duplicate_coordinates.join(", ")}</small>}
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
      navigate(`/jobs/${job.job_id}`);
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
              <small className={reason === "approved_duplicate_coordinate" ? "safe-note" : "blocked"} key={reason}>{volumeBlockerLabels[reason] ?? reason}{reason === "duplicate_coordinate" || reason === "approved_duplicate_coordinate" ? ` (${item.same_coordinate_count}개)` : ""}</small>
            ))}</td>
            <td><NavLink to={`/review/titles?search=${encodeURIComponent(item.name)}`} onClick={onClose}>제목 교정</NavLink></td>
          </tr>)}</tbody>
        </table>
      </div>
      {value.unapproved_duplicate_coordinates.length > 0 && <label className="volume-override">
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
        {preview.preserved_source_items.length > 0 && <div className="volume-preserved">
          <strong>선택하지 않아 원래 폴더에 남겨둘 항목 {preview.preserved_source_items.length}개</strong>
          {preview.preserved_source_items.slice(0, 8).map((path) => <small key={path}>{path}</small>)}
          {preview.preserved_source_items.length > 8 && <small>외 {preview.preserved_source_items.length - 8}개</small>}
        </div>}
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

function jobLabel(jobType: string): string {
  const labels: Record<string, string> = {
    title_requeue: "제목 교정 재입고",
    volume_group_merge: "분권 묶기",
    service_folderling: "Folderling 실제 입고",
    service_scanner: "Scanner / index 갱신",
    service_platform_update: "플랫폼 인기 DB 업데이트",
    service_platform_retry: "플랫폼 실패 결과 재검사",
    service_platform_refresh: "기존 인기값 상향 갱신",
    service_novelpia_auth_retry: "노벨피아 인증 누락 재검사",
    service_google_sheet: "Google Sheet 동기화"
  };
  return labels[jobType] ?? jobType;
}

const folderlingPhaseLabels: Record<string, string> = {
  preflight_start: "사전 검사",
  preflight_result: "사전 검사 완료",
  preflight_failed: "사전 검사 실패",
  actual_run_started: "안전 실행 시작",
  review_actions_result: "기존 검토 처리함 반영",
  workflow_started: "입고 workflow 시작",
  legacy_pass_skipped: "legacy pass 보류",
  dedup_start: "중복 판정 시작",
  snapshot_result: "house snapshot 확인",
  dedup_result: "중복 판정 완료",
  intake_start: "temp 입고 시작",
  intake_result: "temp 입고 완료",
  index_start: "index 갱신 시작",
  index_result: "index 갱신 완료",
  folderling_summary: "결과 집계",
  final_doctor_result: "최종 무결성 검사",
  actual_run_finished: "안전 실행 종료",
  workflow_failed: "입고 workflow 실패"
};

const folderlingStatusLabels: Record<string, string> = {
  ingested: "입고",
  pass_ingested: "승인 입고",
  exact_duplicate: "정확 중복",
  suspected_duplicate: "검토 격리",
  warning: "경고 보류",
  author_conflict: "작가 충돌",
  metadata_only: "메타데이터 판정",
  skipped: "제외",
  failed: "실패",
  empty_directory_cleaned: "빈 폴더 정리"
};

const folderlingReasonLabels: Record<string, string> = {
  journaled_house_ingest: "journal을 남기고 house에 입고",
  exact_fingerprint: "파일 내용·크기 fingerprint 일치",
  volume_coordinate_conflict: "기존 파일과 같은 권 좌표",
  volume_coordinate_hold_failed: "같은 권 좌표 보류 처리 실패",
  excluded_source_item: "운영 보조 파일 또는 제외 폴더",
  unsupported_extension: "지원하지 않는 확장자",
  source_missing_or_not_regular: "파일이 없거나 일반 파일이 아님",
  empty_normalized_name: "정규화 후 파일명이 비어 있음",
  empty_directory: "내용 없는 temp 폴더"
};

function eventText(value: unknown): string {
  return value === null || value === undefined ? "" : String(value);
}

function eventPaths(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}

function fileBasename(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.at(-1) ?? path;
}

function FolderlingJobOutput({ events, result }: { events: JobEvent[]; result: Record<string, unknown> | null }) {
  const [statusFilter, setStatusFilter] = useState("all");
  const [search, setSearch] = useState("");
  const fileEvents = useMemo(
    () => events.filter((event) => event.phase === "file_result"),
    [events]
  );
  const timelineEvents = useMemo(
    () => events.filter((event) => event.phase !== "file_result"),
    [events]
  );
  const counts = useMemo(() => fileEvents.reduce<Record<string, number>>((acc, event) => {
    const status = eventText(event.status) || "unknown";
    acc[status] = (acc[status] ?? 0) + 1;
    return acc;
  }, {}), [fileEvents]);
  const statusOptions = useMemo(
    () => Object.keys(counts).sort((left, right) => (folderlingStatusLabels[left] ?? left).localeCompare(folderlingStatusLabels[right] ?? right, "ko")),
    [counts]
  );
  const visibleFiles = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase();
    return fileEvents.filter((event) => {
      const status = eventText(event.status) || "unknown";
      if (statusFilter !== "all" && status !== statusFilter) return false;
      if (!needle) return true;
      const haystack = [
        event.source_name,
        event.source_path,
        event.destination_path,
        event.reason,
        event.error,
        event.next_action,
        ...eventPaths(event.existing_paths)
      ].map(eventText).join(" ").toLocaleLowerCase();
      return haystack.includes(needle);
    });
  }, [fileEvents, search, statusFilter]);
  const moveCount = Number(result?.move_count ?? counts.ingested ?? 0) + Number(result?.pass_count ?? counts.pass_ingested ?? 0);
  const duplicateCount = Number((result?.dedup_summary as Record<string, unknown> | undefined)?.exact_count ?? counts.exact_duplicate ?? 0);
  const reviewCount = Number(
    (result?.dedup_summary as Record<string, unknown> | undefined)?.review_queue_move_count
    ?? (counts.suspected_duplicate ?? 0) + (counts.author_conflict ?? 0)
  ) + Number(result?.volume_conflict_hold_count ?? counts.warning ?? 0);
  const failureCount = Number(result?.failure_count ?? counts.failed ?? 0);

  return <>
    <section className="folderling-summary-grid" aria-label="Folderling 결과 요약">
      <article className="panel"><span>house 입고</span><strong>{formatNumber(moveCount)}</strong><small>정상 이동</small></article>
      <article className="panel"><span>정확 중복</span><strong>{formatNumber(duplicateCount)}</strong><small>기존 파일 유지</small></article>
      <article className="panel"><span>검토 필요</span><strong>{formatNumber(reviewCount)}</strong><small>격리·충돌·경고</small></article>
      <article className="panel"><span>실패</span><strong>{formatNumber(failureCount)}</strong><small>재확인 필요</small></article>
    </section>
    <section className="panel folderling-timeline-panel">
      <div className="panel-title"><div><span className="eyebrow">FOLDERLING TIMELINE</span><h2>입고 단계</h2></div><span>{timelineEvents.length}개 단계 이벤트</span></div>
      {timelineEvents.length ? <div className="folderling-timeline">{timelineEvents.map((event, index) => {
        const status = eventText(event.status) || "running";
        return <article key={`${event.recorded_at}-${event.phase}-${index}`}>
          <i className={`folderling-dot folderling-dot-${status}`} />
          <div><strong>{folderlingPhaseLabels[event.phase] ?? event.phase}</strong><small>{new Date(event.recorded_at).toLocaleTimeString("ko-KR")}</small></div>
          <span className={`folderling-status folderling-status-${status}`}>{status}</span>
          {Boolean(event.fallback_reason) && <p>{eventText(event.fallback_reason)}</p>}
          {Boolean(event.error) && <p className="blocked">{eventText(event.error)}</p>}
        </article>;
      })}</div> : <div className="empty">아직 Folderling 단계 이벤트가 없습니다.</div>}
    </section>
    <section className="panel folderling-results-panel">
      <div className="panel-title"><div><span className="eyebrow">FILE RESULTS</span><h2>파일별 판정</h2></div><span>{visibleFiles.length}/{fileEvents.length}개 표시</span></div>
      <div className="folderling-toolbar">
        <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="파일명·경로·판정 검색" />
        <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
          <option value="all">모든 판정</option>
          {statusOptions.map((status) => <option key={status} value={status}>{folderlingStatusLabels[status] ?? status} ({counts[status]})</option>)}
        </select>
      </div>
      {visibleFiles.length ? <div className="folderling-table-wrap"><table className="folderling-result-table">
        <thead><tr><th>판정</th><th>원본 후보</th><th>기존 유지·비교 대상</th><th>목적지·다음 조치</th></tr></thead>
        <tbody>{visibleFiles.map((event, index) => {
          const status = eventText(event.status) || "unknown";
          const sourcePath = eventText(event.source_path);
          const destinationPath = eventText(event.destination_path);
          const existingPaths = eventPaths(event.existing_paths);
          const reason = eventText(event.reason);
          return <tr key={`${event.recorded_at}-${sourcePath}-${index}`}>
            <td><span className={`folderling-status folderling-status-${status}`}>{folderlingStatusLabels[status] ?? status}</span><small>{folderlingReasonLabels[reason] ?? reason}</small></td>
            <td><strong title={sourcePath}>{eventText(event.source_name) || fileBasename(sourcePath)}</strong><small title={sourcePath}>{sourcePath}</small></td>
            <td>{existingPaths.length ? existingPaths.map((path) => <div className="folderling-path" key={path}><strong>{fileBasename(path)}</strong><small title={path}>{path}</small></div>) : <span className="muted-value">없음</span>}</td>
            <td>{destinationPath ? <div className="folderling-path"><strong>{fileBasename(destinationPath)}</strong><small title={destinationPath}>{destinationPath}</small></div> : <span className="muted-value">이동 없음</span>}{Boolean(event.next_action) && <em>{eventText(event.next_action)}</em>}{Boolean(event.error) && <em className="blocked">{eventText(event.error)}</em>}</td>
          </tr>;
        })}</tbody>
      </table></div> : <div className="empty">조건에 맞는 파일 판정이 없습니다.</div>}
    </section>
  </>;
}

function formatDuration(value: unknown): string {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "-";
  if (seconds < 60) return `${Math.round(seconds)}초`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = Math.round(seconds % 60);
  return [hours ? `${hours}시간` : "", minutes ? `${minutes}분` : "", `${rest}초`].filter(Boolean).join(" ");
}

const platformPhaseLabels: Record<string, string> = {
  validating: "실행 준비",
  file_analysis: "파일 분석 DB 동기화",
  sync_start: "카탈로그 제목 동기화",
  start: "신규·미수집 조회 시작",
  progress: "플랫폼 조회 진행",
  auth_start: "인증 노벨피아 조회 시작",
  auth_progress: "인증 노벨피아 조회 진행",
  existing_start: "기존 인기값 갱신 시작",
  existing_progress: "기존 인기값 갱신 진행",
  platform_result: "검증·저장 완료",
  job_failed: "작업 실패",
  job_interrupted: "서버 중단 감지"
};

function PlatformJobOutput({ events, result }: { events: JobEvent[]; result: Record<string, unknown> | null }) {
  const progressEvents = events.filter((event) => ["progress", "auth_progress", "existing_progress"].includes(event.phase));
  const latestProgress = progressEvents.at(-1);
  const statusCounts = (result?.status_counts ?? latestProgress?.status_counts ?? {}) as Record<string, unknown>;
  const outcomeCounts = (result?.outcome_counts ?? latestProgress?.outcome_counts ?? {}) as Record<string, unknown>;
  const selectedTitles = Number(result?.selected_titles ?? latestProgress?.selected_titles ?? 0);
  const selectedPlatforms = Number(result?.selected_platforms ?? latestProgress?.selected_platforms ?? 0);
  const resultEvent = [...events].reverse().find((event) => event.phase === "platform_result");
  const elapsed = resultEvent?.elapsed_seconds ?? latestProgress?.elapsed_seconds;
  const eta = latestProgress?.eta_seconds;
  const cards = outcomeCounts.updated !== undefined ? [
    ["갱신 작품", selectedTitles, `${selectedPlatforms.toLocaleString("ko-KR")}개 플랫폼`],
    ["상향 반영", Number(outcomeCounts.updated ?? 0), "기존 값보다 증가"],
    ["변경 없음", Number(outcomeCounts.unchanged ?? 0), "기존 값 유지"],
    ["오류", Number(outcomeCounts.error ?? 0), "원본 값 보존"]
  ] : [
    ["조회 작품", selectedTitles, `${selectedPlatforms.toLocaleString("ko-KR")}개 플랫폼`],
    ["확인", Number(statusCounts.ok ?? 0), "ok"],
    ["검색 결과 없음", Number(statusCounts.not_found ?? 0), "not_found"],
    ["오류", Number(statusCounts.error ?? 0), "error"]
  ];
  return <>
    <section className="platform-summary-grid">
      {cards.map(([label, value, note]) => <article className="panel" key={String(label)}><span>{label}</span><strong>{formatNumber(Number(value))}</strong><small>{note}</small></article>)}
    </section>
    <section className="panel platform-progress-panel">
      <div className="panel-title"><div><span className="eyebrow">PLATFORM PROGRESS</span><h2>수집 진행 근거</h2></div><span>경과 {formatDuration(elapsed)}{eta !== null && eta !== undefined ? ` · 예상 잔여 ${formatDuration(eta)}` : ""}</span></div>
      <div className="platform-facts">
        <span>인증 노벨피아 시도 <strong>{formatNumber(Number(result?.authenticated_novelpia_attempts ?? 0))}</strong></span>
        <span>자동 재로그인 <strong>{formatNumber(Number(result?.authenticated_novelpia_relogins ?? 0))}</strong></span>
        {Boolean(result?.schema_backup) && <span>DB backup <strong title={eventText(result?.schema_backup)}>{fileBasename(eventText(result?.schema_backup))}</strong></span>}
      </div>
      {events.length ? <div className="platform-event-list">{events.map((event, index) => {
        const current = Number(event.completed_titles ?? event.completed ?? 0);
        const total = Number(event.selected_titles ?? event.total ?? 0);
        const percent = Number(event.percent ?? (total ? current / total * 100 : 0));
        return <article key={`${event.recorded_at}-${event.phase}-${index}`}>
          <time>{new Date(event.recorded_at).toLocaleTimeString("ko-KR")}</time>
          <strong>{platformPhaseLabels[event.phase] ?? event.phase}</strong>
          <div>{total > 0 && <><span>{formatNumber(current)}/{formatNumber(total)}</span><div className="progress"><i style={{ width: `${Math.min(100, percent)}%` }} /></div></>}</div>
          {Boolean(event.error_message) && <small className="blocked">{eventText(event.error_message)}</small>}
        </article>;
      })}</div> : <div className="empty">아직 플랫폼 진행 이벤트가 없습니다.</div>}
    </section>
  </>;
}

const sheetPhaseLabels: Record<string, string> = {
  validating: "실행 준비",
  sheet_snapshot: "SQLite 읽기 전용 snapshot",
  sheet_write_start: "임시 탭 쓰기 시작",
  sheet_temp_tabs_created: "임시 탭 생성",
  sheet_values_written: "값 쓰기 완료",
  sheet_links_written: "하이퍼링크 쓰기 완료",
  sheet_swap_completed: "공개 탭 교체 완료",
  sheet_result: "동기화 검증 완료",
  job_failed: "동기화 실패",
  job_interrupted: "서버 중단 감지"
};

function SheetJobOutput({ events, result }: { events: JobEvent[]; result: Record<string, unknown> | null }) {
  const resultEvent = [...events].reverse().find((event) => event.phase === "sheet_result");
  return <>
    <section className="sheet-summary-grid">
      <article className="panel"><span>도서 목록</span><strong>{formatNumber(Number(result?.works_rows ?? 0))}</strong><small>작품 행</small></article>
      <article className="panel"><span>수집 오류</span><strong>{formatNumber(Number(result?.error_rows ?? 0))}</strong><small>error 행</small></article>
      <article className="panel"><span>방향</span><strong>단방향</strong><small>SQLite는 읽기 전용</small></article>
      <article className="panel"><span>경과</span><strong>{formatDuration(resultEvent?.elapsed_seconds)}</strong><small>{eventText(result?.synced_at) || "동기화 시각 대기"}</small></article>
    </section>
    <section className="panel sheet-timeline-panel">
      <div className="panel-title"><div><span className="eyebrow">SHEET TIMELINE</span><h2>안전 교체 단계</h2></div><span>기존 Spreadsheet 링크 유지</span></div>
      <div className="sheet-timeline">{events.map((event, index) => <article key={`${event.recorded_at}-${event.phase}-${index}`}>
        <i className={`folderling-dot folderling-dot-${eventText(event.status) || (event.phase === "job_failed" ? "failed" : "succeeded")}`} />
        <div><strong>{sheetPhaseLabels[event.phase] ?? event.phase}</strong><small>{new Date(event.recorded_at).toLocaleTimeString("ko-KR")}</small></div>
        {event.works_rows !== undefined && <span>도서 {formatNumber(Number(event.works_rows))}행</span>}
        {event.error_rows !== undefined && <span>오류 {formatNumber(Number(event.error_rows))}행</span>}
        {Boolean(event.error_message) && <p className="blocked">{eventText(event.error_message)}</p>}
      </article>)}</div>
    </section>
  </>;
}

function JobDetail() {
  const { jobId = "" } = useParams();
  const [job, setJob] = useState<JobRecord>();
  const [log, setLog] = useState("");
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const load = () => {
    Promise.all([
      api<JobRecord>(`/api/jobs/${jobId}`),
      api<{ job_id: string; text: string }>(`/api/jobs/${jobId}/log`),
      api<{ job_id: string; items: JobEvent[] }>(`/api/jobs/${jobId}/events`)
    ]).then(([record, logResult, eventResult]) => {
      setJob(record);
      setLog(logResult.text);
      setEvents(eventResult.items);
      setError("");
    }).catch((reason) => setError(reason.message));
  };
  useEffect(() => {
    load();
  }, [jobId]);
  useEffect(() => {
    if (!job || ["succeeded", "failed", "needs_review", "interrupted"].includes(job.state)) return;
    const id = window.setInterval(load, 1500);
    return () => window.clearInterval(id);
  }, [job?.state, jobId]);
  if (error && !job) return <ErrorPanel message={error} retry={load} />;
  if (!job) return <Loading />;
  const percent = job.progress.total ? Math.round(job.progress.current / job.progress.total * 100) : 0;
  const visibleLog = filter
    ? log.split("\n").filter((line) => line.toLocaleLowerCase().includes(filter.toLocaleLowerCase())).join("\n")
    : log;
  const copy = async () => {
    await navigator.clipboard.writeText(visibleLog);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };
  return <>
    <PageHeader
      eyebrow="JOB OUTPUT"
      title={jobLabel(job.job_type)}
      description={`작업 ID ${job.job_id}`}
      action={<NavLink className="button secondary" to="/jobs">작업 이력</NavLink>}
    />
    {error && <div className="inline-error">{error}</div>}
    <section className="job-detail-summary">
      <article className="panel"><span>상태</span><strong className={`state-text state-text-${job.state}`}>{job.state}</strong><small>{job.message}</small></article>
      <article className="panel"><span>현재 단계</span><strong>{job.stage}</strong><small>{job.updated_at ? new Date(job.updated_at).toLocaleString("ko-KR") : "-"}</small></article>
      <article className="panel"><span>진행률</span><strong>{job.progress.total ? `${percent}%` : "대기"}</strong><small>{job.progress.current}/{job.progress.total}</small></article>
    </section>
    {job.progress.total > 0 && <div className="progress job-detail-progress"><i style={{ width: `${percent}%` }} /><small>{job.progress.current}/{job.progress.total}</small></div>}
    {job.error && <section className="inline-error"><strong>{job.error.code}</strong><div>{job.error.message}</div></section>}
    {job.job_type === "service_folderling" && <FolderlingJobOutput events={events} result={job.result} />}
    {(job.job_type.startsWith("service_platform_") || job.job_type === "service_novelpia_auth_retry") && <PlatformJobOutput events={events} result={job.result} />}
    {job.job_type === "service_google_sheet" && <SheetJobOutput events={events} result={job.result} />}
    <details className="panel event-panel" open={job.job_type !== "service_folderling"}>
      <summary className="panel-title"><div><span className="eyebrow">STRUCTURED EVENTS</span><h2>전체 구조화 이벤트</h2></div><span>{events.length}개 이벤트</span></summary>
      {events.length ? <div className="event-list">{events.map((event, index) => <article key={`${event.recorded_at}-${index}`}>
        <time>{new Date(event.recorded_at).toLocaleTimeString("ko-KR")}</time>
        <strong>{String(event.phase ?? "event")}</strong>
        <code>{JSON.stringify(event)}</code>
      </article>)}</div> : <div className="empty">아직 구조화 이벤트가 없습니다.</div>}
    </details>
    {job.result && <section className="panel result-panel">
      <div className="panel-title"><div><span className="eyebrow">RESULT</span><h2>완료 결과</h2></div></div>
      <pre>{JSON.stringify(job.result, null, 2)}</pre>
    </section>}
    <section className="panel log-panel">
      <div className="panel-title">
        <div><span className="eyebrow">RAW LOG</span><h2>전체 원본 로그</h2></div>
        <div className="log-actions">
          <button className="button secondary" onClick={copy}>{copied ? "복사됨" : "복사"}</button>
          <a className="button secondary" href={`/api/jobs/${job.job_id}/log/download`}>다운로드</a>
        </div>
      </div>
      <input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="로그에서 검색" />
      <pre>{visibleLog || "아직 로그가 없습니다."}</pre>
    </section>
  </>;
}

function JobList({ jobs, empty, detailed = false }: { jobs: JobRecord[]; empty: string; detailed?: boolean }) {
  if (!jobs.length) return <div className="empty">{empty}</div>;
  return <div className="job-list">{jobs.map((job) => {
    const percent = job.progress.total ? Math.round(job.progress.current / job.progress.total * 100) : 0;
    return <NavLink className="job job-link" to={`/jobs/${job.job_id}`} key={job.job_id}>
      <div className={`job-state state-${job.state}`}>{job.state}</div>
      <div className="job-main">
        <strong>{jobLabel(job.job_type)}</strong>
        <span>{job.message}</span>
        {job.progress.total > 0 && <div className="progress"><i style={{ width: `${percent}%` }} /><small>{job.progress.current}/{job.progress.total}</small></div>}
        {job.error && <div className="job-error">{job.error.message}</div>}
        {detailed && job.result && <div className="job-result">완료 결과가 저장되었습니다 · 작업 ID {job.job_id}</div>}
      </div>
      <time>{new Date(job.updated_at).toLocaleString("ko-KR")}</time>
    </NavLink>;
  })}</div>;
}

function Loading() {
  return <div className="loading"><span />데이터를 확인하고 있습니다.</div>;
}

function ErrorPanel({ message, retry }: { message: string; retry: () => void }) {
  return <div className="error-panel"><h2>화면을 불러오지 못했습니다</h2><p>{message}</p><button className="button secondary" onClick={retry}>다시 시도</button></div>;
}
