import { isImeHandledKey } from "../../../shared/keyboard.ts";

/**
 * 判据要看的字段 —— 原生 KeyboardEvent 天然满足这个形状，调用方把
 * `e.nativeEvent` 整个递过来就行，别在调用处手抄字段：抄漏了哪个，这里就
 * 少一道防线（抄漏 keyCode 正是中文输入法选词把消息发出去的那个 bug）。
 */
export type ComposerKeyInput = {
  key: string;
  shiftKey?: boolean;
  metaKey?: boolean;
  ctrlKey?: boolean;
  isComposing?: boolean;
  keyCode?: number;
};

export function shouldSubmitComposerKey(input: ComposerKeyInput) {
  if (input.key !== "Enter" || isImeHandledKey(input)) return false;
  return !input.shiftKey || Boolean(input.metaKey || input.ctrlKey);
}

/**
 * 发送键什么时候不能按。
 *
 * `sending` 曾经一票否决 —— 那是把两件事当成一件：「这次提交还在飞」和
 * 「agent 还在干活」。会话里一轮能跑几小时，SSE 流全程开着，于是发起那一
 * 轮的这个标签页整场都按不了发送、输入框也是灰的 —— 而后端**一直支持**
 * 中途插话（同一个入口机械分流成 routed=interject）。能插话的场合，跑着
 * 恰恰是最该让人说话的时候。
 *
 * 所以 `canInterject` 为真时，跑着不再禁用；只有"没字可发"才禁。
 */
export function isComposerSendDisabled(
  draft: string,
  sending?: boolean,
  canInterject?: boolean,
) {
  if (draft.trim().length === 0) return true;
  return canInterject ? false : Boolean(sending);
}

/**
 * 右下角那**一个**按钮此刻是什么。
 *
 * 原来是两个并排：一个转圈的发送键（跑的时候输入框还是灰的，那个圈按了也
 * 没用）+ 一个方块停止键。同一个角落两个控件，其中一个恒定无效。
 *
 * 一次只给一个，且总是有意义的那个：手上有字就是「发送」（跑着就插话），
 * 没字而且有东西在跑就是「停止」。
 */
export function composerPrimaryAction({ draft, running, canStop }: {
  draft: string;
  running: boolean;
  canStop: boolean;
}): "send" | "stop" {
  if (draft.trim().length > 0) return "send";
  return running && canStop ? "stop" : "send";
}
