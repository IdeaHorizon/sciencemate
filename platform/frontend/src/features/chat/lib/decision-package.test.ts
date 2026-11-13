import test from "node:test";
import assert from "node:assert/strict";
import { decisionChoiceText, decisionQuestion } from "./decision-package.ts";

// harness 呈递 decision_package 时写给模型的原文（shared/tools/library/decision_package.py）。
const HARNESS = {
  question: "Post-node decision for hypothesis",
  proceed: { label: "PROCEED to next stage", description: "接受本轮产物，进入下一个节点。" },
  redirect: { label: "REDIRECT to upstream → 'literature'", description: "根因在上游 —— 先让上游补一段工作，再回头重跑本节点。" },
};
const FACTS = { sourceNodeType: "hypothesis", redirectTargetNode: "literature" };

test("问句按做完的那一步用人话问，两种语言", () => {
  const pause = { kind: "decision", question: HARNESS.question, facts: FACTS };
  assert.equal(decisionQuestion(pause, "zh"), "「分析」这一步已完成，接下来怎么做？");
  assert.equal(decisionQuestion(pause, "en"), "“Analysis” is done. What should happen next?");
});

test("不是决策包（没有 sourceNodeType）就照上游原文问", () => {
  assert.equal(decisionQuestion({ kind: "human_input", question: "用哪个数据集？" }, "en"), "用哪个数据集？");
});

test("选项按 choice id 说人话，不读给模型看的那句英文", () => {
  assert.deepEqual(decisionChoiceText("proceed", HARNESS.proceed, "zh"),
    { label: "接受并继续", description: "接受这一步的成果，进入下一步。" });
  assert.equal(decisionChoiceText("proceed", HARNESS.proceed, "en").label, "Accept and continue");
  assert.equal(decisionChoiceText("redirect_upstream", HARNESS.redirect, "zh", FACTS).label, "回到「文献调研」");
  assert.equal(decisionChoiceText("redirect_upstream", HARNESS.redirect, "en", FACTS).label, "Go back to “Literature review”");
});

test("认不出的 id（或没有 id）照上游原文，不猜", () => {
  const upstream = { label: "Something new", description: "上游刚加的" };
  assert.deepEqual(decisionChoiceText("something_new", upstream, "zh"), upstream);
  assert.deepEqual(decisionChoiceText(undefined, upstream, "zh"), upstream);
});
