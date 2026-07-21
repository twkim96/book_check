import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { NavLink, useSearchParams } from "react-router-dom";

import { api } from "./api";
import type {
  ExplorerComparison,
  ExplorerFile,
  ExplorerFileDetail,
  ExplorerFileListing,
  ExplorerFolder,
  ExplorerFolderDetail,
  ExplorerFolderListing,
  ExplorerHistoryItem,
  ExplorerQuarantineListing
} from "./types";

export type CatalogTab = "works" | "files" | "folders" | "quarantine";

const tabLabels: Array<[CatalogTab, string]> = [
  ["works", "작품"],
  ["files", "파일"],
  ["folders", "폴더"],
  ["quarantine", "격리"]
];

function formatNumber(value: number): string {
  return new Intl.NumberFormat("ko-KR").format(value);
}

function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function coordinate(item: Partial<ExplorerFile>): string {
  if (item.coordinate_kind === "volume" && item.volume_num !== null) {
    return `${item.volume_num}${item.volume_den && item.volume_den !== 1 ? `/${item.volume_den}` : ""}권`;
  }
  if (item.coordinate_kind === "part" && item.part_num !== null) {
    return `${item.part_num}${item.part_den && item.part_den !== 1 ? `/${item.part_den}` : ""}부`;
  }
  if (item.coordinate_kind === "symbol") return item.coordinate_symbol ?? "기호 좌표";
  if (item.coordinate_kind === "episode") return `${item.episode_start ?? "?"}~${item.episode_end ?? "?"}화`;
  return item.coordinate_raw ?? "좌표 없음";
}

export function CatalogTabs({ active }: { active: CatalogTab }) {
  return <nav className="catalog-tabs" aria-label="카탈로그 분류">
    {tabLabels.map(([tab, label]) => <NavLink className={active === tab ? "active" : ""} key={tab} to={`/catalog?tab=${tab}`}>{label}</NavLink>)}
  </nav>;
}

function Header({ title, description }: { title: string; description: string }) {
  return <header className="page-header">
    <div><span className="eyebrow">READ-ONLY EXPLORER · 1.3.1</span><h1>{title}</h1><p>{description}</p></div>
    <span className="readonly-pill">READ ONLY</span>
  </header>;
}

function Modal({ children, close, wide = false }: { children: ReactNode; close: () => void; wide?: boolean }) {
  useEffect(() => {
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") close(); };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [close]);
  return <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) close(); }}>
    <section className={`modal explorer-modal${wide ? " explorer-modal-wide" : ""}`}>{children}</section>
  </div>;
}

function Pager({ cursor, next, limit, setCursor }: { cursor: string; next: string | null; limit: number; setCursor: (value: string) => void }) {
  return <div className="pagination">
    <button className="button secondary" disabled={!cursor} onClick={() => setCursor(String(Math.max(0, Number(cursor || 0) - limit)))}>이전</button>
    <button className="button secondary" disabled={!next} onClick={() => setCursor(next ?? "")}>다음</button>
  </div>;
}

function History({ label, items }: { label: string; items: ExplorerHistoryItem[] }) {
  return <section className="explorer-history"><h3>{label} <small>{items.length}</small></h3>{items.length ? items.map((item, index) => <article key={`${label}-${index}`}>
    <strong>{item.classification ?? item.verdict ?? item.action ?? item.state ?? "기록"}</strong>
    <small>{item.run_id ? `${item.run_id} · ` : ""}{item.updated_at ?? item.decided_at ?? item.created_at ?? "시각 없음"}</small>
    {item.note && <p>{item.note}</p>}
  </article>) : <p className="muted">기록 없음</p>}</section>;
}

