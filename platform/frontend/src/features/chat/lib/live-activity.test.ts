import test from "node:test";
import assert from "node:assert/strict";
import type { ChatActivity } from "../types.ts";
import { projectLiveActivity } from "./live-activity.ts";

function activity(id: string, raw: Record<string, unknown>): ChatActivity {
  return { id, label: "raw label must not render", raw };
}

test("internal Harness events collapse into one human-readable live status", () => {
  const activities = [
    activity("1", { event: "harness.transcript", detail: "platform_context_bound" }),
    activity("2", { event: "harness.transcript", detail: "platform_session_start" }),
    activity("3", { event: "harness.transcript", detail: "loop_start" }),
    activity("4", { event: "harness.transcript", detail: "hook_injection" }),
    activity("5", { event: "harness.transcript", detail: "llm_request" }),
  ];

  const projection = projectLiveActivity(activities, { running: true, lang: "en" });

  assert.deepEqual(projection, {
    statusText: "Thinking through the research task",
    tone: "working",
  });
  for (const hidden of ["platform_context_bound", "platform_session_start", "loop_start", "hook_injection", "llm_request"]) {
    assert.equal(projection.statusText.includes(hidden), false);
  }
});

test("a real tool changes the single live status without creating event rows", () => {
  const projection = projectLiveActivity([
    activity("1", { event: "llm_start" }),
    activity("2", { event: "tool_start", tool: "semantic_scholar_search", detail: "graph neural network survey" }),
  ], { running: true, lang: "en" });

  assert.equal(projection.statusText, "Searching literature — graph neural network survey");
  assert.equal(Object.keys(projection).includes("events"), false);
});

test("normalized tool.progress and tool_call both describe the running tool", () => {
  assert.equal(projectLiveActivity([
    activity("1", { event: "tool.progress", tool_name: "arxiv_search", detail: "multimodal agents" }),
  ], { running: true, lang: "en" }).statusText, "Searching literature — multimodal agents");
  assert.equal(projectLiveActivity([
    activity("1", { event: "tool_call", tool: "read_artifact", detail: "survey-report" }),
  ], { running: true, lang: "en" }).statusText, "Reading source material — survey-report");
});

test("scratchpad and transport wrappers never become visible labels", () => {
  const projection = projectLiveActivity([
    activity("1", { event: "harness.started", detail: "started" }),
    activity("2", { event: "tool_start", tool: "write_scratchpad", detail: "private note" }),
  ], { running: true, lang: "en" });

  assert.equal(projection.statusText, "Starting the Project assistant");
  assert.equal(projection.statusText.includes("scratchpad"), false);
  assert.equal(projection.statusText.includes("harness"), false);
});

const view = (patch: Record<string, unknown>) => ({
  phase: "alive", waitingOn: null, outcome: null, error: null, canStop: false,
  answer: { via: "composer" }, label: "", runId: null, since: null, ...patch,
} as never);

test("waiting, permission, and failure states remain explicit", () => {
  // 判据读**局面**，不读状态词：`status: "waiting_human"` 那一版是前端手写的
  // run 状态名单，而 2026-08-27 之后调用点传进来的其实是 view 的 phase
  // （值 "alive"）—— 名单不匹配、整块内容消失，测试却一直绿，因为它自己写死
  // 了 "waiting_human"。测试和真实调用点之间没有共享判据 = 两边都绿。
  assert.equal(projectLiveActivity([], { running: false, lang: "en", view: view({ waitingOn: { kind: "human" } }) }).statusText, "Waiting for your input");
  assert.equal(projectLiveActivity([], { running: false, lang: "en", view: view({ waitingOn: { kind: "permission" } }) }).statusText, "Waiting for permission");
  assert.equal(projectLiveActivity([], { running: false, lang: "en", view: view({ phase: "interrupted" }) }).tone, "attention");
  assert.deepEqual(projectLiveActivity([], { running: false, lang: "en", view: view({ phase: "ended", outcome: "failed" }) }), {
    statusText: "Execution failed",
    tone: "failed",
  });
  assert.equal(projectLiveActivity([
    activity("1", { event: "run.waiting_compute" }),
  ], { running: true, lang: "en" }).statusText, "Waiting for background research");
  assert.equal(projectLiveActivity([
    activity("1", { event: "run.paused" }),
  ], { running: true, lang: "en" }).statusText, "Waiting for your input");
});

test("在跑的时候，局面不抢事件流的话语权", () => {
  // view 说 alive/在动 时，"它此刻在干什么"由最近一条活动事件回答 ——
  // 局面只在"没东西可说"或"在等/结束了"时接管。
  assert.equal(projectLiveActivity([
    activity("1", { event: "tool_start", tool: "arxiv_search", detail: "meiyu front" }),
  ], { running: true, lang: "en", view: view({}) }).statusText, "Searching literature — meiyu front");
});

test("empty model responses expose the actual reason and retry counter", () => {
  const activities = [
    activity("1", {
      event: "harness.transcript",
      detail: "void_turn_rolled_back",
      attempt: 3,
      max_attempts: 5,
    }),
  ];
  const projection = projectLiveActivity(activities, { running: true, lang: "en" });

  assert.deepEqual(projection, {
    statusText: "The model returned no usable response · retry 3/5",
    tone: "attention",
  });
  assert.equal(
    projectLiveActivity(activities, { running: false, lang: "en", view: view({}) }).statusText,
    "The model returned no usable response · retry 3/5",
  );
});

test("a different recovery reason is never mislabeled as an empty model response", () => {
  assert.deepEqual(projectLiveActivity([
    activity("1", {
      event: "run.recovering",
      reason: "worker_reconnect",
      attempt: 2,
      maxAttempts: 5,
    }),
  ], { running: true, lang: "en" }), {
    statusText: "Recovering the execution",
    tone: "attention",
  });
});
