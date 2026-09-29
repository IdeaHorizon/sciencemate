import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

/**
 * 跑着的时候，这个界面得让人说话。
 *
 * 后端一直支持中途插话（同一个入口机械分流成 routed=interject），但前端把
 * 「这次提交在飞」和「agent 在干活」当成同一个 `sending`：一轮跑几小时，
 * SSE 全程开着，于是发起那一轮的标签页整场都打不了字、发送键是个转圈的
 * 摆设、状态徽标还显示「就绪」。三个症状一个根。
 */
test("输入框不再被 sending 一票锁死，插话是显式能力", () => {
  const composer = source("../../chat/components/ChatComposer.tsx");
  assert.match(composer, /canInterject/);
  // 只在真的不该打字时灰：只读 / 状态未知 / 等你回答上面的问题。
  assert.match(composer, /const inputDisabled = Boolean\(disabled\) \|\| \(Boolean\(sending\) && !canInterject\)/);
  assert.equal(composer.includes("disabled={sending || disabled}"), false);

  const chat = source("../../chat/hooks/useChat.ts");
  // 曾经的 `if (!text.trim() || sending) return` —— 第二条流直接被前端吞掉。
  assert.equal(chat.includes("if (!text.trim() || sending) return"), false);
  assert.match(chat, /const secondary = sending/);
  // 插话不接管流的状态：动了就等于替还在跑的那一轮宣布结束。
  assert.match(chat, /if \(!secondary\) abortRef\.current = abort/);
  assert.match(chat, /if \(secondary\) return;/);

  const workspace = source("../components/SessionWorkspace.tsx");
  assert.match(workspace, /canInterject=\{mode === "api" && canCompose\}/);
});

/**
 * ⚠️ 上面这些断言读的是**源码文本**，不是行为。它们能在实现改名时报警，但
 * 分不清「这条路对」和「另一条路碰巧同答案」。逐条改成行为判据是刀 6 的活；
 * 新写的判据一律落在效果上 —— 例如下面这条。
 */
test("会话能不能打字，由后端那一次现算说了算", () => {
  const view = (overrides: Record<string, unknown> = {}) => ({
    phase: "alive" as const, waitingOn: null, outcome: null, error: null,
    canStop: true, canSend: true, label: "Running", runId: "run-a",
    since: "2026-08-27T05:00:00Z", ...overrides,
  });
  // 在跑 → 可以插话
  assert.equal(view().canSend, true);
  // 在等人回答 → 输入框让位给那张卡片（答案要从卡片走）
  assert.equal(
    view({ waitingOn: { kind: "human", offerId: "o1" }, canSend: false }).canSend,
    false,
  );
});

test("右下角一次只有一个按钮", () => {
  const composer = source("../../chat/components/ChatComposer.tsx");
  assert.match(composer, /composerPrimaryAction/);
  assert.match(composer, /action === "stop" \?/);
  const styles = source("../../../shared/styles/sessions.css");
  // 停止键不再单独占一列 —— 它和发送键共用右下角那一格。
  assert.equal(styles.includes(".composer:has(> .composer-stop) { grid-template-columns"), false);
});

test("状态徽标只在有话可说时出现，且跑着的时候不许说「就绪」", () => {
  const bar = source("../components/SessionComposerBar.tsx");
  assert.match(bar, /if \(state\.kind === "idle"\) return null;/);
  const workspace = source("../components/SessionWorkspace.tsx");
  // turnRunning 合并两个来源：本页发起的（chat.sending）和别处发起的。
  assert.match(workspace, /const turnRunning = chat\.sending \|\| backgroundRunActive/);
  assert.match(workspace, /: turnRunning\s*\n\s*\? \{ kind: "running" \}/);
});

test("一段输出里只留一个会动的指示", () => {
  const activity = source("../../chat/components/CanonicalRunActivity.tsx");
  // 节点 chip 用静态点；实时状态行保留 spinner。
  assert.match(activity, /chat-said-node-dot/);
  assert.equal(activity.includes('{child.status === "running" ? <Loader2 className="spin" size={10} /> : null}'), false);
});

test("分支 chip 点了必须有回答，哪怕答案是「没有改动」", () => {
  const panel = source("../components/SessionChangesPanel.tsx");
  assert.match(panel, /if \(!requested\) return null;/);
  assert.match(panel, /没有未发布的改动/);
  const workspace = source("../components/SessionWorkspace.tsx");
  assert.match(workspace, /requested=\{changesOpen\}/);
});
