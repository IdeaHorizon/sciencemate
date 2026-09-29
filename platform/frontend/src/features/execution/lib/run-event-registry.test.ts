import test from "node:test";
import assert from "node:assert/strict";
import type { ExecutionEvent } from "./execution-event.ts";
import {
  CanonicalRunEventRegistry,
  type RunEventRegistryOptions,
} from "./run-event-registry.ts";

function event(sequence: number): ExecutionEvent {
  return {
    schemaVersion: 1,
    id: `event-${sequence}`,
    sequence,
    at: "2026-08-06T00:00:00Z",
    workspaceId: "workspace-a",
    projectId: "project-a",
    sessionId: "session-a",
    runId: "run-a",
    origin: "app_command",
    source: {},
    kind: "run.started",
    visibility: "summary",
    payload: {},
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

test("route release keeps one active observer and remount reads its in-memory events", async () => {
  const registry = new CanonicalRunEventRegistry();
  const stream = deferred<{ runId: string; status: string; nextAfterSequence: number }>();
  let streamCalls = 0;
  let streamSignal: AbortSignal | undefined;
  let emit: ((item: ExecutionEvent) => void) | undefined;
  let emitToken: ((item: { runId: string; text: string }) => void) | undefined;
  let emitTokenGap: ((item: { runId: string; recovery: "assistant_message" }) => void) | undefined;
  const cacheSnapshots: number[][] = [];
  const options: RunEventRegistryOptions = {
    key: "session-a:run-a",
    sessionId: "session-a",
    runId: "run-a",
    initialEvents: [event(1)],
    readStream(_cursor, signal, onEvent, onToken, onTokenGap) {
      streamCalls += 1;
      streamSignal = signal;
      emit = onEvent;
      emitToken = onToken;
      emitTokenGap = onTokenGap;
      return stream.promise;
    },
    async readFallback() { return []; },
    onEvents(items) { cacheSnapshots.push(items.map((item) => item.sequence)); },
    onTerminal() {},
  };

  const release = registry.acquire(options, () => {});
  await Promise.resolve();
  release();
  assert.equal(streamSignal?.aborted, false);

  emit?.(event(3));
  emitToken?.({ runId: "run-a", text: "Still working" });
  assert.deepEqual(cacheSnapshots.at(-1), [1, 3]);
  assert.equal(registry.snapshot(options.key)?.assistantDraft, "Still working");
  emitTokenGap?.({ runId: "run-a", recovery: "assistant_message" });
  emitToken?.({ runId: "run-a", text: "unreliable tail" });
  assert.equal(registry.snapshot(options.key)?.assistantDraft, "");
  assert.equal(registry.snapshot(options.key)?.assistantDraftIncomplete, true);

  const remount: number[][] = [];
  const releaseAgain = registry.acquire(options, (snapshot) => {
    remount.push(snapshot.events.map((item) => item.sequence));
  });
  assert.equal(streamCalls, 1);
  assert.deepEqual(remount[0], [1, 3]);
  assert.equal(registry.snapshot(options.key)?.assistantDraft, "");

  stream.resolve({ runId: "run-a", status: "completed", nextAfterSequence: 3 });
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(registry.snapshot(options.key)?.phase, "terminal");
  assert.equal(registry.snapshot(options.key)?.assistantDraft, "");
  assert.equal(registry.snapshot(options.key)?.assistantDraftIncomplete, false);
  releaseAgain();
  registry.stopAll();
});

test("a transport loss pages after the in-memory cursor before reconnecting", async () => {
  const registry = new CanonicalRunEventRegistry();
  const cursors: number[] = [];
  const fallbackCursors: number[] = [];
  let call = 0;
  const terminal = deferred<{ runId: string; status: string; nextAfterSequence: number }>();

  registry.acquire({
    key: "session-a:run-a",
    sessionId: "session-a",
    runId: "run-a",
    initialEvents: [event(2)],
    async readStream(cursor, _signal, onEvent) {
      cursors.push(cursor);
      call += 1;
      if (call === 1) {
        onEvent(event(4));
        throw new Error("network lost");
      }
      return terminal.promise;
    },
    async readFallback(cursor) {
      fallbackCursors.push(cursor);
      return [event(6)];
    },
    onEvents() {},
    onTerminal() {},
    waitBeforeReconnect: async () => {},
  }, () => {});

  for (let turn = 0; turn < 8 && cursors.length < 2; turn += 1) await Promise.resolve();
  assert.deepEqual(fallbackCursors, [4]);
  assert.deepEqual(cursors, [2, 6]);
  terminal.resolve({ runId: "run-a", status: "completed", nextAfterSequence: 6 });
  registry.stopAll();
});

test("after the reconnect limit, re-acquiring on a re-render does not reopen the stream", async () => {
  // 2026-09-09 node20：一条永远契约失败的流，五次失败进 error 后，每次重渲染的
  // acquire 又起一次 follow、又数五次 —— 以渲染的节奏无限重开，每次一条连接。
  const registry = new CanonicalRunEventRegistry();
  let streamCalls = 0;
  const options: RunEventRegistryOptions = {
    key: "session-a:run-a",
    sessionId: "session-a",
    runId: "run-a",
    initialEvents: [event(1)],
    async readStream() {
      streamCalls += 1;
      throw new Error("contract violation");
    },
    async readFallback() {
      return [];
    },
    onEvents() {},
    onTerminal() {},
    maxReconnectAttempts: 2,
    waitBeforeReconnect: async () => undefined,
  };
  let phase = "";
  const release = registry.acquire(options, (snapshot) => { phase = snapshot.phase; });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(streamCalls, 2);
  assert.equal(phase, "error");
  release();
  // 重渲染：再 acquire 一次
  registry.acquire(options, () => undefined);
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(streamCalls, 2, "超过上限后重渲染不许再开流");
});
