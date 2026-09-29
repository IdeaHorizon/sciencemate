import assert from "node:assert/strict";
import test from "node:test";
import type { ExecutionEvent } from "./execution-event.ts";
import {
  EventIdentityConflictError,
  LiveEventReconciler,
} from "./live-event-reconciler.ts";

function event(sequence: number): ExecutionEvent {
  return {
    schemaVersion: 1,
    id: `event-${sequence}`,
    sequence,
    at: `2026-08-03T00:00:0${sequence}.000Z`,
    workspaceId: "workspace",
    projectId: "project",
    sessionId: "session",
    origin: "raw_transcript",
    source: {},
    kind: "run.started",
    visibility: "trace",
    payload: {},
  };
}

test("at-least-once delivery is de-duplicated by event id", () => {
  const reconciler = new LiveEventReconciler();
  reconciler.accept([event(1), event(1)]);
  const snapshot = reconciler.snapshot();
  assert.equal(snapshot.events.length, 1);
  assert.equal(snapshot.lastSequence, 1);
});

test("canonical object key order does not create an identity conflict", () => {
  const reconciler = new LiveEventReconciler();
  const first = {
    ...event(1),
    source: { rawEvent: "command", fileRef: "opaque-ref" },
    payload: { alpha: 1, nested: { left: true, right: false } },
  } satisfies ExecutionEvent;
  const reordered = {
    ...event(1),
    source: { fileRef: "opaque-ref", rawEvent: "command" },
    payload: { nested: { right: false, left: true }, alpha: 1 },
  } satisfies ExecutionEvent;

  reconciler.accept([first, reordered]);
  assert.equal(reconciler.snapshot().events.length, 1);
});

test("sequence gaps stay buffered until afterSequence backfill arrives", async () => {
  const reconciler = new LiveEventReconciler();
  reconciler.accept([event(1), event(4)]);
  assert.equal(reconciler.snapshot().phase, "backfilling");
  assert.equal(reconciler.snapshot().afterSequence, 1);
  assert.deepEqual(reconciler.snapshot().events.map((item) => item.sequence), [1]);

  const requested: number[] = [];
  const snapshot = await reconciler.reconnect(async (afterSequence) => {
    requested.push(afterSequence);
    if (afterSequence === 1) {
      return { items: [event(2)], afterSequence, nextAfterSequence: 2, hasMore: true };
    }
    return { items: [event(3), event(4)], afterSequence, nextAfterSequence: 4, hasMore: false };
  });

  assert.deepEqual(requested, [1, 2]);
  assert.equal(snapshot.phase, "live");
  assert.equal(snapshot.lastSequence, 4);
  assert.deepEqual(snapshot.events.map((item) => item.sequence), [1, 2, 3, 4]);
});

test("same event id with changed identity fails loudly", () => {
  const reconciler = new LiveEventReconciler();
  reconciler.accept([event(1)]);
  assert.throws(
    () => reconciler.accept([{ ...event(1), payload: { changed: true } }]),
    EventIdentityConflictError,
  );
  assert.equal(reconciler.snapshot().phase, "error");
});

test("same event id with changed canonical source or visibility fails loudly", () => {
  const sourceConflict = new LiveEventReconciler();
  sourceConflict.accept([event(1)]);
  assert.throws(
    () => sourceConflict.accept([{
      ...event(1),
      source: { derivedFrom: ["canonical-event-0"] },
    }]),
    EventIdentityConflictError,
  );

  const visibilityConflict = new LiveEventReconciler();
  visibilityConflict.accept([event(1)]);
  assert.throws(
    () => visibilityConflict.accept([{ ...event(1), visibility: "standard" }]),
    EventIdentityConflictError,
  );
});
