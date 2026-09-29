import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { normalizeChatPause } from "./chat-terminal.ts";
import { toChatPause } from "../../sessions/lib/session-presentation.ts";

/**
 * 2026-08-19 死循环的回归（transcript 逐字实录）：
 *
 *   answer_preview: {"offer_id": "", "choice_id": "run_3517…:1",
 *                    "note": "REVISE (re-run source_node with reviewer feedback)"}
 *   → decision_answer_rejected  code=choice_not_offered  ×3
 *
 * `run_3517…:1` 是 session-presentation 编造的渲染 key。它写下时答复走文案
 * 匹配（无害），后来 HumanInputPrompt 升级成回传 `selected.id`，编造 id 就成了
 * 答复本体。两层各自演化，谁都没报错。
 *
 * 不变量：**编造的 id 只能当渲染 key，不许成为可回传的身份（choiceId）。**
 */

const PENDING = {
  runId: "run_3517cbc833f3454e84e9af8f1b7f25be",
  reason: "waiting_human",
  prompt: "Post-node decision for hypothesis",
  context: "NODE COMPLETED: hypothesis",
  options: ["RETRY REVIEWER (…)", "REVISE (…)"],
  optionDetails: [],
  offer: null,
  recommendedOptionIndex: null,
  askingNodeType: "orchestrator",
  pauseKind: null,
  askedAt: null,
} as never;

test("平台没给 id 时（事故原样）：渲染 key 可以编，choiceId 必须缺席", () => {
  const pause = toChatPause(PENDING)!;
  assert.equal(pause.options.length, 2);
  for (const option of pause.options) {
    assert.ok(option.id, "渲染 key 要有（React 需要）");
    assert.equal(option.choiceId, undefined,
      `编造的 ${option.id} 不许成为可回传身份 —— 它就是三连拒的那个`);
  }
});

test("呈递带真 id 时：choiceId 就是那个 id，offerId/facts 一并到位", () => {
  const pending = {
    ...PENDING as object,
    optionDetails: [
      { id: "retry_reviewer", label: "RETRY REVIEWER (…)", description: "", recommended: true },
      { id: "revise", label: "REVISE (…)", description: "", recommended: false },
    ],
    offer: { offer_id: "1787155777-702875:p865ce44e:oc04a2380", facts: { reviewFailed: true } },
  } as never;
  const pause = toChatPause(pending)!;
  assert.deepEqual(pause.options.map((o) => o.choiceId), ["retry_reviewer", "revise"]);
  assert.equal(pause.offerId, "1787155777-702875:p865ce44e:oc04a2380");
  assert.deepEqual(pause.facts, { reviewFailed: true });
});

test("chat-terminal 同一条规则：option-N 兜底不成为身份", () => {
  const pause = normalizeChatPause({
    question: "q",
    optionDetails: [
      { label: "批准执行", description: "" },              // 无 id
      { id: "reject", label: "拒绝", description: "" },    // 有 id
    ],
  })!;
  assert.equal(pause.options[0].choiceId, undefined);
  assert.equal(pause.options[0].id, "option-1");
  assert.equal(pause.options[1].choiceId, "reject");
});

test("身份只在 answer.ts 里取，且只取 choiceId（源码接线判据）", () => {
  // 2026-09-03 起，"选了什么、发什么"不在组件里判：HumanInputPrompt 只调
  // composeAnswer，把构造结果交出去。所以"不许拿渲染 key 当身份"这条不变量
  // 的落点也跟着搬到 answer.ts —— 组件里根本没有 choice_id 可写。
  const component = readFileSync(
    new URL("../components/HumanInputPrompt.tsx", import.meta.url), "utf8")
    // 注释里可以提事故的名字；扫的是代码。
    .replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  assert.match(component, /composeAnswer\(\{/, "组件必须经 composeAnswer 构造答复");
  assert.ok(!/choice_id/.test(component), "组件里不许出现 choice_id —— 那是构造函数的事");

  const constructor = readFileSync(new URL("./answer.ts", import.meta.url), "utf8");
  assert.match(constructor, /choiceId: selected\.choiceId/, "身份只能来自 selected.choiceId");
  assert.ok(!/choiceId:\s*selected\.id\b/.test(constructor),
    "不许把渲染 key selected.id 当 choice_id 回传 —— 那正是事故本体");
});
