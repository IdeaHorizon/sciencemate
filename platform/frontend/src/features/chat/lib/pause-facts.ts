/**
 * 呈递方附带的事实 → 面板上一行能扫读的说明。
 *
 * 这里**不枚举字段名**。上游给 `Offer.facts` 加什么，面板就显示什么 —— 枚举
 * 一次就多一处会静默漏掉的地方，而"每一跳都少几个字段"正是这套东西的由来。
 *
 * 规则只有三条，与具体字段无关：
 *   - 布尔：为真才显示（`reviewFailed: true` → 「review failed」；为假是常态，
 *     不占地方）
 *   - 标量：非空才显示，`key: value`
 *   - 其它（数组 / 对象 / 空串）：不显示 —— 它们是给程序读的，塞进一行 meta
 *     只会挤掉真正能读的那几条
 */
export type PauseFact = { key: string; label: string; value?: string; tone: "flag" | "value" };

/** camelCase / snake_case → 空格分词的小写短语。 */
export function humanizeFactKey(key: string): string {
  return key
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .trim()
    .toLowerCase();
}

export function summarizePauseFacts(facts: Record<string, unknown> | undefined): PauseFact[] {
  if (!facts) return [];
  const out: PauseFact[] = [];
  for (const [key, raw] of Object.entries(facts)) {
    const label = humanizeFactKey(key);
    if (!label) continue;
    if (typeof raw === "boolean") {
      if (raw) out.push({ key, label, tone: "flag" });
      continue;
    }
    if (typeof raw === "number") {
      out.push({ key, label, value: String(raw), tone: "value" });
      continue;
    }
    if (typeof raw === "string") {
      const value = raw.trim();
      if (value) out.push({ key, label, value, tone: "value" });
    }
  }
  return out;
}
