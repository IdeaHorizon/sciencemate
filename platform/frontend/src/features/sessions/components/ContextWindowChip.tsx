"use client";

/**
 * 输入框上方那个「当前上下文 52%」chip 的两半：chip 本体（小量表 + 百分比）
 * 与弹层（数字、分段条、压缩线、上次压缩）。
 *
 * 数从 harness 自报的 `context.updated` 来（见 ../lib/context-window.ts 的
 * 模块头），这里只画，不算：百分比、色调、分段全由那层的纯函数给。
 *
 * 它是**只读**的状态 chip —— composer 条上的每一项要么能操作，要么一眼能
 * 读懂；手动"现在就压缩"需要 worker 侧一条新的管理面 op，还没有，所以这里
 * 也不摆一个按了没反应的按钮。
 */

import Link from "next/link";
import { Gauge } from "lucide-react";

import { useT } from "@/shared/i18n";
import { formatTokenCount } from "@/features/settings/lib/usage-presentation";
import {
  BREAKDOWN_KEYS,
  contextWindowPercent,
  contextWindowSegments,
  contextWindowTone,
  type ContextWindowBreakdown,
  type ContextWindowState,
} from "../lib/context-window";

const SEGMENT_LABEL: Record<keyof ContextWindowBreakdown, { zh: string; en: string }> = {
  system: { zh: "系统提示", en: "System prompt" },
  tools: { zh: "工具定义", en: "Tool definitions" },
  toolResults: { zh: "工具结果", en: "Tool results" },
  summary: { zh: "压缩摘要", en: "Compaction summary" },
  framework: { zh: "框架提示", en: "Framework notices" },
  conversation: { zh: "对话", en: "Conversation" },
};

function percentLabel(ratio: number) {
  return `${Math.round(ratio * 100)}%`;
}

/** chip 里的内容：小量表 + 百分比。外层的 button 由 SessionComposerBar 给。 */
export function ContextWindowChipLabel({ state }: { state: ContextWindowState }) {
  const percent = contextWindowPercent(state);
  return (
    <>
      <Gauge size={13} />
      <span className="composer-chip-text">{percent}%</span>
      <i className="composer-chip-meter" aria-hidden>
        <b style={{ width: `${Math.min(100, Math.max(0, percent))}%` }} />
        {state.compressAt !== null && (
          <em style={{ left: `${Math.round(state.compressAt * 100)}%` }} />
        )}
      </i>
    </>
  );
}

export function contextWindowChipTitle(
  state: ContextWindowState,
  t: ReturnType<typeof useT>,
): string {
  const percent = contextWindowPercent(state);
  const head = t(
    { zh: "当前上下文 {used} / {window}（{percent}%）", en: "Context {used} / {window} ({percent}%)" },
    { used: formatTokenCount(state.effectiveTokens), window: formatTokenCount(state.window), percent },
  );
  return state.compressAt === null
    ? head
    : `${head} · ${t({ zh: "到 {at} 自动压缩", en: "auto-compacts at {at}" }, { at: percentLabel(state.compressAt) })}`;
}

/** 弹层正文。 */
export function ContextWindowPanel({ state }: { state: ContextWindowState }) {
  const t = useT();
  const percent = contextWindowPercent(state);
  const tone = contextWindowTone(state);
  const segments = contextWindowSegments(state);
  const rows = BREAKDOWN_KEYS.filter((key) => state.breakdown[key] > 0);

  return (
    <div className={`context-window-panel is-${tone}`}>
      <p className="composer-popover-title">{t({ zh: "上下文窗口", en: "Context window" })}</p>
      <p className="context-window-headline">
        <strong>{formatTokenCount(state.effectiveTokens)}</strong>
        <span> / {formatTokenCount(state.window)}</span>
        <em>{percent}%</em>
      </p>

      <div
        className="context-window-bar"
        role="img"
        aria-label={t(
          { zh: "已用 {percent}%", en: "{percent}% used" },
          { percent },
        )}
      >
        {segments.map((segment) => (
          <span
            key={segment.key}
            className={`seg-${segment.key}`}
            style={{ width: `${segment.share * 100}%` }}
            title={segment.key === "free"
              ? `${t({ zh: "剩余", en: "Free" })} · ${formatTokenCount(segment.tokens)}`
              : `${t(SEGMENT_LABEL[segment.key])} · ${formatTokenCount(segment.tokens)}`}
          />
        ))}
        {state.compressAt !== null && (
          <i
            className="context-window-mark"
            style={{ left: `${state.compressAt * 100}%` }}
            title={t({ zh: "自动压缩线 {at}", en: "Auto-compaction line at {at}" }, { at: percentLabel(state.compressAt) })}
          />
        )}
      </div>

      {rows.length > 0 && (
        <ul className="context-window-legend">
          {rows.map((key) => (
            <li key={key}>
              <i className={`seg-${key}`} aria-hidden />
              <span>{t(SEGMENT_LABEL[key])}</span>
              <b>{formatTokenCount(state.breakdown[key])}</b>
            </li>
          ))}
        </ul>
      )}

      <dl className="context-window-facts">
        <div>
          <dt>{t({ zh: "自动压缩", en: "Auto-compaction" })}</dt>
          <dd>
            {state.compressAt === null
              ? t({ zh: "已关闭", en: "Off" })
              : tone === "ok"
                ? t({ zh: "到 {at} 时", en: "at {at}" }, { at: percentLabel(state.compressAt) })
                : tone === "near"
                  ? t({ zh: "已过 {at} 线，下一轮开始时压", en: "past the {at} line; compacts when the next turn starts" }, { at: percentLabel(state.compressAt) })
                  : t({ zh: "已过 {at} 紧急线，下一轮强制压", en: "past the {at} emergency line; forced at the next turn" }, { at: percentLabel(state.emergencyAt ?? 1) })}
          </dd>
        </div>
        {state.promptTokens !== null && (
          <div>
            <dt>{t({ zh: "上次请求服务端实收", en: "Provider count, last request" })}</dt>
            <dd>{formatTokenCount(state.promptTokens)}</dd>
          </div>
        )}
        {state.lastCompaction && (
          <div>
            <dt>{t({ zh: "上次压缩", en: "Last compaction" })}</dt>
            <dd>
              {t(
                { zh: "第 {turn} 轮 · {before} → {after}", en: "turn {turn} · {before} → {after}" },
                {
                  turn: state.lastCompaction.turn,
                  before: formatTokenCount(state.lastCompaction.tokensBefore),
                  after: formatTokenCount(state.lastCompaction.tokensAfter),
                },
              )}
            </dd>
          </div>
        )}
        {state.configuredWindow !== state.window && (
          <div>
            <dt>{t({ zh: "窗口已按服务端观测收紧", en: "Window narrowed by provider observation" })}</dt>
            <dd>
              {t(
                { zh: "配置 {configured}，实际按 {window}", en: "configured {configured}, using {window}" },
                { configured: formatTokenCount(state.configuredWindow), window: formatTokenCount(state.window) },
              )}
            </dd>
          </div>
        )}
      </dl>

      <p className="composer-popover-foot">
        {t({
          zh: "百分比按框架判压缩的那把尺算（本地估算 × 校准，含工具定义），所以会比服务端实收略高。",
          en: "The percentage uses the ruler the harness compacts by (local estimate × calibration, tool definitions included), so it runs a little above the provider count.",
        })}
      </p>
      <Link className="composer-popover-link" href="/settings/models">
        {t({ zh: "窗口大小在模型连接设置里 →", en: "Window size is set on the model connection →" })}
      </Link>
    </div>
  );
}
