"use client";

import { useInterfaceSettings } from "@/features/settings/InterfaceSettingsProvider";
import { LANDING_OPTIONS } from "@/features/settings/lib/landing-options";
import type { DefaultLanding, InterfaceSettings } from "@/lib/api";
import { Skeleton } from "@/shared/ui";
import { pushError } from "@/stores/notification";
import { LANGUAGES, LANGUAGE_LABELS, isLanguage, useT, type Phrase } from "@/shared/i18n";



export default function GeneralSettingsPage() {
  const t = useT();
  const { settings, ready, saving, error, save } = useInterfaceSettings();

  // 存好了不弹提示：开关/下拉自己就是结果（见 stores/notification 里那条判据）。
  const persist = async (next: InterfaceSettings) => {
    try {
      await save(next);
    } catch (caught) {
      pushError(caught instanceof Error ? caught.message : t({ zh: "这一项没能存下来", en: "General settings could not be saved" }));
    }
  };

  return (
    <div className="settings-page">
      <header className="settings-page-header"><h1>{t({ zh: "通用", en: "General" })}</h1><p>{t({ zh: "打开时先看到什么，以及执行记录显示到什么程度。", en: "Choose what opens first and how recorded Run information appears." })}</p></header>

      {!ready && <div className="settings-page-loading"><Skeleton height={66} /><Skeleton height={66} /></div>}
      {ready && (
        <>
          <section className="settings-compact-section">
            <div className="settings-compact-heading"><div><h2>{t({ zh: "启动与执行记录", en: "Startup and Runs" })}</h2><p>{t({ zh: "跟着你走的个人偏好，换台机器登录也一样。", en: "Personal preferences applied wherever you sign in." })}</p></div>{saving && <small>{t({ zh: "保存中…", en: "Saving…" })}</small>}</div>
            <div className="settings-row-group">
              <label className="settings-row">
                {/* 这个开关 2026-09-15 被我删过一次 —— 当时它只驱动资讯流，切了
                    工作区一个字都不变。wangd：「还应该保留这个按钮，只是让它真的
                    有用才行啊。你删了按钮这不是糊弄吗」。对的：开关没用是缺陷，
                    删掉开关是把缺陷藏起来。现在整个界面都过
                    `t({ zh, en })`，切它是真的换语言（判据见
                    shared/i18n/every-visible-string-has-both-languages.test.ts）。 */}
                <span><strong>{t({ zh: "界面语言", en: "Interface language" })}</strong><small>{t({ zh: "工作区、设置与资讯流一起换。", en: "Switches the workspace, settings and the research feed together." })}</small></span>
                <select
                  aria-label={t({ zh: "界面语言", en: "Interface language" })}
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
                <span><strong>{t({ zh: "打开时落在哪", en: "Open after sign in" })}</strong><small>{t({ zh: "「上次的会话」会回到你最近那一条；一条都没有时落到项目列表。", en: "Last active Session reopens your most recent one; with none, you land on Projects." })}</small></span>
                <select
                  aria-label={t({ zh: "打开时落在哪", en: "Open after sign in" })}
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
                <span><strong>{t({ zh: "重新走一遍开场", en: "Replay the opening" })}</strong><small>{t({ zh: "再看一遍那几步设置和「这里干嘛的」。已经配好的东西不会被动。", en: "Walk through the setup steps and the what-is-this pointers again. Nothing you already configured is touched." })}</small></span>
                <button type="button" className="onboarding-skip" disabled={!settings.onboarding_done}
                  onClick={() => void persist({ ...settings, onboarding_done: false, project_guide_done: false })}>
                  {settings.onboarding_done ? t({ zh: "再走一遍", en: "Replay" }) : t({ zh: "还没走完", en: "Not finished yet" })}
                </button>
              </div>
              <label className="settings-row settings-toggle-row">
                <span><strong>{t({ zh: "在会话列表里显示花费", en: "Show Run usage" })}</strong><small>{t({ zh: "显示已记录的 token、已知费用与重试次数。关掉它不会删掉这些记录，也不会藏起失败。", en: "Show recorded tokens, known cost and retries. Turning it off deletes no records and hides no failures." })}</small></span>
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

          <section className="settings-compact-section">
            <div className="settings-compact-heading"><div><h2>{t({ zh: "执行过程显示", en: "Execution presentation" })}</h2><p>{t({ zh: "只改你看到多少，不改它实际跑了什么、记了什么。", en: "Controls the live execution record without changing what the Harness runs or records." })}</p></div></div>
            <div className="settings-row-group">
              <label className="settings-row">
                <span><strong>{t({ zh: "默认详细程度", en: "Default execution detail" })}</strong><small>{t({ zh: "简要只列步骤；标准加上读得懂的工具动作；完整再展开原始事件表。", en: "Summary shows steps only; Standard includes readable tool activity; Trace also exposes the canonical event table." })}</small></span>
                <select value={settings.execution_detail} disabled={saving} onChange={(event) => void persist({ ...settings, execution_detail: event.target.value as InterfaceSettings["execution_detail"] })}>
                  <option value="summary">{t({ zh: "简要", en: "Compact" })}</option><option value="standard">{t({ zh: "标准", en: "Standard" })}</option><option value="trace">{t({ zh: "完整", en: "Trace" })}</option>
                </select>
              </label>
              <label className="settings-row settings-toggle-row"><span><strong>{t({ zh: "折起做完的工具调用", en: "Collapse completed tools" })}</strong><small>{t({ zh: "做完的收起来；正在跑的、重试的、失败的照常展开。", en: "Keeps finished tool input and output folded; running, retrying, and failed work remains visible." })}</small></span><input type="checkbox" checked={settings.auto_collapse_completed_tools} disabled={saving} onChange={(event) => void persist({ ...settings, auto_collapse_completed_tools: event.target.checked })} /></label>
              <label className="settings-row settings-toggle-row"><span><strong>{t({ zh: "折起做完的步骤", en: "Collapse completed steps" })}</strong><small>{t({ zh: "做完的收窄；正在进行的和要你拍板的保持展开。", en: "Keeps finished research steps compact; active work and Decisions remain open." })}</small></span><input type="checkbox" checked={settings.auto_collapse_completed_steps} disabled={saving} onChange={(event) => void persist({ ...settings, auto_collapse_completed_steps: event.target.checked })} /></label>
              <label className="settings-row settings-toggle-row"><span><strong>{t({ zh: "跟着新消息滚", en: "Follow active Run" })}</strong><small>{t({ zh: "会话在跑时自动滚到新消息。关掉它就停在你正读的地方。", en: "Moves the conversation to newly recorded messages while a Session is active. Turn this off to keep your reading position." })}</small></span><input type="checkbox" checked={settings.follow_active_run} disabled={saving} onChange={(event) => void persist({ ...settings, follow_active_run: event.target.checked })} /></label>
            </div>
          </section>
          {error && <p className="settings-inline-error" role="alert">{error}</p>}
        </>
      )}
    </div>
  );
}
