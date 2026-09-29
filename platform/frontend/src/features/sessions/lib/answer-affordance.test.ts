/**
 * 「输入交给谁」—— 用**现场那两份真 payload** 回放。
 *
 * ## 这个文件替掉了什么
 *
 * 它的前身 `pending-approval-ui.test.ts` 断言的是 **SessionWorkspace.tsx 的源码
 * 文本**：
 *
 *     assert.match(workspace, /const canonicalPause = useMemo\(/);
 *     assert.match(rendered, /onAnswer=\{canCompose/, "没有回答入口 = 看得见但答不了");
 *
 * 2026-09-01 那 6 小时里，这几条**全绿**。它们证明的是"那几行字还在"，而那
 * 几行字确实还在 —— 消失的是它们渲染出来的东西，因为同一个文件里另一行把
 * `phase` 当状态传给了一道名单判据。断言文案还在 ≈ 没有断言。
 *
 * 判断搬出 JSX 之后（见 answer-affordance.ts 的模块注释），这里可以直接断言
 * **效果**：喂进一份真 payload，它到底给不给得出一个能用的入口。
 */
import assert from "node:assert/strict";
import test from "node:test";

import { sessionInputPlan } from "./answer-affordance.ts";
import { adaptExecutionView } from "./session-adapter.ts";
import type { ResearchSession, SessionPendingApproval } from "../types";

/**
 * node20 会话 de9bd47f（cuikl「肺腺癌前沿研究综述」）**原样**取回的那份呈递。
 *
 * ⚠️ `optionDetails: []` / `offer: null` 是这份样本的要害：写事件那一端当时
 * 还在发平铺字段，读的那一端已经只认 `offer`，于是呈递没有身份。**自造样本
 * 会把这个形状造没** —— 而恰恰是"没有身份"让它掉进坏掉的那条渲染分支，
 * 带身份的决策卡则一切正常。一个真样本不够，两个来源才照得出分叉。
 */
const INCIDENT_PAUSE: SessionPendingApproval = {
  kind: "human_input",
  runId: "run_3fac3c518099434aa597a7b06749ad53",
  reason: "waiting_human",
  prompt: "这篇综述的选题方向，你希望我按哪个来写？（这决定文献铺陈范围和全文主线）",
  context: "你给了 5 个可选方向，默认是“综合前沿综述”。",
  options: ["综合前沿综述", "靶向治疗专项", "免疫治疗专项", "早筛早诊专项", "耐药机制专项"],
  optionDetails: [],
  offer: null,
  recommendedOptionIndex: null,
  askingNodeType: "_orchestrator",
  pauseKind: "structured_question",
  askedAt: "2026-09-01T02:06:05.598796Z",
};

/** 同一次呈递，写端修好之后（release 0bb3e1f 起）：身份齐全，有锚点。 */
const REPAIRED_PAUSE: SessionPendingApproval = {
  ...INCIDENT_PAUSE,
  optionDetails: INCIDENT_PAUSE.options.map((label, index) => ({
    id: `option_${index + 1}`,
    label,
    description: "",
    recommended: index === 0,
  })),
  recommendedOptionIndex: 0,
  offer: { offer_id: "orchestrator__x:q1e8085aa:oa0a7478b", decision_id: "orchestrator__x:q1e8085aa" },
};

function sessionWith(rawView: unknown): ResearchSession {
  return { execution: adaptExecutionView(rawView) } as ResearchSession;
}

const PAUSED_VIEW = (pause: SessionPendingApproval | null) => ({
  phase: "alive",
  waitingOn: { kind: "human" },
  outcome: null,
  error: null,
  canStop: false,
  answer: pause ? { via: "pause", pause } : { via: "composer", degraded: "pause_body_unavailable" },
  label: "Needs your answer",
  runId: INCIDENT_PAUSE.runId,
  since: "2026-09-01T02:05:51Z",
});

// ── 事故回放 ──────────────────────────────────────────────────────────────

test("现场那份 payload 必须产出一张能点的卡，而不是一个锁死的会话", () => {
  const plan = sessionInputPlan({
    session: sessionWith(PAUSED_VIEW(INCIDENT_PAUSE)),
    messages: [],
    sending: false,
  });
  assert.equal(plan.kind, "prompt");
  assert.equal(plan.kind === "prompt" && plan.pause.options.length, 5);
  // 没有呈递身份 → 认不到锚点 → 画在列表底部。这**不是**另一条渲染路径，
  // 只是同一张卡挂在别处（当年它是另一条，而那一条是坏的）。
  assert.equal(plan.kind === "prompt" && plan.anchorMessageId, null);
});

