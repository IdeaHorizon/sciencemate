/**
 * 节点报告的阻塞（v2.1）。
 *
 * `report_blocker` 是新架构的一等机制：节点不硬扛也不静默失败，而是报
 * 事实 + 证据 + 需求，由调度器或人决定怎么解。UI 这一侧要回答的问题只有
 * 一个 —— **现在有什么在等我，它要我做什么**。
 */

import { say, type Language, type Phrase } from "../../../shared/i18n/language.ts";

export interface ProjectBlocker {
  readonly id: string;
  readonly runId: string;
  readonly sessionId: string;
  readonly reportingNode: string;
  readonly category: string;
  readonly summary: string;
  readonly requestedAction: string;
  readonly suggestedOwner: string;
  readonly retryableAfterChange: boolean;
  readonly evidencePaths: readonly string[];
  readonly reportedAt: string;
  readonly runStatus: string;
  readonly stale: boolean;
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function adaptBlockers(payload: unknown): ProjectBlocker[] {
  const rows = (payload as { blockers?: unknown })?.blockers;
  if (!Array.isArray(rows)) return [];
  return rows
    .filter((row): row is Record<string, unknown> => Boolean(row) && typeof row === "object")
    .map((row) => ({
      id: asString(row.id),
      runId: asString(row.runId),
      sessionId: asString(row.sessionId),
      reportingNode: asString(row.reportingNode),
      category: asString(row.category) || "other",
      summary: asString(row.summary),
      requestedAction: asString(row.requestedAction),
      suggestedOwner: asString(row.suggestedOwner),
      retryableAfterChange: row.retryableAfterChange !== false,
      evidencePaths: Array.isArray(row.evidencePaths)
        ? row.evidencePaths.map((path) => String(path))
        : [],
      reportedAt: asString(row.reportedAt),
      runStatus: asString(row.runStatus),
      stale: row.stale === true,
    }))
    .filter((row) => row.id.length > 0);
}

/** 需要用户现在处理的那些 —— stale 的已经不成问题了。 */
export function openBlockers(blockers: readonly ProjectBlocker[]): ProjectBlocker[] {
  return blockers.filter((row) => !row.stale);
}

const CATEGORY_LABEL: Record<string, Phrase> = {
  missing_resource: { zh: "缺资源", en: "Missing resource" },
  missing_input: { zh: "缺输入", en: "Missing input" },
  permission: { zh: "要授权", en: "Needs permission" },
  infeasible: { zh: "按设计做不到", en: "Not feasible as designed" },
  external_dependency: { zh: "等外部依赖", en: "External dependency" },
  other: { zh: "被卡住", en: "Blocked" },
};

/** 认识的类别按界面语言说；不认识的（后端新加的）把类别名本身读通顺了给出来。 */
export function blockerCategoryLabel(category: string, lang: Language): string {
  const label = CATEGORY_LABEL[category];
  return label ? say(label, lang) : blockerTitleCase(category);
}

function blockerTitleCase(value: string): string {
  return value
    .split("_")
    .filter(Boolean)
    .map((part) => part[0]?.toUpperCase() + part.slice(1))
    .join(" ");
}
