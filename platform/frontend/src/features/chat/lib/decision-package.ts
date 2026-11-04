import { say, type Language, type Phrase } from "../../../shared/i18n/language.ts";
import { nodeDisplayName } from "../../execution/lib/display-names.ts";

/**
 * 「这一步做完了，接下来怎么办」那张决策卡（harness 的 decision_package）上给人看的话。
 *
 * harness 呈递时写的问句和选项文案是给模型看的（「Post-node decision for hypothesis」
 * 「PROCEED to next stage」），一个字都不能改；界面按**身份**说人话：问句看呈递带的
 * `sourceNodeType`，选项看 choice id（`shared/tools/library/decision_package.py` 的
 * `_ACTION_CATALOG`）。表里没有的 id 照原样显示上游的文案，不猜。
 */
const CHOICES: Record<string, { label: Phrase; description: Phrase }> = {
  proceed: {
    label: { zh: "接受并继续", en: "Accept and continue" },
    description: { zh: "接受这一步的成果，进入下一步。", en: "Accept this step's output and move on to the next step." },
  },
  retry_reviewer: {
    label: { zh: "重新审查", en: "Review again" },
    description: { zh: "只重新审查一次，沿用这一步已有的成果。", en: "Run the review again and keep this step's existing output." },
  },
  revise: {
    label: { zh: "按审查意见修改", en: "Revise with the feedback" },
    description: { zh: "带着审查意见重新做这一步。", en: "Redo this step using the review feedback." },
  },
  redirect_upstream: {
    label: { zh: "回到前面的步骤", en: "Go back to an earlier step" },
    description: {
      zh: "问题出在更早的步骤：先补做那一步，再重做这一步。",
      en: "The problem is in an earlier step: redo that one first, then this one.",
    },
  },
  abort: {
    label: { zh: "停止这项研究", en: "Stop this research" },
    description: { zh: "结束这项研究，不再继续。", en: "End this research here." },
  },
  edit: {
    label: { zh: "我来手动修改", en: "Edit it myself" },
    description: {
      zh: "先暂停，等你修改成果或知识库之后再决定。",
      en: "Pause so you can edit the output or knowledge base, then decide.",
    },
  },
};

/** 一个选项给人看的名字和说明。`facts` 用来说出「回到哪一步」。 */
export function decisionChoiceText(
  choiceId: string | undefined,
  upstream: { label: string; description?: string },
  lang: Language,
  facts?: Record<string, unknown>,
): { label: string; description?: string } {
  const known = choiceId ? CHOICES[choiceId] : undefined;
  if (!known) return upstream;
  const target = facts?.redirectTargetNode;
  const label = choiceId === "redirect_upstream" && typeof target === "string" && target
    ? say({ zh: "回到「{node}」", en: "Go back to “{node}”" }, lang, { node: nodeDisplayName(target, lang) })
    : say(known.label, lang);
  return { label, description: say(known.description, lang) };
}

/** 决策卡的问句：认得出是哪一步做完了，就用人话问；否则照上游原文。 */
export function decisionQuestion(
  pause: { kind: string; question: string; facts?: Record<string, unknown> },
  lang: Language,
): string {
  const node = pause.kind === "decision" ? pause.facts?.sourceNodeType : undefined;
  if (typeof node !== "string" || !node) return pause.question;
  return say({ zh: "「{node}」这一步已完成，接下来怎么做？", en: "“{node}” is done. What should happen next?" },
    lang, { node: nodeDisplayName(node, lang) });
}
