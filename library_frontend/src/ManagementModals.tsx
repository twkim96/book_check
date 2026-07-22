import { useEffect, useState, type ReactNode } from "react";

import { api, postJson } from "./api";
import type { FileDestinationCandidate, FileDestinationListing, FileRelocatePlan, FolderQuarantinePlan, JobRecord, ManagedFolderAdoptPlan, ManagedFolderPlan, ManagedFolderRelocatePlan, PurgePlan, QuarantinePlan, RelationshipPlan, RestorePlan, TitleCase, TitlePlan, TitlePreview, WorkSearchItem, WorkSearchListing } from "./types";

export function Modal({ close, children, danger = false }: { close: () => void; children: ReactNode; danger?: boolean }) {
  useEffect(() => {
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") close(); };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [close]);
  return <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) close(); }}><section className={`modal management-modal${danger ? " management-modal-danger" : ""}`}>{children}</section></div>;
}

export function Top({ eyebrow, title, close }: { eyebrow: string; title: string; close: () => void }) {
  return <div className="management-modal-top"><div><span className="eyebrow">{eyebrow}</span><h2>{title}</h2></div><button className="button secondary" onClick={close}>닫기</button></div>;
}

function blockerLabel(value: string): string {
  const [kind, detail] = value.split(":", 2);
  if (kind === "selected_variants_require_folder") {
    return `선택한 판본이 관리 폴더 #${detail} 안에 있습니다. 해당 폴더도 함께 선택해야 합니다.`;
  }
  if (kind === "folder_contains_unselected_variants") {
    return `관리 폴더 #${detail}에 선택하지 않은 판본이 함께 있습니다. 먼저 파일을 별도 폴더로 나눠야 합니다.`;
  }
  if (kind === "quarantine_destination_occupied") return "같은 원래 경로의 폴더가 이미 격리 보관함에 있습니다.";
  if (kind === "folder_has_no_registered_books") return "DB에 등록된 도서가 없는 폴더는 이 화면에서 전체 격리할 수 없습니다.";
  if (kind === "inventory_blocked") return `폴더 파일 상태를 안전하게 확정하지 못했습니다: ${detail ?? "원인 미상"}`;
  return value;
}

export function PlanCheck({ sha, blockers }: { sha: string; blockers: string[] }) {
  return <div className={blockers.length ? "management-plan blocked" : "management-plan ready"}><strong>{blockers.length ? "실행 차단" : "현재 계획 실행 가능"}</strong><code>{sha}</code>{blockers.map((value) => <span key={value}>{blockerLabel(value)}</span>)}</div>;
}

function WorkPicker({ value, defaultWorkId, changed }: { value: string; defaultWorkId?: number; changed: (value: string) => void }) {
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<WorkSearchItem[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    const search = query.trim() || (defaultWorkId ? String(defaultWorkId) : "");
    const timer = window.setTimeout(() => {
      api<WorkSearchListing>(`/api/management/works?search=${encodeURIComponent(search)}&limit=20`)
        .then((result) => { setItems(result.items); setError(""); })
        .catch((reason) => setError(reason instanceof Error ? reason.message : "작품을 찾지 못했습니다."));
    }, 180);
    return () => window.clearTimeout(timer);
  }, [query, defaultWorkId]);
  const selected = items.find((item) => String(item.work_bucket_id) === value);
  return <label className="management-wide work-picker">작품 선택
    <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="작품명 또는 작품 번호로 검색"/>
    {error && <small className="picker-error">{error}</small>}
    <div className="work-picker-results">
      {items.map((item) => <button type="button" key={item.work_bucket_id} className={String(item.work_bucket_id) === value ? "selected" : ""} onClick={() => changed(String(item.work_bucket_id))}>
        <span><strong>{item.display_title || "제목 미지정"}</strong><small>작품 #{item.work_bucket_id}</small></span>
        <small>파일 {item.active_file_count.toLocaleString("ko-KR")} · 관리 폴더 {item.active_folder_count}</small>
      </button>)}
    </div>
    {selected && <span className="work-picker-selected">선택됨 · 작품 #{selected.work_bucket_id} {selected.display_title}</span>}
  </label>;
}

function compactBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function FolderPathPicker({ fileId, value, changed }: { fileId: string; value: string; changed: (value: string) => void }) {
  const [query, setQuery] = useState("");
  const [listing, setListing] = useState<FileDestinationListing>();
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    setLoading(true);
    const timer = window.setTimeout(() => {
      api<FileDestinationListing>(`/api/explorer/files/${encodeURIComponent(fileId)}/destinations?search=${encodeURIComponent(query.trim())}&limit=24`)
        .then(setListing).catch(() => setListing(undefined)).finally(() => setLoading(false));
    }, 180);
    return () => window.clearTimeout(timer);
  }, [query, fileId]);
  const selected = listing?.items.find((item) => item.path === value);
  const label = query.trim() ? "검색 결과" : "제목·작품 관계 기반 추천";
  return <section className="management-wide destination-browser">
    <header><div><strong>목적 폴더 선택</strong><small>{listing?.source.core_title ? `현재 core · ${listing.source.core_title}` : "현재 제목과 가까운 폴더를 찾습니다."}</small></div><span>{label}</span></header>
    <div className="destination-search"><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="폴더명·core title·보유 파일명 검색"/>{loading && <small>검색 중…</small>}</div>
    <div className="destination-grid">
      {listing?.items.map((item: FileDestinationCandidate, index) => <button type="button" key={item.path} className={`${item.path === value ? "selected" : ""}${item.current ? " current" : ""}`} onClick={() => changed(item.path)}>
        <div className="destination-rank"><b>{item.current ? "현재" : query.trim() ? "검색" : `추천 ${index + 1}`}</b>{item.managed_folder_id && <span>관리</span>}</div>
        <strong>{item.name}</strong>
        <small className="destination-relative">{item.relative_path}</small>
        <div className="destination-reasons">{item.reasons.map((reason) => <span key={reason}>{reason}</span>)}</div>
        <small>{item.file_count.toLocaleString("ko-KR")}개 · {compactBytes(item.total_size)}{item.core_titles[0] ? ` · ${item.core_titles[0]}` : ""}</small>
      </button>)}
      {!loading && listing && listing.items.length === 0 && <div className="destination-empty">조건에 맞는 기존 폴더가 없습니다. 아래에서 경로를 직접 입력할 수 있습니다.</div>}
    </div>
    <div className="destination-selected"><span>선택한 목적지</span><strong>{selected?.name ?? value.split("/").pop() ?? value}</strong><code>{value}</code></div>
    <details className="destination-advanced"><summary>경로 직접 입력</summary><input value={value} onChange={(event) => changed(event.target.value)} placeholder="/Users/.../txt_house/초성/작품 폴더"/></details>
  </section>;
}

export function RelationshipManager({ leftId, rightId, currentDecisionId, close, done }: { leftId: string; rightId: string; currentDecisionId?: number | null; close: () => void; done: () => void }) {
  const [verdict, setVerdict] = useState<RelationshipPlan["verdict"]>("same_work_distinct_variant");
  const [variantKind, setVariantKind] = useState("other");
  const [note, setNote] = useState("");
  const [plan, setPlan] = useState<RelationshipPlan>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const preview = async () => { setBusy(true); setError(""); try { setPlan(await postJson<RelationshipPlan>("/api/management/relationships/preview", { left_file_id: leftId, right_file_id: rightId, verdict, variant_kind: variantKind, note })); } catch (reason) { setError(reason instanceof Error ? reason.message : "계획을 만들지 못했습니다."); } finally { setBusy(false); } };
  const apply = async () => { if (!plan) return; setBusy(true); setError(""); try { await postJson("/api/management/relationships/apply", { left_file_id: leftId, right_file_id: rightId, verdict, variant_kind: variantKind, note, confirm_count: plan.item_count, confirm_plan_sha256: plan.plan_sha256 }); done(); close(); } catch (reason) { setError(reason instanceof Error ? reason.message : "판정을 저장하지 못했습니다."); } finally { setBusy(false); } };
  const cancel = async () => { if (!currentDecisionId || !window.confirm("현재 사람 판정을 취소하고 두 파일을 다시 미배정 상태로 돌릴까요?")) return; setBusy(true); try { await postJson(`/api/management/relationships/${currentDecisionId}/cancel`, {}); done(); close(); } catch (reason) { setError(reason instanceof Error ? reason.message : "판정을 취소하지 못했습니다."); } finally { setBusy(false); } };
  return <Modal close={close}><Top eyebrow="HUMAN RELATIONSHIP · 1.3.2" title="두 파일 관계 확정" close={close}/><p className="management-description">현재 fingerprint에 묶인 사람 판단을 저장합니다. 파일 내용이 바뀌면 이 판단을 자동 재사용하지 않습니다.</p>{error && <div className="inline-error">{error}</div>}<div className="management-form"><label>판정<select value={verdict} onChange={(event) => { setVerdict(event.target.value as RelationshipPlan["verdict"]); setPlan(undefined); }}><option value="same_work_distinct_variant">같은 작품의 다른 판본·부속</option><option value="distinct_work">제목만 같은 다른 작품</option><option value="same_content">같은 내용</option></select></label><label>판본 종류<select value={variantKind} onChange={(event) => { setVariantKind(event.target.value); setPlan(undefined); }}><option value="other">기타/extra</option><option value="revision">개정판</option><option value="adult">성인판</option><option value="translation">번역판</option><option value="base">기본판</option></select></label><label className="management-wide">판단 메모<input value={note} onChange={(event) => { setNote(event.target.value); setPlan(undefined); }} placeholder="왜 이 관계로 판단했는지 선택 입력"/></label></div>{plan && <PlanCheck sha={plan.plan_sha256} blockers={plan.blocked_reasons}/>}<footer><button className="button secondary" disabled={busy} onClick={preview}>{busy ? "확인 중…" : "계획 확인"}</button>{currentDecisionId && <button className="button danger" disabled={busy} onClick={cancel}>현재 판정 취소</button>}<button className="button primary" disabled={busy || !plan?.apply_available} onClick={apply}>{plan?.mode === "correction" ? "판정 정정 저장" : "판정 저장"}</button></footer></Modal>;
}

