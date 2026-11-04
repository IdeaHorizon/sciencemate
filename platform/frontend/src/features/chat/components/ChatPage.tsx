"use client";

import { useEffect, useState } from "react";
import { ChatMessages } from "./ChatMessages";
import { ChatComposer } from "./ChatComposer";
import type { ChatMessage, ArtifactLinkContext } from "../types";
import type { ChatFailurePresentation } from "../lib/chat-error-presentation";
import { useT } from "@/shared/i18n";

function ChatFailure({ failure }: { failure: ChatFailurePresentation }) {
  return (
    <div className="chat-inline-error" role="alert">
      <strong>{failure.title}</strong>
      <span>{failure.message}</span>
      <small>{failure.recovery}</small>
    </div>
  );
}

/**
 * Full-height chat surface: messages + bottom-anchored composer. With no
 * messages yet, the greeting and a one-line description sit above the composer.
 */
export function ChatPage({
  messages,
  draft,
  setDraft,
  onSend,
  sending,
  artifacts,
  onArtifactClick,
  onConceptClick,
  greeting,
  emptyDescription,
  placeholder,
  children,
  error,
  composerAbove,
  composerContext,
  composerDisabled,
  composerDisabledReason,
  renderMessageAccessory,
  bodyOwnedByAccessory,
  onStop,
  stopping,
  canInterject,
  onLongPaste,
}: {
  messages: ChatMessage[];
  draft: string;
  setDraft: (v: string) => void;
  onSend: () => void;
  sending?: boolean;
  onConceptClick?: (conceptId: string) => void;
  greeting: string;
  emptyDescription?: string;
  placeholder: string;
  children?: React.ReactNode;
  error?: ChatFailurePresentation | null;
  /** 输入框**上方**的常驻条（交付物）。与 `composerContext`（输入框内的
   *  模式/模型那一排）分开：一个说"这次研究产出了什么"，一个说"这一条
   *  消息会怎么跑"。塞进同一个槽位会把两个问题画成一行。 */
  composerAbove?: React.ReactNode;
  composerContext?: React.ReactNode;
  composerDisabled?: boolean;
  composerDisabledReason?: string;
  renderMessageAccessory?: (message: ChatMessage) => React.ReactNode;
  /** 这条消息的正文由它的 accessory 负责画 —— 别再画第二遍。 */
  bodyOwnedByAccessory?: (message: ChatMessage) => boolean;
  onStop?: () => void;
  stopping?: boolean;
  canInterject?: boolean;
  onLongPaste?: (text: string, selection: { start: number; end: number }) => void;
} & ArtifactLinkContext) {
  const t = useT();
  // Avoid hydration mismatch when checking emptiness
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
  }, []);

  const realMessages = messages.filter((m) => m.id !== "welcome");
  const isEmpty = mounted && realMessages.length === 0;

  return (
    <div className="chat-area">
      <ChatMessages
        messages={realMessages}
        sending={sending}
        artifacts={artifacts}
        onArtifactClick={onArtifactClick}
        onConceptClick={onConceptClick}
        renderMessageAccessory={renderMessageAccessory}
        bodyOwnedByAccessory={bodyOwnedByAccessory}
      >
        {isEmpty && (
          <div className="chat-session-empty-copy">
            <h2>{greeting}</h2>
            <p>{emptyDescription ?? t({ zh: "发送第一条消息，开始这个会话。", en: "Send your first message to start this session." })}</p>
          </div>
        )}
        {children}
      </ChatMessages>
      <div className="composer-wrapper">
        {composerAbove}
        {error && <ChatFailure failure={error} />}
        <ChatComposer
          draft={draft}
          setDraft={setDraft}
          onSend={onSend}
          sending={sending}
          placeholder={placeholder}
          disabled={composerDisabled}
          context={composerContext}
          disabledReason={composerDisabledReason}
          onStop={onStop}
          stopping={stopping}
          canInterject={canInterject}
          onLongPaste={onLongPaste}
        />
      </div>
    </div>
  );
}
