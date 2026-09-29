import { say, type Language } from "../../../shared/i18n/language.ts";
import type { ExecutionEvent } from "./execution-event.ts";
import { belongsToRun } from "./run-scope.ts";
import { retryProgressLabel } from "./failure-surface.ts";
import { readPayload, type EventPayloadView } from "./payload-view.ts";
import {
  isVisibleResearchTool,
  researchToolFailurePresentation,
  researchToolInputPreview,
  researchToolRunningStatus,
  researchToolTitle,
} from "./tool-presentation.ts";

export type RunActivityTool = {
  id: string;
  stepId?: string;
  /** 首个事件的 sequence —— 时间线归并的排序键。 */
  sequence: number;
  title: string;
  technicalName?: string;
  status: "running" | "retrying" | "completed" | "failed" | "interrupted";
  /**
   * 这次失败是不是"框架按设计驳回"——ReAct 循环的正常一步。
   * 由 harness 在失败发生的那一层盖章（`errorCode`），不是显示层猜的。
   */
  declined?: boolean;
  input?: string;
  code?: string;
  output?: string;
  outputFormat?: "text" | "terminal";
  error?: {
    title: string;
    message: string;
    recovery: string;
    /** true = 框架按设计驳回（见 `declined`）。 */
    loopLevel?: boolean;
  };
  retryCount: number;
  notices: string[];
};

export type RunActivityArtifact = {
  id: string;
  linkable: boolean;
  name: string;
  mediaType?: string;
  version?: number;
};

export type RunActivityStep = {
  id: string;
  kind: "child" | "tool_group";
  /** 这一步首个事件的 sequence —— 决定它插在时间线的哪个位置。 */
  sequence: number;
  /** 这一步属于哪条 run —— 子节点有自己的 run，叙述也按它归组。 */
  runId?: string;
  title?: string;
  summary?: string;
  status: "running" | "completed" | "failed" | "interrupted";
  /** 这条节点 run 是**接着上次被打断的地方**跑的（死亡续跑），不是新开一条。 */
  resumed?: boolean;
  /** 这是这条 run 的第几次派发（1 起）。同一节点重派会复用 run id，卡按派发段分。 */
  dispatch?: number;
  tools: RunActivityTool[];
};

/** 调度器对用户说的话 —— 对话级，不是节点内部独白。 */
export type RunActivitySaid = {
  id: string;
  runId?: string;
  sequence: number;
  text: string;
  aboutNodeType: string;
  /** 这句话在回答**哪条消息**（插话锚定）。空 = 派发旁注 / CLI 投递，留在 run 窗口。
   *
   * 只认事件自带的消息锚（`repliesToMessageId` / `messageId`）。这里曾经退回
   * **每条事件都带**的 `submissionId` —— 「这一趟提交」被当成了「回答哪条消息」
   * （[[两件事一条规则]]：这一次 / 这一趟）。2026-09-02 实测代价：调度器在一轮
   * 末尾派发 experiment 的那句话带 submissionId=turn-…，被当成回复回声从 run
   * 窗口里滤掉，而它引出的子节点卡已经折在这句话尾巴上 —— 一起消失。主聊天区
   * 于是对一个跑了一小时、76 个动作的子节点**一张卡都没有**；右栏却好好的。
   * `turn-…` 也不等于任何消息的 id，这句话哪个槽位都不渲染，两头落空。
   *
   * 真需要锚的事件都自带真锚（实测：interject.queued 带 messageId、
   * interrupt.acknowledged / 插话答复的 said 带 repliesToMessageId）——退回
   * 提交号从来没救过谁，只会把派发线吞掉。提交号另存 `submissionId`，各归各名。 */
  repliesToMessageId?: string;
  /** 这句话属于哪一次提交（"这一趟"）。归属，不是应答关系。 */
  submissionId?: string;
  /** 送达回执（机械事实，不是发言）。deferred = 没有活跃子节点，下一轮才读。 */
  receipt?: { deferred: boolean };
};

export type RunActivityWorkspaceChange = {
  id: string;
  runId?: string;
  /** 触发这次改动的工具调用之后的事件 sequence —— 内联卡插回原位靠它。 */
  sequence: number;
  tool: string;
  filesChanged: number;
  additions: number;
  deletions: number;
  files: {
    path: string;
    additions?: number;
    deletions?: number;
    /** added / modified / deleted / renamed —— 由采集端说，不从 ±行数猜。 */
    status?: string;
  }[];
  /**
   * 这一次改动的 diff 正文（统一格式）。
   *
   * 没有它，卡片就只能报个行数 —— 用户 2026-08-17 的原话是"点开之后并不能看
   * 到这个 diff 的详细信息"。老事件不带这个字段：此时卡片必须**不画**展开
   * 箭头，而不是画一个点开是空的。
   */
  patch?: string;
  patchTruncated: boolean;
  /** 这次改动是不是纯平台内务（`.research/` 记账）—— 采集端算好的结论。 */
  internalOnly?: boolean;
};