export function QuarantineManager({ sourceId, keepId, close, started }: { sourceId: string; keepId?: string | null; close: () => void; started: (job: JobRecord) => void }) {
  const [plan, setPlan] = useState<QuarantinePlan>(); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const preview = async () => { setBusy(true); setError(""); try { setPlan(await postJson<QuarantinePlan>("/api/management/quarantine/preview", { source_file_id: sourceId, keep_file_id: keepId || null })); } catch (reason) { setError(reason instanceof Error ? reason.message : "격리 계획을 만들지 못했습니다."); } finally { setBusy(false); } };
  const apply = async () => { if (!plan) return; setBusy(true); try { const job = await postJson<JobRecord>("/api/management/quarantine/apply", { source_file_id: sourceId, keep_file_id: keepId || null, confirm_count: 1, confirm_plan_sha256: plan.plan_sha256 }); started(job); close(); } catch (reason) { setError(reason instanceof Error ? reason.message : "격리를 시작하지 못했습니다."); setBusy(false); } };
  return <Modal close={close}><Top eyebrow="USER QUARANTINE · 1.3.5" title="사용자 승인 격리" close={close}/><p className="management-description">자동 중복이라고 주장하지 않고, 사용자가 이 판본을 보유 목록에서 제외했다는 처분을 기록합니다.</p>{error && <div className="inline-error">{error}</div>}{plan && <><div className="management-impact"><span>대상<strong>{plan.source.name}</strong></span><span>유지 파일<strong>{plan.keep?.name ?? "지정 없음"}</strong></span><span>대표 교체<strong>{plan.replacement_representative?.name ?? "없음"}</strong></span><span>관계 영향<strong>{plan.retired_work ? "작품 퇴역" : plan.retired_variant ? "판본 퇴역" : "활성 유지"}</strong></span></div>{plan.fingerprint_preparation_count > 0 && <div className="inline-notice"><span>본문 지문 {plan.fingerprint_preparation_count}개는 실행 전 DB 백업 후 자동으로 준비합니다.</span></div>}<PlanCheck sha={plan.plan_sha256} blockers={plan.blocked_reasons}/></>}<footer><button className="button secondary" disabled={busy} onClick={preview}>격리 계획 확인</button><button className="button danger" disabled={busy || !plan?.apply_available} onClick={apply}>격리 실행</button></footer></Modal>;
}