function FileInspector({ fileId, close, compareWith }: { fileId: string; close: () => void; compareWith: (fileId: string) => void }) {
  const [detail, setDetail] = useState<ExplorerFileDetail>();
  const [error, setError] = useState("");
  useEffect(() => { api<ExplorerFileDetail>(`/api/explorer/files/${encodeURIComponent(fileId)}`).then(setDetail).catch((reason) => setError(reason.message)); }, [fileId]);
  return <Modal close={close} wide><div className="explorer-modal-top"><div><span className="eyebrow">FILE INSPECTOR</span><h2>{detail?.file.name ?? "파일 상세 확인"}</h2></div><button className="button secondary" onClick={close}>닫기</button></div>
    {error && <div className="inline-error">{error}</div>}
    {!detail ? !error && <div className="loading"><span />상세 정보를 확인하고 있습니다.</div> : <>
      <div className="explorer-fact-grid">
        <article><span>core title</span><strong>{detail.file.core_title ?? "없음"}</strong><small>{detail.file.readable_title ?? "읽기 제목 없음"}</small></article>
        <article><span>좌표</span><strong>{coordinate(detail.file)}</strong><small>{detail.file.coordinate_kind ?? "미분류"}</small></article>
        <article><span>관계</span><strong>작품 {detail.file.work_bucket_id ?? "-"} · 변형 {detail.file.variant_id ?? "-"}</strong><small>{detail.file.assignment_state} · {detail.file.variant_kind ?? "미분류"}</small></article>
        <article><span>파일 상태</span><strong>{detail.file.active ? "활성" : "비활성"} · {detail.file.source}</strong><small>{formatBytes(detail.file.size)} · {detail.file.representative ? "대표" : "일반"}{detail.file.protected ? " · 보호" : ""}</small></article>
      </div>
      <code className="explorer-path">{detail.file.canonical_path}</code>
      {detail.file.retired_virtual_path && <div className="explorer-warning">제목 교정 전 가상 이력 경로입니다. 실제 폴더나 격리 파일이 아닙니다.</div>}
      {detail.actions.blocked_reasons.length > 0 && <div className="explorer-blockers"><strong>현재 변경이 차단되는 이유</strong>{detail.actions.blocked_reasons.map((reason) => <span key={reason}>{reason}</span>)}</div>}
      <section className="panel explorer-fingerprint"><h3>본문 지문</h3><dl><dt>상태</dt><dd>{detail.file.fingerprint_status ?? "없음"}</dd><dt>raw SHA</dt><dd>{detail.file.raw_sha256 ?? "없음"}</dd><dt>normalized SHA</dt><dd>{detail.file.normalized_sha256 ?? "없음"}</dd><dt>정규화 길이</dt><dd>{detail.file.normalized_length ? formatNumber(detail.file.normalized_length) : "-"}</dd></dl></section>
      {detail.same_coordinate.length > 0 && <section className="panel explorer-same-coordinate"><h3>같은 작품 좌표 {detail.same_coordinate.length}개</h3>{detail.same_coordinate.map((item) => <button key={item.file_id} onClick={() => compareWith(item.file_id)}><strong>{item.canonical_path.split("/").pop()}</strong><small>{formatBytes(item.size)} · {item.author ?? "작가 미상"}</small></button>)}</section>}
      <div className="explorer-history-grid"><History label="검토" items={detail.reviews} /><History label="사람 결정" items={detail.decisions} /><History label="파일 작업" items={detail.operations} /></div>
      <footer className="explorer-action-footer"><span>격리·이동은 {detail.actions.future_version}에서 확인형 작업으로 제공됩니다.</span>{detail.actions.title_correction && <NavLink className="button secondary" to={`/review/titles?search=${encodeURIComponent(detail.file.name)}`}>제목 교정에서 열기</NavLink>}<button className="button secondary" disabled>격리/이동</button></footer>
    </>}
  </Modal>;
}

