import { belongsToRun } from "./run-scope.ts";
import {
  parseExecutionEvent,
  type ExecutionEvent,
} from "./execution-event.ts";
import {
  ExecutionReadApiError,
  ExecutionReadContractError,
  ExecutionStreamInterruptedError,
  type FetchLike,
} from "./execution-read-client.ts";

const TERMINAL_RUN_STATUSES = new Set([
  "completed",
  "completed_with_warning",
  "incomplete",
  "failed",
  "cancelled",
  "stale_unknown",
]);

export type CanonicalRunStreamEnd = {
  runId: string;
  status: string;
  nextAfterSequence: number;
};

export type TransientRunToken = {
  runId: string;
  text: string;
};

export type TransientRunTokenGap = {
  runId: string;
  recovery: "assistant_message";
};

type SseFrame = {
  event?: string;
  id?: string;
  data: string;
};

export type CanonicalRunEventStreamOptions = {
  baseUrl?: string;
  fetchImpl?: FetchLike;
  sessionId: string;
  runId: string;
  afterSequence: number;
  signal?: AbortSignal;
  onEvent: (event: ExecutionEvent) => void;
  onToken?: (token: TransientRunToken) => void;
  onTokenGap?: (gap: TransientRunTokenGap) => void;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseJson(data: string, label: string): unknown {
  try {
    return JSON.parse(data) as unknown;
  } catch {
    throw new ExecutionReadContractError(`${label} data was not valid JSON`);
  }
}

function parseSequence(value: string | undefined, label: string, minimum: number) {
  if (value === undefined || !/^\d+$/.test(value)) {
    throw new ExecutionReadContractError(`${label} id must be an integer sequence`);
  }
  const sequence = Number(value);
  if (!Number.isSafeInteger(sequence) || sequence < minimum) {
    throw new ExecutionReadContractError(`${label} id must be >= ${minimum}`);
  }
  return sequence;
}

function parseEnd(value: unknown, expectedRunId: string): CanonicalRunStreamEnd {
  if (!isRecord(value)) {
    throw new ExecutionReadContractError("Run stream end data must be an object");
  }
  // run-scope:frame —— 这里判的是**帧**不是事件。子节点跑完了不该关掉父流：
  // 一轮里会有好几个子节点先后结束，只有这条 run 自己的 `run.end` 才是收尾。
  if (value.runId !== expectedRunId) {
    throw new ExecutionReadContractError("Run stream end crossed the requested runId");
  }
  if (typeof value.status !== "string" || !TERMINAL_RUN_STATUSES.has(value.status)) {
    throw new ExecutionReadContractError("Run stream end has an invalid terminal status");
  }
  if (!Number.isSafeInteger(value.nextAfterSequence) || (value.nextAfterSequence as number) < 0) {
    throw new ExecutionReadContractError(
      "Run stream end nextAfterSequence must be a non-negative integer",
    );
  }
  return {
    runId: expectedRunId,
    status: value.status,
    nextAfterSequence: value.nextAfterSequence as number,
  };
}

async function apiError(response: Response): Promise<ExecutionReadApiError> {
  let body: unknown;
  try {
    body = await response.json() as unknown;
  } catch {
    body = undefined;
  }
  const value = isRecord(body) ? body : {};
  return new ExecutionReadApiError({
    status: response.status,
    code: typeof value.code === "string" ? value.code : "stream_failed",
    message: typeof value.message === "string"
      ? value.message
      : `Run event stream failed with status ${response.status}`,
    requestId: typeof value.requestId === "string" || value.requestId === null
      ? value.requestId
      : undefined,
  });
}

/**
 * Follow one authenticated durable Run stream until its explicit terminal end.
 * A normal HTTP EOF is an error: callers must replay from the last accepted
 * sequence before reconnecting, rather than silently presenting a partial Run.
 */
export async function readCanonicalRunEventStream({
  baseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api/v1",
  fetchImpl = globalThis.fetch.bind(globalThis),
  sessionId,
  runId,
  afterSequence,
  signal,
  onEvent,
  onToken,
  onTokenGap,
}: CanonicalRunEventStreamOptions): Promise<CanonicalRunStreamEnd> {
  if (!Number.isSafeInteger(afterSequence) || afterSequence < 0) {
    throw new ExecutionReadContractError("Run stream cursor must be a non-negative integer");
  }
  const query = new URLSearchParams({ runId, afterSequence: String(afterSequence) });
  const response = await fetchImpl(
    `${baseUrl.replace(/\/$/, "")}/sessions/${encodeURIComponent(sessionId)}/events/stream?${query.toString()}`,
    {
      method: "GET",
      credentials: "include",
      headers: { Accept: "text/event-stream" },
      signal,
    },
  );
  if (!response.ok) throw await apiError(response);
  if (!response.body) {
    throw new ExecutionReadContractError("Run event stream response has no body");
  }
  const contentType = response.headers.get("content-type")?.toLowerCase();
  if (contentType && !contentType.includes("text/event-stream")) {
    throw new ExecutionReadContractError("Run event stream returned the wrong content type");
  }

  let cursor = afterSequence;
  let completed: CanonicalRunStreamEnd | undefined;
  let pendingEvent: string | undefined;
  let pendingId: string | undefined;
  let pendingData: string[] = [];

  const dispatch = () => {
    if (pendingData.length === 0 && pendingEvent === undefined && pendingId === undefined) return;
    const frame: SseFrame = {
      event: pendingEvent,
      id: pendingId,
      data: pendingData.join("\n"),
    };
    pendingEvent = undefined;
    pendingId = undefined;
    pendingData = [];

    if (frame.event === "execution") {
      const idSequence = parseSequence(frame.id, "Execution event", 1);
      const event = parseExecutionEvent(parseJson(frame.data, "Execution event"));
      if (event.sequence !== idSequence) {
        throw new ExecutionReadContractError(
          "Execution event SSE id does not match its canonical sequence",
        );
      }
      if (event.sessionId !== sessionId || !belongsToRun(event, runId)) {
        throw new ExecutionReadContractError("Execution event crossed the requested Run identity");
      }
      if (event.sequence <= cursor) {
        throw new ExecutionReadContractError(
          "Execution event sequence did not advance the exclusive cursor",
        );
      }
      cursor = event.sequence;
      onEvent(event);
      return;
    }

    if (frame.event === "end") {
      const end = parseEnd(parseJson(frame.data, "Run stream end"), runId);
      const idSequence = parseSequence(frame.id, "Run stream end", 0);
      if (idSequence !== end.nextAfterSequence || end.nextAfterSequence !== cursor) {
        throw new ExecutionReadContractError(
          "Run stream end cursor does not match the last accepted sequence",
        );
      }
      completed = end;
      return;
    }

    if (frame.event === "reconnect") {
      // 服务端优雅关机：如实收手，从**本地游标**接着来。不当成契约违规 ——
      // 那会让一次重启显示成「数据无法安全展示」（2026-08-23 根因排查）。
      throw new ExecutionStreamInterruptedError(cursor);
    }

    if (frame.event === "token") {
      if (frame.id !== undefined) {
        throw new ExecutionReadContractError(
          "Transient token must not advance the durable sequence cursor",
        );
      }
      const value = parseJson(frame.data, "Transient token");
      // run-scope:frame —— token 是**这条 run 的**流式草稿，子节点的 token
      // 不该往这一轮的回复框里灌字。
      if (!isRecord(value) || value.runId !== runId) {
        throw new ExecutionReadContractError("Transient token crossed the requested runId");
      }
      if (typeof value.text !== "string" || value.text.length === 0) {
        throw new ExecutionReadContractError("Transient token text must be a non-empty string");
      }
      onToken?.({ runId, text: value.text });
      return;
    }

    if (frame.event === "token-gap") {
      if (frame.id !== undefined) {
        throw new ExecutionReadContractError(
          "Transient token gap must not advance the durable sequence cursor",
        );
      }
      const value = parseJson(frame.data, "Transient token gap");
      // run-scope:frame —— 同上，gap 是配 token 的恢复信号。
      if (!isRecord(value) || value.runId !== runId) {
        throw new ExecutionReadContractError("Transient token gap crossed the requested runId");
      }
      if (value.recovery !== "assistant_message") {
        throw new ExecutionReadContractError("Transient token gap has an invalid recovery source");
      }
      onTokenGap?.({ runId, recovery: "assistant_message" });
      return;
    }

    throw new ExecutionReadContractError(
      `Run event stream emitted unsupported event ${frame.event ?? "message"}`,
    );
  };

  const processLine = (line: string) => {
    if (line === "") {
      dispatch();
      return;
    }
    if (line.startsWith(":")) return;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    let value = separator < 0 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") pendingEvent = value;
    else if (field === "id") pendingId = value;
    else if (field === "data") pendingData.push(value);
  };

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (!completed) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newline = buffer.indexOf("\n");
      while (newline >= 0) {
        const rawLine = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        processLine(rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine);
        if (completed) break;
        newline = buffer.indexOf("\n");
      }
    }
    if (!completed) {
      buffer += decoder.decode();
      if (buffer.length > 0) processLine(buffer.endsWith("\r") ? buffer.slice(0, -1) : buffer);
      dispatch();
    }
  } finally {
    // 无论怎么退出（读完、契约抛错、被中止），都把响应体**取消**掉：只 releaseLock
    // 不 cancel，那条 HTTP 连接会一直挂到服务端结束流为止。一个会话停在等人上，
    // 服务端永远不结束；客户端每次重连再开一条 —— 浏览器对同一个源最多六条，
    // 占满后连点按钮的 POST 都排队发不出（2026-09-09 node20，qinp 的浏览器正好六条）。
    try {
      await reader.cancel();
    } catch {
      // 已经关了 / 被中止了 —— 目的达到了
    }
    reader.releaseLock();
  }

  if (completed) return completed;
  if (signal?.aborted) throw signal.reason ?? new DOMException("Aborted", "AbortError");
  throw new ExecutionReadContractError(
    `Run event stream disconnected after sequence ${cursor} without a terminal end`,
  );
}
