import type { ResearchRun, RunUsage } from "@/lib/api";

export function runTitle(run: ResearchRun) {
  const summaryTitle = run.summary?.title;
  if (typeof summaryTitle === "string" && summaryTitle.trim()) return summaryTitle;
  if (!run.nodeType) return "Research run";
  return run.nodeType
    .split("_")
    .filter(Boolean)
    .map((part) => part[0]?.toUpperCase() + part.slice(1))
    .join(" ");
}

/**
 * 这里曾经有一份 `STATUS_LABELS`（11 个 run 状态 → 文案）加两个导出函数
 * `runStatusLabel` / `runStateLabel`，还专门处理了 `stale_unknown` 要读
 * `staleFromStatus` 才说得清楚那一支。
 *
 * 2026-09-01 全部删除，理由有两条，第二条更要紧：
 *
 *   1. **零生产调用方** —— 只有它自己和它的测试在用。列表和徽章早就渲染
 *      后端 `view.label` 了（同一次现算，与徽章/停止按钮/输入框同源）。
 *   2. 它是前端最后一份 run 状态词表。留着它，下一个人就会"顺手"再用它
 *      判一次分支 —— 而手写名单不会因为漏了新取值而变红。
 *
 * 要文案就读 `run.view.label`；要分支就读 `run.view.phase / waitingOn / outcome`。
 */

export function recentSessionRuns(runs: ResearchRun[], limit = 4) {
  const seen = new Set<string>();
  const recent: ResearchRun[] = [];
  for (const run of runs) {
    const sessionKey = `${run.projectId}:${run.sessionId}`;
    if (seen.has(sessionKey)) continue;
    seen.add(sessionKey);
    recent.push(run);
    if (recent.length === limit) break;
  }
  return recent;
}

function formatRunCost(cost: number, currency: string | null) {
  if (!currency) return `${cost.toFixed(4)} cost`;
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency,
      maximumFractionDigits: 4,
    }).format(cost);
  } catch {
    return `${cost.toFixed(4)} ${currency}`;
  }
}

export function runUsageLabel(usage: RunUsage) {
  const parts = [`${usage.totalTokens.toLocaleString()} tokens`];
  parts.push(usage.cost === null ? "Cost unavailable" : formatRunCost(usage.cost, usage.currency));
  if (usage.coverage === "partial") parts.push("Partial usage");
  return parts.join(" · ");
}
