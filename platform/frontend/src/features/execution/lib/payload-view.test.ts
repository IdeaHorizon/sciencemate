import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  parseExecutionEvents,
  type ExecutionEvent,
  type ExecutionEventKind,
} from "./execution-event.ts";
import { projectExecution as projectExecutionRaw } from "./execution-projection.ts";

// 判据断言英文文案，显式说英文（默认是中文）。见 run-activity-detail.test.ts。
const projectExecution: typeof projectExecutionRaw = (events, options = {}) =>
  projectExecutionRaw(events, { ...options, lang: options.lang ?? "en" });
import { readPayload, summarizeSessionResources } from "./payload-view.ts";

function event(
  sequence: number,
  kind: ExecutionEventKind,
  payload: Record<string, unknown>,
): ExecutionEvent {
  return {
    schemaVersion: 1,
    id: `direct-${sequence}`,
    sequence,
    at: `2026-08-03T04:00:${String(sequence).padStart(2, "0")}Z`,
    workspaceId: "workspace-direct",
    projectId: "project-direct",
    sessionId: "session-direct",
    runId: "run-direct",
    origin: "raw_transcript",
    source: {
      rawEvent: kind.replace(".", "_"),
      fileRef: "direct-payload-test",
      byteOffset: sequence * 100,
    },
    kind,
    visibility: kind.startsWith("tool.") ? "standard" : "summary",
    payload,
  };
}

test("completed direct payloads produce tool, artifact, message, and usage views", () => {
  const message = readPayload(event(1, "session.message", {
    messageId: "message-1",
    role: "assistant",
    content: "Canonical content",
    text: "Legacy text must not win",
  }));
  const tool = readPayload(event(2, "tool.completed", {
    stepId: "step-1",
    toolCallId: "tool-1",
    toolName: "search_literature",
    resultSummary: "Found 12 de-duplicated sources.",
    title: "Legacy tool title",
    summary: "Legacy tool summary",
  }));
  const artifact = readPayload(event(3, "artifact.created", {
    artifactId: "artifact-1",
    artifactType: "survey",
    name: "Sampling methods survey",
    version: 1,
    frozen: false,
    artifact: { id: "legacy-artifact", name: "Legacy artifact" },
  }));
  const usageEvent = event(4, "usage.updated", {
    promptTokens: 1820,
    completionTokens: 640,
    totalTokens: 2460,
    cost: null,
    coverage: "partial",
    usage: { tokens: 9999, cost: 42, coverage: "complete" },
  });
  const usage = readPayload(usageEvent);
  const resources = summarizeSessionResources([usageEvent]);

  assert.equal(message.text, "Canonical content");
  assert.equal(tool.title, "Search literature");
  assert.equal(tool.technicalName, "search_literature");
  assert.equal(tool.summary, "Found 12 de-duplicated sources.");
  assert.deepEqual(artifact.artifact, {
    id: "artifact-1",
    name: "Sampling methods survey",
    mediaType: "survey",
    version: 1,
  });
  assert.equal(usage.usage?.totalTokens, 2460);
  assert.equal(usage.usage?.cost, null);
  assert.equal(usage.usage?.coverage, "partial");
  assert.equal(resources.totalTokens, 2460);
  assert.equal(resources.cost, null, "canonical cost:null must remain unavailable");
  assert.equal(resources.coverage, "partial");
});

test("active tool.failed direct fields produce a forced-open error view", () => {
  const projection = projectExecution([
    event(1, "step.started", { stepId: "step-active", title: "Validate inputs" }),
    event(2, "tool.started", {
      stepId: "step-active",
      toolCallId: "tool-active",
      toolName: "fetch_source_metadata",
      arguments: {},
    }),
    event(3, "tool.failed", {
      stepId: "step-active",
      toolCallId: "tool-active",
      toolName: "fetch_source_metadata",
      errorCode: "upstream_timeout",
      errorMessage: "The metadata service timed out.",
      retryable: true,
    }),
    event(4, "run.retrying", { attempt: 1, reason: "recovery" }),
  ]);
  const tool = projection.steps[0].tools[0];
  const resources = summarizeSessionResources(projection.trace);

  assert.equal(tool.title, "Read source material");
  assert.equal(tool.fold, "forced_open");
  assert.deepEqual(tool.error, {
    impact: "The research tool timed out",
    message: "The metadata service timed out.",
    recovery: "Adjust the scope or source access, then ask the agent to continue.",
  });
  assert.equal(resources.observedRetries, 1);
});

