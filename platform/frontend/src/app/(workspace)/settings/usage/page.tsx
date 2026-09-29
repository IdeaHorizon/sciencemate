"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { buildUsageSeries, formatKnownCost, formatTokenCount, usageIntensity } from "@/features/settings/lib/usage-presentation";
import { Empty, Skeleton } from "@/shared/ui";
import { useT } from "@/shared/i18n";

export default function UsageSettingsPage() {
  const t = useT();
  const usage = useQuery({ queryKey: qk.usageSettings(), queryFn: () => api.getUsageSettings() });
  const heatmap = useMemo(() => buildUsageSeries(usage.data?.daily ?? [], 365), [usage.data?.daily]);
  const recent = heatmap.slice(-90);
  const heatmapMax = Math.max(0, ...heatmap.map((day) => day.total_tokens));
  const recentMax = Math.max(0, ...recent.map((day) => day.total_tokens));
  const cost = usage.data ? formatKnownCost(usage.data.known_cost, usage.data.cost_currency) : null;

  return (
    <div className="settings-page usage-settings-page">
      <header className="settings-page-header"><h1>{t({ zh: "用量", en: "Usage" })}</h1><p>{t({ zh: "这个账号记录在案的会话、执行、token、重试与已知模型费用。", en: "Recorded Sessions, Runs, tokens, retries, and known model cost for your account." })}</p></header>

      {usage.isLoading && !usage.data && <div className="settings-page-loading"><Skeleton height={110} /><Skeleton height={180} /><Skeleton height={220} /></div>}
      {usage.isError && <Empty title={t({ zh: "读不到用量", en: "Usage unavailable" })} hint={t({ zh: "服务端没有返回用量账本。", en: "The server did not return the recorded usage ledger." })} />}

      {usage.data && (
        <>
          <section className="usage-kpi-strip" aria-label={t({ zh: "用量概览", en: "Usage summary" })}>
            <div><strong>{usage.data.session_count.toLocaleString()}</strong><span>{t({ zh: "会话", en: "Sessions" })}</span></div>
            <div><strong>{usage.data.run_count.toLocaleString()}</strong><span>{t({ zh: "执行", en: "Runs" })}</span></div>
            <div><strong>{formatTokenCount(usage.data.total_tokens)}</strong><span>{t({ zh: "token 总数", en: "Total tokens" })}</span></div>
            <div><strong>{usage.data.retry_count.toLocaleString()}</strong><span>{t({ zh: "重试", en: "Retries" })}</span></div>
            <div><strong>{usage.data.active_days.toLocaleString()}</strong><span>{t({ zh: "活跃天数", en: "Active days" })}</span></div>
            <div><strong>{usage.data.current_streak.toLocaleString()}</strong><span>{t({ zh: "连续天数", en: "Current streak" })}</span></div>
          </section>

          <section className="settings-compact-section">
            <div className="settings-compact-heading"><div><h2>{t({ zh: "近 365 天", en: "365-day activity" })}</h2><p>{t({ zh: "一个方块是一天，颜色深浅按当天记录的 token 算。", en: "Each square is one calendar day; intensity is based on recorded tokens." })}</p></div><small>{usage.data.longest_streak} day longest streak</small></div>
            <div className="usage-heatmap-wrap">
              <div className="usage-heatmap" aria-label={t({ zh: "近 365 天 token 活动", en: "365-day token activity" })}>
                {heatmap.map((day) => <span key={day.date} className={`level-${usageIntensity(day.total_tokens, heatmapMax)}`} title={`${day.date} · ${day.total_tokens.toLocaleString()} tokens · ${day.run_count} Runs`} />)}
              </div>
              <div className="usage-legend"><span>{t({ zh: "少", en: "Less" })}</span>{[0, 1, 2, 3, 4].map((level) => <i key={level} className={`level-${level}`} />)}<span>{t({ zh: "多", en: "More" })}</span></div>
            </div>
          </section>

          <section className="settings-compact-section">
            <div className="settings-compact-heading"><div><h2>{t({ zh: "近 90 天", en: "Recent 90 days" })}</h2><p>{t({ zh: "每天记录的 token 量，悬停看准确数值。", en: "Daily recorded token volume; hover a bar for exact values." })}</p></div></div>
            <div className="usage-bar-chart" aria-label={t({ zh: "近 90 天 token 历史", en: "90-day token history" })}>
              {recent.map((day) => <span key={day.date} title={`${day.date} · ${day.total_tokens.toLocaleString()} tokens · ${day.run_count} Runs`} style={{ height: `${recentMax && day.total_tokens ? Math.max(2, (day.total_tokens / recentMax) * 100) : 0}%` }} />)}
            </div>
          </section>

          <section className="settings-compact-section">
            <div className="settings-compact-heading"><div><h2>{t({ zh: "token 与费用覆盖", en: "Tokens and cost coverage" })}</h2><p>{t({ zh: "只有那一轮真的记下了金额和币种，这里才显示费用。", en: "Cost is shown only where the Run recorded a value and currency." })}</p></div></div>
            <dl className="settings-row-group settings-account-group">
              <div className="settings-row"><dt><strong>{t({ zh: "输入 token", en: "Prompt tokens" })}</strong><small>{t({ zh: "模型调用记录的输入量", en: "Input recorded by model calls" })}</small></dt><dd>{usage.data.prompt_tokens.toLocaleString()}</dd></div>
              <div className="settings-row"><dt><strong>{t({ zh: "输出 token", en: "Completion tokens" })}</strong><small>{t({ zh: "模型调用记录的输出量", en: "Output recorded by model calls" })}</small></dt><dd>{usage.data.completion_tokens.toLocaleString()}</dd></div>
              <div className="settings-row"><dt><strong>{t({ zh: "已知费用", en: "Known cost" })}</strong><small>{t({ zh: "{known} 次运行带着费用，{unknown} 次没有。", en: "{known} Runs include cost; {unknown} do not." }, { known: usage.data.cost_known_runs, unknown: usage.data.cost_unknown_runs })}</small></dt><dd>{cost ?? t({ zh: "读不到 · 没有记录币种", en: "Unavailable · currency not recorded" })}</dd></div>
            </dl>
          </section>
        </>
      )}
    </div>
  );
}
