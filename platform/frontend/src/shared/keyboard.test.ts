import assert from "node:assert/strict";
import test from "node:test";
import { isImeHandledKey } from "./keyboard.ts";

test("Chromium / Firefox：选词那下 keydown 还在组合态", () => {
  assert.equal(isImeHandledKey({ isComposing: true, keyCode: 229 }), true);
});

test("WebKit：compositionend 先到，选词那下 keydown 的 isComposing 已经是 false，只剩 keyCode=229 说真话", () => {
  // 2026-09-12 WKWebView + 豆包拼音实测的事件形状 —— 就是把词和消息一起发出去的那一下。
  assert.equal(isImeHandledKey({ isComposing: false, keyCode: 229 }), true);
  // 空格上屏那一下同样只剩 keyCode 说真话（同一次实测）。
  assert.equal(isImeHandledKey({ keyCode: 229 }), true);
});

test("上屏之后的回车是真回车", () => {
  assert.equal(isImeHandledKey({ isComposing: false, keyCode: 13 }), false);
  // 测试里手造的事件什么字段都没有：不是输入法的。
  assert.equal(isImeHandledKey({}), false);
});
