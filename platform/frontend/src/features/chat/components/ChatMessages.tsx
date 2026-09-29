"use client";

import { useEffect, useRef } from "react";
import { AlertCircle } from "lucide-react";
import { cn } from "@/shared/ui";
import { useInterfaceSettings } from "@/features/settings/InterfaceSettingsProvider";
import { RichText } from "./RichText";
import type { ChatMessage, ArtifactLinkContext } from "../types";

export function ChatMessages({
  messages,
  sending,
  artifacts,
  onArtifactClick,
  onConceptClick,
  renderMessageAccessory,
  bodyOwnedByAccessory,
  children,
}: {
  messages: ChatMessage[];
  sending?: boolean;
  onConceptClick?: (conceptId: string) => void;
  renderMessageAccessory?: (message: ChatMessage) => React.ReactNode;
  /**
   * 这条消息的正文由它的 accessory 负责画。
   *
   * 唯一的用处是待答提问：run 停下来问人时，问句被原样写成一条 assistant
   * 消息，而能点的那张卡片画的是同一句话。从前用**文案一字不差**把消息整条
   * 藏掉（`pause-echo.ts`，已删）—— 拿文案当身份，正是决策卡无限重现那次
   * 事故的引擎。现在按呈递 id 判定，而且不是"藏掉"：卡片就长在这条消息的
   * 位置上，时间顺序因此天然是对的。
   */
  bodyOwnedByAccessory?: (message: ChatMessage) => boolean;
  children?: React.ReactNode;
} & ArtifactLinkContext) {
  const { settings: interfaceSettings } = useInterfaceSettings();
  const endRef = useRef<HTMLDivElement>(null);
  const prevCountRef = useRef(messages.length);
  const mountedRef = useRef(false);
  useEffect(() => {
    // 首次渲染也要定位到最新一条。原来 prevCountRef 初值就是 messages.length，
    // 于是 `!==` 在挂载时恒为假 —— 打开一个会话落在**记录最顶上**，长会话要自己
    // 一路往下滚才看得到刚才发生了什么。首次用 instant：进页面时来一段平滑滚动
    // 只是让人等，看不出信息。
    if (!mountedRef.current) {
      mountedRef.current = true;
      if (messages.length > 0) endRef.current?.scrollIntoView({ behavior: "instant" });
      prevCountRef.current = messages.length;
      return;
    }
    if (interfaceSettings.follow_active_run && messages.length !== prevCountRef.current) {
      endRef.current?.scrollIntoView({ behavior: "smooth" });
    }
    prevCountRef.current = messages.length;
  }, [interfaceSettings.follow_active_run, messages.length]);

  return (
    <div className="messages">
      {messages.map((m) => {
        const accessory = renderMessageAccessory?.(m);
        const bodyOwned = Boolean(accessory) && Boolean(bodyOwnedByAccessory?.(m));
        const systemRunOwnedByCanonicalActivity = m.role === "system" && Boolean(accessory);
        return (
          <div
            key={m.id}
            className={cn(
              "message",
              m.role,
              m.id.startsWith("__scheduler_") && "system-status",
            )}
          >
            {bodyOwned ? null : m.role === "system" && !systemRunOwnedByCanonicalActivity ? (
              <div className="message-system-alert" role="alert">
                <AlertCircle size={13} aria-hidden="true" />
                <p>{m.text}</p>
              </div>
            ) : m.role !== "system" ? (
              <>
                <span>{m.role === "assistant" ? "Agent" : "You"}</span>
                <div className="message-body">
                  {/* 空 assistant 消息**不再**画三个跳动的点。
                      状态行（chat-transcript-status：Thinking / Analyzing tool
                      results / Waiting for your input…）本来就在正下方，说的是
                      同一件事而且说得更具体 —— 两个都在动，读者要先分辨它们是
                      不是同一件事（wangd 2026-08-19：「有两个表示他正在思考的
                      内容…这两个可以删去一个」）。留信息多的那个。 */}
                  <RichText
                    text={m.text}
                    artifacts={artifacts}
                    onArtifactClick={onArtifactClick}
                    conceptAnnotations={m.conceptAnnotations}
                    onConceptClick={onConceptClick}
                  />
                </div>
              </>
            ) : null}
            {accessory && <div className="message-run-accessory">{accessory}</div>}
          </div>
        );
      })}
      {children}
      <div ref={endRef} />
    </div>
  );
}
