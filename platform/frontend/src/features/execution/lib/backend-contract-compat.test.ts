import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { parseExecutionEvents } from "./execution-event.ts";
import { projectExecution as projectExecutionRaw } from "./execution-projection.ts";

// 判据断言英文文案，显式说英文（默认是中文）。见 run-activity-detail.test.ts。
const projectExecution: typeof projectExecutionRaw = (events, options = {}) =>
  projectExecutionRaw(events, { ...options, lang: options.lang ?? "en" });
import { readPayload, summarizeSessionResources } from "./payload-view.ts";

function loadBackendFixture(name: string) {
  const url = new URL(`../../../../../contracts/fixtures/${name}.jsonl`, import.meta.url);
  return parseExecutionEvents(readFileSync(url, "utf8"));
}

test("backend active fixture exposes attributable tools and a forced-open failure", () => {
  const events = loadBackendFixture("active-run");
  const projection = projectExecution(events);
  const tools = projection.steps.flatMap((step) => step.tools);
  const failedTool = tools.find((tool) => tool.status === "error");

  assert.deepEqual(
    tools.map((tool) => tool.technicalName),
    ["fetch_source_metadata", "fetch_source_metadata"],
  );
  assert.equal(failedTool?.fold, "forced_open");
  assert.equal(failedTool?.error?.impact, "The research tool timed out");
  assert.equal(failedTool?.error?.message.includes("upstream_timeout"), false);
  assert.equal(summarizeSessionResources(events).observedRetries, 1);
});

test("backend completed fixture folds execution while preserving result and unknown cost", () => {
  const events = loadBackendFixture("completed-run");
  const projection = projectExecution(events);
  const artifact = events.find((event) => event.kind === "artifact.created");
  const resources = summarizeSessionResources(events);

  assert.equal(projection.completed, true);
  assert.ok(projection.steps.every((step) => step.fold === "collapsed_auto"));
  assert.equal(artifact && readPayload(artifact).artifact?.name, "Reproducible field sampling survey");
  assert.equal(resources.totalTokens, 2460);
  assert.equal(resources.cost, null);
});

test("backend decision fixture exposes five authoritative choices until resolution", () => {
  const events = loadBackendFixture("decision-run");
  const requiredEvents = events.filter((event) => event.sequence <= 5);
  const pending = projectExecution(requiredEvents);
  const resolved = projectExecution(events);

  assert.equal(pending.decisions.length, 1);
  assert.deepEqual(
    pending.decisions[0].payload.decision?.options.map((choice) => choice.id),
    ["proceed", "revise", "redirect_upstream", "abort", "edit"],
  );
  assert.equal(
    pending.decisions[0].payload.decision?.options.find((choice) => choice.recommended)?.id,
    "redirect_upstream",
  );
  assert.equal(resolved.decisions.length, 0);
});
