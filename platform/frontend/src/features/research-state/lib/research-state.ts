/**
 * research_state —— Analysis 持有的版本化研究状态（v2.1 P3c）。
 *
 * 真相源是 Project Git 仓里的记录：正文是原生文件，**事实**（版本、冻结、
 * 假说裁决表这种结构化 metadata）在账本里（`core/ledger`，RFC 2026-09-12 §6）。
 * 界面从 `/repository/records` 拿 head，不再按文件名正则猜 head 在哪、也不再
 * 拆 JSON 信封 —— 两个都不该由界面猜，后端 `core/research_state_reader` 才是
 * 唯一的读取实现，这里只是它给出的 head 在界面上的投影。
 */

import type { RecordHead } from "@/lib/api";

/** Analysis 职责所在的节点类型（与后端 ANALYSIS_NODES 一致，hypothesis 优先）。 */
const ANALYSIS_NODES = ["hypothesis", "analysis"] as const;

export const RESEARCH_STATE_ID = "research_state__research_state";

export type HypothesisStatus =
  | "active"
  | "supported"
  | "refuted"
  | "inconclusive"
  | "withdrawn";

export type Verdict = "continue" | "pivot" | "ready_candidate" | "abort";

export interface HypothesisRow {
  readonly id: string;
  readonly status: HypothesisStatus | string;
  readonly evidence: readonly string[];
  readonly note?: string;
}

export interface ResearchState {
  readonly version: number;
  readonly parentVersion: number | null;
  readonly verdict: Verdict | string;
  readonly changeReason: string;
  readonly hypotheses: readonly HypothesisRow[];
  readonly completedExperiments: readonly {
    readonly ref: string;
    readonly credibility: string;
  }[];
  readonly gaps: readonly string[];
  readonly nextSteps: readonly string[];
  readonly planVersion: string;
}

/** 账本里 research_state 的 head。没有则 null（= 这个项目还没进入 Analysis 循环）。 */
export function researchStateHead(records: readonly RecordHead[]): RecordHead | null {
  const candidates = records.filter(
    (row) => row.type === "research_state" && row.id === RESEARCH_STATE_ID,
  );
  for (const node of ANALYSIS_NODES) {
    const own = candidates.find((row) => row.producedByNodeType === node);
    if (own) return own;
  }
  return candidates[0] ?? null;
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** 解析 head 的 metadata。形状不对返回 null，不猜。 */
export function parseResearchState(metadata: unknown): ResearchState | null {
  let meta = metadata;
  if (typeof meta === "string") {
    try {
      meta = JSON.parse(meta);
    } catch {
      return null;
    }
  }
  if (!meta || typeof meta !== "object") return null;
  const m = meta as Record<string, unknown>;
  if (typeof m.version !== "number") return null;

  return {
    version: m.version,
    parentVersion: typeof m.parent_version === "number" ? m.parent_version : null,
    verdict: asString(m.verdict),
    changeReason: asString(m.change_reason),
    planVersion: asString(m.plan_version),
    hypotheses: asArray(m.hypotheses)
      .filter((row): row is Record<string, unknown> => Boolean(row) && typeof row === "object")
      .map((row) => ({
        id: asString(row.id),
        status: asString(row.status),
        evidence: asArray(row.evidence).map((item) => String(item)),
        note: asString(row.note) || asString(row.withdrawn_reason) || undefined,
      }))
      .filter((row) => row.id.length > 0),
    completedExperiments: asArray(m.completed_experiments)
      .filter((row): row is Record<string, unknown> => Boolean(row) && typeof row === "object")
      .map((row) => ({
        ref: asString(row.run_id) || asString(row.artifact_id) || "?",
        credibility: asString(row.credibility) || "unknown",
      })),
    gaps: asArray(m.gaps).map((item) => String(item)),
    nextSteps: asArray(m.next_steps).map((item) => String(item)),
  };
}

export function unresolvedHypotheses(state: ResearchState): HypothesisRow[] {
  return state.hypotheses.filter((row) => row.status === "active");
}

export type BadgeTone = "success" | "warning" | "danger" | "muted" | "info";

const STATUS_TONE: Record<string, BadgeTone> = {
  supported: "success",
  refuted: "danger",
  inconclusive: "warning",
  withdrawn: "muted",
  active: "info",
};

export function statusTone(status: string): BadgeTone {
  return STATUS_TONE[status] ?? "muted";
}

const VERDICT_TONE: Record<string, BadgeTone> = {
  continue: "info",
  pivot: "warning",
  ready_candidate: "success",
  abort: "danger",
};

export function verdictTone(verdict: string): BadgeTone {
  return VERDICT_TONE[verdict] ?? "muted";
}
