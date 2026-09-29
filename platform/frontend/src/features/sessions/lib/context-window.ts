import type { ExecutionEvent } from "@/features/execution/lib/execution-event";

/**
 * 「当前上下文 xx / 窗口（xx%）· 到 70% 自动压缩」—— 输入框上方那个 chip 的数据。
 *
 * ## 数从哪来
 *
 * harness 每次 LLM 响应后自报一条 `context.updated`（core/summarizer 的
 * `context_window_report`）。会话读模型带着**已知最新**的一条（`contextWindow`），
 * 在飞的那一轮由事件流实时补上更新的。两处出自同一条事件，这里只按时间挑
 * 最新的，不自己算。
 *
 * ## 为什么百分比按 effectiveTokens 而不是 promptTokens
 *
 * 框架判"该不该压"看的是 effective（本地估算 × 校准，含工具 schema），静态校准
 * 下限 1.3 意味着服务端实收 54% 时框架已经站在 70% 线上。界面若用服务端数字
 * 配框架的压缩线，就会"显示 54% 却已经开始压缩"。所以百分比、分段条、压缩线
 * 三样同尺（effective）；服务端实收另给一行，是事实，不是量尺。
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
  estimatedTokens: number;
  /** 框架用来判压缩的那把尺 —— 百分比与压缩线都按它。 */
  effectiveTokens: number;
  /** 有效窗口（配置窗口与 provider 观测取小）。 */
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
  const window = count(raw.window);
  if (window <= 0) return null;
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
  if (event.kind !== "context.updated") return null;
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

/** 占窗口的百分比，整数；可以超过 100（压缩前的瞬间确实会）。 */
export function contextWindowPercent(state: ContextWindowState): number {
  return Math.round((state.effectiveTokens / state.window) * 100);
}

export type ContextWindowTone = "ok" | "near" | "over";

/**
 * 离压缩线多远：`near` = 已过自动压缩线（下一轮开始就会压），`over` = 已过
 * 紧急线（框架会无视冷却强制压）。没有压缩线（summarizer 关着）就只有 ok。
 */
export function contextWindowTone(state: ContextWindowState): ContextWindowTone {
  const share = state.effectiveTokens / state.window;
  if (state.emergencyAt !== null && share >= state.emergencyAt) return "over";
  if (state.compressAt !== null && share >= state.compressAt) return "near";
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
