import type { Language } from "../../../shared/i18n/language.ts";
import type { ExecutionEvent } from "./execution-event.ts";
import { readPayload, type EventPayloadView } from "./payload-view.ts";
import {
  isVisibleResearchTool,
  researchToolFailurePresentation,
  researchToolTitle,
} from "./tool-presentation.ts";

export type Density = "summary" | "standard" | "trace";
export type FoldState =
  | "collapsed_auto"
  | "completed_waiting_next"
  | "active_open"
  | "manual_open"
  | "manual_closed"
  | "preference_open"
  | "forced_open";

export type ToolProjection = {
  id: string;
  title: string;
  summary?: string;
  technicalName?: string;
  status: "running" | "completed" | "retrying" | "error";
  fold: FoldState;
  startedAt: number;
  durationMs?: number;
  progress?: EventPayloadView["progress"];
  detail?: string;
  outputLines: string[];
  error?: EventPayloadView["error"];
  events: ExecutionEvent[];
};

export type StepProjection = {
  id: string;
  title: string;
  summary?: string;
  status: "running" | "completed" | "error";
  fold: FoldState;
  startedAt: number;
  durationMs?: number;
  tools: ToolProjection[];
  hasDecision: boolean;
  events: ExecutionEvent[];
  association: "canonical_step_id" | "frontend_unassociated_fallback";
};

export type DecisionProjection = {
  event: ExecutionEvent;
  payload: EventPayloadView;
};

export type ResultProjection = {
  event: ExecutionEvent;
  payload: EventPayloadView;
};

export type SessionProjection = {
  messages: Array<{ event: ExecutionEvent; payload: EventPayloadView }>;
  steps: StepProjection[];
  decisions: DecisionProjection[];
  results: ResultProjection[];
  finalSummary?: string;
  completed: boolean;
  trace: ExecutionEvent[];
};

type ProjectionOptions = {
  manualOpenIds?: ReadonlySet<string>;
  manualClosedIds?: ReadonlySet<string>;
  autoCollapseCompletedTools?: boolean;
  autoCollapseCompletedSteps?: boolean;
  /** 界面语言。纯函数收参数，不去够 hook —— 见 shared/i18n/useT.ts。 */
  lang?: Language;
};

function stepAssociation(event: ExecutionEvent, payload: EventPayloadView) {
  if (payload.stepId) {
    return { id: payload.stepId, source: "canonical_step_id" as const };
  }

  // Temporary compatibility fallback, deliberately not inferred from runId.
  // Unassociated Tool events remain in an explicit synthetic bucket until the
  // backend payload convention guarantees stepId/association keys.
  return {
    id: payload.toolCallId
      ? `frontend-unassociated-tool:${payload.toolCallId}`
      : `frontend-unassociated-event:${event.id}`,
    source: "frontend_unassociated_fallback" as const,
  };
}

function toolIdFor(event: ExecutionEvent, payload: EventPayloadView) {
  return payload.toolCallId ?? `tool:${event.id}`;
}

function latestPayload(events: ExecutionEvent[]) {
  return readPayload(events[events.length - 1]);
}

