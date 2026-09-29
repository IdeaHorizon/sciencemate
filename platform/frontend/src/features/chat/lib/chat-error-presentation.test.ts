import test from "node:test";
import assert from "node:assert/strict";
import { presentChatFailure } from "./chat-error-presentation.ts";

test("能让人动手的失败，给一句人话 + 一个动作", () => {
  const failure = presentChatFailure("Stream failed: 409");
  assert.ok(failure);
  assert.equal(failure.title, "This Session changed before the request could start");
  assert.match(failure.recovery, /Refresh the Session/);
  // 原始实现细节永远不进正文
  assert.equal(JSON.stringify(failure).includes("409"), false);
  assert.equal(JSON.stringify(failure).includes("Stream failed"), false);
});

test("认不出的失败 → 什么都不显示", () => {
  // wangd 2026-08-18（第三次指着同一条）：「这个东西以后严禁以任何形式被我
  // 看到。一点点卵用都没有。」
  //
  // 那条兜底文案「这一轮没能完成 / No usable response was returned /
  // Review the request and send it again」三句话没有一句能让人多做对一件事：
  // 会话没死、下一条消息照常从断点接着跑、technical details 里也只有同一句话。
  //
  // 它存在的唯一理由是"兜底必须返回点什么"——判据是"我认不出这个错误"，
  // 渲染出来的却是"你的研究出问题了"。认不出是我们的无知，不是用户的事故。
  assert.equal(presentChatFailure("internal_transport_wrapper exploded"), null);
  assert.equal(presentChatFailure(""), null);
  assert.equal(presentChatFailure("some brand new failure shape"), null);
});

test("网络断了仍然要说 —— 那条用户真能动手（刷新看状态）", () => {
  const failure = presentChatFailure("TypeError: Failed to fetch");
  assert.ok(failure);
  assert.match(failure.title, /Connection/);
});

test("留下来的每一条都必须带一个用户能做的动作", () => {
  // 加新条目的唯一判据：这条消息能让人多做对一件事吗？不能就别加。
  for (const raw of [
    "Stream failed: 409", "status: 401", "status: 403",
    "status: 429", "status: 503", "Failed to fetch",
  ]) {
    const failure = presentChatFailure(raw);
    assert.ok(failure, raw);
    assert.ok(failure.recovery.trim().length > 0, raw);
  }
});

// ── 结构化失败（后端 run_failures 整份记录）───────────────────────────────

test("后端送来的结构化失败原样呈现，不经字符串猜测", async () => {
  const { presentStructuredChatFailure } = await import("./chat-error-presentation.ts");
  const presented = presentStructuredChatFailure({
    type: "error",
    status: "failed",
    code: "upstream_unavailable",
    title: "模型服务不可用",
    body: "上游模型服务（供应商侧）拒绝或挂断了请求。",
    recovery: "点重试（或再发一条消息）就从断点接着跑。",
    retryable: true,
    detail: "ReadTimeout: ...",
  });
  assert.ok(presented);
  assert.equal(presented.title, "模型服务不可用");
  assert.equal(presented.retryable, true);
  // detail 是证据不是文案 —— 不进正文
  assert.equal(JSON.stringify([presented.title, presented.message, presented.recovery]).includes("ReadTimeout"), false);
});

test("没有 title/body 的 error 帧不算结构化失败（老后端/兜底帧）", async () => {
  const { presentStructuredChatFailure } = await import("./chat-error-presentation.ts");
  assert.equal(presentStructuredChatFailure({ type: "error", message: "boom" }), null);
  assert.equal(presentStructuredChatFailure(undefined), null);
});

test("retryable=false 如实透传 —— 重试按钮不许对用户撒谎", async () => {
  const { presentStructuredChatFailure } = await import("./chat-error-presentation.ts");
  const presented = presentStructuredChatFailure({
    title: "The request was too large to send",
    body: "This turn exceeded the size the research runtime accepts.",
    retryable: false,
  });
  assert.ok(presented);
  assert.equal(presented.retryable, false);
});
