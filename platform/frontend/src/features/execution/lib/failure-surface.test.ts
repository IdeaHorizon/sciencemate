import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { retryProgressLabel, shouldShowFailureDrawer } from "./failure-surface.ts";

test("跑着的时候和跑成功之后，都不把「N failed actions」摆在对话里", () => {
  assert.equal(shouldShowFailureDrawer({ view: { phase: "alive", outcome: null }, hasRunFailureDetail: false }), false);
  assert.equal(shouldShowFailureDrawer({ view: { phase: "ended", outcome: "ok" }, hasRunFailureDetail: false }), false);
  assert.equal(shouldShowFailureDrawer({ view: { phase: "ended", outcome: "ok_with_warning" }, hasRunFailureDetail: false }), false);
});

test("失败就是结论本身的时候，一定要给", () => {
  const unresolved = [
    { phase: "ended", outcome: "failed" },
    { phase: "ended", outcome: "incomplete" },
    { phase: "ended", outcome: "cancelled" },
    { phase: "interrupted", outcome: null },
  ];
  for (const view of unresolved) {
    assert.equal(shouldShowFailureDrawer({ view, hasRunFailureDetail: false }), true, JSON.stringify(view));
  }
  // run 级失败原文在场 = 有一个"根"要解释，不管 run 现在什么状态。
  assert.equal(shouldShowFailureDrawer({ view: { phase: "alive", outcome: null }, hasRunFailureDetail: true }), true);
});

test("重试说第几次，不说「recorded 1」", () => {
  assert.equal(retryProgressLabel({ attempt: 2, maxAttempts: 5 }, "en"), "retry 2/5");
  assert.equal(retryProgressLabel({ attempt: 2, maxAttempts: 5 }), "第 2/5 次重试");
  // 事件没带计数就别编一个 —— 只说在重试。
  assert.equal(retryProgressLabel({ attempt: 2 }, "en"), "retrying");
  assert.equal(retryProgressLabel({}, "en"), "retrying");
  assert.equal(retryProgressLabel({}), "重试中");
});

test("失败/重试的显示层接线：判据真的被调用了", () => {
  const activity = readFileSync(
    new URL("../../chat/components/CanonicalRunActivity.tsx", import.meta.url),
    "utf8",
  );
  assert.match(activity, /shouldShowFailureDrawer\(\{/);
  assert.match(activity, /isTailWindow && showFailureDrawer &&/);
  // 这两样整个删掉了：一个不带上下文的计数 + 一句我们自己的记账状态。
  // 判据看**渲染**（className / JSX），不看注释里提到的字样。
  assert.equal(activity.includes('className="chat-run-event-mismatch"'), false);
  assert.equal(/\{retryCount\} recorded \{retryCount === 1 \? "retry" : "retries"\}/.test(activity), false);

  const detail = readFileSync(
    new URL("./run-activity-detail.ts", import.meta.url),
    "utf8",
  );
  assert.match(detail, /retryProgressLabel\(\{ attempt, maxAttempts: maximum \}, lang\)/);

  const css = readFileSync(
    new URL("../../../shared/styles/chat.css", import.meta.url),
    "utf8",
  );
  // 调度器说的话 = 正文，不是带竖线的旁注卡。
  assert.equal(/\.chat-orchestrator-said \{[^}]*border-left/.test(css), false);
});
