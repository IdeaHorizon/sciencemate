import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { modelSwitchNotice } from "./model-switch-notice.ts";

// 原来这里还有两条「空闲会话里换模型」的用例。那一支已经删了：调用方只在执行
// 进行中才弹这句话（空闲时 chip 当场就变了，不弹）。「空闲时不许说下次才生效」
// 这条保证现在由下面最后一条守 —— 它查的是调用点只在 turnInFlightRef 为真时弹。

test("执行进行中换模型，如实说当前执行换不掉", () => {
  const notice = modelSwitchNotice({ modelLabel: "DeepSeek V4 Pro · 积算" }, "zh");
  assert.match(notice, /^已切换到 DeepSeek V4 Pro · 积算/);
  assert.match(notice, /当前执行仍使用原来的模型/);
  assert.match(notice, /下次执行时生效/);
});

test("拿不到模型名也得是一句完整的话", () => {
  assert.equal(modelSwitchNotice({ modelLabel: null }, "zh"), "已切换模型。当前执行仍使用原来的模型，下次执行时生效。");
  assert.equal(modelSwitchNotice({ modelLabel: "   " }, "zh"), "已切换模型。当前执行仍使用原来的模型，下次执行时生效。");
});

test("空闲时不弹：调用点只在执行进行中才说这句话", () => {
  const workspace = readFileSync(new URL("../components/SessionWorkspace.tsx", import.meta.url), "utf8");
  const at = workspace.indexOf("modelSwitchNotice({");
  assert.ok(at > 0);
  assert.match(workspace.slice(Math.max(0, at - 200), at), /if \(turnInFlightRef\.current\)/);
});
