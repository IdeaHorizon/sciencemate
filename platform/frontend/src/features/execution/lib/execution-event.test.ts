import assert from "node:assert/strict";
import test from "node:test";
import {
  FixtureContractError,
  parseExecutionEvent,
  parseExecutionEvents,
} from "./execution-event.ts";

function event(overrides: Record<string, unknown> = {}) {
  return {
    schemaVersion: 1,
    id: "event-1",
    sequence: 1,
    at: "2026-08-03T00:00:00.000Z",
    workspaceId: "workspace",
    projectId: "project",
    sessionId: "session",
    origin: "raw_transcript",
    source: { rawEvent: "run_start", fileRef: "fixture-transcript", byteOffset: 0 },
    kind: "run.started",
    visibility: "trace",
    payload: {},
    ...overrides,
  };
}

test("schemaVersion 1 adapter accepts JSONL and orders by sequence", () => {
  const second = event({ id: "event-2", sequence: 2 });
  const first = event();
  const parsed = parseExecutionEvents(`${JSON.stringify(second)}\n${JSON.stringify(first)}\n`);
  assert.deepEqual(parsed.map((item) => item.id), ["event-1", "event-2"]);
});

test("adapter passes unknown canonical event kinds through instead of killing the stream", () => {
  // 名单是后端词表的抄件。后端加了 `autonomy.downgraded` 而抄件没跟上时，这里一抛
  // 整条事件流报废：从同一游标反复重开、每次漏一条连接、六条占满后页签失聪
  // （2026-09-09 node20）。不认识的种类照常送达；名单由契约测试逼着跟上。
  const parsed = parseExecutionEvent(event({ kind: "workflow.advanced" }));
  assert.equal(parsed.kind, "workflow.advanced");
});

test("adapter rejects unsupported schema versions", () => {
  assert.throws(
    () => parseExecutionEvent(event({ schemaVersion: 2 })),
    (error: unknown) => error instanceof FixtureContractError && /schemaVersion/.test(error.message),
  );
});

test("origin-specific source requirements fail closed", () => {
  assert.throws(
    () => parseExecutionEvent(event({ source: { rawEvent: "run_start" } })),
    /raw_transcript requires/,
  );
  assert.throws(
    () => parseExecutionEvent(event({ origin: "adapter_derived", source: { derivedFrom: [] } })),
    /adapter_derived requires non-empty/,
  );
});
