import { say, type Language, type Phrase } from "../../../shared/i18n/language.ts";

/**
 * 呈递方附带的事实 → 面板上一行能扫读的说明。
 *
 * 认得的几样用人话说（「审查未通过」）；认不得的不丢，原样折进「背景」那一栏的
 * 技术信息里（`键: 值`）。上游给 `Offer.facts` 加什么，面板上都看得到 —— 只是
 * 没起好名字之前，它不以一串英文字段名出现在中文界面的正文里。
 *
 * 值的规则与字段无关：
 *   - 布尔：为真才说（为假是常态，不占地方）
 *   - 标量：非空才说
 *   - 数组：只进技术信息，逗号连起来；对象不显示（给程序读的）
 */
export type PauseFact = { key: string; label: string; value?: string; tone: "flag" | "value" };
export type PauseFacts = { forPeople: PauseFact[]; technical: string[] };

/**
 * 认得的事实（`shared/tools/library/decision_package.py` 呈递时带的那几样）。
 * `"said-elsewhere"` = 卡片别处已经说了（标题旁的标签、问句、选项），这里不重复。
 */
const FACT_LABELS: Record<string, Phrase | "said-elsewhere"> = {
  reviewFailed: { zh: "审查未通过", en: "Review did not pass" },
  reviewFailedReason: { zh: "未通过的原因", en: "Why it did not pass" },
  reviewRetryCapped: { zh: "重新审查的次数已用完", en: "No review retries left" },
  header: "said-elsewhere",
  sourceNodeType: "said-elsewhere",
  redirectTargetNode: "said-elsewhere",
};

function scalar(raw: unknown): string | undefined {
  if (typeof raw === "number") return String(raw);
  if (typeof raw === "string") return raw.trim() || undefined;
  if (Array.isArray(raw)) {
    const parts = raw.filter((part) => typeof part === "string" || typeof part === "number").map(String);
    return parts.length ? parts.join(", ") : undefined;
  }
  return undefined;
}

export function summarizePauseFacts(facts: Record<string, unknown> | undefined, lang: Language): PauseFacts {
  const out: PauseFacts = { forPeople: [], technical: [] };
  if (!facts) return out;
  for (const [key, raw] of Object.entries(facts)) {
    const known = FACT_LABELS[key];
    if (known === "said-elsewhere") continue;
    if (typeof raw === "boolean") {
      if (!raw) continue;
      if (known) out.forPeople.push({ key, label: say(known, lang), tone: "flag" });
      else out.technical.push(key);
      continue;
    }
    const value = scalar(raw);
    if (value === undefined) continue;
    if (known && !Array.isArray(raw)) out.forPeople.push({ key, label: say(known, lang), value, tone: "value" });
    else out.technical.push(`${key}: ${value}`);
  }
  return out;
}
