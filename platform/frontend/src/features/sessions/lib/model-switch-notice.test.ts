import test from "node:test";
import assert from "node:assert/strict";
import { modelSwitchNotice } from "./model-switch-notice.ts";

test("空闲会话里换模型，不许说「下一轮才生效」", () => {
  const notice = modelSwitchNotice({ turnInFlight: false, modelLabel: "Kimi K3 · 积算" });
  assert.equal(notice, "已换成 Kimi K3 · 积算，下一条消息就用它");
  assert.equal(/下一轮/.test(notice), false);
});

test("这一轮在飞的时候，如实说当下这轮换不掉", () => {
  const notice = modelSwitchNotice({ turnInFlight: true, modelLabel: "DeepSeek V4 Pro · 积算" });
  assert.match(notice, /^已换成 DeepSeek V4 Pro · 积算/);
  assert.match(notice, /这一轮已经在跑/);
  assert.match(notice, /下一轮开始生效/);
});

test("拿不到模型名也得是一句完整的话", () => {
  assert.equal(modelSwitchNotice({ turnInFlight: false, modelLabel: null }), "已换模型，下一条消息就用它");
  assert.equal(modelSwitchNotice({ turnInFlight: false, modelLabel: "   " }), "已换模型，下一条消息就用它");
});