test("带身份的同一次呈递画在它自己那条消息下面", () => {
  const plan = sessionInputPlan({
    session: sessionWith(PAUSED_VIEW(REPAIRED_PAUSE)),
    messages: [
      { id: "msg-1", offerId: null },
      { id: "msg-13", offerId: "orchestrator__x:q1e8085aa:oa0a7478b" },
    ],
    sending: false,
  });
  assert.equal(plan.kind, "prompt");
  assert.equal(plan.kind === "prompt" && plan.anchorMessageId, "msg-13");
  // 选项的**身份**必须活着到这里 —— 答复回传它，不回传文案。
  assert.equal(
    plan.kind === "prompt" && plan.pause.options[0].choiceId,
    "option_1",
  );
});

test("在等人、却拿不到那个问题 —— 输入框还给人，并说出来", () => {
  const plan = sessionInputPlan({
    session: sessionWith(PAUSED_VIEW(null)),
    messages: [],
    sending: false,
  });
  // 旧实现在这里两件事同时成立：卡片没有、输入框关着。那就是那 6 小时。
  assert.equal(plan.kind, "composer");
  assert.equal(plan.kind === "composer" && plan.degraded, "pause_body_unavailable");
});

// ── 不变量：永远恰好有一个入口 ─────────────────────────────────────────────

test("能驱动的人永远有入口；不能驱动的人拿到的是原因，不是一个哑掉的界面", () => {
  const cases: unknown[] = [
    PAUSED_VIEW(INCIDENT_PAUSE),
    PAUSED_VIEW(REPAIRED_PAUSE),
    PAUSED_VIEW(null),
    { phase: "alive", waitingOn: null, answer: { via: "composer" }, canStop: true },
    { phase: "ended", waitingOn: null, outcome: "ok", answer: { via: "composer" } },
    { phase: "interrupted", waitingOn: { kind: "human" }, answer: { via: "composer" } },
    { phase: "alive", waitingOn: { kind: "compute" }, answer: { via: "composer" } },
    { phase: "alive", waitingOn: { kind: "human" }, answer: { via: "none", reason: "只读" } },
  ];
  for (const raw of cases) {
    const plan = sessionInputPlan({ session: sessionWith(raw), messages: [], sending: false });
    assert.ok(["prompt", "composer", "locked"].includes(plan.kind));
    if (plan.kind === "locked") assert.ok(plan.reason, "拒绝必须说得出原因");
    if (plan.kind === "prompt") assert.ok(plan.pause.question, "说了走卡片就必须有卡片");
  }
});

test("后端说走卡片却没给卡片 —— 降级成输入框，绝不锁人", () => {
  // 协议漂了、中间层削了字段：这是**我们的**疏忽，不该由用户用一个锁死的
  // 会话来承担。
  const plan = sessionInputPlan({
    session: sessionWith({ phase: "alive", waitingOn: { kind: "human" }, answer: { via: "pause" } }),
    messages: [],
    sending: false,
  });
  assert.equal(plan.kind, "composer");
});

test("认不出来的入口取值也倒向给入口，并留下痕迹", () => {
  const plan = sessionInputPlan({
    session: sessionWith({ phase: "alive", waitingOn: null, answer: { via: "telepathy" } }),
    messages: [],
    sending: false,
  });
  assert.equal(plan.kind, "composer");
  assert.equal(plan.kind === "composer" && plan.degraded, "affordance_unrecognized");
});

test("这个标签页正在发送时不拿旧局面画卡片", () => {
  // 在飞的那一刻手里这份 view 已经是旧的：人可能会去回答一个刚被答掉的问题。
  const plan = sessionInputPlan({
    session: sessionWith(PAUSED_VIEW(REPAIRED_PAUSE)),
    messages: [],
    sending: true,
  });
  assert.equal(plan.kind, "composer");
});

// ── 逐字的待执行内容必须到得了人手里 ───────────────────────────────────────

test("高危审批的那条命令原样送到人面前", () => {
  const pause: SessionPendingApproval = {
    ...INCIDENT_PAUSE,
    // 是哪一类由后端说（`_pending_approval` 一处判），前端不拿 run 状态词猜。
    kind: "permission",
    reason: "waiting_permission",
    prompt: "⚠️ 检测到高危操作（真实外部作业提交），是否批准执行？",
    context: "工具：submit_job\ncommand=/opt/homebrew/bin/lmp_serial -var T 1.00",
    options: ["批准执行", "拒绝"],
    pauseKind: "permission",
    askingNodeType: "experiment",
  };
  const plan = sessionInputPlan({
    session: sessionWith({ ...PAUSED_VIEW(pause), waitingOn: { kind: "permission" } }),
    messages: [],
    sending: false,
  });
  assert.equal(plan.kind, "prompt");
  if (plan.kind !== "prompt") return;
  // 审批一个看不见内容的高危操作等于没有审批。
  assert.match(plan.pause.context ?? "", /lmp_serial/);
  assert.equal(plan.pause.kind, "permission");
  assert.deepEqual(plan.pause.options.map((o) => o.label), ["批准执行", "拒绝"]);
});
