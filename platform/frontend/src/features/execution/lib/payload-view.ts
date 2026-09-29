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
  retries?: number;
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
  resultCount?: number;
  durationMs?: number;
  progress?: ProgressView;
  outputLines: string[];
  correlationId?: string;
  artifact?: {
    id?: string;
    name: string;
    mediaType?: string;
    preview?: string;
    version?: number;
  };
  decision?: {
    id?: string;
    subtype?: string;
    prompt?: string;
    reason?: string;
    recommendation?: string;
    evidence?: string;
    reversible?: boolean;
    options: DecisionOption[];
  };
  error?: {
    code?: string;
    message: string;
    retryable?: boolean;
    impact?: string;
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

function parseLegacyOptions(value: unknown): DecisionOption[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((rawOption) => {
    const option = record(rawOption);
    const id = string(option?.id);
    const label = string(option?.label);
    if (!id || !label) return [];
    return [{
      id,
      label,
      consequence: string(option?.consequence),
      recommended: boolean(option?.recommended),
    }];
  });
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

function parseUsage(event: ExecutionEvent, legacyUsage?: Record<string, unknown>): UsageView | undefined {
  if (event.kind === "usage.updated") {
    const promptTokens = number(event.payload.promptTokens);
    const completionTokens = number(event.payload.completionTokens);
    const directTotal = number(event.payload.totalTokens);
    const totalTokens = directTotal ?? (
      promptTokens !== undefined && completionTokens !== undefined
        ? promptTokens + completionTokens
        : undefined
    );
    const directCostPresent = hasOwn(event.payload, "cost");
    return {
      promptTokens,
      completionTokens,
      totalTokens: totalTokens ?? number(legacyUsage?.tokens),
      cost: directCostPresent
        ? nullableNumber(event.payload.cost)
        : nullableNumber(legacyUsage?.cost),
      currency: string(event.payload.currency) ?? string(legacyUsage?.currency),
      retries: number(legacyUsage?.retries),
      coverage: coverage(event.payload.coverage) ?? coverage(legacyUsage?.coverage),
    };
  }

  if (!legacyUsage) return undefined;
  return {
    totalTokens: number(legacyUsage.tokens),
    cost: nullableNumber(legacyUsage.cost),
    currency: string(legacyUsage.currency),
    retries: number(legacyUsage.retries),
    coverage: coverage(legacyUsage.coverage),
  };
}

/**
 * Compatibility view for canonical Phase-0 direct payload fields. Direct
 * fields always win. Nested fields remain only for the local UI-demo fixtures
 * and can be removed after those demos migrate to the backend contract.
 */
export function readPayload(event: ExecutionEvent): EventPayloadView {
  const payload = event.payload;
  const role = string(payload.role);
  const nestedArtifact = record(payload.artifact);
  const nestedDecision = record(payload.decision);
  const nestedError = record(payload.error);
  const nestedUsage = record(payload.usage);
  const toolName = string(payload.toolName);
  const directArtifact = event.kind.startsWith("artifact.") && string(payload.name)
    ? {
        id: string(payload.artifactId),
        name: string(payload.name) as string,
        mediaType: string(payload.artifactType),
        version: number(payload.version),
      }
    : undefined;
  const recommendedChoiceId = string(payload.recommendedChoiceId);
  const directChoices = parseCanonicalChoices(payload.choices, recommendedChoiceId);
  const hasDirectDecisionFields = event.kind === "decision.required" && [
    "decisionId",
    "subtype",
    "prompt",
    "choices",
    "recommendedChoiceId",
  ].some((key) => hasOwn(payload, key));
  const directDecision = hasDirectDecisionFields
    ? {
        id: string(payload.decisionId),
        subtype: string(payload.subtype),
        prompt: string(payload.prompt),
        reason: string(payload.prompt),
        recommendation: directChoices?.find((choice) => choice.recommended)?.label,
        options: directChoices ?? [],
      }
    : undefined;
  const nestedDecisionView = !hasDirectDecisionFields && nestedDecision
    ? {
        id: string(nestedDecision.id),
        reason: string(nestedDecision.reason),
        recommendation: string(nestedDecision.recommendation),
        evidence: string(nestedDecision.evidence),
        reversible: boolean(nestedDecision.reversible),
        options: parseLegacyOptions(nestedDecision.options),
      }
    : undefined;
  const decision = directDecision ?? nestedDecisionView;
  const directErrorMessage = event.kind.endsWith(".failed")
    ? string(payload.errorMessage)
    : undefined;

  return {
    title:
      humanizeToolName(toolName) ??
      (hasDirectDecisionFields ? undefined : string(payload.title)),
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
    artifact: directArtifact ?? (nestedArtifact && string(nestedArtifact.name)
      ? {
          id: string(nestedArtifact.id),
          name: string(nestedArtifact.name) as string,
          mediaType: string(nestedArtifact.mediaType),
          preview: string(nestedArtifact.preview),
          version: number(nestedArtifact.version),
        }
      : undefined),
    decision,
    error: directErrorMessage
      ? {
          code: string(payload.errorCode),
          message: directErrorMessage,
          retryable: boolean(payload.retryable),
          // 工具失败走的就是这条扁平分支（`_tool_failure_fields`）。以前这里没有
          // recovery，于是工具写的"下一步"到不了人手上，界面只能用兜底文案。
          recovery: string(payload.errorRecovery),
        }
      : nestedError && string(nestedError.message)
        ? {
            code: string(nestedError.code) ?? string(payload.errorCode),
            message: string(nestedError.message) as string,
            impact: string(nestedError.impact),
            recovery: string(nestedError.recovery),
          }
        : undefined,
    usage: parseUsage(event, nestedUsage),
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
  const observedRetryEvents = events.filter((event) =>
    event.kind === "run.retrying" || event.kind === "tool.retrying"
  ).length;
  const legacyRetryCount = usage.reduce(
    (maximum, item) => Math.max(maximum, item.retries ?? 0),
    0,
  );

  return {
    totalTokens,
    cost,
    currency: currencyCompatible ? currencies[0] : undefined,
    coverage: resourceCoverage,
    observedRetries: Math.max(observedRetryEvents, legacyRetryCount),
  };
}
