import type { ExecutionEvent } from "@/features/execution/lib/execution-event";

/** Request measurements belong to one Run. Provider counts describe the last
 * submitted request; local predictions and safety budgets are separate facts.
 */
export interface ContextWindowBreakdown {
  system: number;
  tools: number;
  toolResults: number;
  summary: number;
  framework: number;
  conversation: number;
}

export interface ContextWindowCompaction {
  turn: number;
  tokensBefore: number;
  tokensAfter: number;
}

export interface ContextWindowState {
  /** 这条报告的时刻（事件的 at）。两处来源按它挑最新。 */
  at: string;
  runId: string | null;
  turn: number;
  /** 服务端对同一次请求实收的输入 token；没报就是 null，不是 0。 */
  promptTokens: number | null;
  predictedTokens?: number | null;
  budgetWindow?: number;
  providerWindow?: number | null;
  reservedOutputTokens?: number;
  compaction?: { status: string; strategy: string; reason: string | null } | null;
  estimatedTokens: number;
  /** Input safety budget; not the measured percentage. */
  effectiveTokens: number;
  /** Provider limit when reported, otherwise the configured window. */
  window: number;
  configuredWindow: number;
  /** 自动压缩线（占窗口比例）；summarizer 关着时为 null，那就没有线可画。 */
  compressAt: number | null;
  emergencyAt: number | null;
  breakdown: ContextWindowBreakdown;
  messageCount: number;
  lastCompaction: ContextWindowCompaction | null;
}

export const BREAKDOWN_KEYS = [
  "system",
  "tools",
  "toolResults",
  "summary",
  "framework",
  "conversation",
] as const satisfies readonly (keyof ContextWindowBreakdown)[];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
}

function ratio(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0 && value <= 1 ? value : null;
}

/**
 * 把一条 `context.updated` 载荷（或会话读模型里那份，形状相同、多带 at/runId）
 * 读成状态。窗口不是正数就当没有 —— 按 0 画出来的 100% 比不画更糟。
 */
export function readContextWindow(
  raw: unknown,
  meta?: { at?: string; runId?: string | null },
): ContextWindowState | null {
  if (!isRecord(raw)) return null;
  const reportedWindow = count(raw.window);
  if (reportedWindow <= 0) return null;
  // Old reports conflated a degradation guard with a physical model limit.
  // Without an explicit provider limit, only the configured window has a
  // declared meaning for the measured percentage; retain the guard separately.
  const window = count(raw.providerWindow) || count(raw.configuredWindow) || reportedWindow;
  const at = meta?.at ?? (typeof raw.at === "string" ? raw.at : "");
  if (!at) return null;
  const rawBreakdown = isRecord(raw.breakdown) ? raw.breakdown : {};
  const breakdown = Object.fromEntries(
    BREAKDOWN_KEYS.map((key) => [key, count(rawBreakdown[key])]),
  ) as unknown as ContextWindowBreakdown;
  const last = isRecord(raw.lastCompaction) ? raw.lastCompaction : null;
  const runId = meta?.runId !== undefined
    ? meta.runId
    : typeof raw.runId === "string" ? raw.runId : null;
  return {
    at,
    runId,
    turn: count(raw.turn),
    promptTokens: typeof raw.promptTokens === "number" && Number.isFinite(raw.promptTokens)
      ? Math.max(0, Math.floor(raw.promptTokens))
      : null,
    predictedTokens: count(raw.predictedTokens) || null,
    budgetWindow: count(raw.budgetWindow) || reportedWindow,
    providerWindow: count(raw.providerWindow) || null,
    reservedOutputTokens: count(raw.reservedOutputTokens),
    compaction: isRecord(raw.compaction) ? {
      status: String(raw.compaction.status ?? ""),
      strategy: String(raw.compaction.strategy ?? ""),
      reason: typeof raw.compaction.reason === "string" ? raw.compaction.reason : null,
    } : null,
    estimatedTokens: count(raw.estimatedTokens),
    effectiveTokens: count(raw.effectiveTokens),
    window,
    configuredWindow: count(raw.configuredWindow) || window,
    compressAt: ratio(raw.compressAt),
    emergencyAt: ratio(raw.emergencyAt),
    breakdown,
    messageCount: count(raw.messageCount),
    lastCompaction: last
      ? {
          turn: count(last.turn),
          tokensBefore: count(last.tokensBefore),
          tokensAfter: count(last.tokensAfter),
        }
      : null,
  };
}