function CompareModal({ leftId, rightId, close }: { leftId: string; rightId: string; close: () => void }) {
  const [result, setResult] = useState<ExplorerComparison>();
  const [error, setError] = useState("");
  useEffect(() => { api<ExplorerComparison>(`/api/explorer/compare?left=${encodeURIComponent(leftId)}&right=${encodeURIComponent(rightId)}`).then(setResult).catch((reason) => setError(reason.message)); }, [leftId, rightId]);
  const bool = (value: boolean) => <span className={value ? "explorer-match" : "explorer-different"}>{value ? "일치" : "다름"}</span>;
  return <Modal close={close} wide><div className="explorer-modal-top"><div><span className="eyebrow">PAIR COMPARE</span><h2>두 파일 관계 비교</h2></div><button className="button secondary" onClick={close}>닫기</button></div>
    {error && <div className="inline-error">{error}</div>}
    {!result ? !error && <div className="loading"><span />비교 근거를 불러오고 있습니다.</div> : <>
      <div className="explorer-compare-files">{([result.left, result.right] as const).map((item, index) => <article key={item.file_id}><span>{index === 0 ? "LEFT" : "RIGHT"}</span><strong>{item.name}</strong><small>{item.core_title ?? "core 없음"} · {coordinate(item)}</small><code>{item.canonical_path}</code></article>)}</div>
      <div className="explorer-compare-grid"><span>core title {bool(result.comparison.same_core_title)}</span><span>좌표 {bool(result.comparison.same_coordinate)}</span><span>작가 {bool(result.comparison.same_author)}</span><span>원본 SHA {bool(result.comparison.same_raw_sha256)}</span><span>정규화 SHA {bool(result.comparison.same_normalized_sha256)}</span><span>크기 차이 <b>{formatBytes(Math.abs(result.comparison.size_delta))}</b></span></div>
      <div className="explorer-history-grid"><History label="최근 검토" items={result.latest_review ? [result.latest_review] : []} /><History label="최근 사람 결정" items={result.latest_decision ? [result.latest_decision] : []} /><History label="본문 비교 캐시" items={result.latest_pair_cache ? [result.latest_pair_cache] : []} /></div>
      <section className="explorer-future-verdict"><strong>1.3.2 관계 판정 미리보기</strong>{result.relationship_preview.available_verdicts.map((value) => <span key={value}>{value}</span>)}<button className="button primary" disabled>판정 저장</button></section>
    </>}
  </Modal>;
}

function FileCatalog() {
  const [params, setParams] = useSearchParams();
  const [listing, setListing] = useState<ExplorerFileListing>();
  const [error, setError] = useState("");
  const [draft, setDraft] = useState(params.get("search") ?? "");
  const [selected, setSelected] = useState<string[]>([]);
  const [detailId, setDetailId] = useState<string>();
  const [compare, setCompare] = useState<string[]>();
  const search = params.get("search") ?? "", source = params.get("source") ?? "active", extension = params.get("extension") ?? "all", sort = params.get("sort") ?? "name", direction = params.get("direction") ?? "asc", cursor = params.get("cursor") ?? "";
  const load = () => { const query = new URLSearchParams({ search, source, extension, sort, direction, limit: "50" }); if (cursor) query.set("cursor", cursor); api<ExplorerFileListing>(`/api/explorer/files?${query}`).then((value) => { setListing(value); setError(""); }).catch((reason) => setError(reason.message)); };
  useEffect(load, [search, source, extension, sort, direction, cursor]);
  const update = (values: Record<string, string>) => { const next = new URLSearchParams(params); next.set("tab", "files"); Object.entries(values).forEach(([key, value]) => value ? next.set(key, value) : next.delete(key)); if (!("cursor" in values)) next.delete("cursor"); setParams(next); };
  const toggle = (id: string) => setSelected((current) => current.includes(id) ? current.filter((value) => value !== id) : current.length < 2 ? [...current, id] : [current[1], id]);
  return <><Header title="파일 탐색기" description="DB에 기록된 모든 파일과 본문 지문·작품 관계·검토 이력을 읽기 전용으로 확인합니다."/><CatalogTabs active="files"/>
    {error && <div className="inline-error">{error}</div>}
    <div className="toolbar explorer-toolbar"><form className="search-form" onSubmit={(event: FormEvent) => { event.preventDefault(); update({ search: draft.trim() }); }}><input value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="파일명·경로·core title·작가·ID 검색"/><button className="button secondary">검색</button></form>
      <select value={source} onChange={(event) => update({ source: event.target.value })}><option value="active">활성 전체</option><option value="house">house</option><option value="temp">temp</option><option value="queue">queue</option><option value="quarantine">quarantine</option><option value="inactive">비활성 이력</option><option value="all">전체</option></select>
      <select value={extension} onChange={(event) => update({ extension: event.target.value })}><option value="all">전체 형식</option><option value="txt">TXT</option><option value="epub">EPUB</option><option value="pdf">PDF</option></select>
      <select value={`${sort}:${direction}`} onChange={(event) => { const [nextSort, nextDirection] = event.target.value.split(":"); update({ sort: nextSort, direction: nextDirection }); }}><option value="name:asc">이름순</option><option value="core:asc">core순</option><option value="size:desc">큰 파일순</option><option value="seen:desc">최근 확인순</option></select>
      <button className="button secondary" onClick={load}>갱신</button>
      <button className="button primary" disabled={selected.length !== 2} onClick={() => setCompare(selected)}>두 파일 비교</button>
    </div>
    <section className="table-panel"><div className="table-summary"><span>현재 조건 <strong>{formatNumber(listing?.total ?? 0)}</strong>파일 · 비교 선택 {selected.length}/2</span><span>실제 파일 변경 없음</span></div>
      {!listing ? <div className="loading"><span/>파일 목록을 확인하고 있습니다.</div> : listing.items.length ? <div className="catalog-table-wrap"><table className="explorer-file-table"><thead><tr><th>선택</th><th>파일·경로</th><th>core·좌표</th><th>관계</th><th>크기·지문</th><th>상세</th></tr></thead><tbody>{listing.items.map((item) => <tr key={item.file_id} className={item.retired_virtual_path ? "explorer-retired" : ""}><td><input type="checkbox" checked={selected.includes(item.file_id)} onChange={() => toggle(item.file_id)} aria-label={`${item.name} 비교 선택`}/></td><td><strong>{item.name}</strong><small>{item.parent}</small><small>{item.source} · {item.active ? "활성" : "비활성"}{item.retired_virtual_path ? " · 가상 이력" : ""}</small></td><td><code className="core">{item.core_title ?? "core 없음"}</code><small>{coordinate(item)} · {item.author ?? "작가 미상"}</small></td><td><b>작품 {item.work_bucket_id ?? "-"} · 변형 {item.variant_id ?? "-"}</b><small>{item.assignment_state} · {item.variant_kind ?? "미분류"}</small><small>{item.representative ? "대표 파일" : "일반 파일"}{item.protected ? " · 보호" : ""}</small></td><td><b>{formatBytes(item.size)}</b><small>{item.fingerprint_status ?? "지문 없음"}</small><small>{item.normalized_sha256 ? `${item.normalized_sha256.slice(0, 12)}…` : "normalized SHA 없음"}</small></td><td><button className="button ghost" onClick={() => setDetailId(item.file_id)}>검사</button></td></tr>)}</tbody></table></div> : <div className="empty">조건에 맞는 파일이 없습니다.</div>}
      {listing && <Pager cursor={cursor} next={listing.next_cursor} limit={listing.limit} setCursor={(value) => update({ cursor: value === "0" ? "" : value })}/>}</section>
    {detailId && <FileInspector fileId={detailId} close={() => setDetailId(undefined)} compareWith={(other) => { setDetailId(undefined); setCompare([detailId, other]); }}/>} {compare?.length === 2 && <CompareModal leftId={compare[0]} rightId={compare[1]} close={() => setCompare(undefined)}/>}</>;
}

