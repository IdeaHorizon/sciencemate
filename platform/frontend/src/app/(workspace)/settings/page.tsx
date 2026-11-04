"use client";

import { useInterfaceSettings } from "@/features/settings/InterfaceSettingsProvider";
import { LANDING_OPTIONS } from "@/features/settings/lib/landing-options";
import { EXECUTION_DETAIL_LEVELS } from "@/features/execution/lib/execution-detail-levels";
import type { DefaultLanding, InterfaceSettings } from "@/lib/api";
import { Skeleton } from "@/shared/ui";
import { pushError } from "@/stores/notification";
import { LANGUAGES, LANGUAGE_LABELS, isLanguage, useT } from "@/shared/i18n";

export default function GeneralSettingsPage() {
  const t = useT();
  const { settings, ready, saving, error, save } = useInterfaceSettings();

  // 存好了不弹提示：开关/下拉自己就是结果（见 stores/notification 里那条判据）。
  const persist = async (next: InterfaceSettings) => {
    try {
      await save(next);
    } catch (caught) {
      pushError(caught instanceof Error ? caught.message : t({ zh: "设置保存失败", en: "Couldn't save settings" }));
    }
  };

  return (
    <div className="settings-page">
      <header className="settings-page-header"><h1>{t({ zh: "通用", en: "General" })}</h1></header>

      {!ready && <div className="settings-page-loading"><Skeleton height={66} /><Skeleton height={66} /></div>}
      {ready && (
        <>
          <section className="settings-compact-section">
            <div className="settings-compact-heading"><div><h2>{t({ zh: "语言与启动", en: "Language & startup" })}</h2></div>{saving && <small>{t({ zh: "保存中…", en: "Saving…" })}</small>}</div>
            <div className="settings-row-group">
              <label className="settings-row">
                {/* 这个开关 2026-09-15 被我删过一次 —— 当时它只驱动资讯流，切了
                    工作区一个字都不变。wangd：「还应该保留这个按钮，只是让它真的
                    有用才行啊。你删了按钮这不是糊弄吗」。对的：开关没用是缺陷，
                    删掉开关是把缺陷藏起来。现在整个界面都过
                    `t({ zh, en })`，切它是真的换语言（判据见
                    shared/i18n/every-visible-string-has-both-languages.test.ts）。 */}
                <span><strong>{t({ zh: "界面语言", en: "Language" })}</strong></span>
                <select
                  aria-label={t({ zh: "界面语言", en: "Language" })}
                  value={settings.language}
                  disabled={saving}
                  onChange={(event) => {
                    const next = event.target.value;
                    // 下拉只可能给出词表里的值，但**不假设**：一个非法值存进偏好
                    // 之后，每次读都要被打回默认，而没人知道为什么。
                    if (isLanguage(next)) void persist({ ...settings, language: next });
                  }}
                >
                  {LANGUAGES.map((code) => (
                    <option key={code} value={code}>{LANGUAGE_LABELS[code]}</option>
                  ))}
                </select>
              </label>
              <label className="settings-row">
                <span><strong>{t({ zh: "启动时显示", en: "Open on startup" })}</strong></span>
                <select
                  aria-label={t({ zh: "启动时显示", en: "Open on startup" })}
                  value={settings.default_landing}
                  disabled={saving}
                  onChange={(event) => void persist(
                    { ...settings, default_landing: event.target.value as DefaultLanding },
                  )}
                >
                  {LANDING_OPTIONS.map((option) => <option key={option.value} value={option.value}>{t(option.label)}</option>)}
                </select>
              </label>
              <div className="settings-row">
                <span><strong>{t({ zh: "新手引导", en: "Onboarding" })}</strong><small>{t({ zh: "重新查看首次使用时的设置和功能介绍，已有设置不受影响。", en: "See the first-run setup and feature tour again. Your current settings are kept." })}</small></span>
                <button type="button" className="onboarding-skip" disabled={!settings.onboarding_done}
                  onClick={() => void persist({ ...settings, onboarding_done: false, project_guide_done: false })}>
                  {settings.onboarding_done ? t({ zh: "重新查看", en: "Show again" }) : t({ zh: "尚未完成", en: "Not finished" })}
                </button>
              </div>
            </div>
          </section>

          <section className="settings-compact-section">
            <div className="settings-compact-heading"><div><h2>{t({ zh: "会话显示", en: "Session display" })}</h2></div></div>
            <div className="settings-row-group">
              <label className="settings-row">
                <span><strong>{t({ zh: "执行过程详细程度", en: "Execution detail" })}</strong><small>{t({ zh: "简要只显示步骤；标准再显示每步用了哪些工具；完整再列出全部原始事件。会话里也能随时切换。", en: "Summary shows steps only. Standard adds the tools each step used. Full also lists every raw event. You can switch inside a session too." })}</small></span>
                <select value={settings.execution_detail} disabled={saving} onChange={(event) => void persist({ ...settings, execution_detail: event.target.value as InterfaceSettings["execution_detail"] })}>
                  {EXECUTION_DETAIL_LEVELS.map((level) => <option key={level.value} value={level.value}>{t(level.label)}</option>)}
                </select>
              </label>
              <label className="settings-row settings-toggle-row"><span><strong>{t({ zh: "自动折叠已完成的工具调用", en: "Collapse finished tool calls" })}</strong><small>{t({ zh: "运行中、重试中和失败的不会折叠。", en: "Running, retrying and failed calls stay open." })}</small></span><input type="checkbox" checked={settings.auto_collapse_completed_tools} disabled={saving} onChange={(event) => void persist({ ...settings, auto_collapse_completed_tools: event.target.checked })} /></label>
              <label className="settings-row settings-toggle-row"><span><strong>{t({ zh: "自动折叠已完成的步骤", en: "Collapse finished steps" })}</strong><small>{t({ zh: "进行中和等你决定的步骤不会折叠。", en: "Steps in progress or waiting for your decision stay open." })}</small></span><input type="checkbox" checked={settings.auto_collapse_completed_steps} disabled={saving} onChange={(event) => void persist({ ...settings, auto_collapse_completed_steps: event.target.checked })} /></label>
              <label className="settings-row settings-toggle-row"><span><strong>{t({ zh: "自动滚动到最新消息", en: "Auto-scroll to new messages" })}</strong></span><input type="checkbox" checked={settings.follow_active_run} disabled={saving} onChange={(event) => void persist({ ...settings, follow_active_run: event.target.checked })} /></label>
              <label className="settings-row settings-toggle-row">
                <span><strong>{t({ zh: "在会话列表中显示用量", en: "Show usage in session list" })}</strong><small>{t({ zh: "在每个会话下显示 token 数、费用和重试次数。", en: "Shows tokens, cost and retries under each session." })}</small></span>
                <input
                  type="checkbox"
                  checked={settings.show_run_usage}
                  disabled={saving}
                  onChange={(event) => void persist(
                    { ...settings, show_run_usage: event.target.checked },
                  )}
                />
              </label>
            </div>
          </section>
          {error && <p className="settings-inline-error" role="alert">{error}</p>}
        </>
      )}
    </div>
  );
}