export function contextWindowFromEvent(event: ExecutionEvent): ContextWindowState | null {
  if (event.kind !== "context.updated" || event.parentRunId) return null;
  return readContextWindow(event.payload, { at: event.at, runId: event.runId ?? null });
}

function instant(at: string): number {
  const parsed = Date.parse(at);
  return Number.isFinite(parsed) ? parsed : 0;
}

/**
 * 会话读模型那份 vs 在飞那一轮事件流里的：按时刻挑最新；同一时刻取事件流的
 * （它更可能是刚到的）。两处出自同一条事件，这里不做第二种算法。
 */
export function latestContextWindow(
  fromSession: ContextWindowState | null | undefined,
  liveEvents: readonly ExecutionEvent[] | null | undefined,
): ContextWindowState | null {
  let newest: ContextWindowState | null = fromSession ?? null;
  for (const event of liveEvents ?? []) {
    const state = contextWindowFromEvent(event);
    if (!state) continue;
    if (!newest || instant(state.at) >= instant(newest.at)) newest = state;
  }
  return newest;
}

/** Observed count when available; never present the safety multiplier as fact. */
export function contextWindowUsedTokens(state: ContextWindowState): number {
  return state.promptTokens ?? state.predictedTokens ?? state.effectiveTokens;
}

export function contextWindowBasis(state: ContextWindowState): "measured" | "estimated" {
  return state.promptTokens === null ? "estimated" : "measured";
}

export function contextWindowPercent(state: ContextWindowState): number {
  return Math.round((contextWindowUsedTokens(state) / state.window) * 100);
}

export type ContextWindowTone = "ok" | "near" | "over";

/** Capacity presentation only; scheduling is owned by the runtime. */
export function contextWindowTone(state: ContextWindowState): ContextWindowTone {
  const share = contextWindowUsedTokens(state) / state.window;
  if (share >= 0.9) return "over";
  if (share >= 0.7) return "near";
  return "ok";
}

export interface ContextWindowSegment {
  key: keyof ContextWindowBreakdown | "free";
  tokens: number;
  /** 占窗口的比例，0..1；各段之和 ≤ 1。 */
  share: number;
}

/**
 * 分段条：各段按 effectiveTokens 等比缩放（各段在 harness 侧各自取整，加起来
 * 与总数差几个 token），再补一段 free 到窗口。总数超过窗口时没有 free，
 * 各段按窗口截断到刚好铺满 —— 条是画"占了多少"，不是画"超了多少"。
 */
export function contextWindowSegments(state: ContextWindowState): ContextWindowSegment[] {
  const parts = BREAKDOWN_KEYS.map((key) => ({ key, tokens: state.breakdown[key] }));
  const partsTotal = parts.reduce((sum, part) => sum + part.tokens, 0);
  const used = Math.min(state.effectiveTokens, state.window);
  const scale = partsTotal > 0 ? used / partsTotal : 0;
  const segments: ContextWindowSegment[] = parts
    .filter((part) => part.tokens > 0)
    .map((part) => ({
      key: part.key,
      tokens: Math.round(part.tokens * scale),
      share: (part.tokens * scale) / state.window,
    }));
  const free = Math.max(0, state.window - used);
  if (free > 0) segments.push({ key: "free", tokens: free, share: free / state.window });
  return segments;
}
