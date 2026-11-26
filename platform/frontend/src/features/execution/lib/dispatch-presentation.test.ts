import test from "node:test";
import assert from "node:assert/strict";
import type { ExecutionEvent } from "./execution-event.ts";
import { projectRunActivity } from "./run-activity-detail.ts";
import { buildRunTimeline, withoutMessageAnchoredSaids } from "./run-activity-timeline.ts";
import { canonicalDispatchEvent } from "./dispatch-presentation.ts";
import { mergeCanonicalRunEvents } from "./run-event-reader.ts";
import { LiveEventReconciler } from "./live-event-reconciler.ts";

function event(sequence: number, overrides: Partial<ExecutionEvent> = {}): ExecutionEvent {
  return {
    schemaVersion: 1, id: `e-${sequence}`, sequence, at: "2026-11-13T06:03:15Z",
    workspaceId: "w", projectId: "p", sessionId: "s", runId: "r",
    origin: "app_command", source: {}, visibility: "summary",
    kind: "node.dispatched",
    payload: { nodeType: "experiment", dispatchId: `dispatch-${sequence}`, background: false },
    ...overrides,
  };
}

for (const node of ["experiment", "_reviewer", "_curator", "literature", "writing", "analysis"]) {
  for (const text of ["PRIVATE_TASK\n".repeat(500), 'experiment: {"experiment_spec":"PRIVATE"}',
    "普通格式的内部指令 PRIVATE，不带任何 JSON 或协议标记"]) {
    test(`${node}: old cached dispatch never renders task text (${text.slice(0, 24)})`, () => {
      const original = event(1, {
        kind: "orchestrator.said",
        source: { rawEvent: "node_dispatch_announced" },
        payload: { aboutNodeType: node, text, submissionId: "old-submission" },
      });
      const before = JSON.stringify(original);
      for (const lang of ["zh", "en"] as const) {
        const activity = projectRunActivity([original], false, lang);
        assert.equal(activity.said.length, 1);
        assert.equal(activity.said[0].dispatch?.nodeType, node);
        assert.equal(JSON.stringify(activity).includes("PRIVATE"), false);
        assert.equal(withoutMessageAnchoredSaids(buildRunTimeline(activity, "r")).length, 1);
      }
      assert.equal(JSON.stringify(original), before, "diagnostic record remains untouched");
    });
  }
}

test("historical dispatches without provenance use their declared relationship", () => {
  const activity = projectRunActivity([event(1, {
    kind: "orchestrator.said", payload: { aboutNodeType: "experiment", text: "PRIVATE" },
  })], false, "zh");
  assert.equal(activity.said[0].dispatch?.nodeType, "experiment");
  assert.equal(JSON.stringify(activity).includes("PRIVATE"), false);
});

test("a typed dispatch uses localized status and never accepts an injected text field", () => {
  const raw = event(1);
  raw.payload.text = "PRIVATE";
  raw.payload.nodeInputs = { experiment_spec: "PRIVATE" };
  assert.equal(projectRunActivity([raw], false, "zh").said[0].text, "已交给实验节点处理。");
  assert.equal(projectRunActivity([raw], false, "en").said[0].text, "Dispatched to Experiment.");
});

test("identical status labels keep separate dispatch identities", () => {
  const activity = projectRunActivity([event(1), event(2)], false, "zh");
  const timeline = buildRunTimeline(activity, "r");
  assert.equal(timeline.length, 2);
  assert.deepEqual(activity.said.map((s) => s.dispatch?.dispatchId), ["dispatch-1", "dispatch-2"]);
});

test("old cache and corrected server history reconcile as one dispatch without leaking text", () => {
  const old = event(1, {
    kind: "orchestrator.said", source: { rawEvent: "node_dispatch_announced" },
    payload: { aboutNodeType: "experiment", text: "PRIVATE", submissionId: "turn-1" },
  });
  const corrected = canonicalDispatchEvent(old);
  assert.notEqual(corrected, old);
  assert.equal(canonicalDispatchEvent(corrected), corrected, "normalization is idempotent");
  const merged = mergeCanonicalRunEvents([old], [corrected], "s", "r");
  assert.equal(merged.length, 1);
  assert.equal(JSON.stringify(merged).includes("PRIVATE"), false);
  const live = new LiveEventReconciler();
  live.accept([old]);
  live.accept([corrected]);
  assert.equal(live.snapshot().events.length, 1);
  assert.equal(JSON.stringify(live.snapshot()).includes("PRIVATE"), false);
  const conflict = { ...corrected, payload: { ...corrected.payload, nodeType: "writing" } };
  assert.throws(() => mergeCanonicalRunEvents(merged, [conflict], "s", "r"), /conflicting/);
  assert.throws(() => live.accept([conflict]), /different canonical event/);
});

test("real replies retain code, JSON, links, and message anchors", () => {
  const text = '查看 [交付](experiments/report.md)，字段 `experiment_spec` 是 {"value": 1}。';
  for (const payload of [
    { text, aboutNodeType: "" },
    { text, aboutNodeType: "experiment" },
    { text, aboutNodeType: "experiment", repliesToMessageId: "message-1" },
  ]) {
    const activity = projectRunActivity([event(1, {
      kind: "orchestrator.said", source: { rawEvent: "interrupt_reply" }, payload,
    })], false, "zh");
    assert.equal(activity.said[0].text, text);
    assert.equal(activity.said[0].dispatch, undefined);
  }
});
