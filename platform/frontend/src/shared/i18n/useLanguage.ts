"use client";

import { useInterfaceSettings } from "@/features/settings/InterfaceSettingsProvider";
import { isLanguage, type Language } from "./language.ts";

/**
 * 当前界面语言。
 *
 * 只在**组件树顶上读一次**，然后当参数往下传 —— 和 `now` 一样的处理方式
 * （见 feed-presentation 里 `relativeTime` 的注释）。每个纯函数自己去够一个
 * 全局的话，它们就都不再是纯函数，这个仓库也就没有一条能被测到的呈现逻辑了。
 */
export function useLanguage(): Language {
  const { settings } = useInterfaceSettings();
  return isLanguage(settings.language) ? settings.language : "zh";
}
