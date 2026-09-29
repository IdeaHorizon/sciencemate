import test from "node:test";
import assert from "node:assert/strict";
import { recentSessionRuns, runTitle, runUsageLabel } from "./run-list.ts";

const run = (id: string, sessionId: string) => ({
  id,
  tenantId: "tenant",
  workspaceId: "workspace",
  projectId: "project",
  sessionId,
  parentRunId: null,
  nodeType: "literature_survey",
  status: "waiting_human",
  usage: { promptTokens: 10, completionTokens: 5, totalTokens: 15, cost: null, currency: null, coverage: "partial" as const },
  retryCount: 0,
  createdAt: "2026-08-03T00:00:00Z",
  updatedAt: "2026-08-03T00:00:00Z",
  startedAt: null,
  endedAt: null,
  summary: null,
  view: { phase: "ended" as const, waitingOn: null, outcome: "ok" as const, error: null,
    canStop: false, label: "Completed", runId: "run-a", since: null },
});

test("recent runs are deduplicated by session without inventing entries", () => {
  assert.deepEqual(recentSessionRuns([
    run("child-new", "session-a"),
    run("parent-old", "session-a"),
    run("other", "session-b"),
  ]).map((item) => item.id), ["child-new", "other"]);
});

test("visible runs keep identically named sessions from different projects", () => {
  const first = run("first", "shared-session");
  const second = { ...run("second", "shared-session"), projectId: "other-project" };
  assert.deepEqual(recentSessionRuns([first, second]).map((item) => item.id), ["first", "second"]);
});

test("run labels expose real status and usage coverage", () => {
  const item = run("run-1", "session-a");
  assert.equal(runTitle(item), "Literature Survey");
  // 状态文案读后端那一次现算（`view.label`）——它与徽章、停止按钮、输入框同源。
  // 这里曾经断言前端那份 `runStatusLabel` 名单，而那份名单 2026-09-01 删除：
  // 它零生产调用方，却是前端最后一份 run 状态词表（见 one-vocabulary.test.ts）。
  assert.equal(item.view.label, "Completed");
  assert.equal(runUsageLabel(item.usage), "15 tokens · Cost unavailable · Partial usage");
});