export function FolderQuarantineManager({ folderPath, close, started }: { folderPath: string; close: () => void; started: (job: JobRecord) => void }) {
  const [plan, setPlan] = useState<FolderQuarantinePlan>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const preview = async () => {
    setBusy(true); setError("");
    try { setPlan(await postJson<FolderQuarantinePlan>("/api/management/folders/quarantine/preview", { folder_path: folderPath })); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "폴더 격리 계획을 만들지 못했습니다."); }
    finally { setBusy(false); }
  };
  const apply = async () => {
    if (!plan) return;
    setBusy(true); setError("");
    try {
      const job = await postJson<JobRecord>("/api/management/folders/quarantine/apply", {
        folder_path: folderPath, confirm_count: plan.item_count,
        confirm_plan_sha256: plan.plan_sha256
      });
      started(job); close();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "폴더 격리를 시작하지 못했습니다."); setBusy(false); }
  };
  return <Modal close={close} danger><Top eyebrow="FOLDER QUARANTINE · 1.3.5" title="폴더 전체 사용자 승인 격리" close={close}/>
    <p className="management-description">폴더 안의 DB 도서와 JPG·ZIP 같은 부속 파일을 한 작업으로 격리합니다. 자동 중복 판정이 아니라 사용자가 이 폴더를 보유 목록에서 제외한 이력으로 남습니다.</p>
    {error && <div className="inline-error">{error}</div>}
    {plan && <><div className="management-paths"><small>현재: {plan.source_path}</small><small>격리: {plan.destination_path}</small></div>
      <div className="management-impact"><span>DB 도서<strong>{plan.registered_count}개</strong></span><span>부속 파일<strong>{plan.auxiliary_count}개</strong></span><span>하위 폴더<strong>{plan.directory_count}개</strong></span><span>총 용량<strong>{plan.total_size.toLocaleString("ko-KR")} bytes</strong></span></div>
      {plan.related_folders.length > 0 && <div className="folder-related-keepers"><strong>같은 작품의 다른 관리 폴더</strong>{plan.related_folders.map((folder) => <span key={folder.folder_id}>#{folder.work_bucket_id} · {folder.role} · {folder.canonical_path}</span>)}</div>}
      <div className="folder-quarantine-preview-list">{plan.items.slice(0, 100).map((item) => <span key={item.source_path}><b>{item.registered ? "도서" : "부속"}</b>{item.relative_path}</span>)}{plan.items.length > 100 && <small>외 {plan.items.length - 100}개</small>}</div>
      <PlanCheck sha={plan.plan_sha256} blockers={plan.blocked_reasons}/></>}
    <footer><button className="button secondary" disabled={busy} onClick={preview}>{busy ? "확인 중…" : "폴더 격리 계획 확인"}</button><button className="button danger" disabled={busy || !plan?.apply_available} onClick={apply}>폴더 전체 격리 실행</button></footer>
  </Modal>;
}

export function QuickTitleCorrectionManager({ fileId, close, started }: { fileId: string; close: () => void; started: (job: JobRecord) => void }) {
  const [item, setItem] = useState<TitleCase>();
  const [value, setValue] = useState("");
  const [preview, setPreview] = useState<TitlePreview>();
  const [plan, setPlan] = useState<TitlePlan>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    api<TitleCase>(`/api/review/titles/${encodeURIComponent(fileId)}`)
      .then((result) => { setItem(result); setValue(result.current_body); })
      .catch((reason) => setError(reason instanceof Error ? reason.message : "제목 정보를 불러오지 못했습니다."));
  }, [fileId]);
  const changed = (next: string) => { setValue(next); setPreview(undefined); setPlan(undefined); };
  const check = async () => {
    if (!item) return;
    setBusy(true); setError("");
    try {
      const nextPreview = await postJson<TitlePreview>("/api/review/titles/preview", { file_id: fileId, source_revision: item.source_revision, new_body: value });
      setPreview(nextPreview);
      if (nextPreview.runnable) {
        setPlan(await postJson<TitlePlan>("/api/review/titles/plan", { changes: [{ file_id: fileId, source_revision: item.source_revision, new_body: value }] }));
      } else setPlan(undefined);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "제목 교정 계획을 만들지 못했습니다."); }
    finally { setBusy(false); }
  };
  const apply = async () => {
    if (!item || !plan) return;
    setBusy(true); setError("");
    try {
      const job = await postJson<JobRecord>("/api/review/titles/apply", {
        changes: [{ file_id: fileId, source_revision: item.source_revision, new_body: value }],
        confirm_count: plan.item_count, confirm_plan_sha256: plan.plan_sha256
      });
      started(job); close();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "제목 교정을 시작하지 못했습니다."); setBusy(false); }
  };
  return <Modal close={close}><Top eyebrow="QUICK TITLE CORRECTION · 1.3.5" title="파일 제목 빠른 교정" close={close}/>
    <p className="management-description">기존 제목교정 서비스와 같은 분석·temp 재입고 로직을 사용합니다. 이 모달은 한 파일만 빠르게 계획합니다.</p>
    {error && <div className="inline-error">{error}</div>}
    {!item ? !error && <div className="loading"><span/>제목 정보를 확인하고 있습니다.</div> : <>
      <div className="management-paths"><small>현재 파일: {item.current_name}</small><small>현재 core: {item.core_title}</small></div>
      <div className="management-form"><label className="management-wide">새 파일명 본문<input autoFocus value={value} onChange={(event) => changed(event.target.value)} placeholder="확장자 제외 · 제목 [[19금]] · 구조 {{힌트}}"/></label></div>
      {preview && <><div className="management-impact"><span>변경 후 파일<strong>{preview.materialized_candidate_name}</strong></span><span>변경 후 core<strong>{preview.after_core_title || "-"}</strong></span><span>검색어<strong>{preview.after_query_title || "-"}</strong></span><span>temp 이동<strong>{preview.runnable ? "가능" : "차단"}</strong></span></div><PlanCheck sha={plan?.plan_sha256 ?? preview.source_revision} blockers={preview.blocked_reasons}/></>}
    </>}
    <footer><button className="button secondary" disabled={busy || !item || !value.trim()} onClick={check}>{busy ? "확인 중…" : "교정 계획 확인"}</button><button className="button danger" disabled={busy || !plan?.runnable} onClick={apply}>확인하고 실행</button></footer>
  </Modal>;
}

