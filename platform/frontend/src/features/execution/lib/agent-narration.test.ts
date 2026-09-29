import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { EXECUTION_EVENT_KINDS } from "./execution-event.ts";

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

/**
 * agent 每轮写的说明要显示出来。
 *
 * 现场（wangd 2026-08-11 试用）：「每一步具体在干啥，我感觉看的一头雾水，
 * 它没有告诉这个用户，我现在干了啥？」
 *
 * UI 上是一串工具名（`Search literature — "Christmas pudding"`），而模型每轮
 * 都写了人话（"前两轮宽泛查询结果不理想，换更精准的词"）。后端已经把它落成
 * `agent.message`；这里是最后一段：前端得认它、渲染它。
 */
test("agent.message 是已知事件类型", () => {
  assert.ok(
    (EXECUTION_EVENT_KINDS as readonly string[]).includes("agent.message"),
    "kind 白名单里没有它 —— 硬编码名单对新事件默认漏过",
  );
});

test("时间线把 agent 的话渲染成一条，而不是只显示工具名", () => {
  const view = source("../components/SessionExecutionView.tsx");
  const projection = source("./execution-projection.ts");

  // projection 把 agent.message 归进消息流；组件按 role 渲染。
  // 查这条链的两端，不查某个字面量在哪个文件里出现过。
  assert.match(projection, /event\.kind === "agent\.message"/);
  assert.match(view, /payload\.role === "agent"/);
  assert.match(view, /execution-agent-narration/);
});

test("agent 的话和 assistant 的最终答复分开", () => {
  // 混成一个，收尾摘要就会被过程碎片污染（"前两轮查得太宽泛"不是结论）。
  const view = source("../components/SessionExecutionView.tsx");
  assert.match(view, /payload\.role === "assistant"/);
  assert.match(view, /payload\.role === "agent"/);
});