export type RunActivityDetail = {
  steps: RunActivityStep[];
  directModelResponse: boolean;
  toolCount: number;
  currentStatus?: string;
  retryCount: number;
  artifacts: RunActivityArtifact[];
  /** 每次"工具改了 Project 文件"的原位记录（"Edited foo.py +17 -0"）。 */
  workspaceChanges: RunActivityWorkspaceChange[];
  /** 调度器对用户说的话（派发节点前的那一句）。 */
  said: RunActivitySaid[];
  primaryFailure?: RunActivityTool["error"];
  /**
   * agent 每一轮写的人话，按 stepId 归到它所属的那一组。
   *
   * wangd 试用后的原话：「每一步具体在干啥，我感觉看的一头雾水，它没有告诉
   * 这个用户，我现在干了啥？」—— 工具名答不了"它为什么突然搜圣诞布丁"，而
   * 模型第 3 轮就写了"前两轮查得太宽泛，换精准词"。
   */
  narration: RunActivityNarration[];
};

export type RunActivityNarration = {
  id: string;
  stepId?: string;
  runId?: string;
  /** 产生这句话的事件 sequence —— 让它能插回工具调用之间原来的位置。 */
  sequence: number;
  turn: number;
  text: string;
  previewOnly: boolean;
};

const PREVIEW_LIMIT = 260;
const TRUNCATION_CODES = new Set([
  "string_truncated",
  "array_truncated",
  "object_truncated",
  "maximum_depth",
]);
const REDACTION_CODES = new Set([
  "sensitive_field",
  "sensitive_value",
  "path_minimized",
  "redaction_failed_closed",
]);

function clip(value: string, limit = PREVIEW_LIMIT) {
  if (value.length <= limit) return { text: value, clipped: false };
  return { text: `${value.slice(0, limit).trimEnd()}…`, clipped: true };
}