function FolderInspector({ folder, close }: { folder: ExplorerFolder; close: () => void }) {
  const [detail, setDetail] = useState<ExplorerFolderDetail>();
  const [error, setError] = useState("");
  useEffect(() => { api<ExplorerFolderDetail>(`/api/explorer/folders/detail?path=${encodeURIComponent(folder.path)}`).then(setDetail).catch((reason) => setError(reason.message)); }, [folder.path]);
  return <Modal close={close} wide><div className="explorer-modal-top"><div><span className="eyebrow">FOLDER INVENTORY</span><h2>{folder.name}</h2></div><button className="button secondary" onClick={close}>닫기</button></div>{error && <div className="inline-error">{error}</div>}{!detail ? !error && <div className="loading"><span/>폴더 파일을 확인하고 있습니다.</div> : <><code className="explorer-path">{detail.path}</code><div className="explorer-folder-summary"><span>DB 등록 <strong>{detail.registered_count}</strong></span><span>미등록·부속 <strong>{detail.unregistered_count}</strong></span><span>크기 <strong>{formatBytes(detail.total_size)}</strong></span></div>{detail.truncated && <div className="explorer-warning">안전 한도에 따라 처음 5,000개만 표시합니다.</div>}<div className="explorer-entry-list">{detail.entries.map((entry) => <article key={entry.path} className={entry.registered ? "registered" : "unregistered"}><div><strong>{entry.name}</strong><small>{entry.relative_path}</small></div><span>{entry.registered ? "DB 등록" : "부속/미등록"}{entry.symlink ? " · 링크" : ""}</span><b>{formatBytes(entry.size)}</b></article>)}</div><footer className="explorer-action-footer"><span>이름 변경·이동·격리는 {detail.actions.future_version}에서 제공됩니다.</span><button className="button secondary" disabled>폴더 작업</button></footer></>}</Modal>;
}

