import assert from "node:assert/strict";
import test from "node:test";
import { folderlingProgress } from "./src/folderlingProgress.ts";

const event = (phase, fields = {}) => ({ phase, recorded_at: "2026-09-06T00:00:00Z", ...fields });
const job = (fields = {}) => ({
  job_type: "service_folderling", state: "running", stage: "validating",
  progress: { current: 0, total: 0 }, error: null, result: null, ...fields
});

test("deep scan completion is only the second overall stage", () => {
  const progress = folderlingProgress(job({ progress: { current: 1328, total: 1328 } }), [
    event("auditor_progress", { audit_phase: "deep_scan", completed: 1328, total: 1328 })
  ]);
  assert.equal(progress.current, 1);
  assert.equal(progress.complete, false);
  assert.match(progress.heading, /2\/6단계/);
  assert.deepEqual(progress.states, ["완료", "진행 중", "대기", "대기", "대기", "대기"]);
});

test("rebaseline repeats audit without advancing overall stage", () => {
  const events = [event("auditor_progress"), event("auditor_rebaseline"), event("auditor_progress", { audit_phase: "text_analysis" })];
  const retry = folderlingProgress(job(), events);
  assert.equal(retry.current, 1);
  assert.equal(retry.retrying, true);
  const finished = folderlingProgress(job(), [...events, event("auditor_rebaseline_result")]);
  assert.equal(finished.current, 1);
  assert.equal(finished.retrying, false);
  assert.equal(finished.complete, false);
});

test("index starts stage five even when optional series events are absent", () => {
  const progress = folderlingProgress(job(), [event("intake_result"), event("index_start")]);
  assert.equal(progress.current, 4);
  assert.equal(progress.states[3], "통과");
  assert.equal(progress.complete, false);
});

test("final doctor result while running is stage six, not job completion", () => {
  const progress = folderlingProgress(job(), [event("final_doctor_result")]);
  assert.equal(progress.current, 5);
  assert.equal(progress.complete, false);
  assert.equal(progress.states[5], "진행 중");
});

for (const state of ["succeeded", "needs_review"]) {
  test(`${state} completes all stages`, () => {
    const progress = folderlingProgress(job({ state, stage: state }), []);
    assert.equal(progress.complete, true);
    assert.deepEqual(progress.states, Array(6).fill("완료"));
  });
}

test("review requiring pre-execution reconfirmation does not complete", () => {
  const progress = folderlingProgress(job({ state: "needs_review", stage: "needs_review", error: { code: "reconfirmation_required" } }), [event("preflight_start")]);
  assert.equal(progress.complete, false);
  assert.equal(progress.current, 0);
  assert.equal(progress.stopped, true);
});

for (const state of ["queued", "cancelled"]) {
  test(`${state} does not claim a stage has run`, () => {
    const progress = folderlingProgress(job({ state }), []);
    assert.equal(progress.current, -1);
    assert.equal(progress.complete, false);
    assert.deepEqual(progress.states, Array(6).fill("대기"));
  });
}

for (const state of ["failed", "interrupted"]) {
  test(`${state} preserves the last known stage`, () => {
    const progress = folderlingProgress(job({ state, stage: state }), [event("intake_start"), event(`job_${state}`)]);
    assert.equal(progress.current, 2);
    assert.equal(progress.states[2], "중단");
    assert.equal(progress.states[3], "대기");
    assert.equal(progress.complete, false);
  });
}

test("truncated event history falls back to stage or last_event", () => {
  assert.equal(folderlingProgress(job({ stage: "auditor_deep_scan" }), []).current, 1);
  assert.equal(folderlingProgress(job({ last_event: event("file_result", { stage: "queue_exact" }) }), []).current, 2);
  assert.equal(folderlingProgress(job({ stage: "index_start" }), [event("auditor_progress")]).current, 4);
  assert.equal(folderlingProgress(job({ state: "failed", stage: "failed" }), []).current, -1);
});
