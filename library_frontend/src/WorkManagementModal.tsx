import { useEffect, useMemo, useState } from "react";

import { api, postJson } from "./api";
import { Modal, PlanCheck, Top } from "./ManagementModals";
import type {
  JobRecord,
  RepresentativePlan,
  WorkAliasPlan,
  WorkAliasRetirePlan,
  WorkManagementDetail,
  WorkMergePlan,
  WorkSplitPlan,
} from "./types";


function toggleNumber(value: number, values: number[], setValues: (next: number[]) => void) {
  setValues(values.includes(value) ? values.filter((item) => item !== value) : [...values, value]);
}


export function WorkManagementModal({
  workId,
  close,
  started,
}: {
  workId: number;
  close: () => void;
  started: (job: JobRecord) => void;
}) {
  const [detail, setDetail] = useState<WorkManagementDetail>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = () => api<WorkManagementDetail>(`/api/management/works/${workId}`)
    .then((value) => { setDetail(value); setError(""); })
    .catch((reason) => setError(reason instanceof Error ? reason.message : "작품 관계를 읽지 못했습니다."));
  useEffect(() => { void load(); }, [workId]);

  const [targetWorkId, setTargetWorkId] = useState("");
  const [mergePlan, setMergePlan] = useState<WorkMergePlan>();
  const previewMerge = async () => {
    setBusy(true); setError("");
    try { setMergePlan(await postJson<WorkMergePlan>("/api/management/works/merge/preview", { source_work_id: workId, target_work_id: Number(targetWorkId) })); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "병합 계획을 만들지 못했습니다."); }
    finally { setBusy(false); }
  };
  const applyMerge = async () => {
    if (!mergePlan) return; setBusy(true);
    try {
      const job = await postJson<JobRecord>("/api/management/works/merge/apply", { source_work_id: workId, target_work_id: Number(targetWorkId), confirm_count: mergePlan.item_count, confirm_plan_sha256: mergePlan.plan_sha256 });
      started(job); close();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "병합을 시작하지 못했습니다."); setBusy(false); }
  };

  const [splitTitle, setSplitTitle] = useState("");
  const [splitVariants, setSplitVariants] = useState<number[]>([]);
  const [splitFolders, setSplitFolders] = useState<number[]>([]);
  const [splitAliases, setSplitAliases] = useState<number[]>([]);
  const [splitPlan, setSplitPlan] = useState<WorkSplitPlan>();
  const splitPayload = { source_work_id: workId, variant_ids: splitVariants, display_title: splitTitle, folder_ids: splitFolders, alias_ids: splitAliases };
  const previewSplit = async () => {
    setBusy(true); setError("");
    try { setSplitPlan(await postJson<WorkSplitPlan>("/api/management/works/split/preview", splitPayload)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "분리 계획을 만들지 못했습니다."); }
    finally { setBusy(false); }
  };
  const applySplit = async () => {
    if (!splitPlan) return; setBusy(true);
    try {
      const job = await postJson<JobRecord>("/api/management/works/split/apply", { ...splitPayload, confirm_count: splitPlan.item_count, confirm_plan_sha256: splitPlan.plan_sha256 });
      started(job); close();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "분리를 시작하지 못했습니다."); setBusy(false); }
  };

  const [aliasKind, setAliasKind] = useState<WorkAliasPlan["alias_kind"]>("core_title");
  const [aliasValue, setAliasValue] = useState("");
  const [aliasFolderId, setAliasFolderId] = useState("");
  const [replaceAliasId, setReplaceAliasId] = useState("");
  const [aliasPlan, setAliasPlan] = useState<WorkAliasPlan>();
  const aliasPayload = { alias_kind: aliasKind, alias_value: aliasValue, work_bucket_id: workId, preferred_folder_id: aliasFolderId ? Number(aliasFolderId) : null, replace_alias_id: replaceAliasId ? Number(replaceAliasId) : null };
  const previewAlias = async () => {
    setBusy(true); setError("");
    try { setAliasPlan(await postJson<WorkAliasPlan>("/api/management/works/aliases/preview", aliasPayload)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "별칭 계획을 만들지 못했습니다."); }
    finally { setBusy(false); }
  };
  const applyAlias = async () => {
    if (!aliasPlan) return; setBusy(true);
    try {
      const job = await postJson<JobRecord>("/api/management/works/aliases/apply", { ...aliasPayload, confirm_count: aliasPlan.item_count, confirm_plan_sha256: aliasPlan.plan_sha256 });
      started(job); close();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "별칭 저장을 시작하지 못했습니다."); setBusy(false); }
  };
  const [retirePlan, setRetirePlan] = useState<WorkAliasRetirePlan>();
  const previewAliasRetire = async (aliasId: number) => {
    setBusy(true); setError("");
    try { setRetirePlan(await postJson<WorkAliasRetirePlan>("/api/management/works/aliases/retire/preview", { alias_id: aliasId })); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "별칭 해제 계획을 만들지 못했습니다."); }
    finally { setBusy(false); }
  };
  const applyAliasRetire = async () => {
    if (!retirePlan) return; setBusy(true);
    try {
      const job = await postJson<JobRecord>("/api/management/works/aliases/retire/apply", { alias_id: retirePlan.alias.alias_id, confirm_count: 1, confirm_plan_sha256: retirePlan.plan_sha256 });
      started(job); close();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "별칭 해제를 시작하지 못했습니다."); setBusy(false); }
  };

  const [representativeVariantId, setRepresentativeVariantId] = useState("");
  const [representativeFileId, setRepresentativeFileId] = useState("");
  const [representativePlan, setRepresentativePlan] = useState<RepresentativePlan>();
  const representativeFiles = useMemo(
    () => detail?.files.filter((file) => String(file.variant_id) === representativeVariantId) ?? [],
    [detail, representativeVariantId],
  );
  const representativePayload = { variant_id: Number(representativeVariantId), file_id: representativeFileId };
  const previewRepresentative = async () => {
    setBusy(true); setError("");
    try { setRepresentativePlan(await postJson<RepresentativePlan>("/api/management/variants/representative/preview", representativePayload)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "대표 변경 계획을 만들지 못했습니다."); }
    finally { setBusy(false); }
  };
  const applyRepresentative = async () => {
    if (!representativePlan) return; setBusy(true);
    try {
      const job = await postJson<JobRecord>("/api/management/variants/representative/apply", { ...representativePayload, confirm_count: 1, confirm_plan_sha256: representativePlan.plan_sha256 });
      started(job); close();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "대표 변경을 시작하지 못했습니다."); setBusy(false); }
  };

  const invalidateSplit = () => setSplitPlan(undefined);
  return <Modal close={close}>
    <Top eyebrow="WORK RELATIONSHIP · 1.3.4" title={`작품 #${workId} 관리`} close={close}/>
    {error && <div className="inline-error">{error}</div>}
    {!detail ? !error && <div className="loading"><span/>작품 관계를 확인하고 있습니다.</div> : <>
      <div className="management-impact">
        <span>작품<strong>{detail.work.display_title ?? "제목 미지정"}</strong></span>
        <span>상태<strong>{detail.work.status}</strong></span>
        <span>판본<strong>{detail.variants.length}개</strong></span>
        <span>관리 폴더<strong>{detail.folders.filter((item) => item.state === "active").length}개</strong></span>
        <span>활성 별칭<strong>{detail.aliases.filter((item) => item.active).length}개</strong></span>
      </div>
      {detail.events.length > 0 && <div className="management-choice-list"><strong>최근 관계 이력</strong>{detail.events.slice(0, 8).map((event, index) => <label key={String(event.event_id ?? index)}><span>#{String(event.event_id ?? "-")} {String(event.action ?? "event")} · {String(event.created_at ?? "")}</span></label>)}</div>}

      <details className="management-work-section" open>
        <summary>별칭과 입고 목적 폴더</summary>
        <p className="management-description">core title 별칭이 맞으면 Folderling이 선택된 폴더로 입고하고 이 작품의 별도 variant로 보존합니다.</p>
        <div className="management-form">
          <label>별칭 종류<select value={aliasKind} onChange={(event) => { setAliasKind(event.target.value as WorkAliasPlan["alias_kind"]); setAliasPlan(undefined); }}><option value="core_title">core title</option><option value="readable_title">읽기 제목</option><option value="folder_name">폴더명</option></select></label>
          <label>입고 폴더<select value={aliasFolderId} onChange={(event) => { setAliasFolderId(event.target.value); setAliasPlan(undefined); }}><option value="">대표 폴더 자동 선택</option>{detail.folders.filter((item) => item.state === "active").map((folder) => <option value={folder.folder_id} key={folder.folder_id}>#{folder.folder_id} {folder.role} · {folder.canonical_path}</option>)}</select></label>
          <label className="management-wide">별칭<input value={aliasValue} onChange={(event) => { setAliasValue(event.target.value); setAliasPlan(undefined); }} placeholder="Re 제로부터 시작하는 이세계 생활"/></label>
          <label>교체할 alias ID<input type="number" min="1" value={replaceAliasId} onChange={(event) => { setReplaceAliasId(event.target.value); setAliasPlan(undefined); }} placeholder="충돌 시에만"/></label>
        </div>
        {aliasPlan && <><div className="management-paths"><small>저장 key: {aliasPlan.alias_key}</small>{aliasPlan.existing_alias && <small>현재 alias #{aliasPlan.existing_alias.alias_id} · work {aliasPlan.existing_alias.work_bucket_id}</small>}</div><PlanCheck sha={aliasPlan.plan_sha256} blockers={aliasPlan.blocked_reasons}/></>}
        <div className="management-choice-list"><strong>현재 활성 별칭</strong>{detail.aliases.filter((item) => item.active).map((alias) => <label key={alias.alias_id}><span>#{alias.alias_id} {alias.alias_kind} · {alias.alias_display} · 폴더 {alias.preferred_folder_id ?? "대표 자동"}</span><button className="button ghost" disabled={busy} onClick={() => previewAliasRetire(alias.alias_id)}>해제 계획</button></label>)}</div>
        {retirePlan && <><PlanCheck sha={retirePlan.plan_sha256} blockers={retirePlan.blocked_reasons}/><footer><button className="button danger" disabled={busy || !retirePlan.apply_available} onClick={applyAliasRetire}>별칭 해제</button></footer></>}
        <footer><button className="button secondary" disabled={busy || !aliasValue.trim()} onClick={previewAlias}>별칭 계획 확인</button><button className="button primary" disabled={busy || !aliasPlan?.apply_available} onClick={applyAlias}>별칭·라우팅 저장</button></footer>
      </details>

      <details className="management-work-section">
        <summary>다른 작품으로 병합</summary>
        <p className="management-description">현재 작품의 variant·폴더·alias를 대상 작품으로 옮기고 현재 work는 이력 보존 상태로 퇴역합니다. 파일 bytes는 움직이지 않습니다.</p>
        <div className="management-form"><label className="management-wide">대상 work ID<input type="number" min="1" value={targetWorkId} onChange={(event) => { setTargetWorkId(event.target.value); setMergePlan(undefined); }}/></label></div>
        {mergePlan && <><div className="management-impact"><span>이동 판본<strong>{mergePlan.item_count}개</strong></span><span>edition 전환 폴더<strong>{mergePlan.demoted_folder_ids.join(", ") || "없음"}</strong></span></div><PlanCheck sha={mergePlan.plan_sha256} blockers={mergePlan.blocked_reasons}/></>}
        <footer><button className="button secondary" disabled={busy || !targetWorkId} onClick={previewMerge}>병합 계획 확인</button><button className="button primary" disabled={busy || !mergePlan?.apply_available} onClick={applyMerge}>작품 병합</button></footer>
      </details>

      <details className="management-work-section">
        <summary>선택 판본을 새 작품으로 분리</summary>
        <p className="management-description">선택한 variant만 새 work로 옮깁니다. 폴더와 alias는 사용자가 명시적으로 함께 선택하며, 물리 파일은 이동하지 않습니다.</p>
        <div className="management-form"><label className="management-wide">새 작품 표시 제목<input value={splitTitle} onChange={(event) => { setSplitTitle(event.target.value); invalidateSplit(); }}/></label></div>
        <div className="management-choice-list"><strong>판본</strong>{detail.variants.filter((item) => item.status === "active").map((variant) => <label key={variant.variant_id}><input type="checkbox" checked={splitVariants.includes(variant.variant_id)} onChange={() => { toggleNumber(variant.variant_id, splitVariants, setSplitVariants); invalidateSplit(); }}/><span>#{variant.variant_id} {variant.variant_kind} · 파일 {variant.active_file_count}개 · 대표 {variant.representative_file_id ?? "없음"}</span></label>)}</div>
        <div className="management-choice-list"><strong>함께 옮길 폴더</strong>{detail.folders.filter((item) => item.state === "active").map((folder) => <label key={folder.folder_id}><input type="checkbox" checked={splitFolders.includes(folder.folder_id)} onChange={() => { toggleNumber(folder.folder_id, splitFolders, setSplitFolders); invalidateSplit(); }}/><span>#{folder.folder_id} {folder.role} · {folder.canonical_path}</span></label>)}</div>
        <div className="management-choice-list"><strong>함께 옮길 별칭</strong>{detail.aliases.filter((item) => item.active).map((alias) => <label key={alias.alias_id}><input type="checkbox" checked={splitAliases.includes(alias.alias_id)} onChange={() => { toggleNumber(alias.alias_id, splitAliases, setSplitAliases); invalidateSplit(); }}/><span>#{alias.alias_id} {alias.alias_kind} · {alias.alias_display}</span></label>)}</div>
        {splitPlan && <><div className="management-impact"><span>이동 판본<strong>{splitPlan.item_count}개</strong></span><span>경로 해제 alias<strong>{splitPlan.cleared_alias_routes.join(", ") || "없음"}</strong></span></div><PlanCheck sha={splitPlan.plan_sha256} blockers={splitPlan.blocked_reasons}/></>}
        <footer><button className="button secondary" disabled={busy || !splitTitle.trim() || !splitVariants.length} onClick={previewSplit}>분리 계획 확인</button><button className="button primary" disabled={busy || !splitPlan?.apply_available} onClick={applySplit}>새 작품으로 분리</button></footer>
      </details>

      <details className="management-work-section">
        <summary>판본 대표 파일 변경</summary>
        <div className="management-form">
          <label>variant<select value={representativeVariantId} onChange={(event) => { setRepresentativeVariantId(event.target.value); setRepresentativeFileId(""); setRepresentativePlan(undefined); }}><option value="">선택</option>{detail.variants.filter((item) => item.status === "active").map((variant) => <option value={variant.variant_id} key={variant.variant_id}>#{variant.variant_id} {variant.variant_kind}</option>)}</select></label>
          <label>새 대표 파일<select value={representativeFileId} onChange={(event) => { setRepresentativeFileId(event.target.value); setRepresentativePlan(undefined); }}><option value="">선택</option>{representativeFiles.map((file) => <option value={file.file_id} key={file.file_id}>{file.canonical_path}</option>)}</select></label>
        </div>
        {representativePlan && <PlanCheck sha={representativePlan.plan_sha256} blockers={representativePlan.blocked_reasons}/>}<footer><button className="button secondary" disabled={busy || !representativeVariantId || !representativeFileId} onClick={previewRepresentative}>대표 변경 확인</button><button className="button primary" disabled={busy || !representativePlan?.apply_available} onClick={applyRepresentative}>대표 파일 변경</button></footer>
      </details>
    </>}
  </Modal>;
}