export function RestoreManager({ operationId, defaultReferenceId, close, started }: { operationId: number; defaultReferenceId?: string | null; close: () => void; started: (job: JobRecord) => void }) {
  const [referenceId, setReferenceId] = useState(defaultReferenceId ?? ""); const [verdict, setVerdict] = useState<RestorePlan["verdict"]>("same_work_distinct_variant"); const [note, setNote] = useState(""); const [plan, setPlan] = useState<RestorePlan>(); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const preview = async () => { setBusy(true); setError(""); try { setPlan(await postJson<RestorePlan>("/api/management/quarantine/restore/preview", { operation_id: operationId, reference_file_id: referenceId || null, verdict, note })); } catch (reason) { setError(reason instanceof Error ? reason.message : "복원 계획을 만들지 못했습니다."); } finally { setBusy(false); } };
  const apply = async () => { if (!plan) return; setBusy(true); try { const job = await postJson<JobRecord>("/api/management/quarantine/restore/apply", { operation_id: operationId, reference_file_id: referenceId || null, verdict, note, confirm_count: 1, confirm_plan_sha256: plan.plan_sha256 }); started(job); close(); } catch (reason) { setError(reason instanceof Error ? reason.message : "복원을 시작하지 못했습니다."); setBusy(false); } };
  return <Modal close={close}><Top eyebrow="RESTORE AS DISTINCT · 1.3.2" title="중복 아님으로 복원" close={close}/><p className="management-description">원래 경로가 비어 있을 때만 복원하며, 다음 중복 검사에서 다시 격리되지 않도록 비교 파일과의 관계를 함께 저장합니다.</p>{error && <div className="inline-error">{error}</div>}<div className="management-form"><label className="management-wide">비교할 활성 파일 ID<input value={referenceId} onChange={(event) => { setReferenceId(event.target.value); setPlan(undefined); }} placeholder="같은 작품 또는 구분할 작품의 file ID"/></label><label>복원 관계<select value={verdict} onChange={(event) => { setVerdict(event.target.value as RestorePlan["verdict"]); setPlan(undefined); }}><option value="same_work_distinct_variant">같은 작품의 다른 판본</option><option value="distinct_work">제목만 같은 다른 작품</option></select></label><label>메모<input value={note} onChange={(event) => { setNote(event.target.value); setPlan(undefined); }}/></label></div>{plan && <><div className="management-paths"><small>격리: {plan.quarantine_path}</small><small>복원: {plan.destination_path}</small></div><PlanCheck sha={plan.plan_sha256} blockers={plan.blocked_reasons}/></>}<footer><button className="button secondary" disabled={busy} onClick={preview}>복원 계획 확인</button><button className="button primary" disabled={busy || !plan?.apply_available} onClick={apply}>복원 실행</button></footer></Modal>;
}

