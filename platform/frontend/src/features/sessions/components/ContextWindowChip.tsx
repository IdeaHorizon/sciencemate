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
  framework: { zh: "平台提示", en: "Platform notices" },
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
    : `${head} · ${t({ zh: "达到 {at} 时自动压缩", en: "auto-compacts at {at}" }, { at: percentLabel(state.compressAt) })}`;
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
            title={t({ zh: "自动压缩阈值 {at}", en: "Auto-compaction threshold {at}" }, { at: percentLabel(state.compressAt) })}
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
                ? t({ zh: "达到 {at} 时", en: "at {at}" }, { at: percentLabel(state.compressAt) })
                : tone === "near"
                  ? t({ zh: "已超过 {at}，下次请求模型前压缩", en: "Past {at}; compacts before the next model request" }, { at: percentLabel(state.compressAt) })
                  : t({ zh: "已超过 {at} 上限，下次请求模型前强制压缩", en: "Past the {at} limit; compaction is forced before the next model request" }, { at: percentLabel(state.emergencyAt ?? 1) })}
          </dd>
        </div>
        {state.promptTokens !== null && (
          <div>
            <dt>{t({ zh: "上次请求实际 token 数", en: "Actual tokens, last request" })}</dt>
            <dd>{formatTokenCount(state.promptTokens)}</dd>
          </div>
        )}
        {state.lastCompaction && (
          <div>
            <dt>{t({ zh: "上次压缩", en: "Last compaction" })}</dt>
            <dd>
              {t(
                { zh: "第 {turn} 步 · {before} → {after}", en: "Step {turn} · {before} → {after}" },
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
            <dt>{t({ zh: "实际窗口小于配置", en: "Effective window is smaller than configured" })}</dt>
            <dd>
              {t(
                { zh: "配置为 {configured}，实际使用 {window}", en: "configured {configured}, using {window}" },
                { configured: formatTokenCount(state.configuredWindow), window: formatTokenCount(state.window) },
              )}
            </dd>
          </div>
        )}
      </dl>

      <p className="composer-popover-foot">
        {t({
          zh: "百分比是本地估算（含工具定义），通常略高于实际 token 数。",
          en: "The percentage is a local estimate (including tool definitions), so it usually runs slightly above the actual count.",
        })}
      </p>
      <Link className="composer-popover-link" href="/settings/models">
        {t({ zh: "在模型连接中设置窗口大小 →", en: "Set the window size in model connections →" })}
      </Link>
    </div>
  );
}