function projectTool(
  id: string,
  events: ExecutionEvent[],
  stepCompleted: boolean,
  hasNextTool: boolean,
  manualOpen: boolean,
  manualClosed: boolean,
  autoCollapseCompleted: boolean,
  lang: Language,
): ToolProjection {
  const started = events.find((event) => event.kind === "tool.started") ?? events[0];
  const startedPayload = readPayload(started);
  const latest = events[events.length - 1];
  const latestView = latestPayload(events);
  const failedEvent = [...events].reverse().find((event) => event.kind === "tool.failed");
  const failedView = failedEvent ? readPayload(failedEvent) : undefined;
  const hasError = Boolean(failedEvent);

  let status: ToolProjection["status"] = "running";
  if (latest.kind === "tool.completed") status = "completed";
  if (latest.kind === "tool.retrying") status = "retrying";
  if (latest.kind === "tool.failed") status = "error";

  let fold: FoldState;
  if (hasError) fold = "forced_open";
  else if (manualClosed) fold = "manual_closed";
  else if (manualOpen) fold = "manual_open";
  else if (status === "running" || status === "retrying") fold = "active_open";
  else if (!stepCompleted && !hasNextTool) fold = "completed_waiting_next";
  else if (!autoCollapseCompleted) fold = "preference_open";
  else fold = "collapsed_auto";

  const failure = failedView
    ? researchToolFailurePresentation(
        startedPayload.technicalName ?? latestView.technicalName,
        failedView.error,
        lang,
      )
    : undefined;

  return {
    id,
    title: researchToolTitle(
      startedPayload.technicalName ?? latestView.technicalName,
      startedPayload.toolArguments,
      startedPayload.title ?? latestView.title,
      lang,
    ),
    summary: latestView.summary ?? startedPayload.summary,
    technicalName: startedPayload.technicalName ?? latestView.technicalName,
    status,
    fold,
    startedAt: started.sequence,
    durationMs: latestView.durationMs,
    progress: latestView.progress ?? startedPayload.progress,
    detail: latestView.detail ?? startedPayload.detail,
    outputLines: latestView.outputLines.length
      ? latestView.outputLines
      : startedPayload.outputLines,
    error: failure ? {
      message: failure.message,
      impact: failure.title,
      recovery: failure.recovery,
    } : undefined,
    events,
  };
}