function FolderCatalog() {
  const [params, setParams] = useSearchParams(); const [listing, setListing] = useState<ExplorerFolderListing>(); const [error, setError] = useState(""); const [draft, setDraft] = useState(params.get("search") ?? ""); const [detail, setDetail] = useState<ExplorerFolder>();
  const search = params.get("search") ?? "", state = params.get("state") ?? "all", sort = params.get("sort") ?? "name", direction = params.get("direction") ?? "asc", cursor = params.get("cursor") ?? "";
  const load = (refresh = false) => { const query = new URLSearchParams({ search, state, sort, direction, limit: "50" }); if (cursor) query.set("cursor", cursor); if (refresh) query.set("refresh", "1"); api<ExplorerFolderListing>(`/api/explorer/folders?${query}`).then((value) => { setListing(value); setError(""); }).catch((reason) => setError(reason.message)); }; useEffect(() => load(), [search, state, sort, direction, cursor]);
  const update = (values: Record<string, string>) => { const next = new URLSearchParams(params); next.set("tab", "folders"); Object.entries(values).forEach(([key, value]) => value ? next.set(key, value) : next.delete(key)); if (!("cursor" in values)) next.delete("cursor"); setParams(next); };
  return <><Header title="폴더 탐색기" description="house 폴더의 DB 관계를 먼저 보고, 상세를 열 때만 실제 폴더의 미등록·부속 파일을 제한적으로 확인합니다."/><CatalogTabs active="folders"/>{error && <div className="inline-error">{error}</div>}<div className="toolbar explorer-toolbar"><form className="search-form" onSubmit={(event) => { event.preventDefault(); update({ search: draft.trim() }); }}><input value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="폴더·core title·파일명 검색"/><button className="button secondary">검색</button></form><select value={state} onChange={(event) => update({ state: event.target.value })}><option value="all">전체 폴더</option><option value="grouped">묶음 폴더</option><option value="single_file">파일 1개</option><option value="mixed_core">core 혼합</option><option value="mixed_work">작품 관계 혼합</option></select><select value={`${sort}:${direction}`} onChange={(event) => { const [a, b] = event.target.value.split(":"); update({ sort: a, direction: b }); }}><option value="name:asc">이름순</option><option value="files:desc">파일 많은순</option><option value="size:desc">큰 폴더순</option><option value="depth:desc">깊은 경로순</option></select><button className="button secondary" onClick={() => load(true)}>실제 상태 갱신</button></div>
    <section className="table-panel"><div className="table-summary"><span>현재 조건 <strong>{formatNumber(listing?.total ?? 0)}</strong>폴더</span><span>가상 retired 경로 제외 · 상세만 실제 폴더 조회</span></div>{!listing ? <div className="loading"><span/>DB 폴더 관계를 확인하고 있습니다.</div> : listing.items.length ? <div className="catalog-table-wrap"><table className="explorer-folder-table"><thead><tr><th>폴더</th><th>보유</th><th>core title</th><th>작품·변형</th><th>상태</th><th>상세</th></tr></thead><tbody>{listing.items.map((item) => <tr key={item.path}><td><strong>{item.name}</strong><small>{item.relative_path}</small></td><td><b>{item.file_count}개</b><small>{formatBytes(item.total_size)}</small></td><td><span>{item.core_titles.slice(0, 3).join(" · ") || "없음"}</span>{item.core_titles.length > 3 && <small>외 {item.core_titles.length - 3}개</small>}</td><td><b>작품 {item.work_bucket_ids.length} · 변형 {item.variant_ids.length}</b><small>{item.sample_files.slice(0, 2).join(" · ")}</small></td><td>{item.mixed_core && <span className="explorer-state warning">core 혼합</span>}{item.mixed_work && <span className="explorer-state warning">작품 혼합</span>}{!item.mixed_core && !item.mixed_work && <span className="explorer-state ok">일관됨</span>}</td><td><button className="button ghost" onClick={() => setDetail(item)}>파일 확인</button></td></tr>)}</tbody></table></div> : <div className="empty">조건에 맞는 폴더가 없습니다.</div>}{listing && <Pager cursor={cursor} next={listing.next_cursor} limit={listing.limit} setCursor={(value) => update({ cursor: value === "0" ? "" : value })}/>}</section>{detail && <FolderInspector folder={detail} close={() => setDetail(undefined)}/>}</>;
}

