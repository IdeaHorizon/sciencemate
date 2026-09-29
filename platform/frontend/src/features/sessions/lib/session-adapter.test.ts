import test from "node:test";
import assert from "node:assert/strict";
import {
  adaptCanonicalMessages,
  adaptCanonicalSession,
  groupSession,
  groupSessions,
} from "./session-adapter.ts";

const view = (overrides = {}) => ({
  phase: "ended" as const,
  waitingOn: null,
  outcome: "ok" as const,
  error: null,
  canStop: false,
  canSend: true,
  label: "Completed",
  runId: "run-a",
  since: "2026-08-03T09:00:00Z",
  ...overrides,
});

const canonical = (overrides: Record<string, unknown> = {}) => ({
  id: "session-a",
  projectId: "project-a",
  title: "Map the current evidence landscape",
  lifecycleStatus: "active" as const,
  createdAt: "2026-08-03T09:00:00Z",
  updatedAt: "2026-08-03T10:00:00Z",
  executionView: view(),
  runCount: 1,
  retryCount: 1,
  usage: {
    promptTokens: 100,
    completionTokens: 50,
    totalTokens: 150,
    cost: 0.02,
    currency: "USD",
    coverage: "complete" as const,
  },
  ...overrides,
});

test("a canonical session renders the backend view instead of deriving one", () => {
  const session = adaptCanonicalSession(canonical());
  assert.equal(session.execution.phase, "ended");
  assert.equal(session.execution.outcome, "ok");
  assert.equal(session.runCount, 1);
  assert.equal(session.usage.totalTokens, 150);
  assert.equal(session.retryCount, 1);
  assert.equal(session.baseCommitSha, null);
  assert.equal(session.headCommitSha, null);
});

test("canonical Session carries the two git commits that bound it", () => {
  // 版本 = git 提交（RFC X1）。从前这里是 project_revisions 的 id 与自增号 ——
  // 一个只在那张表里有意义的号码；现在是能拿去 `git show` 的东西。
  const session = adaptCanonicalSession(canonical({
    baseCommitSha: "a".repeat(40),
    headCommitSha: "b".repeat(40),
    aheadBy: 3,
    behindBy: 1,
  }));
  assert.equal(session.baseCommitSha, "a".repeat(40));
  assert.equal(session.headCommitSha, "b".repeat(40));
  assert.equal(session.aheadBy, 3);
  assert.equal(session.behindBy, 1);
});

test("canonical Session exposes the real Git branch and immutable commit identities", () => {
  const session = adaptCanonicalSession(canonical({
    gitBranch: "session/session-a",
    gitBaseCommitSha: "1111111111111111111111111111111111111111",
    gitHeadCommitSha: "2222222222222222222222222222222222222222",
  }));

  assert.equal(session.gitBranch, "session/session-a");
  assert.equal(session.gitBaseCommitSha, "1111111111111111111111111111111111111111");
  assert.equal(session.gitHeadCommitSha, "2222222222222222222222222222222222222222");
});

test("group priority keeps conflict and attention visible before running or unpublished", () => {
  const conflicted = adaptCanonicalSession(canonical({ conflictCount: 1, executionView: view({ phase: "alive", outcome: null }) }));
  const unpublished = adaptCanonicalSession(canonical({ unpublishedChangeCount: 2 }));
  const archived = adaptCanonicalSession(canonical({ lifecycleStatus: "archived" as const }));
  assert.equal(groupSession(conflicted), "needs_attention");
  assert.equal(groupSession(unpublished), "unpublished");
  assert.equal(groupSession(archived), "archived");
});

test("a session with no unpublished file changes stays in recent", () => {
  // 从前这里还有一个 changeSetStatus（open / publishing）参与判断 —— 那是
  // change_sets 表的状态机。现在只有一个事实：git 上有没有还没发布的改动。
  const nothingPending = adaptCanonicalSession(canonical({ unpublishedChangeCount: 0 }));
  const pending = adaptCanonicalSession(canonical({ unpublishedChangeCount: 2 }));

  assert.equal(groupSession(nothingPending), "recent");
  assert.equal(groupSession(pending), "unpublished");
});

test("session groups are sorted by last activity and every session appears once", () => {
  const older = adaptCanonicalSession(canonical({ id: "old", updatedAt: "2026-08-02T10:00:00Z" }));
  const newer = adaptCanonicalSession(canonical({ id: "new", updatedAt: "2026-08-03T11:00:00Z" }));
  const groups = groupSessions([older, newer]);
  assert.deepEqual(groups.recent.map((item) => item.id), ["new", "old"]);
  assert.equal(Object.values(groups).flat().length, 2);
});

test("canonical messages preserve runId independently from commandId", () => {
  const [message] = adaptCanonicalMessages([{
    id: "message-a",
    sessionId: "session-a",
    sequence: 1,
    role: "system",
    content: "Execution failed",
    commandId: "command-a",
    runId: "run-a",
    createdAt: "2026-08-04T00:00:00Z",
  }]);

  assert.equal(message.commandId, "command-a");
  assert.equal(message.runId, "run-a");
  assert.equal(message.role, "system");
});

