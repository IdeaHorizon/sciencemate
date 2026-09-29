import assert from "node:assert/strict";
import test from "node:test";
import {
  composerPrimaryAction,
  isComposerSendDisabled,
  shouldSubmitComposerKey,
} from "./composer-keyboard.ts";

test("composer sends with Enter while Shift+Enter preserves a newline", () => {
  assert.equal(shouldSubmitComposerKey({ key: "Enter" }), true);
  assert.equal(shouldSubmitComposerKey({ key: "Enter", shiftKey: true }), false);
});

test("composer keeps Cmd/Ctrl+Enter compatibility and ignores composition", () => {
  assert.equal(shouldSubmitComposerKey({ key: "Enter", metaKey: true }), true);
  assert.equal(shouldSubmitComposerKey({ key: "Enter", ctrlKey: true }), true);
  assert.equal(
    shouldSubmitComposerKey({ key: "Enter", shiftKey: true, metaKey: true }),
    true,
  );
  assert.equal(shouldSubmitComposerKey({ key: "Enter", isComposing: true }), false);
  assert.equal(shouldSubmitComposerKey({ key: "Escape" }), false);
});

test("中文输入法选词的回车不是发送 —— WebKit 上 isComposing 已经是 false，只有 keyCode=229 还说得出真相", () => {
  // 2026-09-12 WKWebView + 豆包拼音实测：打 "agent" 回车上屏，keydown 长这样。
  assert.equal(shouldSubmitComposerKey({ key: "Enter", isComposing: false, keyCode: 229 }), false);
  // 上屏之后再按的那下回车才是发送。
  assert.equal(shouldSubmitComposerKey({ key: "Enter", isComposing: false, keyCode: 13 }), true);
});

test("composer send is disabled for blank drafts and while sending", () => {
  assert.equal(isComposerSendDisabled("", false), true);
  assert.equal(isComposerSendDisabled("  \n", false), true);
  assert.equal(isComposerSendDisabled("Research this", true), true);
  assert.equal(isComposerSendDisabled("Research this", false), false);
});

test("跑着也能发：能插话的会话里，sending 不再一票否决发送键", () => {
  assert.equal(isComposerSendDisabled("继续查一下 1950 年代的配给制", true, true), false);
  // 不能插话的场合（全局启动器）保持原样：别让人重复提交同一句。
  assert.equal(isComposerSendDisabled("hi", true, false), true);
  // 没字永远发不出去。
  assert.equal(isComposerSendDisabled("   ", false, true), true);
});

test("右下角一次只有一个按钮，且总是有意义的那个", () => {
  assert.equal(composerPrimaryAction({ draft: "插一句", running: true, canStop: true }), "send");
  assert.equal(composerPrimaryAction({ draft: "", running: true, canStop: true }), "stop");
  assert.equal(composerPrimaryAction({ draft: "", running: false, canStop: true }), "send");
  // 停不了的时候不给停止键 —— 一个按了没反应的按钮比没有按钮更糟。
  assert.equal(composerPrimaryAction({ draft: "", running: true, canStop: false }), "send");
});
