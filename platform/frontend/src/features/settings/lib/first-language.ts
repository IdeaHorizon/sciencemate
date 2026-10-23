import type { InterfaceLanguageHint } from "@/lib/api";
import { languageForASystem, type Language } from "../../../shared/i18n/language.ts";

/**
 * 一个账号第一次打开时该替他定哪种语言 —— 选过了就是 `null`（只认他选的，不再猜）。
 *
 * 先后：
 *   1. 他在登录页上点过的那个（那时还没有账号可存，记在这台浏览器上）；
 *   2. 他在 Windows 安装器上点过的那个（默认的英文不算 —— 安装器只在点过时才记）；
 *   3. 他电脑上的系统语言列表（只有桌面版的后端读得到整张表）；
 *   4. 浏览器给的（组织服务器上；桌面版的 WKWebView 只给第一项，所以排在后面）。
 *
 * 判据只有一条，在 `languageForASystem`：列表里有中文就中文，否则英文。
 */
export function languageToSettleOnFirstOpen(
  hint: InterfaceLanguageHint,
  chosenBeforeSignIn: Language | null,
  browserLanguages: readonly string[],
): Language | null {
  if (hint.chosen) return null;
  if (chosenBeforeSignIn) return chosenBeforeSignIn;
  if (hint.chosen_at_install) return hint.chosen_at_install;
  return languageForASystem(hint.system_languages.length > 0 ? hint.system_languages : browserLanguages);
}
