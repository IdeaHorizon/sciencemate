import test from "node:test";
import assert from "node:assert/strict";
import {
  contextWindowFromEvent,
  contextWindowPercent,
  contextWindowSegments,
  contextWindowTone,
  latestContextWindow,
  readContextWindow,
} from "./context-window.ts";
import type { ExecutionEvent } from "../../execution/lib/execution-event.ts";

/**
 * 输入框上方那个「当前上下文 xx%」chip 的数据层。
 *
 * 判据：
 *   1. 读得宽容、拒得干脆 —— 缺字段取 0，窗口不是正数整条不要。
 *   2. 会话读模型那份与在飞事件流里的，按时刻挑最新，不自己算第二遍。
 *   3. 百分比、色调、分段条三样同尺（effectiveTokens），分段铺满不越界。
 */

const payload = {
  turn: 12,
  promptTokens: 480_000,
  estimatedTokens: 400_000,
  effectiveTokens: 520_000,
  window: 1_000_000,
  configuredWindow: 1_000_000,
  compressAt: 0.7,
  emergencyAt: 0.9,
  breakdown: { system: 20_000, tools: 30_000, toolResults: 300_000, summary: 40_000, framework: 10_000, conversation: 120_000 },
  messageCount: 88,
  lastCompaction: { turn: 7, tokensBefore: 800_000, tokensAfter: 300_000 },
};

function event(at: string, overrides: Partial<typeof payload> = {}, kind = "context.updated"): ExecutionEvent {
  return {
    schemaVersion: 1,
    id: `evt-${at}`,
    sequence: 1,
    at,
    workspaceId: "w",
    projectId: "p",
    sessionId: "s",
    runId: "run-live",
    origin: "raw_transcript",
    source: { rawEvent: "context_window" },
    kind: kind as ExecutionEvent["kind"],
    visibility: "summary",
    payload: { ...payload, ...overrides },
  };
}

test("读得宽容：缺的数字取 0，服务端没报 promptTokens 就是 null 而不是 0", () => {
  const state = readContextWindow({ window: 100, effectiveTokens: 40, at: "2026-09-19T08:00:00Z" });
  assert.ok(state);
  assert.equal(state.promptTokens, null);
  assert.equal(state.estimatedTokens, 0);
  assert.equal(state.configuredWindow, 100, "没报配置窗口就等于有效窗口");
  assert.equal(state.compressAt, null, "没报压缩线就没有线");
  assert.deepEqual(state.breakdown, { system: 0, tools: 0, toolResults: 0, summary: 0, framework: 0, conversation: 0 });
  assert.equal(state.lastCompaction, null);
});

test("拒得干脆：窗口不是正数、或没有时刻，整条不要", () => {
  assert.equal(readContextWindow({ ...payload, window: 0, at: "2026-09-19T08:00:00Z" }), null);
  assert.equal(readContextWindow({ ...payload, window: -5, at: "2026-09-19T08:00:00Z" }), null);
  assert.equal(readContextWindow({ ...payload }), null, "没有时刻就没法和别处比新旧");
  assert.equal(readContextWindow("nonsense"), null);
});

test("事件只认 context.updated 这一种", () => {
  assert.equal(contextWindowFromEvent(event("2026-09-19T08:00:00Z", {}, "usage.updated")), null);
  const state = contextWindowFromEvent(event("2026-09-19T08:00:00Z"));
  assert.ok(state);
  assert.equal(state.runId, "run-live");
  assert.equal(state.at, "2026-09-19T08:00:00Z");
});

test("两处来源按时刻挑最新；同一时刻取事件流的", () => {
  const fromSession = readContextWindow({ ...payload, effectiveTokens: 100, at: "2026-09-19T08:05:00Z", runId: "run-old" });
  const older = event("2026-09-19T08:00:00Z", { effectiveTokens: 50 });
  const newer = event("2026-09-19T08:10:00Z", { effectiveTokens: 600_000 });
  const same = event("2026-09-19T08:05:00Z", { effectiveTokens: 77 });

  assert.equal(latestContextWindow(fromSession, [older])?.effectiveTokens, 100, "旧事件不能盖过会话里更新的那条");
  assert.equal(latestContextWindow(fromSession, [older, newer])?.effectiveTokens, 600_000);
  assert.equal(latestContextWindow(fromSession, [same])?.effectiveTokens, 77, "同一时刻取刚到的那条");
  assert.equal(latestContextWindow(null, [])?.effectiveTokens, undefined);
  assert.equal(latestContextWindow(null, [older])?.effectiveTokens, 50);
  assert.equal(latestContextWindow(fromSession, undefined)?.effectiveTokens, 100);
});

test("百分比与色调按 effectiveTokens 而不是服务端实收", () => {
  const state = readContextWindow({ ...payload, at: "2026-09-19T08:00:00Z" });
  assert.ok(state);
  assert.equal(contextWindowPercent(state), 52);
  assert.equal(contextWindowTone(state), "ok");
  assert.equal(contextWindowTone({ ...state, effectiveTokens: 700_000 }), "near", "到了自动压缩线");
  assert.equal(contextWindowTone({ ...state, effectiveTokens: 950_000 }), "over", "过了紧急线");
  assert.equal(contextWindowTone({ ...state, effectiveTokens: 950_000, compressAt: null, emergencyAt: null }), "ok", "没有线就没有近远");
  assert.equal(contextWindowPercent({ ...state, effectiveTokens: 1_100_000 }), 110, "压缩前的瞬间确实会超过 100");
});

test("分段条：各段等比缩放到 effective，再补一段 free 到窗口，加起来刚好铺满", () => {
  const state = readContextWindow({ ...payload, at: "2026-09-19T08:00:00Z" });
  assert.ok(state);
  const segments = contextWindowSegments(state);
  const total = segments.reduce((sum, seg) => sum + seg.share, 0);
  assert.ok(Math.abs(total - 1) < 1e-9, `各段之和 ${total}`);
  assert.equal(segments.at(-1)?.key, "free");
  assert.equal(segments.at(-1)?.tokens, 480_000);
  assert.deepEqual(segments.map((seg) => seg.key), ["system", "tools", "toolResults", "summary", "framework", "conversation", "free"]);
});

test("分段条：超过窗口时没有 free，各段按窗口截到刚好铺满；为 0 的段不画", () => {
  const state = readContextWindow({
    ...payload,
    effectiveTokens: 1_200_000,
    breakdown: { system: 0, tools: 0, toolResults: 900_000, summary: 0, framework: 0, conversation: 300_000 },
    at: "2026-09-19T08:00:00Z",
  });
  assert.ok(state);
  const segments = contextWindowSegments(state);
  assert.deepEqual(segments.map((seg) => seg.key), ["toolResults", "conversation"]);
  const total = segments.reduce((sum, seg) => sum + seg.share, 0);
  assert.ok(Math.abs(total - 1) < 1e-9, `各段之和 ${total}`);
  assert.equal(segments[0].tokens, 750_000);
});

test("分段条：harness 没给分段时只有 free", () => {
  const state = readContextWindow({ window: 1000, effectiveTokens: 400, at: "2026-09-19T08:00:00Z" });
  assert.ok(state);
  assert.deepEqual(contextWindowSegments(state), [{ key: "free", tokens: 600, share: 0.6 }]);
});
