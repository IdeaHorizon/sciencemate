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
import { say, type Language, type Phrase } from "../../../shared/i18n/language.ts";

/** Prefer the canonical producer; legacy records remain readable. */
const ANALYSIS_NODES = ["analysis", "hypothesis"] as const;

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

/**
 * verdict / 假设状态是 Analysis 写进账本的枚举值，界面上换成人话。含义取自
 * `nodes/analysis/harness.yaml` 的「verdict 决定下一步」那一段。表里没有的照原样显示。
 */
const VERDICT_LABEL: Record<string, Phrase> = {
  continue: { zh: "按计划继续", en: "Continue" },
  pivot: { zh: "调整方向", en: "Pivot" },
  ready_candidate: { zh: "可以开始写作", en: "Ready to write" },
  abort: { zh: "终止", en: "Abort" },
};

export function verdictLabel(verdict: string, lang: Language): string {
  const label = VERDICT_LABEL[verdict];
  return label ? say(label, lang) : verdict;
}

const STATUS_LABEL: Record<string, Phrase> = {
  active: { zh: "待判定", en: "Open" },
  supported: { zh: "已支持", en: "Supported" },
  refuted: { zh: "已推翻", en: "Refuted" },
  inconclusive: { zh: "无定论", en: "Inconclusive" },
  withdrawn: { zh: "已撤回", en: "Withdrawn" },
};

export function hypothesisStatusLabel(status: string, lang: Language): string {
  const label = STATUS_LABEL[status];
  return label ? say(label, lang) : status;
}
