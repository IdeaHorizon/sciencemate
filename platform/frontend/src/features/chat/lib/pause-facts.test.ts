import test from "node:test";
import assert from "node:assert/strict";
import { humanizeFactKey, summarizePauseFacts } from "./pause-facts.ts";

test("真实形状：review 失败要显示，没被封顶不占地方", () => {
  // 取自 node20 实测 decision.required 的字段集。
  assert.deepEqual(
    summarizePauseFacts({
      producingRunId: "1787155777-702875",
      sourceNodeType: "hypothesis",
      reviewFailed: true,
      reviewFailedReason: "reviewer 未产出 review_critique",
      reviewRetryCapped: false,
      reviewCritiqueArtifactId: "",
      artifactIdsProduced: ["a", "b"],
    }),
    [
      { key: "producingRunId", label: "producing run id", value: "1787155777-702875", tone: "value" },
      { key: "sourceNodeType", label: "source node type", value: "hypothesis", tone: "value" },
      { key: "reviewFailed", label: "review failed", tone: "flag" },
      { key: "reviewFailedReason", label: "review failed reason", value: "reviewer 未产出 review_critique", tone: "value" },
    ],
  );
});

test("不枚举字段名 —— 上游加什么就显示什么", () => {
  const facts = summarizePauseFacts({ somethingNobodyPlannedFor: "42", anotherFlag: true });
  assert.deepEqual(facts.map((f) => f.label), ["something nobody planned for", "another flag"]);
});

test("为假的布尔不占地方；空串、数组、对象不显示", () => {
  assert.deepEqual(summarizePauseFacts({ a: false, b: "", c: [1], d: { x: 1 }, e: null }), []);
});

test("数字显示（重试余额、需要几个人批这类）", () => {
  assert.deepEqual(
    summarizePauseFacts({ requiredApprovalCount: 2 }),
    [{ key: "requiredApprovalCount", label: "required approval count", value: "2", tone: "value" }],
  );
});

test("没有 facts 就是空", () => {
  assert.deepEqual(summarizePauseFacts(undefined), []);
});

test("键名分词", () => {
  assert.equal(humanizeFactKey("reviewRetryCapped"), "review retry capped");
  assert.equal(humanizeFactKey("producing_run_id"), "producing run id");
});
