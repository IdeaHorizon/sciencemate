"use client";

import { useCallback } from "react";
import { Loader2, Send, Square } from "lucide-react";
import {
  composerPrimaryAction,
  isComposerSendDisabled,
  shouldSubmitComposerKey,
} from "../lib/composer-keyboard";
import { draftHasPendingPaste, isLongPaste } from "../lib/long-paste";
import { useT } from "@/shared/i18n";

export function ChatComposer({
  draft,
  setDraft,
  onSend,
  sending,
  placeholder,
  disabled,
  context,
  disabledReason,
  onStop,
  stopping,
  canInterject,
  onLongPaste,
}: {
  draft: string;
  setDraft: (v: string) => void;
  onSend: () => void;
  sending?: boolean;
  placeholder?: string;
  disabled?: boolean;
  context?: React.ReactNode;
  disabledReason?: string;
  /** 有正在跑的轮时传入 —— 没字可发的时候，右下角那个按钮就是它。 */
  onStop?: () => void;
  stopping?: boolean;
  /**
   * 这个会话支持中途插话（后端把话投进正在跑的调度器的收件箱）。
   *
   * 为真时，「跑着」不再禁用输入框和发送键 —— 跑着恰恰是最该让人说话的
   * 时候（调方向、问进度、叫停某个子任务）。为假的场合（全局启动器）保持
   * 原样：一次提交没回来之前别让人重复发。
   */
  canInterject?: boolean;
  /**
   * 贴进来的东西够长（见 lib/long-paste）就不进输入框，交给调用方存成文件。
   * 不传 = 这里没有上传通道（全局启动器），照常贴。
   */
  onLongPaste?: (text: string, selection: { start: number; end: number }) => void;
}) {
  const t = useT();
  const sendDisabled = disabled
    || isComposerSendDisabled(draft, sending, canInterject)
    || draftHasPendingPaste(draft);
  // 输入框只在**真的不该打字**的时候灰掉（只读 / 状态未知 / 等你回答上面的
  // 问题）。原来它还额外吃一个 `sending` —— 而 sending 在一条流开着的整场
  // 都是真，于是发起那一轮的标签页几小时都打不了字。
  const inputDisabled = Boolean(disabled) || (Boolean(sending) && !canInterject);
  const action = composerPrimaryAction({
    draft,
    running: Boolean(sending) || Boolean(onStop),
    canStop: Boolean(onStop),
  });
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      // 原生事件整个递给判据：它要看哪些字段由它自己读（isComposing 之外还有
      // keyCode —— WebKit 上中文输入法选词那下回车只在 keyCode 上留了记号）。
      if (!shouldSubmitComposerKey(e.nativeEvent)) return;
      e.preventDefault();
      if (!sendDisabled) onSend();
    },
    [onSend, sendDisabled],
  );
  const handlePaste = useCallback(
    (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
      if (!onLongPaste) return;
      const text = e.clipboardData.getData("text/plain");
      if (!isLongPaste(text)) return;
      e.preventDefault();
      onLongPaste(text, { start: e.currentTarget.selectionStart, end: e.currentTarget.selectionEnd });
    },
    [onLongPaste],
  );

  return (
    <div className="composer">
      {(context || disabledReason) && (
        <div className="composer-context">
          {context}
          {disabledReason && <small>{disabledReason}</small>}
        </div>
      )}
      <textarea
        value={draft}
        disabled={inputDisabled}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={handleKeyDown}
        onPaste={handlePaste}
        placeholder={placeholder ?? t({ zh: "问点什么…", en: "Ask anything…" })}
      />
      {action === "stop" ? (
        <button
          type="button"
          className="composer-stop"
          disabled={stopping}
          onClick={onStop}
          aria-label={t({ zh: "停止当前轮", en: "Stop the current turn" })}
          title={t({ zh: "停止当前轮：立即切断当前生成（约 1–2 秒内停）", en: "Stop this turn: cuts off the current generation, usually within 1–2 seconds" })}
        >
          {stopping ? <Loader2 className="spin" size={16} /> : <Square size={14} />}
        </button>
      ) : (
        <button
          type="button"
          disabled={sendDisabled}
          onClick={onSend}
          aria-label={t({ zh: "发送", en: "Send" })}
          title={canInterject && sending ? t({ zh: "它正在跑 —— 这句话会投给正在干活的调度器", en: "It is running — this message goes to the orchestrator that is working right now" }) : t({ zh: "发送", en: "Send" })}
        >
          {sending && !canInterject ? <Loader2 className="spin" size={18} /> : <Send size={18} />}
        </button>
      )}
    </div>
  );
}
