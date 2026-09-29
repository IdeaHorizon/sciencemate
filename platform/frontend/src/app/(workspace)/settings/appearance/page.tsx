"use client";

import { Check } from "lucide-react";
import { useInterfaceSettings } from "@/features/settings/InterfaceSettingsProvider";
import type { InterfaceDensity, InterfaceFontScale, InterfaceSettings, InterfaceTheme } from "@/lib/api";
import { Skeleton, cn } from "@/shared/ui";
import { pushError } from "@/stores/notification";
import { useT, type Phrase } from "@/shared/i18n";

const THEMES: Array<{ value: InterfaceTheme; label: string; description: Phrase }> = [
  { value: "system", label: "System", description: { zh: "跟这台设备走", en: "Follow this device" } },
  { value: "light", label: "Light", description: { zh: "浅色", en: "Light surfaces" } },
  { value: "dark", label: "Dark", description: { zh: "深色", en: "Dark surfaces" } },
];

export default function AppearanceSettingsPage() {
  const t = useT();
  const { settings, ready, saving, error, save } = useInterfaceSettings();
  const persist = async (next: InterfaceSettings) => {
    try {
      await save(next);
    } catch (caught) {
      pushError(caught instanceof Error ? caught.message : t({ zh: "外观设置没能存下来", en: "Appearance could not be saved" }));
    }
  };

  if (!ready) return <div className="settings-page"><header className="settings-page-header"><h1>{t({ zh: "外观", en: "Appearance" })}</h1></header><div className="settings-page-loading"><Skeleton height={180} /><Skeleton height={180} /></div></div>;

  return (
    <div className="settings-page">
      <header className="settings-page-header"><h1>{t({ zh: "外观", en: "Appearance" })}</h1><p>{t({ zh: "显示偏好，换个浏览器窗口也记得住。", en: "Personal display preferences, saved across browser sessions." })}</p></header>

      <section className="settings-compact-section">
        <div className="settings-compact-heading"><div><h2>{t({ zh: "主题", en: "Theme" })}</h2><p>{t({ zh: "选了立刻生效，项目、会话、设置一起变。", en: "Applied immediately across Projects, Sessions, and Settings." })}</p></div>{saving && <small>Saving…</small>}</div>
        <div className="appearance-theme-options" role="radiogroup" aria-label={t({ zh: "配色主题", en: "Color theme" })}>
          {THEMES.map((theme) => (
            <button
              type="button"
              role="radio"
              aria-checked={settings.theme === theme.value}
              className={cn("appearance-theme-option", `theme-${theme.value}`, settings.theme === theme.value && "is-selected")}
              disabled={saving}
              key={theme.value}
              onClick={() => void persist({ ...settings, theme: theme.value })}
            >
              <span className="appearance-theme-preview" aria-hidden="true"><i /><i /><i /><i /></span>
              <span><strong>{theme.label}</strong><small>{t(theme.description)}</small></span>
              {settings.theme === theme.value && <Check size={14} />}
            </button>
          ))}
        </div>
      </section>

      <section className="settings-compact-section">
        <div className="settings-compact-heading"><div><h2>{t({ zh: "布局与动效", en: "Layout and motion" })}</h2><p>{t({ zh: "只调疏密和字号，不改内容本身。", en: "Adjust information density and readable type without changing content." })}</p></div></div>
        <div className="settings-row-group">
          <label className="settings-row"><span><strong>{t({ zh: "疏密", en: "Density" })}</strong><small>{t({ zh: "宽松更透气；紧凑一屏能多放几行。", en: "Comfortable adds breathing room; Compact fits more rows on screen." })}</small></span><select value={settings.density} disabled={saving} onChange={(event) => void persist({ ...settings, density: event.target.value as InterfaceDensity })}><option value="comfortable">{t({ zh: "宽松", en: "Comfortable" })}</option><option value="compact">{t({ zh: "紧凑", en: "Compact" })}</option></select></label>
          <label className="settings-row"><span><strong>{t({ zh: "字号", en: "Text size" })}</strong><small>{t({ zh: "整体缩放界面文字，层级关系不变。", en: "Scales interface type while preserving the same information hierarchy." })}</small></span><select value={settings.font_scale} disabled={saving} onChange={(event) => void persist({ ...settings, font_scale: Number(event.target.value) as InterfaceFontScale })}><option value={90}>90%</option><option value={100}>100% · Default</option><option value={110}>110%</option><option value={120}>120%</option></select></label>
          <label className="settings-row settings-toggle-row"><span><strong>{t({ zh: "减少动效", en: "Reduce motion" })}</strong><small>{t({ zh: "去掉非必要的过渡与状态动画。", en: "Minimizes nonessential transitions and animated status changes." })}</small></span><input type="checkbox" checked={settings.reduce_motion} disabled={saving} onChange={(event) => void persist({ ...settings, reduce_motion: event.target.checked })} /></label>
        </div>
        {error && <p className="settings-inline-error" role="alert">{error}</p>}
      </section>
    </div>
  );
}
