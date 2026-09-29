/**
 * 这一下按键是给输入法的，还是给我们的？
 *
 * 中文输入法打字时，回车 / 空格是在候选框里选词，不是在跟页面说话。浏览器
 * 用两个字段说这件事，**两个都得看**：
 *
 * - `isComposing`：Chromium / Firefox（Windows 壳的 WebView2、网页版）先发
 *   keydown 再发 compositionend，所以选词那下 keydown 上它是 true。
 * - `keyCode === 229`：WebKit（Mac 壳的 WKWebView、Safari）顺序相反 —— 先
 *   compositionend 再 keydown，等 keydown 到的时候组合态已经结束，
 *   `isComposing` 是 false。它只在 keyCode 上留了记号：凡是输入法处理过的键
 *   一律报 229（VK_PROCESSKEY），三家引擎都遵守这个约定。
 *
 * 2026-09-12 在 WKWebView + 豆包拼音上实测，打 "agent" 按回车上屏：
 *
 *     compositionend(data="agent")
 *     keydown(key="Enter", keyCode=229, isComposing=false)
 *
 * 只看 isComposing 的判据把这下回车当成了发送 —— 词上了屏，消息也飞了。
 * 上屏之后再按的那下回车才是 keyCode=13，那才是发送。
 */
export function isImeHandledKey(event: { isComposing?: boolean; keyCode?: number }): boolean {
  return Boolean(event.isComposing) || event.keyCode === 229;
}
