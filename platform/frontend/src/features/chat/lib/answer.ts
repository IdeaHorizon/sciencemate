import type { ChatPauseOption } from "./chat-terminal.ts";
import type {
  ChatMessageIn,
  ChatRequest,
  ChoiceAnswer,
  TextAnswer,
} from "@/lib/generated/chat-request";

/**
 * 「这次提交是什么」—— **整个前端只在这个文件里回答一次。**
 *
 * ## 为什么必须只有一个构造函数（2026-09-03）
 *
 * 一次点击从卡片走到 HTTP 请求要经过五层交接。此前每一层都拿自己看得见的
 * 碎片重新判一遍"有没有东西可发"：卡片看 choice+附言、workspace 看
 * `(!text && !choice)`、hook 只看 `text.trim()`、后端 schema 只看 `message`
 * 的 `min_length`。8-31 让"选了选项时附言可空"通过了前两层，后两层原样 ——
 * 于是"只点选项不写附言"在 hook 里被一句 `return;` 静默吞掉，请求根本没出
 * 浏览器（cuib 09-03，会话 de4632cc）。同形事故此前已经发生过三次。
 *
 * 根因不是哪一道闸写错，是**值以裸字符串过界**：卡片明明知道这是合法答复，
 * 传下去的却是 `""`，下游只能再猜一次。所以修法不是把四道闸改成一致，是让
 * 下游**没有东西可猜**：
 *
 *   - `Answer` 是和类型。`choice` 分支附言天然可空，`text` 分支的正文是
 *     `NonEmptyText` —— 一个只有本文件能造出来的品牌类型。
 *   - 下游签名一律 `(answer: Answer)`。传字符串是编译错误；想再判一次"空不空"
 *     得先拆包，而品牌类型没有拆包的合法途径。
 *   - 线格式由 `buildChatRequest` 一处生成，目标类型是**从后端模型生成**的
 *     `@/lib/generated/chat-request`，不是手抄。
 *
 * 返回 `null` 就是"没有可发的东西"，按钮据此灰掉。它是构造失败，不是一种
 * 需要下游处理的答复 —— 所以它不会走到 send。
 */

declare const nonEmpty: unique symbol;

/** 只能由本文件造出来的非空、已去首尾空白的文本。 */
export type NonEmptyText = string & { readonly [nonEmpty]: true };

export type Answer =
  /** 点了呈递里带身份的某一项；附言可空。 */
  | {
      readonly kind: "choice";
      readonly offerId: string | null;
      readonly choiceId: string;
      /** 人看到的那行字。只用于本地气泡，不上线（线上的身份是 choiceId）。 */
      readonly label: string;
      readonly note: string;
    }
  /** 说了一句话：开新一轮、插话、或回答自由文本问题。 */
  | { readonly kind: "text"; readonly text: NonEmptyText };

function nonEmptyText(raw: string): NonEmptyText | null {
  const trimmed = raw.trim();
  return trimmed ? (trimmed as NonEmptyText) : null;
}

/** 输入框那条路：一句话。空白 → null（发送键灰）。 */
export function composeText(draft: string): Answer | null {
  const text = nonEmptyText(draft);
  return text ? { kind: "text", text } : null;
}

/**
 * 待答卡片那条路。
 *
 * - 选项**带真身份**（`choiceId`）→ `choice`，附言原样带上，可空。
 * - 选项没有身份（老式纯文本选项）→ 文案就是答复本体，服务端按文案精确匹配。
 * - 没选选项 → 自由文本答复。
 */
export function composeAnswer(input: {
  selected: ChatPauseOption | null | undefined;
  note: string;
  offerId: string | null | undefined;
}): Answer | null {
  const { selected } = input;
  if (selected?.choiceId) {
    return {
      kind: "choice",
      offerId: input.offerId ?? null,
      choiceId: selected.choiceId,
      label: selected.label,
      note: input.note.trim(),
    };
  }
  if (selected) return composeText(selected.value);
  return composeText(input.note);
}

function unreachable(value: never): never {
  throw new Error(`unreachable answer kind: ${JSON.stringify(value)}`);
}

/** `Answer` → 线格式。目标类型是生成的，字段名跟着后端走。 */
export function toWireAnswer(answer: Answer): ChoiceAnswer | TextAnswer {
  switch (answer.kind) {
    case "choice":
      return {
        kind: "choice",
        offer_id: answer.offerId,
        choice_id: answer.choiceId,
        note: answer.note,
      };
    case "text":
      return { kind: "text", text: answer.text };
    default:
      return unreachable(answer);
  }
}

/** 整个前端唯一一处组装 `ChatRequest` 的地方。 */
export function buildChatRequest(
  answer: Answer,
  context: { history: ChatMessageIn[]; conversationId: string | null },
): ChatRequest {
  return {
    answer: toWireAnswer(answer),
    history: context.history,
    conversation_id: context.conversationId,
  };
}

/** 对话里给这次提交画的那条气泡：选项答复显示身份（附言另起一行）。 */
export function answerBubbleText(answer: Answer): string {
  switch (answer.kind) {
    case "choice":
      return answer.note ? `${answer.label}\n${answer.note}` : answer.label;
    case "text":
      return answer.text;
    default:
      return unreachable(answer);
  }
}
