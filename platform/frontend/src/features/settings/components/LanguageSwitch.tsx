"use client";

/**
 * 界面语言的切换钮 —— **用目标语言写着**。
 *
 * ## 为什么不是设置页里那个下拉就够了
 *
 * 那个下拉在「设置 → 外观」里，入口、页名、标签全是当前语言。一个不懂中文的人
 * 打开是一屏中文，他找不到「设置」两个字（wangd 2026-10-19）；反过来，英文系统
 * 上被判成英文的中文研究者也一样。两头的病是同一个：**换语言的入口只用当前语言写**。
 *
 * 所以这里的字永远是**另一种语言自己的名字**（中文界面上写 English，英文界面上写
 * 中文）—— 认得它的人一眼就认得。名字取自 `LANGUAGE_LABELS`，不在这里写死。
 *
 * ## 两种样子
 *
 *   - `toggle`：一个按钮，写着另一种语言 —— 侧栏常驻、登录页右上；
 *   - `both`：两种并排、当前那个按下 —— 开场第一张卡片最上面，第一次打开就看得见
 *     "语言是可以选的"，而且预选（按系统语言定的）是哪个一目了然。
 */

import { Globe } from "lucide-react";
import { useInterfaceSettings } from "@/features/settings/InterfaceSettingsProvider";
import { LANGUAGES, LANGUAGE_LABELS, useLanguage, useT, type Language } from "@/shared/i18n";
import { cn } from "@/shared/ui";

export function LanguageSwitch({ variant = "toggle", className }: { variant?: "toggle" | "both"; className?: string }) {
  const t = useT();
  const current = useLanguage();
  const { chooseLanguage, saving } = useInterfaceSettings();
  const choose = (language: Language) => {
    if (language !== current) void chooseLanguage(language).catch(() => {});
  };

  if (variant === "both") {
    return (
      <div className={cn("language-switch-both", className)} role="radiogroup"
        aria-label={t({ zh: "界面语言", en: "Interface language" })}>
        <Globe size={14} aria-hidden="true" />
        {LANGUAGES.map((language) => (
          <button key={language} type="button" role="radio" aria-checked={language === current}
            lang={language === "zh" ? "zh-CN" : "en"}
            className={cn("language-switch-option", language === current && "is-active")}
            disabled={saving} onClick={() => choose(language)}>
            {LANGUAGE_LABELS[language]}
          </button>
        ))}
      </div>
    );
  }

  const other: Language = current === "zh" ? "en" : "zh";
  const label = t({ zh: "界面语言：换成 {other}", en: "Interface language: switch to {other}" },
    { other: LANGUAGE_LABELS[other] });
  return (
    <button type="button" className={cn("language-switch-toggle", className)}
      lang={other === "zh" ? "zh-CN" : "en"} aria-label={label} title={label}
      disabled={saving} onClick={() => choose(other)}>
      <Globe size={16} aria-hidden="true" />
      <span>{LANGUAGE_LABELS[other]}</span>
    </button>
  );
}