test("nested UI demo Decision falls back without losing its five options", () => {
  const fixture = JSON.parse(readFileSync(
    new URL("../fixtures/decision-run.json", import.meta.url),
    "utf8",
  )) as unknown;
  const required = parseExecutionEvents(fixture).find(
    (item) => item.kind === "decision.required",
  );
  assert.ok(required);

  const view = readPayload(required);
  const projection = projectExecution([required]);
  assert.equal(view.title, "Primary evidence is unavailable");
  assert.deepEqual(
    view.decision?.options.map((choice) => choice.id),
    ["proceed", "revise", "redirect_upstream", "abort", "edit"],
  );
  assert.equal(
    view.decision?.options.find((choice) => choice.recommended)?.id,
    "redirect_upstream",
  );
  assert.equal(projection.decisions[0].payload.decision?.options.length, 5);
});

test("canonical direct Decision fields never merge with nested demo fields", () => {
  const view = readPayload(event(1, "decision.required", {
    title: "Legacy title",
    prompt: "Canonical prompt",
    choices: [{ choiceId: "proceed", label: "Continue" }],
    decision: {
      id: "legacy-decision",
      reason: "Legacy reason",
      options: [{ id: "legacy-option", label: "Legacy option" }],
    },
  }));

  assert.equal(view.title, undefined);
  assert.equal(view.decision?.reason, "Canonical prompt");
  assert.deepEqual(view.decision?.options.map((choice) => choice.id), ["proceed"]);
  assert.equal(view.correlationId, undefined);
});

test("post-node direct decision exposes all five choices and resolves by decisionId", () => {
  const prompt = "The evidence review found a gap. How should the research continue?";
  const required = event(1, "decision.required", {
    decisionId: "decision-1",
    subtype: "post_node",
    prompt,
    choices: [
      { choiceId: "proceed", label: "Continue" },
      { choiceId: "revise", label: "Revise this step" },
      { choiceId: "redirect_upstream", label: "Go back upstream" },
      { choiceId: "abort", label: "Stop research" },
      { choiceId: "edit", label: "Edit manually" },
    ],
    recommendedChoiceId: "redirect_upstream",
    requiredApprovalCount: 1,
  });
  const view = readPayload(required);
  const pending = projectExecution([required]);
  const resolved = projectExecution([
    required,
    event(2, "decision.resolved", {
      decisionId: "decision-1",
      selectedChoiceId: "redirect_upstream",
      terminal: false,
      acceptedResponseCount: 1,
      requiredApprovalCount: 1,
    }),
  ]);

  assert.equal(view.title, undefined, "canonical Decision without title keeps the generic UI heading");
  assert.equal(view.decision?.prompt, prompt);
  assert.equal(view.decision?.reason, prompt);
  assert.deepEqual(
    view.decision?.options.map((choice) => choice.id),
    ["proceed", "revise", "redirect_upstream", "abort", "edit"],
  );
  assert.equal(
    view.decision?.options.find((choice) => choice.recommended)?.id,
    "redirect_upstream",
  );
  assert.equal(pending.decisions.length, 1);
  assert.equal(resolved.decisions.length, 0);
});

test("known direct usage cost aggregates with currency while unknown stays unavailable", () => {
  const known = event(1, "usage.updated", {
    promptTokens: 100,
    completionTokens: 25,
    totalTokens: 125,
    cost: 0.0125,
    currency: "USD",
    coverage: "complete",
  });
  const summary = summarizeSessionResources([known]);
  assert.equal(summary.totalTokens, 125);
  assert.equal(summary.cost, 0.0125);
  assert.equal(summary.currency, "USD");
  assert.equal(summary.coverage, "complete");
  assert.equal(summary.observedRetries, 0);
});