export function projectExecution(
  events: readonly ExecutionEvent[],
  options: ProjectionOptions = {},
): SessionProjection {
  const ordered = [...events].sort((left, right) => left.sequence - right.sequence);
  const manualOpenIds = options.manualOpenIds ?? new Set<string>();
  const manualClosedIds = options.manualClosedIds ?? new Set<string>();
  const autoCollapseCompletedTools = options.autoCollapseCompletedTools ?? true;
  const autoCollapseCompletedSteps = options.autoCollapseCompletedSteps ?? true;
  const lang = options.lang ?? "zh";
  const completed = ordered.some((event) => event.kind === "session.completed");
  const stepEvents = new Map<string, ExecutionEvent[]>();
  const toolEvents = new Map<string, ExecutionEvent[]>();
  const toolStepIds = new Map<string, string>();
  const stepAssociationSources = new Map<string, StepProjection["association"]>();
  const messages: SessionProjection["messages"] = [];
  const pendingDecisions = new Map<string, DecisionProjection>();
  const results: ResultProjection[] = [];
  let finalSummary: string | undefined;

  for (const event of ordered) {
    const payload = readPayload(event);
    if (event.kind === "session.message") {
      messages.push({ event, payload });
      if (payload.role === "assistant") finalSummary = payload.text ?? finalSummary;
    }
    // agent 每轮写的说明和用户消息同属"对话"，走同一条流按时间穿插显示 ——
    // 它和工具调用交织，才看得出"为什么下一步换了策略"。
    if (event.kind === "agent.message") {
      messages.push({ event, payload: { ...payload, role: "agent" } });
    }
    if (event.kind === "decision.required" || event.kind === "permission.required") {
      pendingDecisions.set(payload.correlationId ?? event.id, { event, payload });
    }
    if (event.kind === "decision.resolved" || event.kind === "permission.resolved") {
      if (payload.correlationId) pendingDecisions.delete(payload.correlationId);
    }
    if (
      event.kind === "artifact.created" ||
      event.kind === "artifact.versioned" ||
      event.kind === "artifact.frozen"
    ) {
      results.push({ event, payload });
    }

    if (event.kind.startsWith("step.")) {
      const association = stepAssociation(event, payload);
      stepEvents.set(association.id, [...(stepEvents.get(association.id) ?? []), event]);
      stepAssociationSources.set(association.id, association.source);
    }
    if (event.kind.startsWith("tool.")) {
      if (payload.technicalName && !isVisibleResearchTool(payload.technicalName)) continue;
      const toolId = toolIdFor(event, payload);
      const association = stepAssociation(event, payload);
      toolEvents.set(toolId, [...(toolEvents.get(toolId) ?? []), event]);
      toolStepIds.set(toolId, association.id);
      stepAssociationSources.set(association.id, association.source);
    }
  }

  const decisions = [...pendingDecisions.values()];

  const stepIdsFromTools = [...new Set(toolStepIds.values())];
  for (const stepId of stepIdsFromTools) {
    if (!stepEvents.has(stepId)) stepEvents.set(stepId, []);
  }

  const steps = [...stepEvents.entries()]
    .map(([id, ownEvents]) => {
      const matchingTools = [...toolEvents.entries()]
        .filter(([toolId]) => toolStepIds.get(toolId) === id)
        .sort(([, left], [, right]) => left[0].sequence - right[0].sequence);
      const firstToolEvent = matchingTools[0]?.[1][0];
      const started = ownEvents.find((event) => event.kind === "step.started")
        ?? ownEvents[0]
        ?? firstToolEvent;
      if (!started) return undefined;

      const end = [...ownEvents].reverse().find((event) =>
        event.kind === "step.completed" || event.kind === "step.failed",
      );
      const status: StepProjection["status"] = end?.kind === "step.failed"
        ? "error"
        : end?.kind === "step.completed"
          ? "completed"
          : "running";
      const tools = matchingTools.map(([toolId, eventsForTool], index) =>
        projectTool(
          toolId,
          eventsForTool,
          status !== "running",
          index < matchingTools.length - 1,
          manualOpenIds.has(toolId),
          manualClosedIds.has(toolId),
          autoCollapseCompletedTools,
          lang,
        ));
      const eventSequences = ownEvents.map((event) => event.sequence);
      const startedAt = Math.min(started.sequence, ...eventSequences);
      const hasDecision = decisions.some(({ payload }) => payload.stepId === id);
      const hasError = status === "error" || tools.some((tool) => tool.status === "error");
      const startView = readPayload(started);
      const endView = end ? readPayload(end) : undefined;
      const isProducingChild = started.source.rawEvent === "subagent_call_start";

      return {
        id,
        title: isProducingChild
          ? startView.title ?? "Research step"
          : tools.length > 0
            ? "Research tools"
            : status === "error"
              ? "Research step failed"
              : "Research step",
        summary: endView?.summary ?? startView.summary,
        status,
        fold: "active_open" as FoldState,
        startedAt,
        durationMs: endView?.durationMs,
        tools,
        hasDecision,
        events: ownEvents,
        association: stepAssociationSources.get(id) ?? "frontend_unassociated_fallback",
        hasError,
        isProducingChild,
      };
    })
    .filter((step): step is NonNullable<typeof step> => Boolean(step))
    .filter((step) => step.tools.length > 0 || step.hasError || step.hasDecision || step.isProducingChild)
    .sort((left, right) => left.startedAt - right.startedAt)
    .map((step, index, allSteps) => {
      const hasNextStep = index < allSteps.length - 1;
      let fold: FoldState;
      if (step.hasError || step.hasDecision) fold = "forced_open";
      else if (manualClosedIds.has(step.id)) fold = "manual_closed";
      else if (manualOpenIds.has(step.id)) fold = "manual_open";
      else if (step.status === "running") fold = "active_open";
      else if (!completed && !hasNextStep) fold = "completed_waiting_next";
      else if (!autoCollapseCompletedSteps) fold = "preference_open";
      else fold = "collapsed_auto";

      const { hasError: _hasError, isProducingChild: _isProducingChild, ...projection } = step;
      void _hasError;
      void _isProducingChild;
      return { ...projection, fold };
    });

  return {
    messages,
    steps,
    decisions,
    results,
    finalSummary,
    completed,
    trace: ordered,
  };
}
