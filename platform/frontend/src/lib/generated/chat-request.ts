// GENERATED FILE — do not edit by hand.
//
// Source of truth: platform/backend/app/api/v1/chat.py (Pydantic models).
// Regenerate:      cd platform/backend && uv run python ../contracts/generate_wire_types.py
// Enforced by:     platform/backend/tests/test_wire_types_are_generated.py
//
// 这份文件是后端线契约的投影。前端不许再手写第二份 —— 两份手写定义正是
// 「什么算一次合法提交」在四层各自演化的土壤（2026-09-03）。

export type Answer = ChoiceAnswer | TextAnswer;

export interface ChatMessageIn {
  role?: "user" | "assistant";
  content: string;
}

/**
 * 人点了呈递里的**某一项** —— 身份是 `{offer_id, choice_id}`，附言可空。
 *
 * 带上身份，harness 侧判定答复是一次集合成员检查；文案回传只是兼容层：
 * 2026-08-19 实测，界面印的选项与框架的合法集分叉时，文案解析会**静默丢弃**
 * 人的授权（点三次、三次都消失）。
 */
export interface ChoiceAnswer {
  kind: "choice";
  offer_id?: string | null;
  choice_id: string;
  note?: string;
}

/**
 * 人说了一句话 —— 开新一轮、插话、或回答一个自由文本问题。
 */
export interface TextAnswer {
  kind: "text";
  text: string;
}

export interface ChatRequest {
  answer: Answer;
  history?: Array<ChatMessageIn>;
  conversation_id?: string | null;
}
