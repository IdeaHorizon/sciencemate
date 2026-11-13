import test from "node:test";
import assert from "node:assert/strict";
import type { ExecutionEvent } from "./execution-event.ts";
import { ExecutionReadContractError } from "./execution-read-client.ts";
import {
  mergeCanonicalRunEvents,
  readCanonicalRunEvents,
  readCanonicalRunEventsAfter,
} from "./run-event-reader.ts";

function event(sequence: number, runId = "run-a"): ExecutionEvent {
  return {
    schemaVersion: 1,
    id: `event-${sequence}`,
    sequence,
    at: "2026-08-04T00:00:00Z",
    workspaceId: "workspace-a",
    projectId: "project-a",
    sessionId: "session-a",
    runId,
    origin: "app_command",
    source: {},
    kind: "run.started",
    visibility: "summary",
    payload: {},
  };
}

test("Run event reader paginates with the exact Run filter", async () => {
  const calls: Array<[string, number, number | undefined, string | undefined]> = [];
  const events = await readCanonicalRunEvents({
    async listSessionEvents(sessionId, cursor, limit, runId) {
      calls.push([sessionId, cursor, limit, runId]);
      return cursor === 0
        ? { items: [event(2)], afterSequence: 0, nextAfterSequence: 2, hasMore: true }
        : { items: [event(4)], afterSequence: 2, nextAfterSequence: 4, hasMore: false };
    },
    async listCurrentDecisions() { return { items: [] }; },
  }, "session-a", "run-a");

  assert.deepEqual(events.map((item) => item.sequence), [2, 4]);
  assert.deepEqual(calls, [
    ["session-a", 0, 200, "run-a"],
    ["session-a", 2, 200, "run-a"],
  ]);
});

test("Run event reader rejects crossed Run identity", async () => {
  await assert.rejects(
    () => readCanonicalRunEvents({
      async listSessionEvents() {
        return { items: [event(1, "run-b")], afterSequence: 0, nextAfterSequence: 1, hasMore: false };
      },
      async listCurrentDecisions() { return { items: [] }; },
    }, "session-a", "run-a"),
    (error: unknown) => error instanceof ExecutionReadContractError && /crossed/.test(error.message),
  );
});

test("Run event fallback starts after the last stream sequence", async () => {
  const cursors: number[] = [];
  const events = await readCanonicalRunEventsAfter({
    async listSessionEvents(_sessionId, cursor) {
      cursors.push(cursor);
      return {
        items: cursor === 9 ? [event(12)] : [],
        afterSequence: cursor,
        nextAfterSequence: cursor === 9 ? 12 : cursor,
        hasMore: false,
      };
    },
    async listCurrentDecisions() { return { items: [] }; },
  }, "session-a", "run-a", 9);

  assert.deepEqual(cursors, [9]);
  assert.deepEqual(events.map((item) => item.sequence), [12]);
});

test("page and stream deliveries merge idempotently across session-scoped sequence gaps", () => {
  const merged = mergeCanonicalRunEvents(
    [event(2), event(5)],
    [event(5), event(9)],
    "session-a",
    "run-a",
  );
  assert.deepEqual(merged.map((item) => item.sequence), [2, 5, 9]);
});

test("Run event merge fails loudly on sequence or canonical identity conflicts", () => {
  assert.throws(
    () => mergeCanonicalRunEvents(
      [event(2)],
      [{ ...event(2), id: "different-event" }],
      "session-a",
      "run-a",
    ),
    /conflicting identities/,
  );
  assert.throws(
    () => mergeCanonicalRunEvents(
      [event(2)],
      [{ ...event(2), payload: { changed: true } }],
      "session-a",
      "run-a",
    ),
    /conflicting canonical content/,
  );
});

test("duplicate history preserves array and event identity without reading shared payloads", () => {
  let payloadReads = 0;
  const original = event(1);
  Object.defineProperty(original, "payload", {
    enumerable: true,
    get() { payloadReads += 1; return { output: "large output" }; },
  });
  const history = [original];
  assert.equal(mergeCanonicalRunEvents(history, history, "session-a", "run-a"), history);
  assert.equal(payloadReads, 0, "shared immutable events do not need deep serialization");
  const delivered = structuredClone(event(2));
  const existing = [delivered];
  assert.equal(mergeCanonicalRunEvents(existing, [event(2)], "session-a", "run-a"), existing);
});

test("leaving a page aborts history pagination before another request", async () => {
  const controller = new AbortController();
  let requests = 0;
  await assert.rejects(readCanonicalRunEventsAfter({
    async listSessionEvents(_sessionId, afterSequence, _limit, _runId, _children, signal) {
      requests += 1;
      assert.equal(signal, controller.signal);
      controller.abort();
      return { items: [event(1)], afterSequence, nextAfterSequence: 1, hasMore: true };
    },
    async listCurrentDecisions() { return { items: [] }; },
  }, "session-a", "run-a", 0, controller.signal), { name: "AbortError" });
  assert.equal(requests, 1);
});
