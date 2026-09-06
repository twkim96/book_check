import type { JobEvent, JobRecord } from "./types";

export const folderlingSteps = [
  "사전 검사·실행 준비", "본문 감사·중복 판정", "파일 입고·잔여 정리",
  "시리즈 정리", "인덱스 갱신", "최종 검사·마무리"
];

const phases = [
  ["validating", "preflight_start", "preflight_result", "preflight_failed", "volume_staging_recovery", "actual_run_started", "review_actions_result", "workflow_started", "legacy_pass_skipped"],
  ["dedup_start", "snapshot_result", "auditor_progress", "auditor_rebaseline", "auditor_rebaseline_result", "dedup", "dedup_result"],
  ["intake_start", "intake", "post_intake_exact", "post_intake_exact_result", "queue_exact", "queue_exact_result", "queue_strong", "queue_strong_result", "active_decision_review_result", "unpack_cleanup", "intake_result"],
  ["bare_volume_context_result", "series_group_start", "series_group_item", "series_group_result"],
  ["index_start", "index_result"],
  ["authorized_performance_metrics", "folderling_summary", "final_doctor_result", "performance_metrics", "actual_run_finished"]
];

function stepFor(phase: string): number {
  if (phase.startsWith("auditor_")) return 1;
  return phases.findIndex((names) => names.includes(phase));
}

export function folderlingProgress(job: JobRecord, events: JobEvent[]) {
  const complete = job.state === "succeeded" || (job.state === "needs_review" && job.error?.code !== "reconfirmation_required");
  const waiting = job.state === "queued" || job.state === "cancelled";
  const stopped = ["failed", "interrupted", "cancelled"].includes(job.state) || (job.state === "needs_review" && !complete);
  let current = -1;
  let seriesSeen = false;
  let retrying = false;
  // The event endpoint may be truncated; the persisted stage/last event still
  // provide a lower bound. Never count audit subphases as overall completion.
  for (const event of [...events, ...(job.last_event ? [job.last_event] : [])]) {
    const step = stepFor(event.phase === "file_result" ? String(event.stage ?? "") : event.phase);
    current = Math.max(current, step);
    seriesSeen ||= step === 3;
    if (event.phase === "auditor_rebaseline") retrying = true;
    if (event.phase === "auditor_rebaseline_result") retrying = false;
  }
  current = Math.max(current, stepFor(job.stage));
  if (waiting) current = -1;
  if (complete) current = 5;
  const states = folderlingSteps.map((_, index) => {
    if (complete) return "완료";
    if (index === 3 && current > 3 && !seriesSeen) return "통과";
    if (index < current) return "완료";
    if (index === current) return stopped ? "중단" : "진행 중";
    return "대기";
  });
  const heading = complete ? (job.state === "needs_review" ? "전체 작업 완료 · 검토할 결과 있음" : "전체 작업 완료")
    : waiting ? (job.state === "cancelled" ? "실행 전 취소" : "전체 작업 대기 중")
    : current < 0 ? (stopped ? "작업 중단 · 마지막 단계 확인 필요" : "전체 작업 준비 중")
    : `${current + 1}/6단계 · ${folderlingSteps[current]}${stopped ? " · 중단" : ""}`;
  return { current, complete, stopped, waiting, states, heading, retrying: retrying && current === 1 };
}
