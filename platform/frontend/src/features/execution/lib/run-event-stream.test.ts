import test from "node:test";
import assert from "node:assert/strict";
import type { ExecutionEvent } from "./execution-event.ts";
import {
  ExecutionReadApiError,
  ExecutionReadContractError,
  ExecutionStreamInterruptedError,
} from "./execution-read-client.ts";
import { readCanonicalRunEventStream } from "./run-event-stream.ts";

function event(sequence: number, overrides: Partial<ExecutionEvent> = {}): ExecutionEvent {
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
    ...overrides,
  };
}

function sseResponse(chunks: string[], status = 200, contentType = "text/event-stream") {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  }), { status, headers: { "content-type": contentType } });
}

function executionFrame(item: ExecutionEvent, lineBreak = "\n") {
  return [
    `id: ${item.sequence}`,
    "event: execution",
    `data: ${JSON.stringify(item)}`,
    "",
    "",
  ].join(lineBreak);
}

function endFrame(sequence: number, status = "completed", lineBreak = "\n") {
  return [
    `id: ${sequence}`,
    "event: end",
    `data: ${JSON.stringify({ runId: "run-a", status, nextAfterSequence: sequence })}`,
    "",
    "",
  ].join(lineBreak);
}

test("authenticated fetch SSE parses fragmented execution, keepalive, and terminal frames", async () => {
  const first = event(4);
  const second = event(7, { kind: "tool.started" });
  const source = `${executionFrame(first, "\r\n")}: keepalive\r\n\r\n${executionFrame(second)}${endFrame(7)}`;
  const chunks = [source.slice(0, 13), source.slice(13, 91), source.slice(91, 207), source.slice(207)];
  const received: ExecutionEvent[] = [];
  let requestedUrl = "";
  let requestInit: RequestInit | undefined;

  const end = await readCanonicalRunEventStream({
    baseUrl: "https://platform.test/api/v1/",
    sessionId: "session-a",
    runId: "run-a",
    afterSequence: 2,
    fetchImpl: async (url, init) => {
      requestedUrl = url;
      requestInit = init;
      return sseResponse(chunks);
    },
    onEvent: (item) => received.push(item),
  });

  assert.equal(
    requestedUrl,
    "https://platform.test/api/v1/sessions/session-a/events/stream?runId=run-a&afterSequence=2",
  );
  assert.equal(requestInit?.method, "GET");
  assert.equal(requestInit?.credentials, "include");
  assert.deepEqual(requestInit?.headers, { Accept: "text/event-stream" });
  assert.deepEqual(received.map((item) => item.sequence), [4, 7]);
  assert.deepEqual(end, { runId: "run-a", status: "completed", nextAfterSequence: 7 });
});

test("a terminal Run with no events can end at cursor zero", async () => {
  const end = await readCanonicalRunEventStream({
    sessionId: "session-a",
    runId: "run-a",
    afterSequence: 0,
    fetchImpl: async () => sseResponse([endFrame(0, "cancelled")]),
    onEvent() { throw new Error("unexpected execution event"); },
  });
  assert.equal(end.nextAfterSequence, 0);
  assert.equal(end.status, "cancelled");
});

test("transient token frames never enter the canonical sequence", async () => {
  const tokens: string[] = [];
  const durable: number[] = [];
  const token = [
    "event: token",
    `data: ${JSON.stringify({ runId: "run-a", text: "Draft text" })}`,
    "",
    "",
  ].join("\n");
  const end = await readCanonicalRunEventStream({
    sessionId: "session-a",
    runId: "run-a",
    afterSequence: 5,
    fetchImpl: async () => sseResponse([token, endFrame(5)]),
    onEvent: (item) => durable.push(item.sequence),
    onToken: (item) => tokens.push(item.text),
  });
  assert.deepEqual(tokens, ["Draft text"]);
  assert.deepEqual(durable, []);
  assert.equal(end.nextAfterSequence, 5);
});

test("transient token ids are rejected because tokens cannot move afterSequence", async () => {
  const token = [
    "id: 6",
    "event: token",
    `data: ${JSON.stringify({ runId: "run-a", text: "Draft text" })}`,
    "",
    "",
  ].join("\n");
  await assert.rejects(
    () => readCanonicalRunEventStream({
      sessionId: "session-a",
      runId: "run-a",
      afterSequence: 5,
      fetchImpl: async () => sseResponse([token]),
      onEvent() {},
    }),
    /must not advance the durable sequence cursor/,
  );
});

test("token-gap stops transient presentation without moving the durable cursor", async () => {
  const gaps: Array<{ runId: string; recovery: string }> = [];
  const gap = [
    "event: token-gap",
    `data: ${JSON.stringify({ runId: "run-a", recovery: "assistant_message" })}`,
    "",
    "",
  ].join("\n");
  const end = await readCanonicalRunEventStream({
    sessionId: "session-a",
    runId: "run-a",
    afterSequence: 5,
    fetchImpl: async () => sseResponse([gap, endFrame(5)]),
    onEvent() {},
    onTokenGap: (item) => gaps.push(item),
  });
  assert.deepEqual(gaps, [{ runId: "run-a", recovery: "assistant_message" }]);
  assert.equal(end.nextAfterSequence, 5);
});

