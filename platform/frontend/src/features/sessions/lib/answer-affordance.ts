import type { ChatPause } from "@/features/chat";
import type { ResearchSession } from "../types";
import { toChatPause } from "./session-presentation.ts";

/**
 * 「这个会话此刻把输入交给谁」—— **一个纯函数**，组件照着渲染，不再自己判断。
 *
 * ## 为什么这件事必须是纯函数（2026-09-01 的第二半）
 *
 * 那次锁死的最后一跳发生在 JSX 里：
 *
 *     <ChatRunActivity status={session.execution.phase} … />   ← 传的是 "alive"
 *     const waiting = status === "waiting_human" || status === "waiting_permission";
 *     if (!running && !attention && !failed) return null;      ← 整张卡没了
 *
 * 同一个组件在**另一个**调用点把 waitingOn 翻译对了。两个调用点、两份翻译，
 * 而 prop 类型是 `string | null` —— `"alive"` 落进去编译器一声不吭。
 *
 * 这个仓库的前端测试是 `node --test` 跑 `*.test.ts`，**碰不到 .tsx**。也就是说
 * 判断只要待在 JSX 里，就没有任何东西测得到它。所以修法不是"把那一行改对"，
 * 是**把判断搬出 JSX**：组件退化成一个 `switch (plan.kind)`，没有自己的条件
 * 可以写错；判断落在这里，能用现场那份真 payload 直接回放。
 *
 * 不可测的地方不许有判断。
 */
/** 认领锚点只需要这两样。会话消息与聊天消息都满足它，不必二选一。 */
type OfferBearingMessage = { id: string; offerId?: string | null };

export type SessionInputPlan =
  /** 卡片是入口。`pause` 已经是能直接渲染的形状，组件不再做第二次转换。 */
  | { kind: "prompt"; pause: ChatPause; anchorMessageId: string | null }
  /** 输入框是入口。`degraded` 有值时说明本该是卡片，如实告诉用户。 */
  | { kind: "composer"; degraded?: string }
  /** 谁都不能动。`recheckAt` 是这个判断自己会过期的时刻。 */
  | { kind: "locked"; reason: string; recheckAt: string | null };

export function sessionInputPlan(input: {
  session: ResearchSession;
  messages: readonly OfferBearingMessage[];
  /** 这个标签页此刻正在发送 —— 后端不知道的唯一一件事。 */
  sending: boolean;
}): SessionInputPlan {
  const answer = input.session.execution.answer;
  if (input.sending) {
    // 在飞的那一刻手里这份 view 已经是旧的：拿它渲染一张"待答卡片"，人可能
    // 会去回答一个刚刚已经被答掉的问题。等这一轮的终帧带回新的局面。
    return { kind: "composer" };
  }
  if (answer.via === "none") {
    return { kind: "locked", reason: answer.reason, recheckAt: answer.until ?? null };
  }
  if (answer.via === "pause") {
    const pause = toChatPause(answer.pause);
    // 类型上 via==="pause" 就该有卡；真转不出来（问句都没有）时**不许**留在
    // prompt 分支 —— 那就是"说了走卡片却没有卡片"。降级成输入框，别锁人。
    if (!pause) return { kind: "composer", degraded: "pause_body_unavailable" };
    return { kind: "prompt", pause, anchorMessageId: anchorFor(pause, input.messages) };
  }
  return { kind: "composer", degraded: answer.degraded };
}

/**
 * 待答卡片画在**它自己那条消息**的位置上。
 *
 * 按呈递 id 认领（消息带着后端 `session_messages.offer_id`），不拿文案当身份
 * —— 拿文案当身份正是决策卡无限重现那次事故的引擎。
 *
 * 认不领时返回 null：卡片改画在列表底部。那不是"第二条路径"，是"没有锚点"
 * 这个明确情形的出口，判据机械可判。
 *
 * ⚠️ 2026-09-01：底部那条出口此前**是坏的**（它是传错 status 的那个调用点），
 * 而只有"呈递没有身份"时才会走到它 —— 于是缺陷藏了起来：带 offer_id 的
 * 决策卡一切正常，不带的（`request_human_input`、老会话）整张卡消失。
 * 现在两个位置渲染的是同一个 plan，不存在"另一条分支"。
 */
function anchorFor(
  pause: ChatPause,
  messages: readonly OfferBearingMessage[],
): string | null {
  const offerId = pause.offerId?.trim();
  if (!offerId) return null;
  return messages.findLast((message) => message.offerId === offerId)?.id ?? null;
}
