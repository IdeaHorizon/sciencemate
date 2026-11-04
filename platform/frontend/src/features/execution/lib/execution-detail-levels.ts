import type { Phrase } from "../../../shared/i18n/language.ts";
import type { Density } from "./execution-projection.ts";

/**
 * 「详细程度」三档的名字：会话里的切换器（SessionExecutionView 的 DensityControl）
 * 和设置页「执行过程详细程度」的下拉说的是同一件事，两处都从这里取 ——
 * 简要 / 标准 / 完整，Summary / Standard / Full。
 */
export const EXECUTION_DETAIL_LEVELS: readonly { value: Density; label: Phrase }[] = [
  { value: "summary", label: { zh: "简要", en: "Summary" } },
  { value: "standard", label: { zh: "标准", en: "Standard" } },
  { value: "trace", label: { zh: "完整", en: "Full" } },
];