test("unexpected EOF preserves accepted events but requires replay/reconnect", async () => {
  const received: number[] = [];
  await assert.rejects(
    () => readCanonicalRunEventStream({
      sessionId: "session-a",
      runId: "run-a",
      afterSequence: 0,
      fetchImpl: async () => sseResponse([executionFrame(event(3))]),
      onEvent: (item) => received.push(item.sequence),
    }),
    (error: unknown) => error instanceof ExecutionReadContractError
      && /disconnected after sequence 3/.test(error.message),
  );
  assert.deepEqual(received, [3]);
});

test("stream rejects crossed identity, mismatched ids, and unsupported terminal state", async () => {
  await assert.rejects(
    () => readCanonicalRunEventStream({
      sessionId: "session-a",
      runId: "run-a",
      afterSequence: 0,
      fetchImpl: async () => sseResponse([
        executionFrame(event(1, { runId: "run-b" })),
      ]),
      onEvent() {},
    }),
    /crossed the requested Run identity/,
  );

  const mismatched = executionFrame(event(2)).replace("id: 2", "id: 1");
  await assert.rejects(
    () => readCanonicalRunEventStream({
      sessionId: "session-a",
      runId: "run-a",
      afterSequence: 0,
      fetchImpl: async () => sseResponse([mismatched]),
      onEvent() {},
    }),
    /does not match its canonical sequence/,
  );

  await assert.rejects(
    () => readCanonicalRunEventStream({
      sessionId: "session-a",
      runId: "run-a",
      afterSequence: 0,
      fetchImpl: async () => sseResponse([endFrame(0, "running")]),
      onEvent() {},
    }),
    /invalid terminal status/,
  );
});

test("stream preserves canonical API errors for bounded retry policy", async () => {
  await assert.rejects(
    () => readCanonicalRunEventStream({
      sessionId: "session-a",
      runId: "run-a",
      afterSequence: 0,
      fetchImpl: async () => new Response(
        JSON.stringify({ code: "not_found", message: "Run not found.", requestId: "request-a" }),
        { status: 404, headers: { "content-type": "application/json" } },
      ),
      onEvent() {},
    }),
    (error: unknown) => error instanceof ExecutionReadApiError
      && error.status === 404
      && error.code === "not_found"
      && error.requestId === "request-a",
  );
});

test("a server shutdown is a reconnect, not a contract violation", async () => {
  // 2026-08-23 根因排查：后端优雅关机时会主动收流（`event: reconnect`）。
  // 在此之前流只会默默 EOF，前端抛的是 ExecutionReadContractError，UI 文案
  // 「返回了本版应用无法安全展示的数据」—— 对着一次重启说这句话是错的，而且
  // 它会把界面打到 error 档，而不是可重试的 offline/stale 档。
  const events: ExecutionEvent[] = [];
  const error = await readCanonicalRunEventStream({
    baseUrl: "http://api.test/api/v1",
    fetchImpl: async () => sseResponse([
      executionFrame(event(4)),
      "event: reconnect\ndata: {\"reason\":\"server_shutdown\"}\n\n",
    ]),
    sessionId: "session-a",
    runId: "run-a",
    afterSequence: 3,
    onEvent: (item) => events.push(item),
  }).then(() => null, (err) => err);

  assert.ok(error instanceof ExecutionStreamInterruptedError,
    "关机收流必须是它自己的类型 —— 契约违规那一档会把界面打到 error");
  assert.ok(!(error instanceof ExecutionReadContractError));
  assert.equal(error.nextAfterSequence, 4, "要从**已经收下的**最后一条接着来");
  assert.deepEqual(events.map((e) => e.sequence), [4], "关机前收下的事件不能丢");
});

test("a stream that fails its contract cancels the response body so the connection is released", async () => {
  // 2026-09-09 node20：解析器对一种新事件抛错 → 只 releaseLock 不 cancel → 那条
  // HTTP 连接挂到服务端结束流为止（等人的 run 永远不结束）；重连再开一条，六条
  // 占满后浏览器对这个源再也发不出任何请求，连点按钮的 POST 都在排队。
  let cancelled = false;
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(
        `id: 5\nevent: execution\ndata: ${JSON.stringify(event(5, { sessionId: "another-session" }))}\n\n`,
      ));
    },
    cancel() {
      cancelled = true;
    },
  });
  const response = new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
  await assert.rejects(
    () => readCanonicalRunEventStream({
      baseUrl: "http://backend/api/v1",
      fetchImpl: async () => response,
      sessionId: "session-a",
      runId: "run-a",
      afterSequence: 0,
      onEvent: () => undefined,
    }),
    (error: unknown) => error instanceof ExecutionReadContractError,
  );
  assert.equal(cancelled, true, "契约抛错后响应体必须被取消，连接才会归还");
});

test("unknown event kinds flow through the stream instead of breaking it", async () => {
  const received: string[] = [];
  const source = `${executionFrame(event(3, { kind: "autonomy.downgraded" as ExecutionEvent["kind"] }))}${endFrame(3)}`;
  const end = await readCanonicalRunEventStream({
    baseUrl: "http://backend/api/v1",
    fetchImpl: async () => sseResponse([source]),
    sessionId: "session-a",
    runId: "run-a",
    afterSequence: 0,
    onEvent: (item) => { received.push(item.kind); },
  });
  assert.deepEqual(received, ["autonomy.downgraded"]);
  assert.equal(end.nextAfterSequence, 3);
});
