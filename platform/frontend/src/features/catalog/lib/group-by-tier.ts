import type { CatalogEntry, CatalogTier } from "@/lib/api";

/** 三档的固定顺序 —— 交付物在最前，那是用户来这一页要看的东西。 */
export const TIER_ORDER: CatalogTier[] = ["deliverable", "output", "working"];

/**
 * 按分层分组。
 *
 * **是排序，不是过滤**：三个键恒在，每一条产出都必须落进某一档。少列一条，
 * 用户就会以为那件东西不存在 —— 而"工作过程"那一档正是最容易被顺手滤掉的
 * （它在真项目上有 200 多条）。看不见的东西没法被审计。
 *
 * 抽成纯函数是为了让上面那句话**能被测**：写在组件里就只能靠正则去比对源码，
 * 而正则会被一个 `(row) =>` 绕过去（实测：变异之后断言没转红）。
 */
export function groupByTier(entries: CatalogEntry[]): Record<CatalogTier, CatalogEntry[]> {
  const grouped: Record<CatalogTier, CatalogEntry[]> = {
    deliverable: [], output: [], working: [],
  };
  for (const entry of entries) {
    // 后端给了没见过的 tier 时归到最保守那一档，而不是丢掉。
    (grouped[entry.tier] ?? grouped.working).push(entry);
  }
  return grouped;
}
