import assert from "node:assert/strict";
import test from "node:test";
import type { ExecutionEvent, ExecutionEventKind } from "./execution-event.ts";
import { projectExecution as projectExecutionRaw } from "./execution-projection.ts";

// 判据断言英文文案，显式说英文（默认是中文）。见 run-activity-detail.test.ts。
const projectExecution: typeof projectExecutionRaw = (events, options = {}) =>
  projectExecutionRaw(events, { ...options, lang: options.lang ?? "en" });

function event(
  sequence: number,
  kind: ExecutionEventKind,
  payload: Record<string, unknown> = {},
): ExecutionEvent {
  return {
    schemaVersion: 1,
    id: `event-${sequence}`,
    sequence,
    at: `2026-08-03T00:00:${String(sequence).padStart(2, "0")}.000Z`,
    workspaceId: "workspace",
    projectId: "project",
    sessionId: "session",
    runId: "run",
    origin: "raw_transcript",
    source: {},
    kind,
    visibility: kind.startsWith("tool.") ? "standard" : "summary",
    payload,
  };
}

const EVENTS: ExecutionEvent[] = [
  event(1, "session.started"),
  event(2, "step.started", { stepId: "step-1", title: "First step" }),
  event(3, "tool.started", { stepId: "step-1", toolCallId: "tool-1", title: "First tool" }),
  event(4, "tool.completed", { stepId: "step-1", toolCallId: "tool-1", summary: "First tool completed" }),
  event(5, "step.completed", { stepId: "step-1", summary: "First step completed" }),
  event(6, "step.started", { stepId: "step-2", title: "Second step" }),
  event(7, "tool.started", { stepId: "step-2", toolCallId: "tool-2", title: "Risky tool" }),
  event(8, "tool.failed", {
    stepId: "step-2",
    toolCallId: "tool-2",
    error: { message: "Unavailable", impact: "Verification blocked" },
  }),
  event(9, "decision.required", {
    stepId: "step-2",
    title: "Choose recovery",
    decision: { id: "decision-1", options: [] },
  }),
  event(10, "artifact.created", {
    stepId: "step-2",
    artifact: { id: "artifact-1", name: "Partial result" },
  }),
  event(11, "session.completed"),
];

test("completed ancestors fold recursively while errors and decisions stay open", () => {
  const projection = projectExecution(EVENTS);
  assert.equal(projection.steps[0].fold, "collapsed_auto");
  assert.equal(projection.steps[0].tools[0].fold, "collapsed_auto");
  assert.equal(projection.steps[1].fold, "forced_open");
  assert.equal(projection.steps[1].tools[0].fold, "forced_open");
  assert.equal(projection.decisions.length, 1);
  assert.equal(projection.results.length, 1);
  assert.equal(projection.completed, true);
});

test("decision.resolved removes the matching pending decision", () => {
  const projection = projectExecution([
    ...EVENTS,
    event(12, "decision.resolved", { decisionId: "decision-1" }),
  ]);
  assert.equal(projection.decisions.length, 0);
});

test("tool association never guesses a canonical step from runId and hides empty preparation", () => {
  const projection = projectExecution([
    event(1, "step.started", { stepId: "canonical-step", title: "Canonical step" }),
    event(2, "tool.started", { toolCallId: "unassociated-tool", title: "Unassociated tool" }),
  ]);
  assert.equal(projection.steps.length, 1);
  assert.equal(projection.steps[0].association, "frontend_unassociated_fallback");
  assert.equal(projection.steps[0].tools.length, 1);
});

test("internal scratchpad tools and their completed preparation step stay out of standard projection", () => {
  const projection = projectExecution([
    event(1, "step.started", { stepId: "root", title: "Orchestrator activity" }),
    event(2, "tool.started", { stepId: "root", toolCallId: "scratch", toolName: "write_scratchpad", arguments: { content: "private" } }),
    event(3, "tool.completed", { stepId: "root", toolCallId: "scratch", toolName: "write_scratchpad", resultSummary: "saved" }),
    event(4, "step.completed", { stepId: "root" }),
    event(5, "session.completed"),
  ]);

  assert.deepEqual(projection.steps, []);
  assert.equal(projection.trace.length, 5, "trace retains the auditable canonical record");
});

test("synthetic context resolution is Trace-only for a plain answer", () => {
  const projection = projectExecution([
    event(1, "tool.started", {
      toolCallId: "context",
      toolName: "resolve_research_context",
      arguments: { project_id: "project" },
    }),
    event(2, "tool.completed", {
      toolCallId: "context",
      toolName: "resolve_research_context",
      resultSummary: "Context resolved",
    }),
    event(3, "session.message", { role: "assistant", content: "A plain answer." }),
    event(4, "session.completed"),
  ]);

  assert.deepEqual(projection.steps, []);
  assert.equal(projection.messages.length, 1);
  assert.equal(projection.trace.length, 4, "Trace retains synthetic context resolution");
});

test("manual expansion can independently reopen a completed step and its tool", () => {
  const projection = projectExecution(EVENTS, {
    manualOpenIds: new Set(["step-1", "tool-1"]),
  });
  assert.equal(projection.steps[0].fold, "manual_open");
  assert.equal(projection.steps[0].tools[0].fold, "manual_open");
});

test("manual closing overrides the open-by-default preference for completed work", () => {
  const projection = projectExecution(EVENTS, {
    autoCollapseCompletedSteps: false,
    autoCollapseCompletedTools: false,
    manualClosedIds: new Set(["step-1", "tool-1"]),
  });
  assert.equal(projection.steps[0].fold, "manual_closed");
  assert.equal(projection.steps[0].tools[0].fold, "manual_closed");
  assert.equal(projection.steps[1].fold, "forced_open", "attention cannot be hidden by a default or manual override");
});
