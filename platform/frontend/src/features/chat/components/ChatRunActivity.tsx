"use client";

import { AlertCircle, Loader2 } from "lucide-react";
import type { ChatActivity } from "../types";
import type { SessionExecutionView } from "@/features/sessions/types";
import { projectLiveActivity } from "../lib/live-activity";
import { useLanguage } from "@/shared/i18n";

/**
 * 一行「它此刻在干什么」。
 *
 * ## 这里曾经有一个 `status?: string | null`（2026-09-01 删除）
 *
 * 它的判据是一份手写名单：
 *
 *     const waiting = status === "waiting_human" || status === "waiting_permission";
 *     if (!running && !attention && !failed) return null;
 *
 * 而 2026-08-27 把真相源换成了后端现算的 view 之后，调用点开始各自把 view
 * **翻译**成状态串 —— 一个翻译对了（`waitingOn.kind` → "waiting_human"），
 * 另一个直接把 `phase` 传了进来（值是 `"alive"`）。`string` 是开放类型，
 * 编译器一声不吭；名单不匹配，`return null`，整张待答卡片消失 6 小时。
 *
 * 现在这个组件**吃 view 本身**：没有翻译，就没有两份翻译。而"要不要画那张
 * 卡"已经不归它管了 —— 见 `sessions/lib/answer-affordance.ts`。
 */
export function ChatRunActivity({
  activities,
  view,
  running,
}: {
  activities: ChatActivity[];
  view: SessionExecutionView;
  /** 这个标签页此刻正在发送 —— 后端不知道的唯一一件事。 */
  running: boolean;
}) {
  const lang = useLanguage();
  const projection = projectLiveActivity(activities, { running, view, lang });
  return (
    <div className="chat-transient-activity">
      <div
        className={`chat-transcript-status state-${projection.tone}`}
        role={projection.tone === "failed" ? "alert" : "status"}
        aria-live="polite"
      >
        {running && projection.tone === "working"
          ? <Loader2 className="spin" size={11} />
          : <AlertCircle size={11} />}
        <span>{projection.statusText}</span>
      </div>
    </div>
  );
}