export function PurgeManager({ operationIds, close, started }: { operationIds: number[]; close: () => void; started: (job: JobRecord) => void }) {
  const [plan, setPlan] = useState<PurgePlan>(); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  useEffect(() => { setBusy(true); postJson<PurgePlan>("/api/management/quarantine/purge/preview", { operation_ids: operationIds }).then(setPlan).catch((reason) => setError(reason.message)).finally(() => setBusy(false)); }, [operationIds]);
  const apply = async () => { if (!plan) return; setBusy(true); try { const job = await postJson<JobRecord>("/api/management/quarantine/purge/apply", { operation_ids: operationIds, confirm_count: plan.item_count, confirm_plan_sha256: plan.plan_sha256 }); started(job); close(); } catch (reason) { setError(reason instanceof Error ? reason.message : "영구 삭제를 시작하지 못했습니다."); setBusy(false); } };
  return <Modal close={close} danger><Top eyebrow="IRREVERSIBLE PURGE · 1.3.2" title="격리 파일 영구 삭제" close={close}/><div className="management-danger-note"><strong>파일 bytes는 복구할 수 없습니다.</strong><span>아래 대상과 용량을 확인한 뒤 영구 삭제 실행을 누르세요. DB의 파일 identity, fingerprint와 operation 이력은 남습니다.</span></div>{error && <div className="inline-error">{error}</div>}{plan && <><div className="management-impact"><span>선택 항목<strong>{plan.item_count}개</strong></span><span>회수 용량<strong>{new Intl.NumberFormat("ko-KR").format(plan.total_size)} bytes</strong></span></div><div className="management-purge-list">{plan.items.map((item) => <span key={item.operation_id}><b>{item.name}</b><small>{item.age_days ?? "?"}일 보관 · {item.size.toLocaleString("ko-KR")} bytes</small></span>)}</div><PlanCheck sha={plan.plan_sha256} blockers={plan.blocked_reasons}/></>}<footer><button className="button danger" disabled={busy || !plan?.apply_available} onClick={apply}>영구 삭제 실행</button></footer></Modal>;
}

export function FileRelocateManager({ fileId, currentName, currentParent, close, started }: { fileId: string; currentName: string; currentParent: string; close: () => void; started: (job: JobRecord) => void }) {
  const [targetDirectory, setTargetDirectory] = useState(currentParent);
  const [newName, setNewName] = useState(currentName);
  const [plan, setPlan] = useState<FileRelocatePlan>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const preview = async () => { setBusy(true); setError(""); try { setPlan(await postJson<FileRelocatePlan>("/api/management/files/relocate/preview", { file_id: fileId, target_directory: targetDirectory, new_name: newName })); } catch (reason) { setError(reason instanceof Error ? reason.message : "파일 정리 계획을 만들지 못했습니다."); } finally { setBusy(false); } };
  const apply = async () => { if (!plan) return; setBusy(true); setError(""); try { const job = await postJson<JobRecord>("/api/management/files/relocate/apply", { file_id: fileId, target_directory: targetDirectory, new_name: newName, confirm_count: plan.item_count, confirm_plan_sha256: plan.plan_sha256 }); started(job); close(); } catch (reason) { setError(reason instanceof Error ? reason.message : "파일 정리를 시작하지 못했습니다."); setBusy(false); } };
  const changed = () => setPlan(undefined);
  const diffCount = plan ? Object.keys(plan.projection_diff.analysis).length + Object.keys(plan.projection_diff.coordinate).length : 0;
  return <Modal close={close}><Top eyebrow="FILE ORGANIZE · 1.3.5" title="파일 이름·위치 정리" close={close}/><p className="management-description">분석 결과가 같은 정리만 현재 위치에서 실행합니다. core title·작가·좌표가 달라지는 이름은 빠른 제목 교정을 사용하세요.</p>{error && <div className="inline-error">{error}</div>}<div className="management-form"><label className="management-wide">파일명<input value={newName} onChange={(event) => { setNewName(event.target.value); changed(); }}/></label><FolderPathPicker fileId={fileId} value={targetDirectory} changed={(value) => { setTargetDirectory(value); changed(); }}/></div>{plan && <><div className="management-paths"><small>현재: {plan.source.canonical_path}</small><small>변경: {plan.destination_path}</small></div><div className="management-impact"><span>파일명<strong>{plan.rename ? "변경" : "유지"}</strong></span><span>폴더<strong>{plan.move ? "이동" : "유지"}</strong></span><span>분석 projection<strong>{plan.projection_same ? "동일" : `${diffCount}개 변경`}</strong></span><span>처리 경로<strong>{plan.route === "journaled_relocate" ? "안전 정리" : "제목 교정 필요"}</strong></span></div><PlanCheck sha={plan.plan_sha256} blockers={plan.blocked_reasons}/>{plan.route === "title_correction" && <div className="explorer-warning">이 이름은 작품 분석이 달라집니다. 이 창에서는 실행하지 않고 빠른 제목 교정을 사용해야 합니다.</div>}</>}<footer><button className="button secondary" disabled={busy} onClick={preview}>{busy ? "확인 중…" : "계획 확인"}</button><button className="button primary" disabled={busy || !plan?.apply_available} onClick={apply}>파일 정리 실행</button></footer></Modal>;
}

