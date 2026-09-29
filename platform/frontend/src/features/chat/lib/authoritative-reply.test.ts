import test from "node:test";
import assert from "node:assert/strict";
import { authoritativeReplyText } from "./authoritative-reply.ts";

test("a turn converges to the reply contract's text, not the streamed pile-up", () => {
  // 现场：调度器跨五个节点跑了一个多小时，流式把每一轮的散文首尾相接堆成
  // 一坨留在对话里，下面"第 N 轮"叙述又把同一段画一遍。
  const accumulated = "我先明确一下你的诉求…（起 literature）…（拿到综述）…（决定下一步）";
  assert.equal(
    authoritativeReplyText(accumulated, { reply: "文献调研完成，31 篇论文已入库。" }),
    "文献调研完成，31 篇论文已入库。",
  );
});

test("no authoritative reply means keep what the user can already see", () => {
  // 收敛是"换成更权威的那份"，不是"清空"。老版本 / 异常路径 / pause 逃逸
  // 都可能不带终稿，这时累积文本是这一轮仅有的可见产出。
  const accumulated = "正在起 literature 节点…";
  assert.equal(authoritativeReplyText(accumulated, {}), accumulated);
  assert.equal(authoritativeReplyText(accumulated, { reply: "" }), accumulated);
  assert.equal(authoritativeReplyText(accumulated, { reply: "   " }), accumulated);
  assert.equal(authoritativeReplyText(accumulated, { reply: 42 }), accumulated);
});

test("the reply is trimmed but never re-wrapped", () => {
  assert.equal(authoritativeReplyText("x", { reply: "  收尾了。  " }), "收尾了。");
});
