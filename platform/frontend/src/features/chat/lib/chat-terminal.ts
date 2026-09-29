import { adaptExecutionView } from "../../sessions/lib/session-adapter.ts";
import type { SessionExecutionView } from "@/features/sessions/types";

export type ChatPauseOption = {
  /** 渲染用的 key。可能是前端编造的（`option-2` / `runId:1`），**不许回传**。 */
  id: string;
  /**
   * 可回传的身份 —— 只有**呈递方真的给了 id** 才有值。
   *
   * 2026-08-19 实测：编造 id 被当成身份回传，`{"choice_id":
   * "run_3517…:1"}` 去撞合法集 `[retry_reviewer, …]`，`choice_not_offered`
   * 三连拒，人点三次零反馈。编造的 id 能当 React key，不能当授权。
   * 没有 choiceId 时答复走文案精确匹配（value）。
   */
  choiceId?: string;
  label: string;
  value: string;
  description?: string;
  recommended?: boolean;
};

export type ChatPause = {
  question: string;
  context?: string;
  askingNodeType?: string;
  askingRunId?: string;
  kind: "human_input" | "permission" | "decision";
  /** ≤12 字的短标签，用于扫读（如「采样方案」「预算」）。 */
  header?: string;
  /**
   * 这一次呈递的身份。答复要带着它回去 —— 服务端据此确认"你回答的是当前
   * 这次呈递"，而不是上一次（选项集此后可能已经变了）。
   */
  offerId?: string;
  /**
   * 呈递方附带的事实 —— 回答「为什么问你」（reviewFailed / reviewRetryCapped /
   * producingRunId / …）。
   *
   * 故意是不透明字典：这里不枚举字段名。上游给 `Offer.facts` 加什么，面板就该
   * 能拿到什么 —— 枚举一次就多一处会静默漏掉的地方，那正是这整套东西的由来。
   */
  facts?: Record<string, unknown>;
  options: ChatPauseOption[];
};

/**
 * 这次流式连接的终帧。
 *
 * ## 它为什么不再自己带 pause / status / resumable（2026-09-01）
 *
 * 终帧曾经发三个平铺字段，而 REST payload 发 `executionView`：同一件事两种
 * 形状。前端于是长出两套解析，再加一句 `livePause = canonicalPause ?? terminal.pause`
 * 决定谁赢 —— 三份各自演化，分叉时没有一层报错。
 *
 * 现在两条路送**同一个对象**（后端 `session_execution_view` 一处生产）。
 * `view` 缺席 = 这一帧答不出局面（取消、记账失败），去重新查一次会话 ——
 * 而不是拿一份编出来的局面往下走。
 */
export type ChatTerminalState = {
  view: SessionExecutionView | null;
  runId: string | null;
  sessionId: string | null;
};

export const EMPTY_CHAT_TERMINAL: ChatTerminalState = {
  view: null,
  runId: null,
  sessionId: null,
};

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function stringValue(...values: unknown[]) {
  return values.find((value) => typeof value === "string" && value.trim()) as string | undefined;
}

function normalizeOption(value: unknown, index: number): ChatPauseOption | null {
  if (typeof value === "string" && value.trim()) {
    // 形状保持一致：recommended 显式给 false，别让消费方分辨 undefined 与 false。
    // 纯字符串选项没有身份 —— 不设 choiceId，答复走文案。
    return { id: value, label: value, value, recommended: false };
  }
  const option = asRecord(value);
  if (!option) return null;
  const label = stringValue(option.label, option.text, option.title, option.value, option.id);
  if (!label) return null;
  const givenId = stringValue(option.id);
  return {
    id: givenId ?? `option-${index + 1}`,
    // 身份只来自呈递方给的 id；`option-N` 兜底是渲染 key，不是身份。
    choiceId: givenId,
    label,
    value: stringValue(option.value, option.id, option.label, option.text) ?? label,
    description: stringValue(option.description, option.detail),
    recommended: option.recommended === true || option.is_recommended === true,
  };
}

function pauseKind(pause: Record<string, unknown> | null, metadata: Record<string, unknown> | null): ChatPause["kind"] {
  const value = stringValue(pause?.pause_kind, pause?.type, pause?.kind, metadata?.type, metadata?.kind);
  if (value === "highrisk_confirm" || value === "permission") return "permission";
  if (value === "decision_package" || value === "decision") return "decision";
  return "human_input";
}

export function normalizeChatPause(value: unknown, fallbackQuestion?: unknown): ChatPause | null {
  const outer = asRecord(value);
  // 这一次呈递是权威那份：带 choice id、offer_id、以及呈递方附带的判断依据。
  // 平台各层只搬运它、不改写它，所以这里拿到的与 harness 一字不差。
  // 缺席 = 老 pause / 自由文本问答，退回外层那些兼容字段。
  const offer = asRecord(outer?.offer);
  const pauseRecord = offer ? { ...outer, ...offer } : outer;
  const metadata = asRecord(pauseRecord?.metadata);
  // 优先用结构化选项（带 description 和 recommended）——人要靠 description 判断
  // 后果。裸 options 只是几个词，等于没给判据。
  // 后端字段名两种都收：optionDetails（新投影）/ option_details（harness 原样）。
  const rawOptions =
    pauseRecord?.optionDetails ?? pauseRecord?.option_details ?? pauseRecord?.options;
  const recommendedIndex =
    typeof pauseRecord?.recommendedOptionIndex === "number"
      ? pauseRecord.recommendedOptionIndex
      : typeof metadata?.recommended_option_index === "number"
        ? metadata.recommended_option_index as number
        : null;
  const options = Array.isArray(rawOptions)
    ? rawOptions
        .map(normalizeOption)
        .filter((option): option is ChatPauseOption => !!option)
        .map((option, index) =>
          recommendedIndex === index ? { ...option, recommended: true } : option)
    : [];
  const question = stringValue(pauseRecord?.question, pauseRecord?.prompt, fallbackQuestion);
  if (!question) return null;
  return {
    header: stringValue(pauseRecord?.header, metadata?.header),
    question,
    context: stringValue(pauseRecord?.context, pauseRecord?.situation),
    askingNodeType: stringValue(
      pauseRecord?.asking_node_type,
      pauseRecord?.askingNodeType,
      pauseRecord?.node_type,
    ),
    askingRunId: stringValue(pauseRecord?.asking_run_id, pauseRecord?.askingRunId),
    kind: pauseKind(pauseRecord, metadata),
    offerId: stringValue(pauseRecord?.offerId, pauseRecord?.offer_id),
    facts: asRecord(pauseRecord?.facts) ?? undefined,
    options,
  };
}

export function normalizeChatTerminal(meta: Record<string, unknown>): ChatTerminalState {
  return {
    // 服务端没说局面，就**不替它说**。这里原来在 `meta.status` 缺席时捏一个
    // "completed" 出来 —— 一条被打断的流（连接断了、worker 没了）于是在界面上
    // 变成「已完成」。缺席只有一个诚实的答案：null。
    view: meta.view ? adaptExecutionView(meta.view) : null,
    runId: stringValue(meta.run_id, meta.runId) ?? null,
    sessionId: stringValue(meta.session_id, meta.sessionId) ?? null,
  };
}