export function ManagedFolderManager({ defaultWorkId, defaultParent, close, started }: { defaultWorkId?: number; defaultParent: string; close: () => void; started: (job: JobRecord) => void }) {
  const [workId, setWorkId] = useState(defaultWorkId ? String(defaultWorkId) : "");
  const [parentDirectory, setParentDirectory] = useState(defaultParent);
  const [folderName, setFolderName] = useState("");
  const [role, setRole] = useState<ManagedFolderPlan["role"]>("edition");
  const [plan, setPlan] = useState<ManagedFolderPlan>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const preview = async () => { setBusy(true); setError(""); try { setPlan(await postJson<ManagedFolderPlan>("/api/management/folders/create/preview", { work_bucket_id: Number(workId), parent_directory: parentDirectory, folder_name: folderName, role })); } catch (reason) { setError(reason instanceof Error ? reason.message : "관리 폴더 계획을 만들지 못했습니다."); } finally { setBusy(false); } };
  const apply = async () => { if (!plan) return; setBusy(true); setError(""); try { const job = await postJson<JobRecord>("/api/management/folders/create/apply", { work_bucket_id: Number(workId), parent_directory: parentDirectory, folder_name: folderName, role, confirm_count: plan.item_count, confirm_plan_sha256: plan.plan_sha256 }); started(job); close(); } catch (reason) { setError(reason instanceof Error ? reason.message : "관리 폴더 생성을 시작하지 못했습니다."); setBusy(false); } };
  const changed = () => setPlan(undefined);
  return <Modal close={close}><Top eyebrow="MANAGED FOLDER · 1.3.5" title="새 작품 폴더 만들기" close={close}/><p className="management-description">빈 폴더만 만드는 대신 작품과 역할을 DB에 함께 기록합니다. 대표 폴더는 작품당 하나이며, 추가 판본 폴더는 별도 판본을 사용합니다.</p>{error && <div className="inline-error">{error}</div>}<div className="management-form"><WorkPicker value={workId} defaultWorkId={defaultWorkId} changed={(value) => { setWorkId(value); changed(); }}/><label>폴더 역할<select value={role} onChange={(event) => { setRole(event.target.value as ManagedFolderPlan["role"]); changed(); }}><option value="primary">대표 폴더</option><option value="edition">별도 판본 폴더</option><option value="auxiliary">부속 폴더</option></select></label><label className="management-wide">새 폴더명<input value={folderName} onChange={(event) => { setFolderName(event.target.value); changed(); }} placeholder="작품 폴더 이름"/></label><label className="management-wide">상위 house 폴더<input value={parentDirectory} onChange={(event) => { setParentDirectory(event.target.value); changed(); }}/></label></div>{plan && <><div className="management-paths"><small>작품: #{plan.work.work_bucket_id} {plan.work.display_title ?? "제목 미지정"}</small><small>생성: {plan.destination_path}</small></div><PlanCheck sha={plan.plan_sha256} blockers={plan.blocked_reasons}/></>}<footer><button className="button secondary" disabled={busy || !workId || !folderName.trim()} onClick={preview}>{busy ? "확인 중…" : "생성 계획 확인"}</button><button className="button primary" disabled={busy || !plan?.apply_available} onClick={apply}>관리 폴더 생성</button></footer></Modal>;
}

export function ManagedFolderRelocateManager({ folderId, currentPath, currentName, close, started }: { folderId: number; currentPath: string; currentName: string; close: () => void; started: (job: JobRecord) => void }) {
  const parent = currentPath.slice(0, Math.max(1, currentPath.lastIndexOf("/")));
  const [targetParent, setTargetParent] = useState(parent);
  const [newName, setNewName] = useState(currentName);
  const [plan, setPlan] = useState<ManagedFolderRelocatePlan>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const preview = async () => { setBusy(true); setError(""); try { setPlan(await postJson<ManagedFolderRelocatePlan>("/api/management/folders/relocate/preview", { folder_id: folderId, target_parent: targetParent, new_name: newName })); } catch (reason) { setError(reason instanceof Error ? reason.message : "폴더 정리 계획을 만들지 못했습니다."); } finally { setBusy(false); } };
  const apply = async () => { if (!plan) return; setBusy(true); setError(""); try { const job = await postJson<JobRecord>("/api/management/folders/relocate/apply", { folder_id: folderId, target_parent: targetParent, new_name: newName, confirm_count: plan.item_count, confirm_plan_sha256: plan.plan_sha256 }); started(job); close(); } catch (reason) { setError(reason instanceof Error ? reason.message : "폴더 정리를 시작하지 못했습니다."); setBusy(false); } };
  const changed = () => setPlan(undefined);
  return <Modal close={close}><Top eyebrow="FOLDER ORGANIZE · 1.3.5" title="관리 폴더 이름·위치 정리" close={close}/><p className="management-description">폴더 아래 DB 도서와 JPG·ZIP 같은 부속 파일, 빈 하위 폴더를 한 operation group으로 이동합니다. 목적지가 있거나 symlink가 발견되면 실행하지 않습니다.</p>{error && <div className="inline-error">{error}</div>}<div className="management-form"><label className="management-wide">폴더명<input value={newName} onChange={(event) => { setNewName(event.target.value); changed(); }}/></label><label className="management-wide">이동할 상위 house 폴더<input value={targetParent} onChange={(event) => { setTargetParent(event.target.value); changed(); }}/></label></div>{plan && <><div className="management-paths"><small>현재: {plan.source_path}</small><small>변경: {plan.destination_path}</small></div><div className="management-impact"><span>DB 도서<strong>{plan.registered_count}개</strong></span><span>부속 파일<strong>{plan.auxiliary_count}개</strong></span><span>하위 폴더<strong>{plan.directory_count}개</strong></span><span>총 용량<strong>{plan.total_size.toLocaleString("ko-KR")} bytes</strong></span></div><PlanCheck sha={plan.plan_sha256} blockers={plan.blocked_reasons}/></>}<footer><button className="button secondary" disabled={busy} onClick={preview}>{busy ? "확인 중…" : "이동 계획 확인"}</button><button className="button primary" disabled={busy || !plan?.apply_available} onClick={apply}>폴더 정리 실행</button></footer></Modal>;
}

export function ManagedFolderAdoptManager({ folderPath, defaultWorkId, close, started }: { folderPath: string; defaultWorkId?: number; close: () => void; started: (job: JobRecord) => void }) {
  const [workId, setWorkId] = useState(defaultWorkId ? String(defaultWorkId) : "");
  const [role, setRole] = useState<ManagedFolderAdoptPlan["role"]>("primary");
  const [plan, setPlan] = useState<ManagedFolderAdoptPlan>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const preview = async () => { setBusy(true); setError(""); try { setPlan(await postJson<ManagedFolderAdoptPlan>("/api/management/folders/adopt/preview", { folder_path: folderPath, work_bucket_id: Number(workId), role })); } catch (reason) { setError(reason instanceof Error ? reason.message : "현재 폴더의 관리 등록 계획을 만들지 못했습니다."); } finally { setBusy(false); } };
  const apply = async () => { if (!plan) return; setBusy(true); setError(""); try { const job = await postJson<JobRecord>("/api/management/folders/adopt/apply", { folder_path: folderPath, work_bucket_id: Number(workId), role, confirm_count: plan.item_count, confirm_plan_sha256: plan.plan_sha256 }); started(job); close(); } catch (reason) { setError(reason instanceof Error ? reason.message : "현재 폴더의 관리 등록을 시작하지 못했습니다."); setBusy(false); } };
  const changed = () => setPlan(undefined);
  return <Modal close={close}><Top eyebrow="ADOPT FOLDER · 1.3.5" title="현재 폴더 관리 등록" close={close}/><p className="management-description">파일을 움직이지 않고 현재 폴더와 작품 관계를 DB에 등록합니다. 폴더 안에 다른 작품 관계가 섞여 있으면 등록하지 않습니다.</p>{error && <div className="inline-error">{error}</div>}<div className="management-form"><WorkPicker value={workId} defaultWorkId={defaultWorkId} changed={(value) => { setWorkId(value); changed(); }}/><label>폴더 역할<select value={role} onChange={(event) => { setRole(event.target.value as ManagedFolderAdoptPlan["role"]); changed(); }}><option value="primary">대표 폴더</option><option value="edition">별도 판본 폴더</option><option value="auxiliary">부속 폴더</option></select></label></div>{plan && <><div className="management-paths"><small>폴더: {plan.folder_path}</small><small>작품: #{plan.work.work_bucket_id} {plan.work.display_title ?? "제목 미지정"}</small></div><div className="management-impact"><span>DB 도서<strong>{plan.registered_count}개</strong></span><span>부속 파일<strong>{plan.auxiliary_count}개</strong></span><span>감지된 작품<strong>{plan.found_work_ids.join(", ") || "없음"}</strong></span><span>하위 폴더<strong>{plan.directory_count}개</strong></span></div><PlanCheck sha={plan.plan_sha256} blockers={plan.blocked_reasons}/></>}<footer><button className="button secondary" disabled={busy || !workId} onClick={preview}>{busy ? "확인 중…" : "등록 계획 확인"}</button><button className="button primary" disabled={busy || !plan?.apply_available} onClick={apply}>현재 폴더 관리 등록</button></footer></Modal>;
}
