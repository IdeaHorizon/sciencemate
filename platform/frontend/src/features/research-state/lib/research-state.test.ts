import assert from "node:assert/strict";
import test from "node:test";

import type { RecordHead } from "../../../lib/api.ts";
import {
  parseResearchState,
  researchStateHead,
  statusTone,
  unresolvedHypotheses,
  verdictTone,
} from "./research-state.ts";

/**
 * 账本 head 的形状（2026-09-12，`/repository/records`）：事实在 metadata 里，
 * 正文是 `plan/research_state__research_state.md` 这个原生文件。
 */
function head(overrides: Partial<RecordHead> = {}): RecordHead {
  return {
    id: "research_state__research_state",
    type: "research_state",
    name: "research_state",
    path: "plan/research_state__research_state.md",
    version: 9,
    frozen: false,
    frozenVersion: 0,
    frozenAt: "",
    createdAt: "2026-09-06T19:36:27+00:00",
    producedByNodeType: "hypothesis",
    producedByRunId: "1788723286-9837f2",
    metadata: {
      version: 9,
      parent_version: 8,
      verdict: "continue",
      change_reason: "H2 补了两轮实验",
      plan_version: "v3",
      hypotheses: [
        { id: "H1", status: "supported", evidence: ["experiment_log__run1"] },
        { id: "H2", status: "active", evidence: [] },
        { status: "refuted" },
      ],
      completed_experiments: [{ run_id: "1788723629-376101", credibility: "high" }],
      gaps: ["H2 缺尾部数据"],
      next_steps: ["跑 L=128"],
    },
    ...overrides,
  };
}

test("head 从账本记录里挑：类型 + 身份，hypothesis 优先于兼容目录 analysis", () => {
  const legacy = head({ producedByNodeType: "analysis", version: 3 });
  const current = head();
  assert.equal(researchStateHead([legacy, current]), current);
  assert.equal(researchStateHead([legacy]), legacy);
  // 别的类型、别的身份不算
  assert.equal(researchStateHead([head({ type: "research_plan" })]), null);
  assert.equal(researchStateHead([head({ id: "research_state__draft" })]), null);
  assert.equal(researchStateHead([]), null);
});

test("解析 metadata：版本链、裁决、假说表、缺口与下一步", () => {
  const state = parseResearchState(head().metadata);
  assert.ok(state);
  assert.equal(state.version, 9);
  assert.equal(state.parentVersion, 8);
  assert.equal(state.verdict, "continue");
  assert.equal(state.planVersion, "v3");
  // 没有 id 的行不是假说
  assert.deepEqual(state.hypotheses.map((row) => row.id), ["H1", "H2"]);
  assert.deepEqual(state.completedExperiments, [{ ref: "1788723629-376101", credibility: "high" }]);
  assert.deepEqual(state.gaps, ["H2 缺尾部数据"]);
  assert.deepEqual(state.nextSteps, ["跑 L=128"]);
  assert.deepEqual(unresolvedHypotheses(state).map((row) => row.id), ["H2"]);
});

test("metadata 形状不对就返回 null，不猜", () => {
  assert.equal(parseResearchState({}), null);
  assert.equal(parseResearchState({ version: "9" }), null);
  assert.equal(parseResearchState("{not json"), null);
  // 后端偶尔把 metadata 存成字符串（旧记录）—— 能解开就照解
  assert.ok(parseResearchState(JSON.stringify({ version: 2, hypotheses: [] })));
});

test("色调表只认已知词，不认识的落到 muted", () => {
  assert.equal(verdictTone("ready_candidate") !== verdictTone("nonsense"), true);
  assert.equal(statusTone("refuted") !== statusTone("whatever"), true);
});
