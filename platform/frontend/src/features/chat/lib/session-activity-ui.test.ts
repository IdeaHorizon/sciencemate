import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const component = readFileSync(
  new URL("../components/CanonicalRunActivity.tsx", import.meta.url),
  "utf8",
);
const messagesComponent = readFileSync(
  new URL("../components/ChatMessages.tsx", import.meta.url),
  "utf8",
);
const humanInput = readFileSync(
  new URL("../components/HumanInputPrompt.tsx", import.meta.url),
  "utf8",
);
const chatStyles = readFileSync(
  new URL("../../../shared/styles/chat.css", import.meta.url),
  "utf8",
);
const chatHook = readFileSync(
  new URL("../hooks/useChat.ts", import.meta.url),
  "utf8",
);
const chatMessages = readFileSync(
  new URL("../components/ChatMessages.tsx", import.meta.url),
  "utf8",
);
const richText = readFileSync(
  new URL("../components/RichText.tsx", import.meta.url),
  "utf8",
);
const sessionStyles = readFileSync(
  new URL("../../../shared/styles/sessions.css", import.meta.url),
  "utf8",
);

test("completed tools fold and artifacts link to their canonical Project record", () => {
  assert.match(component, /tool\.status === "running" \|\| tool\.status === "retrying"/);
  assert.match(component, /\/projects\/\$\{encodeURIComponent\(detail\.run\.projectId\)\}\/artifacts\//);
  assert.equal(component.includes("Used ${step.tools.length}"), false);
});

test("failure is one primary explanation with folded technical history", () => {
  assert.match(component, /chat-run-primary-error/);
  assert.match(component, /<FailedToolHistory tools=\{failedTools\} steps=\{failedSteps\}/);
  assert.match(component, /去执行历史看原始记录/);
  assert.equal(component.includes("tool.error.code"), false);
  assert.equal(component.includes("Stale Unknown"), false);
  assert.match(messagesComponent, /systemRunOwnedByCanonicalActivity/);
  assert.match(messagesComponent, /m\.role === "system" && !systemRunOwnedByCanonicalActivity/);
});

test("terminal parent status reconciles unfinished activity instead of rendering it as live", () => {
  // 「父 run 断了没有」现在是后端 view 回答的一个布尔，不再由显示层按状态词猜。
  // 这一支的真行为判据在 run-activity-detail.test.ts（"terminal parent fixture
  // interrupts unfinished children…"）；这里只守它确实被接了上去。
  assert.match(component, /projectRunActivity\(\s*eventsQuery\.data,[\s\S]{0,200}?view\.phase === "interrupted"/);
  assert.match(component, /tool\.status === "interrupted"/);
  assert.match(component, /这一轮被取消了，没做完的研究活动中断在这里。/);
});

test("HITL requires an explicit confirmation and supports durable canonical pauses", () => {
  // 运行记录里那张是**只读**的；能点的那张由会话级 answer 唯一决定，
  // 渲染在 SessionWorkspace（见 one-vocabulary.test.ts 的"只构造一次"）。
  assert.match(component, /canonicalRunPause\(detail\)/);
  assert.match(component, /<PausedRecord/);
  assert.doesNotMatch(component, /<HumanInputPrompt/);
  assert.match(humanInput, /你不点，它就一直等着。/);
  assert.match(humanInput, /回答并继续/);
  // 谁在问仍然要显示 —— 位置从「Asked by X」独占一行改成标题右侧的一行 meta，
  // 但这条信息不能消失。
  assert.match(humanInput, /human-input-from/);
  assert.match(humanInput, /nodeLabel\(pause\.askingNodeType, t\)/);
  assert.match(humanInput, /zh: "背景"/);
  assert.match(humanInput, /role="radiogroup"/);
  assert.match(humanInput, /disabled=\{!interactive\}/);
  // 「只有驾驶者能回答」不再由这张卡自己解释：它只在后端说"入口就是你"时
  // 才被构造出来，所以它没有"其实点不了"这个状态要交代。拒绝的理由随
  // `answer.via === "none"` 一起下发，落在输入框上。
  assert.doesNotMatch(humanInput, /resumable[,:?]/);
  assert.equal(humanInput.includes("onClick={() => onAnswer?.(option.value)}"), false);
});

test("能操作的东西排在次要材料前面 —— 否则要划过一整屏 payload 才够得着", () => {
  // 实测（1440×900，面板宽 680）：重排前面板高 578px，context 原始 dump 独占
  // 224px，选项排在顶部往下 368px 处。
  const optionsAt = humanInput.indexOf('role="radiogroup"');
  const contextAt = humanInput.indexOf('<summary>{t({ zh: "背景"');
  const freeformAt = humanInput.indexOf("或者自己写一个答案");
  assert.ok(optionsAt > -1 && contextAt > -1 && freeformAt > -1, "三块都要在");
  assert.ok(optionsAt < contextAt, "选项必须排在 context 之前");
  assert.ok(optionsAt < freeformAt, "选项必须排在自由文本框之前");
  // 次要材料折起来，别常驻。permission 类是例外 —— 被批的命令默认可见
  // （见 permission-context-visible.test.ts），所以这里只锁"是折面"，
  // 不锁 open 属性的有无。
  assert.match(humanInput, /<details className="human-input-detail"[^>]*>\s*<summary>\{t\(\{ zh: "背景"/);
});

test("short user messages size to content while long and multiline text wraps", () => {
  assert.match(chatStyles, /\.message\.user \.message-body \{ display: flex; width: 100%; justify-content: flex-end; \}/);
  assert.match(chatStyles, /\.message\.user \.message-body \.message-rich-text \{ width: fit-content; max-width: 86%;/);
  assert.match(chatStyles, /overflow-wrap: break-word; word-break: normal/);
  assert.match(chatStyles, /\.message\.user \.message-body \.message-rich-text > p\.rich-text-paragraph \{ white-space: pre-wrap; \}/);
  assert.match(sessionStyles, /\.session-document-canvas \.message\.user \.message-body p \{ width: auto; max-width: none;/);
  assert.equal(sessionStyles.includes(".message.user .message-body p { width: fit-content"), false);
  assert.equal(sessionStyles.includes(".message.user .message-body p { width: max-content"), false);
});

test("the user bubble DOM has exactly one width-owning wrapper around paragraph content", () => {
  assert.match(chatMessages, /className="message-body"/);
  assert.match(chatMessages, /<RichText[\s\S]*?text=\{m\.text\}/);
  assert.match(richText, /<div className="message-rich-text">/);
  assert.match(richText, /<p className="rich-text-paragraph"/);
  assert.equal(richText.includes('style={{ width:'), false);
});

test("a route-restored Run can show its app-lifetime transient assistant draft", () => {
  assert.match(component, /showStreamingDraft && eventsQuery\.assistantDraft/);
  assert.match(component, /chat-run-streaming-response/);
  assert.match(chatHook, /typeof event\.runId === "string"/);
  assert.match(chatHook, /message\.id === assistantId \? \{ \.\.\.message, runId:/);
});
