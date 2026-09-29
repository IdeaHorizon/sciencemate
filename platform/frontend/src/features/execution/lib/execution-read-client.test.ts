import assert from "node:assert/strict";
import test from "node:test";
import type { ExecutionEvent } from "./execution-event.ts";
import {
  createExecutionReadClient,
  ExecutionReadApiError,
  ExecutionReadContractError,
  type DurableDecision,
  type FetchLike,
} from "./execution-read-client.ts";
import {
  ExecutionSessionReader,
  startExecutionSessionPolling,
} from "./execution-session-reader.ts";

function event(sequence: number): ExecutionEvent {
  return {
    schemaVersion: 1,
    id: `api-event-${sequence}`,
    sequence,
    at: `2026-08-03T05:00:0${sequence}Z`,
    workspaceId: "workspace-server-scope",
    projectId: "project-server-scope",
    sessionId: "session/a",
    runId: "run-api",
    origin: "app_command",
    source: {},
    kind: sequence === 1 ? "run.queued" : "run.started",
    visibility: "summary",
    payload: {},
  };
}

function decision(): DurableDecision {
  return {
    tenantId: "tenant-from-server",
    workspaceId: "workspace-server-scope",
    projectId: "project-server-scope",
    sessionId: "session/a",
    id: "decision-api",
    runId: "run-api",
    attemptNo: 1,
    status: "pending",
    subtype: "post_node",
    prompt: "How should the research continue?",
    context: {},
    choices: [
      {
        choiceId: "proceed",
        label: "Continue",
        description: null,
        consequence: "Continue from the current checkpoint.",
        reversible: true,
      },
    ],
    recommendedChoiceId: "proceed",
    selectedChoiceId: null,
    authority: {
      authorityType: "initiating_user",
      authoritySubjects: ["user-1"],
      requiredApprovalCount: 1,
      actionSetVersion: "post-node-v1",
      policySnapshotId: "policy-1",
      expiresAt: null,
    },
    acceptedResponseCount: 0,
    createdAt: "2026-08-03T05:00:00Z",
    updatedAt: "2026-08-03T05:00:00Z",
    resolvedAt: null,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

test("reader uses exclusive afterSequence for initial and incremental EventPage pulls", async () => {
  const requestedCursors: number[] = [];
  const requests: Array<{ input: string; init?: RequestInit }> = [];
  const fetchImpl: FetchLike = async (input, init) => {
    requests.push({ input, init });
    const url = new URL(input, "http://client.test");
    if (url.pathname.endsWith("/events")) {
      const afterSequence = Number(url.searchParams.get("afterSequence"));
      requestedCursors.push(afterSequence);
      return jsonResponse({
        items: afterSequence === 0 ? [event(1)] : [event(2)],
        afterSequence,
        nextAfterSequence: afterSequence + 1,
        hasMore: false,
      });
    }
    return jsonResponse({ items: [decision()] });
  };
  const client = createExecutionReadClient({ baseUrl: "/api/v1", fetchImpl });
  const reader = new ExecutionSessionReader({
    client,
    sessionId: "session/a",
    expectedProjectId: "project-server-scope",
  });

  const initial = await reader.sync();
  const incremental = await reader.sync();

  assert.deepEqual(requestedCursors, [0, 1]);
  assert.deepEqual(initial.snapshot.events.map((item) => item.sequence), [1]);
  assert.deepEqual(incremental.snapshot.events.map((item) => item.sequence), [1, 2]);
  assert.equal(incremental.decisions[0].tenantId, "tenant-from-server");
  assert.equal(incremental.decisions[0].choices[0].choiceId, "proceed");

  const eventRequest = requests.find(({ input }) => input.includes("/events?"));
  assert.ok(eventRequest);
  assert.match(
    eventRequest.input,
    /^\/api\/v1\/sessions\/session%2Fa\/events\?afterSequence=0&limit=200$/,
  );
  assert.equal(eventRequest.init?.method, "GET");
  assert.equal(eventRequest.init?.credentials, "include");
  const headers = new Headers(eventRequest.init?.headers);
  assert.equal(headers.get("Accept"), "application/json");
  assert.equal(
    [...headers.keys()].some((name) => name.toLowerCase().includes("tenant")),
    false,
    "tenant scope must come from server-side auth, not a client header",
  );
  assert.equal(eventRequest.input.toLowerCase().includes("tenant"), false);
});

test("EventPage cursor echo and next cursor are validated", async () => {
  const mismatchedEcho = createExecutionReadClient({
    baseUrl: "/api/v1",
    fetchImpl: async () => jsonResponse({
      items: [event(3)],
      afterSequence: 1,
      nextAfterSequence: 3,
      hasMore: false,
    }),
  });
  await assert.rejects(
    () => mismatchedEcho.listSessionEvents("session", 2),
    (error: unknown) => error instanceof ExecutionReadContractError && /exclusive cursor/.test(error.message),
  );

  const invalidNext = createExecutionReadClient({
    baseUrl: "/api/v1",
    fetchImpl: async () => jsonResponse({
      items: [event(3)],
      afterSequence: 2,
      nextAfterSequence: 4,
      hasMore: false,
    }),
  });
  await assert.rejects(
    () => invalidNext.listSessionEvents("session", 2),
    (error: unknown) => error instanceof ExecutionReadContractError && /greatest returned/.test(error.message),
  );
});

test("Run-scoped event reads send and enforce the canonical runId filter", async () => {
  const requests: string[] = [];
  const client = createExecutionReadClient({
    baseUrl: "/api/v1",
    fetchImpl: async (input) => {
      requests.push(input);
      return jsonResponse({
        items: [event(1)],
        afterSequence: 0,
        nextAfterSequence: 1,
        hasMore: false,
      });
    },
  });

  await client.listSessionEvents("session/a", 0, 200, "run-api");
  assert.match(requests[0], /runId=run-api$/);

  await assert.rejects(
    () => client.listSessionEvents("session/a", 0, 200, "run-other"),
    (error: unknown) => error instanceof ExecutionReadContractError && /requested runId/.test(error.message),
  );
});

test("typed API errors retain canonical code/message without adding scope", async () => {
  const client = createExecutionReadClient({
    baseUrl: "/api/v1",
    fetchImpl: async () => jsonResponse({
      code: "session_not_found",
      message: "Session is not visible in the authorized scope.",
      requestId: "request-1",
    }, 404),
  });
  await assert.rejects(
    () => client.listCurrentDecisions("missing"),
    (error: unknown) => (
      error instanceof ExecutionReadApiError &&
      error.status === 404 &&
      error.code === "session_not_found" &&
      error.requestId === "request-1"
    ),
  );
});

test("session endpoints fail loudly when response identities cross the requested session", async () => {
  const eventClient = createExecutionReadClient({
    baseUrl: "/api/v1",
    fetchImpl: async () => jsonResponse({
      items: [event(1)],
      afterSequence: 0,
      nextAfterSequence: 1,
      hasMore: false,
    }),
  });
  await assert.rejects(
    () => eventClient.listSessionEvents("other-session", 0),
    (error: unknown) => error instanceof ExecutionReadContractError && /requested sessionId/.test(error.message),
  );

  const decisionClient = createExecutionReadClient({
    baseUrl: "/api/v1",
    fetchImpl: async () => jsonResponse({ items: [decision()] }),
  });
  await assert.rejects(
    () => decisionClient.listCurrentDecisions("other-session"),
    (error: unknown) => error instanceof ExecutionReadContractError && /requested sessionId/.test(error.message),
  );
});

test("reader fails loudly when an Event or Decision crosses the URL project", async () => {
  const eventReader = new ExecutionSessionReader({
    sessionId: "session/a",
    expectedProjectId: "url-project",
    client: {
      listSessionEvents: async (_sessionId, afterSequence) => ({
        items: [event(1)],
        afterSequence,
        nextAfterSequence: 1,
        hasMore: false,
      }),
      listCurrentDecisions: async () => ({ items: [] }),
    },
  });
  await assert.rejects(
    () => eventReader.sync(),
    (error: unknown) => error instanceof ExecutionReadContractError && /URL projectId/.test(error.message),
  );
  assert.equal(eventReader.reconciler.snapshot().events.length, 0);

  const decisionReader = new ExecutionSessionReader({
    sessionId: "session/a",
    expectedProjectId: "url-project",
    client: {
      listSessionEvents: async (_sessionId, afterSequence) => ({
        items: [],
        afterSequence,
        nextAfterSequence: afterSequence,
        hasMore: false,
      }),
      listCurrentDecisions: async () => ({ items: [decision()] }),
    },
  });
  await assert.rejects(
    () => decisionReader.sync(),
    (error: unknown) => error instanceof ExecutionReadContractError && /URL projectId/.test(error.message),
  );
});

test("current Decisions accept actionable states and fail loudly on every historical state", async () => {
  const actionableClient = createExecutionReadClient({
    baseUrl: "/api/v1",
    fetchImpl: async () => jsonResponse({
      items: [{ ...decision(), status: "partially_approved", acceptedResponseCount: 1 }],
    }),
  });
  assert.equal(
    (await actionableClient.listCurrentDecisions("session/a")).items[0].status,
    "partially_approved",
  );

  for (const status of ["resolved", "expired", "cancelled"] as const) {
    const historicalClient = createExecutionReadClient({
      baseUrl: "/api/v1",
      fetchImpl: async () => jsonResponse({
        items: [{ ...decision(), status, selectedChoiceId: "proceed" }],
      }),
    });
    await assert.rejects(
      () => historicalClient.listCurrentDecisions("session/a"),
      (error: unknown) => (
        error instanceof ExecutionReadContractError &&
        error.message.includes(`non-actionable status ${status}`)
      ),
    );
  }
});

test("a current Decision read failure is an error, never a successful empty Decision list", async () => {
  const reader = new ExecutionSessionReader({
    sessionId: "session/a",
    expectedProjectId: "project-server-scope",
    client: {
      listSessionEvents: async (_sessionId, afterSequence) => ({
        items: [],
        afterSequence,
        nextAfterSequence: afterSequence,
        hasMore: false,
      }),
      listCurrentDecisions: async () => {
        throw new ExecutionReadApiError({
          status: 503,
          code: "decision_store_unavailable",
          message: "Current Decision state is unavailable.",
        });
      },
    },
  });
  let successfulRead = false;
  const errorSeen = new Promise<unknown>((resolve) => {
    const stop = startExecutionSessionPolling({
      reader,
      onRead: () => { successfulRead = true; },
      onError: (error) => {
        stop();
        resolve(error);
      },
      setTimer: () => "timer",
      clearTimer: () => undefined,
    });
  });

  const error = await errorSeen;
  assert.equal(successfulRead, false);
  assert.ok(error instanceof ExecutionReadApiError);
});

test("polling prevents re-entry and suppresses callbacks after disposal", async () => {
  let settleRead: ((value: ReturnType<ExecutionSessionReader["sync"]> extends Promise<infer T> ? T : never) => void) | undefined;
  let syncCount = 0;
  let tick: (() => void) | undefined;
  let clearCount = 0;
  const reader = {
    sync() {
      syncCount += 1;
      return new Promise<Awaited<ReturnType<ExecutionSessionReader["sync"]>>>((resolve) => {
        settleRead = resolve;
      });
    },
  } as ExecutionSessionReader;
  let readCallbacks = 0;
  let errorCallbacks = 0;

  const stop = startExecutionSessionPolling({
    reader,
    onRead: () => { readCallbacks += 1; },
    onError: () => { errorCallbacks += 1; },
    setTimer: (callback) => {
      tick = callback;
      return "timer";
    },
    clearTimer: (timer) => {
      assert.equal(timer, "timer");
      clearCount += 1;
    },
  });

  assert.equal(syncCount, 1);
  tick?.();
  assert.equal(syncCount, 1, "a pending poll must not be re-entered");
  stop();
  settleRead?.({
    snapshot: {
      phase: "live",
      events: [],
      lastSequence: 0,
      bufferedCount: 0,
      afterSequence: null,
    },
    decisions: [],
  });
  await Promise.resolve();

  assert.equal(clearCount, 1);
  assert.equal(readCallbacks, 0, "a disposed view must not receive pending state");
  assert.equal(errorCallbacks, 0);
});
