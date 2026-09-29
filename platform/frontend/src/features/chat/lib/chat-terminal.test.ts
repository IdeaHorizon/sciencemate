import test from "node:test";
import assert from "node:assert/strict";
import { normalizeChatPause, normalizeChatTerminal } from "./chat-terminal.ts";

/**
 * 终帧的形状（2026-09-01 起）：**同一个 view**，不是它的第三种平铺投影。
 *
 * 这里原来有四条用例，全部围绕 `status` / `resumable` / `pause` 三个平铺字段
 * 展开 —— 而 REST payload 发的是 `executionView`。同一件事两种形状，前端于是
 * 长出两套解析、再加一句 `livePause = A ?? B` 决定谁赢。三份各自演化，
 * 分叉时没有一层报错。
 *
 * pause 归一化本身（选项身份、permission/decision 分类）没有变，那几条用例
 * 仍然在下面，只是不再经由终帧那条已经不存在的路。
 */
test("终帧把局面原样带回来，不再自己拼一个 status 出来", () => {
  const state = normalizeChatTerminal({
    run_id: "run-7",
    session_id: "session-4",
    view: {
      phase: "alive",
      waitingOn: { kind: "human" },
      outcome: null,
      error: null,
      canStop: false,
      label: "Needs your answer",
      runId: "run-7",
      since: null,
      answer: {
        via: "pause",
        pause: {
          kind: "human_input",
          runId: "run-7",
          reason: "",
          prompt: "Which evidence threshold should be used?",
          context: "The two thresholds produce different false-positive rates.",
          options: ["Use strict threshold"],
          optionDetails: [
            { id: "strict", label: "Use strict threshold", description: "Prioritize precision", recommended: true },
          ],
          offer: null,
          recommendedOptionIndex: 0,
          askingNodeType: "experiment",
          pauseKind: "structured_question",
          askedAt: null,
        },
      },
    },
  });

  assert.equal(state.runId, "run-7");
  assert.equal(state.sessionId, "session-4");
  assert.equal(state.view?.waitingOn?.kind, "human");
  assert.equal(state.view?.answer.via, "pause");
  // 选项的**身份**必须活着走完这一跳 —— 答复回传它，不回传文案。
  assert.equal(
    state.view?.answer.via === "pause" ? state.view.answer.pause.optionDetails[0].id : null,
    "strict",
  );
});

test("终帧没带局面时不替服务端编一个出来", () => {
  // 后端三条真实收尾路径都带 view；缺席只发生在流被打断、或记账本身失败时
  // —— 那时说「已完成」就是谎话。缺席的诚实答案是 null，由会话那一次现算回答。
  assert.deepEqual(normalizeChatTerminal({ type: "done" }), {
    view: null,
    runId: null,
    sessionId: null,
  });
});

test("认不出来的局面不会被塞进一个假的 view", () => {
  const state = normalizeChatTerminal({ view: { phase: "nonsense" } });
  // 兜底一律倒向"给入口"：拿不准的时候锁住用户是最坏的那个结果。
  assert.equal(state.view?.phase, "alive");
  assert.equal(state.view?.answer.via, "composer");
});

test("high-risk confirmation is presented as a permission pause", () => {
  const pause = normalizeChatPause({
    question: "Approve installing Gromacs?",
    options: ["Approve", "Deny"],
    metadata: { type: "highrisk_confirm" },
  });
  assert.equal(pause?.kind, "permission");
});

test("direct canonical pause_kind preserves a decision prompt", () => {
  const pause = normalizeChatPause({
    pause_kind: "decision_package",
    question: "Choose the next research branch.",
    asking_node_type: "review",
    options: [{ id: "revise", label: "Revise", recommended: true }],
  });
  assert.equal(pause?.kind, "decision");
  assert.equal(pause?.askingNodeType, "review");
  assert.equal(pause?.options[0]?.recommended, true);
});

test("structured options carry descriptions and surface the recommendation", () => {
  // 台账 #3：前端 ChatPauseOption 早就有 description / recommended 两个字段，
  // 但后端从来没填 —— 人在 UI 上只看得见几个词，判断不了后果。
  const pause = normalizeChatPause({
    question: "低速率组怎么补？",
    header: "采样方案",
    optionDetails: [
      { label: "等平台修复", description: "不推进" },
      { label: "用 v13 重跑", description: "指名 prereg，约 25 分钟" },
    ],
    recommendedOptionIndex: 1,
  });
  assert.ok(pause);
  assert.equal(pause.header, "采样方案");
  assert.equal(pause.options.length, 2);
  assert.equal(pause.options[1].description, "指名 prereg，约 25 分钟");
  assert.equal(pause.options[1].recommended, true);
  assert.equal(pause.options[0].recommended, false);
});

test("recommendation also arrives via harness-native metadata", () => {
  const pause = normalizeChatPause({
    question: "q",
    option_details: [{ label: "A", description: "da" }, { label: "B", description: "db" }],
    metadata: { recommended_option_index: 0, header: "预算" },
  });
  assert.ok(pause);
  assert.equal(pause.header, "预算");
  assert.equal(pause.options[0].recommended, true);
});

test("bare string options still normalize (legacy pauses)", () => {
  const pause = normalizeChatPause({ question: "q", options: ["甲", "乙"] });
  assert.ok(pause);
  assert.equal(pause.options.length, 2);
  assert.equal(pause.options[0].recommended, false);
});
