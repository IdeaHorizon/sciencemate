import type { FeedCuration } from "@/lib/api";
import type { Language } from "../../../shared/i18n/language.ts";
import { fc } from "./feed-copy.ts";

/**
 * 开关下面那行状态字。
 *
 * 抽成纯函数是因为这里的分支是**会说错话**的地方：一个开着但因为没配模型
 * 而没工作的开关，如果显示成"已开启"，用户会一直等一个永远不来的结果。
 * 本仓库没有渲染测试，所以能被测到的前提是它不在 .tsx 里。
 */
export function curationStatusLine(
  state: FeedCuration,
  lang: Language,
): { text: string; tone: "idle" | "ok" | "error" } {
  if (!state.available) {
    return { tone: "error", text: fc("curation.unavailable", lang) };
  }
  if (!state.enabled) {
    return { tone: "idle", text: fc("curation.off", lang) };
  }
  if (state.last_error) {
    // 开着但没工作，要说清为什么 —— 否则和"确实没什么可推断的"长得一样。
    return { tone: "error", text: fc("curation.failed", lang, { error: state.last_error }) };
  }
  if (state.inferred_domains.length > 0) {
    return {
      tone: "ok",
      text: fc("curation.inferred", lang, {
        count: state.inferred_domains.length,
        // 顿号是中文的列表分隔符，英文里得用逗号 —— 连接符本身也是文案。
        fields: state.inferred_domain_labels.join(lang === "zh" ? "、" : ", "),
      }),
    };
  }
  if (!state.last_run_at) {
    return { tone: "idle", text: fc("curation.first_run", lang) };
  }
  // 跑过、没报错、也没推断出东西：多半是课题描述太笼统。说实话，并给出下一步。
  return { tone: "idle", text: fc("curation.nothing_inferred", lang) };
}