function QuarantineCatalog() {
  const [params, setParams] = useSearchParams(); const [listing, setListing] = useState<ExplorerQuarantineListing>(); const [error, setError] = useState(""); const [draft, setDraft] = useState(params.get("search") ?? ""); const search = params.get("search") ?? "", state = params.get("state") ?? "all", cursor = params.get("cursor") ?? "";
  const load = () => { const query = new URLSearchParams({ search, state, limit: "50" }); if (cursor) query.set("cursor", cursor); api<ExplorerQuarantineListing>(`/api/explorer/quarantine?${query}`).then((value) => { setListing(value); setError(""); }).catch((reason) => setError(reason.message)); }; useEffect(load, [search, state, cursor]);
  const update = (values: Record<string, string>) => { const next = new URLSearchParams(params); next.set("tab", "quarantine"); Object.entries(values).forEach(([key, value]) => value ? next.set(key, value) : next.delete(key)); if (!("cursor" in values)) next.delete("cursor"); setParams(next); };
  return <><Header title="격리 보관함" description="DB 작업 이력과 실제 trash_bin을 대조합니다. 복원과 영구 삭제는 1.3.2에서 확인형 작업으로 제공합니다."/><CatalogTabs active="quarantine"/>{error && <div className="inline-error">{error}</div>}<div className="explorer-quarantine-summary">{(["present", "missing", "untracked", "purged"] as const).map((value) => <button className={state === value ? "active" : ""} key={value} onClick={() => update({ state: value })}><span>{{ present: "실제 보관", missing: "파일 없음", untracked: "이력 없음", purged: "삭제 이력" }[value]}</span><strong>{formatNumber(listing?.summary[value] ?? 0)}</strong></button>)}</div><div className="toolbar explorer-toolbar"><form className="search-form" onSubmit={(event) => { event.preventDefault(); update({ search: draft.trim() }); }}><input value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="격리 파일·원본 경로·보존 파일 검색"/><button className="button secondary">검색</button></form><select value={state} onChange={(event) => update({ state: event.target.value })}><option value="all">전체 상태</option><option value="present">실제 보관</option><option value="missing">파일 없음</option><option value="untracked">이력 없음</option><option value="purged">삭제 이력</option></select><button className="button secondary" onClick={load}>실제 상태 갱신</button></div>
    <section className="table-panel"><div className="table-summary"><span>현재 조건 <strong>{formatNumber(listing?.total ?? 0)}</strong>파일</span><span>읽기 전용 · 복원/삭제 불가</span></div>{!listing ? <div className="loading"><span/>격리 이력과 파일을 대조하고 있습니다.</div> : listing.items.length ? <div className="explorer-quarantine-list">{listing.items.map((item, index) => <article key={`${item.operation_id ?? "untracked"}-${item.path}-${index}`}><span className={`explorer-state ${item.physical_state}`}>{item.physical_state}</span><div><strong>{item.name}</strong><small>격리: {item.path}</small>{item.source_path && <small>원본: {item.source_path}</small>}{item.keep_path && <small>보존: {item.keep_path}</small>}</div><div><b>{item.action ?? "DB 이력 없음"}</b><small>{item.category} · {item.tracked ? `작업 #${item.operation_id}` : "미추적"}</small></div><div><b>{formatBytes(item.size)}</b><small>{item.age_days === null ? "시각 없음" : `${item.age_days}일 경과`}</small></div><button className="button secondary" disabled>복원/삭제</button></article>)}</div> : <div className="empty">조건에 맞는 격리 항목이 없습니다.</div>}{listing && <Pager cursor={cursor} next={listing.next_cursor} limit={listing.limit} setCursor={(value) => update({ cursor: value === "0" ? "" : value })}/>}</section></>;
}

export function CatalogExplorer({ tab }: { tab: Exclude<CatalogTab, "works"> }) {
  if (tab === "files") return <FileCatalog/>;
  if (tab === "folders") return <FolderCatalog/>;
  return <QuarantineCatalog/>;
}
