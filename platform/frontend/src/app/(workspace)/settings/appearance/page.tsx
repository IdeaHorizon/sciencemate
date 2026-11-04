"use client";

import { Check } from "lucide-react";
import { useInterfaceSettings } from "@/features/settings/InterfaceSettingsProvider";
import type { InterfaceDensity, InterfaceFontScale, InterfaceSettings, InterfaceTheme } from "@/lib/api";
import { Skeleton, cn } from "@/shared/ui";
import { pushError } from "@/stores/notification";
import { useT, type Phrase } from "@/shared/i18n";

const THEMES: Array<{ value: InterfaceTheme; label: Phrase }> = [
  { value: "system", label: { zh: "跟随系统", en: "System" } },
  { value: "light", label: { zh: "浅色", en: "Light" } },
  { value: "dark", label: { zh: "深色", en: "Dark" } },
];

export default function AppearanceSettingsPage() {
  const t = useT();
  const { settings, ready, saving, error, save } = useInterfaceSettings();
  const persist = async (next: InterfaceSettings) => {
    try {
      await save(next);
    } catch (caught) {
      pushError(caught instanceof Error ? caught.message : t({ zh: "外观设置保存失败", en: "Appearance could not be saved" }));
    }
  };

  if (!ready) return <div className="settings-page"><header className="settings-page-header"><h1>{t({ zh: "外观", en: "Appearance" })}</h1></header><div className="settings-page-loading"><Skeleton height={180} /><Skeleton height={180} /></div></div>;

  return (
    <div className="settings-page">
      <header className="settings-page-header"><h1>{t({ zh: "外观", en: "Appearance" })}</h1></header>

      <section className="settings-compact-section">
        <div className="settings-compact-heading"><div><h2>{t({ zh: "主题", en: "Theme" })}</h2></div>{saving && <small>{t({ zh: "保存中…", en: "Saving…" })}</small>}</div>
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
              <span><strong>{t(theme.label)}</strong></span>
              {settings.theme === theme.value && <Check size={14} />}
            </button>
          ))}
        </div>
      </section>

      <section className="settings-compact-section">
        <div className="settings-compact-heading"><div><h2>{t({ zh: "布局与动效", en: "Layout and motion" })}</h2></div></div>
        <div className="settings-row-group">
          <label className="settings-row"><span><strong>{t({ zh: "界面密度", en: "Density" })}</strong><small>{t({ zh: "紧凑模式下一屏能显示更多内容。", en: "Compact fits more on screen." })}</small></span><select value={settings.density} disabled={saving} onChange={(event) => void persist({ ...settings, density: event.target.value as InterfaceDensity })}><option value="comfortable">{t({ zh: "宽松", en: "Comfortable" })}</option><option value="compact">{t({ zh: "紧凑", en: "Compact" })}</option></select></label>
          <label className="settings-row"><span><strong>{t({ zh: "文字大小", en: "Text size" })}</strong></span><select value={settings.font_scale} disabled={saving} onChange={(event) => void persist({ ...settings, font_scale: Number(event.target.value) as InterfaceFontScale })}><option value={90}>90%</option><option value={100}>{t({ zh: "100%（默认）", en: "100% (default)" })}</option><option value={110}>110%</option><option value={120}>120%</option></select></label>
          <label className="settings-row settings-toggle-row"><span><strong>{t({ zh: "减少动态效果", en: "Reduce motion" })}</strong><small>{t({ zh: "关闭不必要的过渡动画。", en: "Turns off nonessential transitions." })}</small></span><input type="checkbox" checked={settings.reduce_motion} disabled={saving} onChange={(event) => void persist({ ...settings, reduce_motion: event.target.checked })} /></label>
        </div>
        {error && <p className="settings-inline-error" role="alert">{error}</p>}
      </section>
    </div>
  );
}
