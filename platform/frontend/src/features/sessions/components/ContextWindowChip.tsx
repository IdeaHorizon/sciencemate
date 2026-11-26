"use client";

import Link from "next/link";
import { Gauge } from "lucide-react";
import { useT } from "@/shared/i18n";
import { formatTokenCount } from "@/features/settings/lib/usage-presentation";
import {
  contextWindowBasis,
  contextWindowPercent,
  contextWindowTone,
  contextWindowUsedTokens,
  type ContextWindowState,
} from "../lib/context-window";

export function ContextWindowChipLabel({ state }: { state: ContextWindowState }) {
  const percent = contextWindowPercent(state);
  return <>
    <Gauge size={13} />
    <span className="composer-chip-text">{contextWindowBasis(state) === "estimated" ? "≈" : ""}{percent}%</span>
    <i className="composer-chip-meter" aria-hidden>
      <b style={{ width: `${Math.min(100, Math.max(0, percent))}%` }} />
    </i>
  </>;
}

export function contextWindowChipTitle(state: ContextWindowState, t: ReturnType<typeof useT>): string {
  return t(
    { zh: "调度器上次请求 · {basis} {used} / {window}（{percent}%）", en: "Orchestrator last request · {basis} {used} / {window} ({percent}%)" },
    { basis: contextWindowBasis(state) === "measured" ? t({ zh: "实测", en: "measured" }) : t({ zh: "估算", en: "estimated" }),
      used: formatTokenCount(contextWindowUsedTokens(state)), window: formatTokenCount(state.window),
      percent: contextWindowPercent(state) },
  );
}

export function ContextWindowPanel({ state }: { state: ContextWindowState }) {
  const t = useT();
  const result = state.compaction;
  return <div className={`context-window-panel is-${contextWindowTone(state)}`}>
    <p className="composer-popover-title">{t({ zh: "调度器 · 上次请求", en: "Orchestrator · last request" })}</p>
    <p className="context-window-headline">
      <strong>{contextWindowBasis(state) === "estimated" ? "≈" : ""}{formatTokenCount(contextWindowUsedTokens(state))}</strong>
      <span> / {formatTokenCount(state.window)}</span>
      <em>{contextWindowPercent(state)}%</em>
    </p>
    <dl className="context-window-facts">
      <div>
        <dt>{t({ zh: "计数来源", en: "Count source" })}</dt>
        <dd>{contextWindowBasis(state) === "measured"
          ? t({ zh: "模型服务实收", en: "Provider measurement" })
          : t({ zh: "本地估算，服务端未报告", en: "Local estimate; provider count unavailable" })}</dd>
      </div>
      <div>
        <dt>{t({ zh: "窗口来源", en: "Window source" })}</dt>
        <dd>{state.providerWindow
          ? t({ zh: "服务端报告的上限", en: "Provider-reported limit" })
          : t({ zh: "模型连接中的配置", en: "Model connection configuration" })}</dd>
      </div>
      <div>
        <dt>{t({ zh: "输入预算估算", en: "Input safety estimate" })}</dt>
        <dd>{formatTokenCount(state.effectiveTokens)} / {formatTokenCount(state.budgetWindow ?? state.window)}</dd>
      </div>
      {!!state.reservedOutputTokens && <div>
        <dt>{t({ zh: "本次输出预留", en: "Response allowance" })}</dt>
        <dd>{formatTokenCount(state.reservedOutputTokens)}</dd>
      </div>}
      {result && <div>
        <dt>{t({ zh: "上次整理结果", en: "Last compaction result" })}</dt>
        <dd>{result.status === "ready"
          ? result.strategy === "archive"
            ? t({ zh: "已归档旧历史，可按需取回", en: "History archived and retrievable" })
            : result.strategy === "unchanged"
              ? t({ zh: "内容已满足整理目标", en: "Context already meets the target" })
              : t({ zh: "已压缩并满足预算", en: "Compacted within budget" })
          : result.reason === "history_storage_unavailable"
            ? t({ zh: "历史暂时无法归档，原内容已保留", en: "History storage unavailable; original content retained" })
            : t({ zh: "必要内容已保留，未达到整理目标", en: "Protected content retained; compaction target not reached" })}</dd>
      </div>}
      {state.lastCompaction && <div>
        <dt>{t({ zh: "上次整理", en: "Last compaction" })}</dt>
        <dd>{formatTokenCount(state.lastCompaction.tokensBefore)} → {formatTokenCount(state.lastCompaction.tokensAfter)}</dd>
      </div>}
    </dl>
    <p className="composer-popover-foot">{t({
      zh: "各节点有独立上下文。这里显示调度器上次请求；输入预算估算含安全余量，不代表模型实际占用。",
      en: "Each node has its own context. This is the orchestrator's last request; its safety estimate is separate from measured usage.",
    })}</p>
    <Link className="composer-popover-link" href="/settings/models">
      {t({ zh: "设置模型窗口 →", en: "Configure model window →" })}
    </Link>
  </div>;
}