function record(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function structuredOutput(value: string) {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
    if (typeof parsed === "string" && /^[{[]/.test(parsed.trim())) parsed = JSON.parse(parsed);
  } catch {
    return /^[{[]/.test(value.trim()) ? { hidden: true as const } : undefined;
  }
  const root = record(parsed);
  if (!root) return { hidden: true as const };
  const candidate = record(root.result) ?? record(root.data) ?? root;
  const readableOutput = (...values: unknown[]) => {
    const value = values.find((item) =>
      (typeof item === "string" && item.length > 0)
      || (Array.isArray(item) && item.some((line) => typeof line === "string")));
    if (typeof value === "string") return value.trimEnd();
    return Array.isArray(value)
      ? value.filter((line): line is string => typeof line === "string").join("\n").trimEnd()
      : "";
  };
  const stdout = readableOutput(candidate.stdout, candidate.stdout_tail);
  const stderr = readableOutput(candidate.stderr, candidate.stderr_tail);
  const returnCode = [
    candidate.return_code,
    candidate.returncode,
    candidate.exit_code,
    candidate.returnCode,
    candidate.exitCode,
  ]
    .find((item) => typeof item === "number" || typeof item === "string");
  if (stdout || stderr || returnCode !== undefined) {
    const sections: string[] = [];
    if (stdout) sections.push(`stdout\n${stdout}`);
    if (stderr) sections.push(`stderr\n${stderr}`);
    if (returnCode !== undefined) sections.push(`Exit code ${String(returnCode)}`);
    return { text: sections.join("\n\n"), format: "terminal" as const };
  }
  const summary = [candidate.summary, candidate.message, candidate.result]
    .find((item) => typeof item === "string" || typeof item === "number");
  if (summary !== undefined) return { text: String(summary), format: "text" as const };
  if (typeof candidate.count === "number") {
    return { text: `${candidate.count} recorded results`, format: "text" as const };
  }
  return { hidden: true as const };
}

function outputPreview(payload: EventPayloadView) {
  if (payload.outputLines.length) {
    return { ...clip(payload.outputLines.join("\n"), 1600), format: "terminal" as const };
  }
  if (!payload.summary) return undefined;
  const structured = structuredOutput(payload.summary);
  if (structured && "hidden" in structured) return undefined;
  const presentation = structured ?? { text: payload.summary, format: "text" as const };
  return { ...clip(presentation.text, 1600), format: presentation.format };
}

function codePreview(argumentsValue?: Record<string, unknown>) {
  const code = argumentsValue?.code;
  return typeof code === "string" && code.trim() ? clip(code.trim(), 1600) : undefined;
}

function containsMarker(value: unknown, marker: string) {
  try {
    return JSON.stringify(value).includes(marker);
  } catch {
    return false;
  }
}

function warningCodes(events: readonly ExecutionEvent[]) {
  const codes = new Map<string, Set<string>>();
  for (const event of events) {
    if (event.kind !== "redaction.warning") continue;
    const sourceEventId = typeof event.payload.sourceEventId === "string"
      ? event.payload.sourceEventId
      : event.source.derivedFrom?.[0];
    if (!sourceEventId || !Array.isArray(event.payload.codes)) continue;
    const bucket = codes.get(sourceEventId) ?? new Set<string>();
    for (const code of event.payload.codes) {
      if (typeof code === "string") bucket.add(code);
    }
    codes.set(sourceEventId, bucket);
  }
  return codes;
}

function toolStatus(events: readonly ExecutionEvent[], parentInterrupted: boolean): RunActivityTool["status"] {
  const latest = events.at(-1)?.kind;
  if (latest === "tool.failed") return "failed";
  if (latest === "tool.completed") return "completed";
  if (parentInterrupted) return "interrupted";
  if (latest === "tool.retrying") return "retrying";
  return "running";
}

function currentRunStatus(events: readonly ExecutionEvent[], terminal: boolean, lang: Language) {
  if (terminal) return undefined;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    const payload = readPayload(event);
    if (event.kind.startsWith("tool.") && payload.technicalName && !isVisibleResearchTool(payload.technicalName)) {
      continue;
    }
    if (event.kind === "tool.started" || event.kind === "tool.progress" || event.kind === "tool.long_running") {
      if (!payload.technicalName || isVisibleResearchTool(payload.technicalName)) {
        return researchToolRunningStatus(payload.technicalName, payload.toolArguments, payload.detail, lang);
      }
      continue;
    }
    if (event.kind === "tool.completed") return say({ zh: "正在看工具结果", en: "Analyzing tool results" }, lang);
    if (event.kind === "tool.failed") return say({ zh: "有个研究工具要人看一下", en: "A research tool needs attention" }, lang);
    if (event.kind === "run.recovering" && event.payload.reason === "void_turn") {
      const attempt = typeof event.payload.attempt === "number" ? event.payload.attempt : undefined;
      const maximum = typeof event.payload.maxAttempts === "number" ? event.payload.maxAttempts : undefined;
      return `${say({ zh: "模型没给出可用的回复", en: "The model returned no usable response" }, lang)} · ${retryProgressLabel({ attempt, maxAttempts: maximum }, lang)}`;
    }
    if (event.kind === "run.retrying") {
      // 事件里本来就带着 attempt / maxAttempts（后端 execution_ingest 建这条
      // 事件时写的），显示层一直没读 —— 于是人只看得到"在重试"，看不到
      // "第几次、还剩几次"。
      const attempt = typeof event.payload.attempt === "number" ? event.payload.attempt : undefined;
      const maximum = typeof event.payload.maxAttempts === "number" ? event.payload.maxAttempts : undefined;
      return `${say({ zh: "模型请求失败了", en: "The model request failed" }, lang)} · ${retryProgressLabel({ attempt, maxAttempts: maximum }, lang)}`;
    }
    // v2.1：节点报的阻塞是一等信号（节点只报事实+证据+需求，调度器决定怎么解）。
    // 此前平台不投影它，用户只能看到 run 莫名 incomplete，毫无线索。
    if (event.kind === "run.blocked") {
      const node = typeof event.payload.reportingNode === "string" ? event.payload.reportingNode : "";
      const summary = typeof event.payload.summary === "string" ? event.payload.summary : "";
      const head = node
        ? say({ zh: "{node} 卡住了", en: "{node} is blocked" }, lang, { node })
        : say({ zh: "有一步卡住了", en: "A step is blocked" }, lang);
      return summary ? `${head} · ${summary.slice(0, 120)}` : head;
    }
    // 停靠中：worker 活着、只是按退避间隔在等下一次复查（RFC D10）。
    //
    // 8-21 现场：unattended 停靠在 4 小时的复查点上，用户问「怎么样了？」，
    // 读到的是「平台在记录这次运行时撞上了内部错误」—— 三样全错。真实局面
    // 是"它在等，等到某个时刻，因为某个原因"，而这三样 worker 全都自报了。
    //
    // 说清楚**还要等多久**：不给时间尺度的"稍等"比不说更糟（用户无法决定
    // 是去做别的还是在这儿等）。给的是它自己报的那个时刻，不是我们猜的。
    if (event.kind === "run.parked") {
      const until = typeof event.payload.untilEpoch === "number" ? event.payload.untilEpoch : 0;
      const why = typeof event.payload.why === "string" ? event.payload.why : "";
      const minutes = until > 0 ? Math.round((until * 1000 - Date.now()) / 60000) : 0;
      const when = minutes > 60
        ? say({ zh: "约 {hours} 小时后复查", en: "rechecks in about {hours}h" }, lang, { hours: Math.round(minutes / 60) })
        : minutes > 0
          ? say({ zh: "约 {minutes} 分钟后复查", en: "rechecks in about {minutes} min" }, lang, { minutes })
          : say({ zh: "即将复查", en: "rechecking shortly" }, lang);
      // 一句话就能叫醒它 —— 这是 D12 已经落地的能力，用户该知道自己有这个选项。
      const parked = say({ zh: "停靠中", en: "Parked" }, lang);
      const wake = say({ zh: "发一句话可立即唤醒", en: "send a message to wake it now" }, lang);
      return `${parked} · ${when}${why ? `（${why}）` : ""} · ${wake}`;
    }
    if (event.kind === "decision.required" || event.kind === "run.paused") return say({ zh: "在等你回话", en: "Waiting for your input" }, lang);
    if (event.kind === "permission.required") return say({ zh: "在等授权", en: "Waiting for permission" }, lang);
    if (event.kind === "run.status_unknown" || event.kind === "connection.lost") return say({ zh: "执行状态不明", en: "Execution status is unknown" }, lang);
    if (event.kind === "session.message" && payload.role === "assistant") return say({ zh: "正在生成回复", en: "Generating the response" }, lang);
    if (event.kind === "usage.updated") return say({ zh: "正在生成回复", en: "Generating the response" }, lang);
    if (event.kind === "step.started" && event.source.rawEvent === "subagent_call_start") {
      return payload.title
        ? say({ zh: "正在跑 {title}", en: "Running {title}" }, lang, { title: payload.title })
        : say({ zh: "正在跑一步研究", en: "Running a research step" }, lang);
    }
    if (event.kind === "step.started") return payload.title
      ? say({ zh: "正在开始 {title}", en: "Starting {title}" }, lang, { title: payload.title })
      : say({ zh: "正在开始一步记录在案的动作", en: "Starting a recorded step" }, lang);
    if (event.kind === "run.started") {
      // 传输细节不是研究状态：模型徽标在 composer 上一直可见，状态行
      // 统一用 thinking（wangd 2026-08-18）。
      return say({ zh: "在想", en: "Thinking" }, lang);
    }
    if (event.kind === "run.queued") return say({ zh: "在等一个执行位", en: "Waiting for an execution slot" }, lang);
  }
  return say({ zh: "正在启动项目助手", en: "Starting the Project assistant" }, lang);
}

export function projectRunActivity(
  events: readonly ExecutionEvent[],
  /**
   * 父 run 是不是**没走到终点就断了**。以前这里收的是一个状态字符串，调用方
   * 得自己知道哪些状态算"被打断"（又一份手写名单）；现在由后端的 view 直接
   * 回答这个布尔。事件流里推不出来时（没有父 run 的终态事件）才回落到下面
   * 那段自己扫。
   */
  parentInterruptedHint?: boolean,
  lang: Language = "zh",
): RunActivityDetail {
  const ordered = [...events].sort((left, right) => left.sequence - right.sequence);
  // 父 run 的终态只认**它自己**的生命周期事件（子事件都带 parentRunId）。
  // 事件流含子节点后，扫全量会把"某个子节点失败了"当成"这一轮结束了"——
  // 实测：experiment 失败、调度器接着派 literature，literature 被判成
  // interrupted。身份和范围，还是那条规则（[[两件事一条规则]]）。
  const recordedTerminalStatus = ordered.some((event) => event.kind === "run.cancelled" && !event.parentRunId)
    ? "cancelled"
    : ordered.some((event) => event.kind === "run.failed" && !event.parentRunId)
      ? "failed"
      : ordered.some((event) => event.kind === "run.status_unknown" && !event.parentRunId)
        // 本地 token，不借用 run 状态词表的写法 —— 借用会让人以为这里在读
        // run.status（它不是：它是从**事件种类**推出来的）。
        ? "unknown"
        : undefined;
  const parentInterrupted = parentInterruptedHint ?? recordedTerminalStatus !== undefined;
  const warnings = warningCodes(ordered);
  const stepEvents = new Map<string, ExecutionEvent[]>();
  const toolEvents = new Map<string, ExecutionEvent[]>();

  for (const event of ordered) {
    const payload = readPayload(event);
    if (event.kind.startsWith("step.") && payload.stepId) {
      stepEvents.set(payload.stepId, [...(stepEvents.get(payload.stepId) ?? []), event]);
    }
    if (event.kind.startsWith("tool.")) {
      if (payload.technicalName && !isVisibleResearchTool(payload.technicalName)) continue;
      const id = payload.toolCallId ?? `unassociated:${event.id}`;
      toolEvents.set(id, [...(toolEvents.get(id) ?? []), event]);
    }
  }

  const tools = [...toolEvents.entries()].map(([id, ownEvents]): RunActivityTool => {
    const startEvent = ownEvents.find((event) => event.kind === "tool.started") ?? ownEvents[0];
    const endEvent = [...ownEvents].reverse().find((event) =>
      event.kind === "tool.completed" || event.kind === "tool.failed");
    const start = readPayload(startEvent);
    const end = readPayload(endEvent ?? ownEvents[ownEvents.length - 1]);
    const inputValue = researchToolInputPreview(start.toolArguments, lang);
    const input = inputValue ? clip(inputValue) : undefined;
    const code = codePreview(start.toolArguments);
    const output = endEvent?.kind === "tool.completed" ? outputPreview(end) : undefined;
    const failure = endEvent?.kind === "tool.failed"
      ? researchToolFailurePresentation(start.technicalName ?? end.technicalName, end.error, lang)
      : undefined;
    const codes = new Set(ownEvents.flatMap((event) => [...(warnings.get(event.id) ?? [])]));
    const notices: string[] = [];
    if (
      [...codes].some((code) => TRUNCATION_CODES.has(code))
      || containsMarker(start.toolArguments, "[TRUNCATED]")
      || containsMarker(end.summary, "[TRUNCATED]")
    ) notices.push(say({ zh: "原始载荷在入库时被截断", en: "Source payload truncated during ingest" }, lang));
    if (
      [...codes].some((code) => REDACTION_CODES.has(code))
      || containsMarker(start.toolArguments, "[REDACTED]")
    ) notices.push(say({ zh: "敏感值已打码", en: "Sensitive values redacted" }, lang));
    if (input?.clipped || code?.clipped || output?.clipped) notices.push(say({ zh: "预览为显示做了截断", en: "Preview shortened for display" }, lang));

    return {
      id,
      stepId: start.stepId ?? end.stepId,
      sequence: ownEvents[0]?.sequence ?? 0,
      title: researchToolTitle(
        start.technicalName ?? end.technicalName,
        start.toolArguments,
        start.title ?? end.title,
        lang,
      ),
      technicalName: start.technicalName ?? end.technicalName,
      status: toolStatus(ownEvents, parentInterrupted),
      input: input?.text,
      code: code?.text,
      output: output?.text,
      outputFormat: output?.format,
      error: failure,
      declined: failure?.loopLevel === true,
      retryCount: ownEvents.filter((event) => event.kind === "tool.retrying").length,
      notices,
    };
  });

  // 一步属于哪条 run —— 子节点的动作现在也在这批事件里（`includeChildren`），
  // 而它们各有自己的 run_id。哪条 run 是"这一轮自己的"，取事件里最靠前那条：
  // 顶层 run 的第一条事件必然排在它派出去的子节点之前。
  const rootRunId = ordered.find((event) => !event.parentRunId)?.runId
    ?? ordered[0]?.runId;
  const stepRunId = new Map<string, string | undefined>();
  const nodeTypeByRun = new Map<string, string>();
  for (const event of ordered) {
    const payload = readPayload(event);
    if (payload.stepId && !stepRunId.has(payload.stepId)) {
      stepRunId.set(payload.stepId, event.runId);
    }
    // 直接读原始 payload：`readPayload` 是**类型化视图**，只透传它认识的字段，
    // nodeType 不在其中。视图是给渲染用的，取事实要回源。
    const nodeType = (event.payload as Record<string, unknown> | undefined)?.nodeType;
    if (event.runId && typeof nodeType === "string" && nodeType && !nodeTypeByRun.has(event.runId)) {
      nodeTypeByRun.set(event.runId, nodeType);
    }
  }

  const stepIds = new Set([
    ...stepEvents.keys(),
    ...tools.flatMap((tool) => tool.stepId ? [tool.stepId] : []),
  ]);
  // 每条 run 的终态，从它自己的生命周期事件现算。子节点的根 step **没有**
  // step.completed —— 它的结束写在 run.completed / run.failed 里；只读 step.*
  // 会让子节点组永远显示"进行中"（实测：literature 完成一小时后仍标运行）。
  // 判据与研究地图同源：这条 run 有没有终态事件。
  // ── 一条 run 的「派发段」：哪次 run.started 是新派发，哪次只是续跑 ─────────
  //
  // 调度器复用确定性 run id（`…::_orchestrator->experiment@d1`）：同一个节点派
  // 第二次还是这个 id。但库里分得清两件事（全库回放 2026-09-02）：
  //   · 死亡续跑 = `run.started` **紧跟**一条 `run.resumed`（每次续跑都是这个
  //     形状，如 3620/3621）—— 接着上次的地方跑，是同一件事；
  //   · 重派 = 不带 `run.resumed` 的 `run.started`（1434 首派 → 1904 incomplete
  //     → 2134 重派 → 2295 failed）—— 新的一件事。
  //
  // 旧写法只认 run id，且判据是「最后一次开跑之后有没有终态」（2026-08-22，会话
  // 41ac6a66：hypothesis seq 323 completed、seq 1558 又 started，卡却一直写"完成"）。
  // 那一刀治好了"重派后仍显示完成"，却把两次派发并成一张卡、状态取末事件 ——
  // 课题二 2026-09-02：`→ experiment 失败 · 112 actions` 出现两次，而首派的真实
  // 结局是报 blocker 收工（incomplete）。[[复用的 id 要带起点]]：起点就是派发段。
  const LIFECYCLE = new Set(["run.started", "run.resumed", "run.completed", "run.incomplete", "run.failed", "run.cancelled"]);
  //: runId → 每次**新派发**的 run.started sequence（续跑的那条 started 不算）。
  const dispatchStartsByRun = new Map<string, number[]>();
  //: runId → 它的父 run（库里的事实；合成卡时用它判"是不是这一轮派出去的"）。
  const parentByRun = new Map<string, string>();
  {
    const lastLifecycle = new Map<string, string>();
    for (const event of ordered) {
      if (event.runId && event.parentRunId && !parentByRun.has(event.runId)) {
        parentByRun.set(event.runId, event.parentRunId);
      }
      if (!event.runId || !LIFECYCLE.has(event.kind)) continue;
      if (event.kind === "run.started") {
        const starts = dispatchStartsByRun.get(event.runId) ?? [];
        starts.push(event.sequence);
        dispatchStartsByRun.set(event.runId, starts);
      } else if (event.kind === "run.resumed" && lastLifecycle.get(event.runId) === "run.started") {
        // 刚才那条 started 是续跑，不是新派发 —— 收回（第一段永远保留）。
        const starts = dispatchStartsByRun.get(event.runId);
        if (starts && starts.length > 1) starts.pop();
      }
      lastLifecycle.set(event.runId, event.kind);
    }
  }
  const dispatchOf = (runId: string, sequence: number): number => {
    let no = 0;
    for (const start of dispatchStartsByRun.get(runId) ?? []) if (start <= sequence) no += 1;
    return Math.max(no, 1);
  };
  const latestDispatchOf = (runId: string): number =>
    Math.max((dispatchStartsByRun.get(runId) ?? []).length, 1);
  const dispatchKey = (runId: string, dispatchNo: number) => `${runId}#${dispatchNo}`;
  // 每个派发段自己的终态；续跑段落在同一个派发段里。开跑清掉本段旧终态
  // （incomplete 之后被续跑的那种：3699 → 3925 incomplete → 3935 started/resumed）。
  const terminalByDispatch = new Map<string, RunActivityStep["status"]>();
  const resumedDispatches = new Set<string>();
  for (const event of ordered) {
    if (!event.runId) continue;
    const key = dispatchKey(event.runId, dispatchOf(event.runId, event.sequence));
    if (event.kind === "run.started") {
      terminalByDispatch.delete(key);
    } else if (event.kind === "run.resumed") {
      resumedDispatches.add(key);
    } else if (event.kind === "run.completed" || event.kind === "run.incomplete") {
      terminalByDispatch.set(key, "completed");
    } else if (event.kind === "run.failed") {
      terminalByDispatch.set(key, "failed");
    } else if (event.kind === "run.cancelled") {
      terminalByDispatch.set(key, "interrupted");
    }
  }
  //: 一个 stepId 在**一个派发段**里是一张卡。线上每次派发都发新的 root step
  //: （全库回放：派发数 ≤ step 数，无一例外）；若同一个 stepId 的事件跨了派发段
  //: （合成样本里有这种形状），按段切开 —— 与真实重派走同一条路。
  const segmentsOf = (
    ownerRunId: string | undefined,
    ownEvents: ExecutionEvent[],
    ownTools: RunActivityTool[],
  ): Array<[number, { events: ExecutionEvent[]; tools: RunActivityTool[] }]> => {
    const groups = new Map<number, { events: ExecutionEvent[]; tools: RunActivityTool[] }>();
    const groupFor = (sequence: number) => {
      const no = ownerRunId ? dispatchOf(ownerRunId, sequence) : 1;
      let group = groups.get(no);
      if (!group) {
        group = { events: [], tools: [] };
        groups.set(no, group);
      }
      return group;
    };
    for (const event of ownEvents) groupFor(event.sequence).events.push(event);
    for (const tool of ownTools) groupFor(tool.sequence).tools.push(tool);
    return [...groups.entries()].sort((a, b) => a[0] - b[0]);
  };
  const emittedDispatches = new Set<string>();
  const steps: RunActivityStep[] = [...stepIds].flatMap((id): RunActivityStep[] => {
    // 「这一步是子节点跑的」有两种说法：老 transcript 里的
    // `subagent_call_start`，和**这一步的事件属于另一条 run**。后者才是权威
    // 的那份 —— 子节点 run 有自己的 run_id 和 parent_run_id，是库里的事实，
    // 不依赖某个 raw 事件名恰好没变过。
    const ownerRunId = stepRunId.get(id);
    const allEvents = stepEvents.get(id) ?? [];
    const allTools = tools.filter((tool) => tool.stepId === id);
    return segmentsOf(ownerRunId, allEvents, allTools).map(([dispatchNo, segment], index): RunActivityStep => {
      const ownEvents = segment.events;
      const ownTools = segment.tools;
      const start = ownEvents.find((event) => event.kind === "step.started");
      const end = [...ownEvents].reverse().find((event) =>
        event.kind === "step.completed" || event.kind === "step.failed");
      const startPayload = start ? readPayload(start) : undefined;
      const endPayload = end ? readPayload(end) : undefined;
      const stepSequence = Math.min(
        ...ownEvents.map((event) => event.sequence),
        ...ownTools.map((tool) => tool.sequence),
        Number.MAX_SAFE_INTEGER,
      );
      // 被后一次派发顶掉、自己又没收到终态的那段：它不可能还在跑 —— 已中断。
      const superseded = Boolean(ownerRunId && dispatchNo < latestDispatchOf(ownerRunId));
      const runTerminal = ownerRunId
        ? terminalByDispatch.get(dispatchKey(ownerRunId, dispatchNo)) ?? (superseded ? "interrupted" : undefined)
        : undefined;
      const status: RunActivityStep["status"] = end?.kind === "step.failed"
        ? "failed"
        : end?.kind === "step.completed"
          ? "completed"
          : runTerminal
            ?? (parentInterrupted ? "interrupted" : "running");
      const dispatched = Boolean(ownerRunId && rootRunId && ownerRunId !== rootRunId);
      const child = dispatched || start?.source.rawEvent === "subagent_call_start";
      // 子节点这一组要说得出**是谁** —— "Research step" 回答不了
      // 「literature 结束了没有、现在轮到谁」。
      const dispatchedTitle = dispatched
        ? nodeTypeByRun.get(ownerRunId!) ?? ownerRunId
        : undefined;
      if (ownerRunId) emittedDispatches.add(dispatchKey(ownerRunId, dispatchNo));
      return {
        //: 第一段沿用 stepId（右栏按它聚焦），后面的段带上派发号。
        id: index === 0 ? id : `${id}@d${dispatchNo}`,
        runId: ownerRunId,
        kind: child ? "child" : "tool_group",
        resumed: Boolean(ownerRunId && resumedDispatches.has(dispatchKey(ownerRunId, dispatchNo))),
        dispatch: ownerRunId ? dispatchNo : undefined,
        sequence: stepSequence,
        title: dispatchedTitle ?? (child || allTools.length > 0
          ? startPayload?.title
          : status === "failed"
            ? "Research step failed"
            : undefined),
        summary: endPayload?.summary,
        status,
        tools: ownTools,
      };
    });
  }).filter((step) => step.tools.length > 0 || step.kind === "child" || step.status === "failed");

  // ── 没留下 step 的派发段也要有卡（从生命周期合成）────────────────────────
  //
  // 线上每次派发都发 root step（全库回放无例外），但"派出去立刻失败/被打断"的
  // 退化形状里可能一条 step 都没有 —— 那句 `→ hypothesis` 找不到卡，退化成灰字；
  // 而 08-22 的底线是「真结束了要如实回到终态」。合成一张 0-action 的卡：位置在
  // 开跑处，结局取该段终态。
  for (const [runId, starts] of dispatchStartsByRun) {
    // 只给**这一轮派出去的子节点**合成：有父 run、且父就是这一轮的根。
    const parentRunId = parentByRun.get(runId);
    if (!rootRunId || !parentRunId || !belongsToRun({ runId, parentRunId }, rootRunId)) continue;
    starts.forEach((startSequence, index) => {
      const dispatchNo = index + 1;
      const key = dispatchKey(runId, dispatchNo);
      if (emittedDispatches.has(key)) return;
      const superseded = dispatchNo < starts.length;
      steps.push({
        id: key,
        runId,
        kind: "child",
        resumed: resumedDispatches.has(key),
        dispatch: dispatchNo,
        sequence: startSequence,
        title: nodeTypeByRun.get(runId) ?? runId,
        status: terminalByDispatch.get(key) ?? (superseded || parentInterrupted ? "interrupted" : "running"),
        tools: [],
      });
    });
  }
  steps.sort((a, b) => a.sequence - b.sequence);

  // ── 一条子 run = 一张卡，哪怕它中途被打断又续跑（2026-08-18）─────────────
  //
  // 死亡续跑之后是**同一条 run** 接着走（同 run_id、同目录、transcript 追加），
  // 但恢复时会重新发一条 root step —— 于是同一条 run 在 UI 上裂成两张卡：
  //
  //     literature 已中断 · 8 actions
  //     literature 进行中 · 5 actions
  //
  // 读起来仍旧是"它又新开了一个"，而那正是这次要消灭的观感
  // （wangd：「上一个被打断，然后新开一个，非常离谱」）。
  //
  // 归并判据是 runId —— 库里的事实，不是 stepId（stepId 是"这一段"的 id，
  // 一条 run 被打断几次就有几段）。动作按发生顺序接起来，位置取最早那段，
  // 状态取最后那段（run 的现状）。
  //
  // 但只在**同一个派发段**内归并（2026-09-02）：重派是新的一件事，另起一张卡，
  // 各戴各的结局 —— 判据与上面的派发段同源。
  const mergedChildren = new Map<string, RunActivityStep>();
  const merged: RunActivityStep[] = [];
  for (const step of steps) {
    if (step.kind !== "child" || !step.runId) {
      merged.push(step);
      continue;
    }
    const mergeKey = dispatchKey(step.runId, step.dispatch ?? 1);
    const seen = mergedChildren.get(mergeKey);
    if (!seen) {
      mergedChildren.set(mergeKey, step);
      merged.push(step);
      continue;
    }
    seen.tools = [...seen.tools, ...step.tools].sort((a, b) => a.sequence - b.sequence);
    seen.sequence = Math.min(seen.sequence, step.sequence);
    seen.status = step.status;          // 现状 = 最后那段的状态
    seen.resumed = seen.resumed || step.resumed;
    seen.summary = step.summary ?? seen.summary;
    seen.title = seen.title ?? step.title;
  }
  steps.length = 0;
  steps.push(...merged);

  const unassociated = tools.filter((tool) => !tool.stepId);
  if (unassociated.length) {
    const unassociatedStatus: RunActivityStep["status"] = unassociated.some(
      (tool) => tool.status === "failed" && !tool.declined)
      ? "failed"
      : unassociated.every((tool) => tool.status === "completed")
        ? "completed"
        : parentInterrupted
          ? "interrupted"
          : "running";
    steps.push({
      id: "unassociated-tools",
      kind: "tool_group",
      sequence: Math.min(...unassociated.map((tool) => tool.sequence)),
      status: unassociatedStatus,
      tools: unassociated,
    });
  }

  const hasAssistantResponse = ordered.some((event) => {
    const payload = readPayload(event);
    return event.kind === "session.message" && payload.role === "assistant" && Boolean(payload.text);
  });
  const completed = ordered.some((event) => event.kind === "run.completed" || event.kind === "session.completed");
  const artifacts = ordered.flatMap((event): RunActivityArtifact[] => {
    if (!event.kind.startsWith("artifact.")) return [];
    const artifact = readPayload(event).artifact;
    if (!artifact) return [];
    return [{
      id: artifact.id ?? event.id,
      linkable: Boolean(artifact.id),
      name: artifact.name,
      mediaType: artifact.mediaType,
      version: artifact.version,
    }];
  }).filter((artifact, index, all) => all.findIndex((candidate) => candidate.id === artifact.id) === index);
  // run 级的失败横幅只认**真失败**。框架驳回一次、循环下一轮改对了，那不是
  // 这条 run 的结论（wangd 2026-08-21：「这些根本没必要显示成错误吧」）。
  const primaryFailure = [...tools].reverse().find((tool) => tool.error && !tool.declined)?.error;

  const narration: RunActivityNarration[] = ordered
    .filter((event) => event.kind === "agent.message")
    .map((event) => {
      const raw = (event.payload ?? {}) as Record<string, unknown>;
      return {
        id: event.id,
        stepId: typeof raw.stepId === "string" ? raw.stepId : undefined,
        runId: event.runId,
        sequence: event.sequence,
        turn: typeof raw.turn === "number" ? raw.turn : 0,
        text: String(raw.text ?? ""),
        previewOnly: raw.previewOnly === true,
      };
    })
    .filter((item) => item.text.trim().length > 0);

  const workspaceChanges: RunActivityWorkspaceChange[] = ordered
    .filter((event) => event.kind === "workspace.changed")
    .map((event) => {
      const raw = (event.payload ?? {}) as Record<string, unknown>;
      const files = Array.isArray(raw.files)
        ? raw.files.flatMap((entry) => {
            const item = record(entry);
            const path = typeof item?.path === "string" ? item.path : "";
            if (!path) return [];
            return [{
              path,
              additions: typeof item?.additions === "number" ? item.additions : undefined,
              deletions: typeof item?.deletions === "number" ? item.deletions : undefined,
              status: typeof item?.status === "string" ? item.status : undefined,
            }];
          })
        : [];
      const patch = typeof raw.patch === "string" && raw.patch.trim() ? raw.patch : undefined;
      return {
        id: event.id,
        runId: event.runId,
        sequence: event.sequence,
        tool: String(raw.tool ?? ""),
        filesChanged: typeof raw.filesChanged === "number" ? raw.filesChanged : files.length,
        additions: typeof raw.additions === "number" ? raw.additions : 0,
        deletions: typeof raw.deletions === "number" ? raw.deletions : 0,
        files,
        patch,
        patchTruncated: raw.patchTruncated === true,
        internalOnly: raw.internalOnly === true,
      };
    })
    // 纯平台内务的改动不上时间线（上下文压缩审计、run manifest…）。判据是
    // 采集端算好的 `internalOnly`，前端不写第二份名单。实测一个会话里
    // compression_log 被改了 16 次，每次都占一张「Edited N files」卡，
    // 用户问"这个是干嘛的"—— 它确实是真实改动，但对"研究做了什么"零信息。
    // 老事件没有这个字段 → 照旧显示（不追溯猜测）。
    .filter((change) => !change.internalOnly);

  const said: RunActivitySaid[] = ordered
    .filter((event) => event.kind === "orchestrator.said"
      || event.kind === "interrupt.acknowledged"
      || event.kind === "interject.queued")
    .map((event): RunActivitySaid => {
      const raw = (event.payload ?? {}) as Record<string, unknown>;
      if (event.kind === "interject.queued") {
        // 平台说「我收下了」。**留痕**而不只是流内提示：断流、刷新、换设备
        // 之后，"人说了什么"和"平台答了什么"都还在（RFC P1）。
        //
        // 与下面的 `interrupt.acknowledged`（worker 说「我读到了」）是两件事：
        // 收下 ≠ 被读到。合并的话，"入队黑洞"就再也看不出来了。
        const occupied = raw.occupancy === "occupied";
        const live = raw.delivery === "delivered_to_live_runtime";
        return {
          id: event.id,
          runId: event.runId,
          sequence: event.sequence,
          text: live
            ? say({ zh: "已收下 —— 它会在下一个边界读到这句话", en: "Received — it will be read at the next boundary" }, lang)
            : occupied
              ? say({ zh: "已收下并排队 —— 上一轮还占着这个会话", en: "Received and queued — the previous turn still holds this Session" }, lang)
              : say({ zh: "已收下 —— 会话续跑时会立即处理", en: "Received — it will be handled as soon as the Session resumes" }, lang),
          aboutNodeType: "",
          repliesToMessageId: String(raw.messageId ?? ""),
          submissionId: String(raw.submissionId ?? "") || undefined,
        };
      }
      if (event.kind === "interrupt.acknowledged") {
        // 回执没有正文：它是"已送达"这个事实，措辞归渲染层。
        return {
          id: event.id,
          runId: event.runId,
          sequence: event.sequence,
          text: "",
          aboutNodeType: "",
          repliesToMessageId: String(raw.repliesToMessageId ?? ""),
          submissionId: String(raw.submissionId ?? "") || undefined,
          receipt: { deferred: raw.deferred === true },
        };
      }
      return {
        id: event.id,
        runId: event.runId,
        sequence: event.sequence,
        text: String(raw.text ?? ""),
        aboutNodeType: String(raw.aboutNodeType ?? ""),
        repliesToMessageId: String(raw.repliesToMessageId ?? ""),
        submissionId: String(raw.submissionId ?? "") || undefined,
      };
    })
    .filter((item) => item.receipt !== undefined || item.text.trim().length > 0);

  return {
    steps,
    narration,
    said,
    directModelResponse: tools.length === 0 && hasAssistantResponse,
    toolCount: tools.length,
    currentStatus: currentRunStatus(ordered, completed || parentInterrupted, lang),
    retryCount: ordered.filter((event) =>
      event.kind === "run.retrying" || event.kind === "run.recovering").length,
    artifacts,
    workspaceChanges,
    primaryFailure,
  };
}
