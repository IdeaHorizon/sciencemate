import test from "node:test";
import assert from "node:assert/strict";
import { summarizePauseFacts } from "./pause-facts.ts";

// 取自 node20 实测 decision.required 的字段集。
const REAL = {
  producingRunId: "1787155777-702875",
  sourceNodeType: "hypothesis",
  reviewFailed: true,
  reviewFailedReason: "reviewer 未产出 review_critique",
  reviewRetryCapped: false,
  reviewCritiqueArtifactId: "",
  artifactIdsProduced: ["a", "b"],
  redirectTargetNode: "literature",
};

test("真实形状：认得的用人话说，为假的不占地方，卡片别处说过的不重复", () => {
  assert.deepEqual(summarizePauseFacts(REAL, "zh").forPeople, [
    { key: "reviewFailed", label: "审查未通过", tone: "flag" },
    { key: "reviewFailedReason", label: "未通过的原因", value: "reviewer 未产出 review_critique", tone: "value" },
  ]);
  assert.deepEqual(summarizePauseFacts(REAL, "en").forPeople.map((fact) => fact.label),
    ["Review did not pass", "Why it did not pass"]);
});

test("中文界面的正文里不出现英文字段名", () => {
  for (const fact of summarizePauseFacts(REAL, "zh").forPeople) {
    assert.doesNotMatch(fact.label, /[A-Za-z]/, `字段名漏到了正文里：${fact.label}`);
  }
});

test("没起名字的不丢 —— 上游加什么，技术信息里都看得到", () => {
  assert.deepEqual(summarizePauseFacts(REAL, "zh").technical,
    ["producingRunId: 1787155777-702875", "artifactIdsProduced: a, b"]);
  const facts = summarizePauseFacts({ somethingNobodyPlannedFor: "42", anotherFlag: true }, "zh");
  assert.deepEqual(facts.forPeople, []);
  assert.deepEqual(facts.technical, ["somethingNobodyPlannedFor: 42", "anotherFlag"]);
});

test("为假的布尔、空串、对象、null 都不显示", () => {
  assert.deepEqual(summarizePauseFacts({ a: false, b: "", d: { x: 1 }, e: null }, "zh"),
    { forPeople: [], technical: [] });
});

test("没有 facts 就是空", () => {
  assert.deepEqual(summarizePauseFacts(undefined, "en"), { forPeople: [], technical: [] });
});
