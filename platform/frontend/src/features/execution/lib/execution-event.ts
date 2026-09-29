export const EXECUTION_EVENT_KINDS = [
  "session.started",
  "session.message",
  // agent 每轮写的说明（2026-08-11）。用户原话：「每一步具体在干啥，
  // 我感觉看的一头雾水」——模型其实每轮都写了人话，只是没送到。
  "agent.message",
  // 调度器派发节点前**对用户说的话**（2026-08-17）。与 agent.message 分开的
  // 理由：那是节点内部独白（一次 run 五十多条），这是对话。同一个样式渲染，
  // 信号就被噪音淹了。
  "orchestrator.said",
  // 插话送达回执（2026-08-18）：**机械事实**（worker 已取走这句话），不是
  // 调度器说的话。旧路径发在 progress 通道，会被下一条工具进度覆盖 ——
  // 用户按下回车后 2 分钟黑屏。payload.repliesToMessageId 锚回用户那条消息。
  "interrupt.acknowledged",
  "session.completed",
  "step.started",
  "step.progress",
  "step.completed",
  "step.failed",
  "tool.started",
  "tool.progress",
  "tool.retrying",
  "tool.long_running",
  "tool.completed",
  "tool.failed",
  "task.created",
  "task.started",
  "task.updated",
  "task.completed",
  "task.blocked",
  "artifact.created",
  "artifact.versioned",
  "artifact.frozen",
  // 一次工具调用改了哪些 Project 文件（每文件 ±行数）——时间线内联
  // "Edited foo.py +17 -0" 卡片的数据源。
  "workspace.changed",
  "review.completed",
  "knowledge.updated",
  "knowledge.conflict_detected",
  "decision.required",
  "decision.resolved",
  "permission.required",
  "permission.resolved",
  "run.queued",
  "run.started",
  "run.paused",
  "run.resumed",
  "run.injected",
  "run.cancelled",
  "run.retrying",
  "run.recovering",
  "run.blocked",
  // worker 自报「我在睡，睡到几点」（RFC D10 活动维）。它是事实不是判决：
  // 平台据此延长活动租约，到点没动静照样衰减为 unknown。
  "run.parked",
  // ⚠️ 与上面的 `interrupt.acknowledged` 是**两件事**，别合并：
  //   interject.queued        平台说「我收下了」（入队那一刻，可能没人在跑）
  //   interrupt.acknowledged  worker 说「我读到了」（它真的取走并要处理了）
  // 收下 ≠ 被读到。合成一个的话，"入队黑洞"（收下了但永远没人取）就再也
  // 看不出来了 —— 而那正是 D10 可寻址那一维要人看见的东西。
  "interject.queued",
  "run.completed",
  "run.incomplete",
  "run.failed",
  "run.status_unknown",
  "usage.updated",
  // 调度器上一次请求把窗口占到了哪里（harness 每次 LLM 响应后自报）。
  // 输入框上方那个"当前上下文 xx%"chip 读的就是它 —— 与 usage.updated 是
  // 同一时刻的两件事：那条记花费，这条记占用。
  "context.updated",
  "budget.warning",
  "autonomy.downgraded",
  "autonomy.weak_walls",
  "material.added",
  "run.rejoined",
  "redaction.warning",
  // 摄取层拒收了一条 transcript 记录（内容不变量被违反）。这是**见证**，
  // 不是故障：turn 还活着，只是这一条没落成正常事件。
  "record.rejected",
  "connection.lost",
  "connection.recovered",
] as const;

export type ExecutionEventKind = (typeof EXECUTION_EVENT_KINDS)[number];
export type EventOrigin =
  | "raw_transcript"
  | "adapter_derived"
  | "app_command"
  | "reconciliation";
export type EventVisibility = "summary" | "standard" | "trace";

export type ExecutionEvent = {
  schemaVersion: 1;
  id: string;
  sequence: number;
  at: string;
  workspaceId: string;
  projectId: string;
  sessionId: string;
  runId?: string;
  parentRunId?: string;
  origin: EventOrigin;
  source: {
    rawEvent?: string;
    fileRef?: string;
    byteOffset?: number;
    derivedFrom?: string[];
  };
  kind: ExecutionEventKind;
  visibility: EventVisibility;
  payload: Record<string, unknown>;
};

const ORIGINS = new Set<string>([
  "raw_transcript",
  "adapter_derived",
  "app_command",
  "reconciliation",
]);
const VISIBILITIES = new Set<string>(["summary", "standard", "trace"]);

export class FixtureContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "FixtureContractError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requiredString(
  value: Record<string, unknown>,
  key: string,
  index: number,
): string {
  const field = value[key];
  if (typeof field !== "string" || field.length === 0) {
    throw new FixtureContractError(`Event ${index}: ${key} must be a non-empty string`);
  }
  return field;
}

function optionalString(
  value: Record<string, unknown>,
  key: string,
  index: number,
): string | undefined {
  const field = value[key];
  if (field === undefined) return undefined;
  if (typeof field !== "string" || field.length === 0) {
    throw new FixtureContractError(`Event ${index}: ${key} must be a non-empty string`);
  }
  return field;
}

