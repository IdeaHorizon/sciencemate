import type { ExecutionEvent } from "./execution-event.ts";

export type DecisionOption = {
  id: string;
  label: string;
  consequence?: string;
  recommended?: boolean;
};

export type ProgressView =
  | { mode: "elapsed_only"; elapsedMs: number }
  | {
      mode: "derived" | "reported";
      completed: number;
      total: number;
      unit: string;
      exact: boolean;
    };

export type UsageView = {
  promptTokens?: number;
  completionTokens?: number;
  totalTokens?: number;
  cost?: number | null;
  currency?: string;
  coverage?: "complete" | "partial";
};

export type EventPayloadView = {
  title?: string;
  summary?: string;
  detail?: string;
  // "agent" = 节点每轮写的说明（2026-08-11）。和 "assistant" 分开：
  // assistant 是回给用户的最终答复，agent 是过程中的思路
  //（"前两轮查得太宽泛，换精准词"）——混成一个，收尾摘要就会被过程碎片污染。
  role?: "user" | "assistant" | "system" | "agent";
  /** 第几轮（agent.message 才有）。0/缺失 = 不显示轮次。 */
  turn?: number;
  /** 正文来自旧 transcript 的截断预览 —— 完不完整未知，UI 要如实标注。 */
  previewOnly?: boolean;
  /** 这条事件属于哪个节点（`run.started` 带）。子节点分组的标题取自它。 */
  nodeType?: string;
  text?: string;
  stepId?: string;
  toolCallId?: string;
  actionGroupId?: string;
  technicalName?: string;
  toolArguments?: Record<string, unknown>;
  /**
   * 工具 envelope 自报的结局（`tool.completed` 带，#947）：`success` / `pause` /
   * `started_background` …。权限 pause 和批准后的真实执行都是 `tool.completed`，
   * 分开它俩只能读这个字段 —— `summary` 可能是截断的文本，别在里面猜。
   * 缺失 = 判不出，不等于成功。
   */
  resultStatus?: string;
  resultCount?: number;
  durationMs?: number;
  progress?: ProgressView;
  outputLines: string[];
  correlationId?: string;
  artifact?: {
    id?: string;
    name: string;
    mediaType?: string;
    version?: number;
  };
  decision?: {
    id?: string;
    subtype?: string;
    prompt?: string;
    reason?: string;
    recommendation?: string;
    options: DecisionOption[];
  };
  error?: {
    code?: string;
    message: string;
    retryable?: boolean;
    recovery?: string;
  };
  usage?: UsageView;
};

export type SessionResourceSummary = {
  totalTokens: number | null;
  cost: number | null;
  currency?: string;
  coverage?: "complete" | "partial";
  observedRetries: number;
};

