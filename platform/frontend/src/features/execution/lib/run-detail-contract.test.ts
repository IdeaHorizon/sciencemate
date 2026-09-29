import test from "node:test";
import assert from "node:assert/strict";
import { assertCanonicalRunDetail } from "./run-detail-contract.ts";

const detail = () => ({
  run: {
    id: "run-a",
    tenantId: "tenant-a",
    workspaceId: "workspace-a",
    projectId: "project-a",
    sessionId: "session-a",
    parentRunId: null,
    nodeType: "chat",
    status: "completed",
    usage: {
      promptTokens: 1,
      completionTokens: 1,
      totalTokens: 2,
      cost: null,
      currency: null,
      coverage: "partial" as const,
    },
    retryCount: 0,
    createdAt: "2026-08-04T00:00:00Z",
    updatedAt: "2026-08-04T00:00:01Z",
    startedAt: "2026-08-04T00:00:00Z",
    endedAt: "2026-08-04T00:00:01Z",
    summary: null,
  view: { phase: "ended" as const, waitingOn: null, outcome: "ok" as const, error: null,
    canStop: false, canSend: true, label: "Completed", runId: "run-a", since: null },
  },
  attempts: [],
  eventCount: 4,
});

test("canonical Run detail accepts its requested identity and persisted counters", () => {
  const value = detail();
  assert.equal(assertCanonicalRunDetail(value, "run-a"), value);
});

test("canonical Run detail fails loudly on crossed identity", () => {
  assert.throws(
    () => assertCanonicalRunDetail(detail(), "run-b"),
    /identity mismatch/,
  );
});

test("canonical Run detail never converts malformed counters to zero", () => {
  const value = detail();
  value.eventCount = -1;
  assert.throws(() => assertCanonicalRunDetail(value, "run-a"), /eventCount/);
});