/**
 * Frontend-local compatibility adapter for UI Spec §27.4 schemaVersion 1.
 * It deliberately validates only the frozen event envelope. Payload typing is
 * isolated in payload-view.ts until the backend publishes canonical payloads.
 */
export function parseExecutionEvent(value: unknown, index = 0): ExecutionEvent {
  if (!isRecord(value)) {
    throw new FixtureContractError(`Event ${index}: expected an object`);
  }
  if (value.schemaVersion !== 1) {
    throw new FixtureContractError(`Event ${index}: unsupported schemaVersion`);
  }

  const sequence = value.sequence;
  if (!Number.isSafeInteger(sequence) || (sequence as number) < 1) {
    throw new FixtureContractError(`Event ${index}: sequence must be a positive integer`);
  }

  // 种类只校验"是个非空字符串"，不校验"在我的名单里"。名单是后端词表的**抄件**，
  // 后端加一种（`autonomy.downgraded`，2026-09-09）而抄件没跟上时，这里一抛，
  // 整条事件流报废：客户端从同一游标反复重开、每次漏一条连接，六条占满后整个
  // 页签再也发不出任何请求（node20，qinp 的课题"跑不起来"的第二层）。
  // 不认识的种类照常送达，界面按通用形状画；名单本身由契约测试逼着跟上。
  const kind = requiredString(value, "kind", index);

  const origin = requiredString(value, "origin", index);
  if (!ORIGINS.has(origin)) {
    throw new FixtureContractError(`Event ${index}: invalid origin ${origin}`);
  }

  const visibility = requiredString(value, "visibility", index);
  if (!VISIBILITIES.has(visibility)) {
    throw new FixtureContractError(`Event ${index}: invalid visibility ${visibility}`);
  }

  const sourceValue = value.source;
  if (!isRecord(sourceValue)) {
    throw new FixtureContractError(`Event ${index}: source must be an object`);
  }
  const derivedFromValue = sourceValue.derivedFrom;
  if (
    derivedFromValue !== undefined &&
    (!Array.isArray(derivedFromValue) ||
      derivedFromValue.some((item) => typeof item !== "string" || item.length === 0))
  ) {
    throw new FixtureContractError(`Event ${index}: source.derivedFrom must be strings`);
  }
  const byteOffsetValue = sourceValue.byteOffset;
  if (
    byteOffsetValue !== undefined &&
    (!Number.isSafeInteger(byteOffsetValue) || (byteOffsetValue as number) < 0)
  ) {
    throw new FixtureContractError(`Event ${index}: source.byteOffset must be non-negative`);
  }

  if (!isRecord(value.payload)) {
    throw new FixtureContractError(`Event ${index}: payload must be an object`);
  }

  const rawEvent = optionalString(sourceValue, "rawEvent", index);
  const fileRef = optionalString(sourceValue, "fileRef", index);
  if (
    origin === "raw_transcript" &&
    (!rawEvent || !fileRef || byteOffsetValue === undefined)
  ) {
    throw new FixtureContractError(
      `Event ${index}: raw_transcript requires source.rawEvent, source.fileRef, and source.byteOffset`,
    );
  }
  if (
    origin === "adapter_derived" &&
    (!Array.isArray(derivedFromValue) ||
      derivedFromValue.length === 0 ||
      new Set(derivedFromValue).size !== derivedFromValue.length)
  ) {
    throw new FixtureContractError(
      `Event ${index}: adapter_derived requires non-empty source.derivedFrom`,
    );
  }

  return {
    schemaVersion: 1,
    id: requiredString(value, "id", index),
    sequence: sequence as number,
    at: requiredString(value, "at", index),
    workspaceId: requiredString(value, "workspaceId", index),
    projectId: requiredString(value, "projectId", index),
    sessionId: requiredString(value, "sessionId", index),
    runId: optionalString(value, "runId", index),
    parentRunId: optionalString(value, "parentRunId", index),
    origin: origin as EventOrigin,
    source: {
      rawEvent,
      fileRef,
      byteOffset: byteOffsetValue as number | undefined,
      derivedFrom: derivedFromValue as string[] | undefined,
    },
    kind: kind as ExecutionEventKind,
    visibility: visibility as EventVisibility,
    payload: value.payload,
  };
}

export function parseExecutionEvents(input: unknown): ExecutionEvent[] {
  let values: unknown;
  if (typeof input === "string") {
    const trimmed = input.trim();
    if (!trimmed) return [];
    if (trimmed.startsWith("[")) {
      values = JSON.parse(trimmed) as unknown;
    } else {
      values = trimmed.split(/\r?\n/).map((line) => JSON.parse(line) as unknown);
    }
  } else {
    values = input;
  }

  if (!Array.isArray(values)) {
    throw new FixtureContractError("Execution events must be an array or JSONL string");
  }

  const events = values.map((value, index) => parseExecutionEvent(value, index));
  return events.sort((left, right) => left.sequence - right.sequence);
}