function record(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function string(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function number(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function nullableNumber(value: unknown): number | null | undefined {
  if (value === null) return null;
  return number(value);
}

function boolean(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

function hasOwn(value: Record<string, unknown>, key: string) {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function coverage(value: unknown): UsageView["coverage"] {
  return value === "complete" || value === "partial" ? value : undefined;
}

function humanizeToolName(value: string | undefined) {
  if (!value) return undefined;
  const normalized = value.replace(/[_-]+/g, " ").trim();
  return normalized ? normalized[0].toUpperCase() + normalized.slice(1) : undefined;
}

function parseProgress(value: unknown): ProgressView | undefined {
  const item = record(value);
  const mode = string(item?.mode);
  if (mode === "elapsed_only") {
    return { mode, elapsedMs: number(item?.elapsedMs) ?? 0 };
  }
  if (mode === "derived" || mode === "reported") {
    const completed = number(item?.completed);
    const total = number(item?.total);
    const unit = string(item?.unit);
    if (completed === undefined || total === undefined || !unit || total <= 0) {
      return undefined;
    }
    return {
      mode,
      completed,
      total,
      unit,
      exact: boolean(item?.exact) ?? false,
    };
  }
  return undefined;
}

function parseCanonicalChoices(value: unknown, recommendedChoiceId?: string) {
  if (!Array.isArray(value)) return undefined;
  return value.flatMap((rawChoice): DecisionOption[] => {
    const choice = record(rawChoice);
    const id = string(choice?.choiceId);
    const label = string(choice?.label);
    if (!id || !label) return [];
    return [{ id, label, recommended: id === recommendedChoiceId }];
  });
}

function parseUsage(event: ExecutionEvent): UsageView | undefined {
  if (event.kind !== "usage.updated") return undefined;
  const promptTokens = number(event.payload.promptTokens);
  const completionTokens = number(event.payload.completionTokens);
  const totalTokens = number(event.payload.totalTokens) ?? (
    promptTokens !== undefined && completionTokens !== undefined
      ? promptTokens + completionTokens
      : undefined
  );
  return {
    promptTokens,
    completionTokens,
    totalTokens,
    cost: nullableNumber(event.payload.cost),
    currency: string(event.payload.currency),
    coverage: coverage(event.payload.coverage),
  };
}

/**
 * Typed view over the Phase-0 direct payload fields
 * (`platform/contracts/payload-conventions.phase0.json`). Only what the backend
 * writes is read; the UI-demo fixtures use the same fields.
 */
export function readPayload(event: ExecutionEvent): EventPayloadView {
  const payload = event.payload;
  const role = string(payload.role);
  const toolName = string(payload.toolName);
  const artifact = event.kind.startsWith("artifact.") && string(payload.name)
    ? {
        id: string(payload.artifactId),
        name: string(payload.name) as string,
        mediaType: string(payload.artifactType),
        version: number(payload.version),
      }
    : undefined;
  const recommendedChoiceId = string(payload.recommendedChoiceId);
  const choices = parseCanonicalChoices(payload.choices, recommendedChoiceId);
  const hasDecisionFields = event.kind === "decision.required" && [
    "decisionId",
    "subtype",
    "prompt",
    "choices",
    "recommendedChoiceId",
  ].some((key) => hasOwn(payload, key));
  const decision = hasDecisionFields
    ? {
        id: string(payload.decisionId),
        subtype: string(payload.subtype),
        prompt: string(payload.prompt),
        reason: string(payload.prompt),
        recommendation: choices?.find((choice) => choice.recommended)?.label,
        options: choices ?? [],
      }
    : undefined;
  const errorMessage = event.kind.endsWith(".failed")
    ? string(payload.errorMessage)
    : undefined;

  return {
    title: humanizeToolName(toolName) ?? string(payload.title),
    summary: string(payload.resultSummary) ?? string(payload.summary),
    detail: string(payload.detail),
    role:
      role === "user" || role === "assistant" || role === "system"
        ? role
        : undefined,
    text:
      (event.kind === "session.message" ? string(payload.content) : undefined) ??
      string(payload.text),
    stepId: string(payload.stepId),
    toolCallId: string(payload.toolCallId),
    actionGroupId: string(payload.actionGroupId),
    technicalName: toolName ?? string(payload.technicalName),
    toolArguments: event.kind === "tool.started" ? record(payload.arguments) : undefined,
    resultStatus: event.kind === "tool.completed" ? string(payload.resultStatus) : undefined,
    resultCount: number(payload.resultCount),
    durationMs: number(payload.durationMs),
    progress: parseProgress(payload.progress),
    outputLines: Array.isArray(payload.outputLines)
      ? payload.outputLines.filter((line): line is string => typeof line === "string")
      : [],
    correlationId:
      decision?.id ??
      string(payload.decisionId) ??
      string(payload.permissionId) ??
      undefined,
    artifact,
    decision,
    error: errorMessage
      ? {
          code: string(payload.errorCode),
          message: errorMessage,
          retryable: boolean(payload.retryable),
          // 工具失败走的就是这条扁平分支（`_tool_failure_fields`）。以前这里没有
          // recovery，于是工具写的"下一步"到不了人手上，界面只能用兜底文案。
          recovery: string(payload.errorRecovery),
        }
      : undefined,
    usage: parseUsage(event),
  };
}

export function summarizeSessionResources(
  events: readonly ExecutionEvent[],
): SessionResourceSummary {
  const usage = events
    .filter((event) => event.kind === "usage.updated")
    .map(readPayload)
    .flatMap((payload) => payload.usage ? [payload.usage] : []);

  const totalTokens = usage.length > 0 && usage.every((item) => item.totalTokens !== undefined)
    ? usage.reduce((sum, item) => sum + (item.totalTokens as number), 0)
    : null;

  const costsKnown = usage.length > 0 && usage.every((item) => typeof item.cost === "number");
  const currencies = [...new Set(usage.map((item) => item.currency).filter(Boolean))] as string[];
  const currencyCompatible = currencies.length <= 1;
  const cost = costsKnown && currencyCompatible
    ? usage.reduce((sum, item) => sum + (item.cost as number), 0)
    : null;
  const resourceCoverage = usage.length === 0
    ? undefined
    : usage.every((item) => item.coverage === "complete")
      ? "complete"
      : "partial";
  const observedRetries = events.filter((event) =>
    event.kind === "run.retrying" || event.kind === "tool.retrying"
  ).length;

  return {
    totalTokens,
    cost,
    currency: currencyCompatible ? currencies[0] : undefined,
    coverage: resourceCoverage,
    observedRetries,
  };
}
