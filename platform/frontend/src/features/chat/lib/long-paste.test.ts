import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { isComposerSendDisabled } from "./composer-keyboard.ts";
import {
  countLines,
  draftHasPendingPaste,
  insertAtSelection,
  isLongPaste,
  LONG_PASTE_MIN_CHARS,
  LONG_PASTE_MIN_LINES,
  pastedFileName,
  pastedFileReference,
  pendingPasteToken,
  replaceToken,
} from "./long-paste.ts";

const lines = (n: number) => Array.from({ length: n }, (_, i) => `row ${i}`).join("\n");

test("几十行的代码照常贴，几千行的日志转文件", () => {
  assert.equal(isLongPaste(lines(30)), false);
  assert.equal(isLongPaste(lines(LONG_PASTE_MIN_LINES - 1)), false);
  assert.equal(isLongPaste(lines(LONG_PASTE_MIN_LINES)), true);
  assert.equal(isLongPaste(lines(5000)), true);
  // 一行不换行的大 JSON 也算：卡顿看的是字符数，不只是行数。
  assert.equal(isLongPaste("x".repeat(LONG_PASTE_MIN_CHARS)), true);
});

test("行数按人数的方式数：结尾换行不多算一行", () => {
  assert.equal(countLines(""), 0);
  assert.equal(countLines("a"), 1);
  assert.equal(countLines("a\nb"), 2);
  assert.equal(countLines("a\nb\n"), 2);
});

test("文件名同一秒贴两次也不撞（后端对同名异内容回 409）", () => {
  const now = new Date(2026, 8, 23, 14, 5, 9);
  assert.equal(pastedFileName(now, 1), "pasted-20260923-140509-1.txt");
  assert.notEqual(pastedFileName(now, 1), pastedFileName(now, 2));
});

test("完整一趟：贴 → 占位时发不出 → 回来换成路径 → 能发", () => {
  const text = lines(3000);
  const draft0 = "帮我看看这段日志：\n\n哪里报错了？";
  const at = draft0.indexOf("\n\n") + 1;
  const token = pendingPasteToken(1, countLines(text), "zh");

  const draft1 = insertAtSelection(draft0, at, at, token);
  assert.ok(draft1.startsWith("帮我看看这段日志：\n"));
  assert.ok(draft1.endsWith("\n哪里报错了？"));
  assert.ok(draft1.length < 200, "输入框里不该出现那 3000 行");
  assert.equal(draftHasPendingPaste(draft1), true);

  // 上传期间用户接着打字 —— 占位按内容找，不按下标。
  const draft2 = `${draft1} 重点看 ERROR`;
  const draft3 = replaceToken(draft2, token, pastedFileReference("sources/pasted-x-1.txt", 3000, "zh"));
  assert.equal(draftHasPendingPaste(draft3), false);
  assert.match(draft3, /sources\/pasted-x-1\.txt/);
  assert.match(draft3, /3000 行/);
  assert.match(draft3, /重点看 ERROR$/);
});

test("英文界面的占位同样拦住发送", () => {
  const draft = `see ${pendingPasteToken(4, 812, "en")}`;
  assert.equal(draftHasPendingPaste(draft), true);
  assert.equal(draftHasPendingPaste(`see ${pastedFileReference("sources/a.txt", 812, "en")}`), false);
});

test("上传失败：原文放回占位处，一个字不丢", () => {
  const text = lines(400);
  const token = pendingPasteToken(2, 400, "zh");
  const draft = insertAtSelection("前缀|后缀", 3, 3, token);
  assert.equal(replaceToken(draft, token, text), `前缀|${text}后缀`);
});

test("上传期间用户删掉了占位 —— 就是不要了，不塞回去", () => {
  assert.equal(replaceToken("什么都没有", pendingPasteToken(3, 900, "zh"), "sources/x.txt"), "什么都没有");
});

test("粘贴替换选中的那段，和原生粘贴一样", () => {
  assert.equal(insertAtSelection("abcdef", 1, 4, "X"), "aXef");
  assert.equal(insertAtSelection("abc", 99, 99, "X"), "abcX");
});

test("输入框真的接了这条路：onPaste 挂上、发送键认占位", () => {
  const composer = readFileSync(new URL("../components/ChatComposer.tsx", import.meta.url), "utf8");
  assert.match(composer, /onPaste=\{handlePaste\}/);
  assert.match(composer, /draftHasPendingPaste\(draft\)/);
  // 发送键判据本身不认识占位 —— 挡住它的是上面那一条，别以为这里兜住了。
  assert.equal(isComposerSendDisabled(pendingPasteToken(1, 900, "zh")), false);

  const workspace = readFileSync(new URL("../../sessions/components/SessionWorkspace.tsx", import.meta.url), "utf8");
  assert.match(workspace, /onLongPaste=\{mode === "api" && canCompose \? pasteAsFile : undefined\}/);
});
