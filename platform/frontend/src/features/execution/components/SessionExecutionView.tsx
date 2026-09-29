"use client";

import {
  AlertCircle,
  Check,
  ChevronDown,
  ChevronRight,
  Circle,
  FileText,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  Search,
  Sparkles,
  Unplug,
} from "lucide-react";
import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { cn } from "@/shared/ui";
import { useInterfaceSettings } from "@/features/settings/InterfaceSettingsProvider";
import { API_BASE_URL, api } from "@/lib/api";
import {
  EXECUTION_FIXTURES,
  type ExecutionFixture,
  type FixtureId,
} from "../fixtures";
import {
  projectExecution,
  type Density,
  type SessionProjection,
  type StepProjection,
  type ToolProjection,
} from "../lib/execution-projection";
import {
  LiveEventReconciler,
  type ReconcilerSnapshot,
} from "../lib/live-event-reconciler";
import {
  createExecutionReadClient,
  ExecutionReadApiError,
  ExecutionReadContractError,
  ExecutionStreamInterruptedError,
  type DurableDecision,
} from "../lib/execution-read-client";
import {
  ExecutionSessionReader,
  startExecutionSessionPolling,
} from "../lib/execution-session-reader";
import {
  summarizeSessionResources,
  type EventPayloadView,
} from "../lib/payload-view";
import { groupByChildRun, type ChildRunGroup } from "../lib/child-run-grouping";
import { useT, useLanguage, type Phrase } from "@/shared/i18n";

type SessionExecutionViewProps = {
  projectId: string;
  sessionId: string;
  mode: "api" | "demo";
};

type ApiViewState = "loading" | "online" | "offline" | "stale" | "error";

function formatDuration(milliseconds?: number) {
  if (milliseconds === undefined) return undefined;
  const seconds = Math.max(0, Math.round(milliseconds / 1000));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function formatCost(cost: number, currency?: string) {
  if (!currency) return `${cost.toFixed(4)} cost`;
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency,
      maximumFractionDigits: 4,
    }).format(cost);
  } catch {
    return `${cost.toFixed(4)} ${currency}`;
  }
}

function initializeReconciler(fixture: ExecutionFixture) {
  const reconciler = new LiveEventReconciler();
  reconciler.accept(fixture.events);
  return reconciler;
}

function ConnectionStatus({ snapshot }: { snapshot: ReconcilerSnapshot }) {
  const t = useT();
  const labels: Record<ReconcilerSnapshot["phase"], Phrase> = {
    idle: { zh: "演示数据就绪", en: "Fixture ready" },
    live: { zh: "演示数据在跑", en: "Fixture live" },
    offline: { zh: "离线", en: "Offline" },
    backfilling: { zh: "正在补断档", en: "Backfilling gap" },
    reconnecting: { zh: "正在重连", en: "Reconnecting" },
    error: { zh: "连接出错", en: "Connection error" },
  };

  return (
    <span className={cn("execution-connection", `execution-connection-${snapshot.phase}`)}>
      <span aria-hidden="true" />
      {t(labels[snapshot.phase])}
    </span>
  );
}

function ApiConnectionStatus({ state }: { state: ApiViewState }) {
  const t = useT();
  const labels: Record<ApiViewState, Phrase> = {
    loading: { zh: "正在连接", en: "Connecting" },
    online: { zh: "接口在线", en: "API online" },
    offline: { zh: "离线", en: "Offline" },
    stale: { zh: "状态未确认", en: "Status unconfirmed" },
    error: { zh: "读不到记录", en: "Record unavailable" },
  };

  return (
    <span className={cn("execution-connection", `execution-connection-${state}`)}>
      <span aria-hidden="true" />
      {t(labels[state])}
    </span>
  );
}

function DensityControl({ value, onChange }: { value: Density; onChange: (density: Density) => void }) {
  const t = useT();
  const items: Array<{ value: Density; label: string }> = [
    { value: "summary", label: "Summary" },
    { value: "standard", label: "Standard" },
    { value: "trace", label: "Trace" },
  ];

  return (
    <div className="execution-density" aria-label={t({ zh: "执行细节密度", en: "Execution detail density" })}>
      {items.map((item) => (
        <button
          key={item.value}
          type="button"
          aria-pressed={value === item.value}
          className={value === item.value ? "is-active" : undefined}
          onClick={() => onChange(item.value)}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}

function FixtureControls({
  fixture,
  fixtureId,
  snapshot,
  sourceCount,
  playing,
  lastBackfill,
  onFixtureChange,
  onTogglePlay,
  onReset,
  onSimulateGap,
  onReconnect,
}: {
  fixture: ExecutionFixture;
  fixtureId: FixtureId;
  snapshot: ReconcilerSnapshot;
  sourceCount: number;
  playing: boolean;
  lastBackfill: number | null;
  onFixtureChange: (fixtureId: FixtureId) => void;
  onTogglePlay: () => void;
  onReset: () => void;
  onSimulateGap: () => void;
  onReconnect: () => void;
}) {
  const t = useT();
  return (
    <section className="fixture-player" aria-label={t({ zh: "夹具播放器", en: "Fixture player" })}>
      <div className="fixture-player-primary">
        <label>
          <span>{t({ zh: "夹具", en: "Fixture" })}</span>
          <select
            value={fixtureId}
            onChange={(event) => onFixtureChange(event.target.value as FixtureId)}
          >
            {Object.values(EXECUTION_FIXTURES).map((item) => (
              <option key={item.id} value={item.id}>{item.label}</option>
            ))}
          </select>
        </label>
        <span className="fixture-player-description">{fixture.description}</span>
        <span className="fixture-player-provenance">{t({ zh: "界面演示数据 · 不是真实记录", en: "UI demo data · not canonical" })}</span>
        <span className="fixture-player-sequence">
          sequence {snapshot.lastSequence}/{sourceCount}/{fixture.events.length}
        </span>
      </div>
      <div className="fixture-player-actions">
        <button type="button" onClick={onTogglePlay}>
          {playing ? <Pause size={14} /> : <Play size={14} />}
          {playing ? "Pause" : sourceCount >= fixture.events.length ? "Replay" : "Play"}
        </button>
        <button type="button" onClick={onReset}>
          <RotateCcw size={14} />{t({ zh: "重置", en: "Reset" })}</button>
        <button type="button" onClick={onSimulateGap} disabled={sourceCount >= fixture.events.length}>
          <Unplug size={14} />{t({ zh: "模拟断流", en: "Simulate gap" })}</button>
        <button type="button" onClick={onReconnect} disabled={snapshot.phase !== "offline" && snapshot.phase !== "backfilling"}>
          <RefreshCw size={14} />{t({ zh: "重连", en: "Reconnect" })}</button>
        {lastBackfill !== null && (
          <code>GET /events?afterSequence={lastBackfill}</code>
        )}
      </div>
    </section>
  );
}

function ToolStatusIcon({ tool }: { tool: ToolProjection }) {
  if (tool.status === "error") return <AlertCircle size={15} />;
  if (tool.status === "completed") return <Check size={15} />;
  if (tool.status === "retrying") return <RefreshCw size={15} />;
  return <Circle size={13} />;
}

function ToolRow({
  tool,
  trace,
  onToggle,
}: {
  tool: ToolProjection;
  trace: boolean;
  onToggle: (id: string, wasOpen: boolean) => void;
}) {
  const t = useT();
  const open = tool.fold !== "collapsed_auto" && tool.fold !== "manual_closed";
  const progress = tool.progress;

  return (
    <div className={cn("execution-tool", `execution-tool-${tool.status}`, open && "is-open")}>
      <button
        type="button"
        className="execution-activity-row"
        aria-expanded={open}
        onClick={() => onToggle(tool.id, open)}
      >
        <ToolStatusIcon tool={tool} />
        <span className="execution-activity-copy">
          <strong>{open ? tool.title : tool.summary ?? tool.title}</strong>
          {trace && tool.technicalName && <code>{tool.technicalName}</code>}
        </span>
        {tool.durationMs !== undefined && <time>{formatDuration(tool.durationMs)}</time>}
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </button>
      {open && (
        <div className="execution-activity-detail">
          {tool.detail && <p>{tool.detail}</p>}
          {progress?.mode === "elapsed_only" && (
            <p className="execution-progress-copy">
              Running for {formatDuration(progress.elapsedMs)} · total not reported
            </p>
          )}
          {progress && progress.mode !== "elapsed_only" && (
            <p className="execution-progress-copy">
              {progress.completed} of {progress.total} {progress.unit}
              {!progress.exact && " · derived"}
            </p>
          )}
          {tool.outputLines.length > 0 && (
            <ul className="execution-output-lines">
              {tool.outputLines.slice(0, 8).map((line) => <li key={line}>{line}</li>)}
            </ul>
          )}
          {tool.error && (
            <div className="execution-error" role="alert">
              <strong>{tool.error.impact ?? t({ zh: "这个动作没做完", en: "This action did not complete" })}</strong>
              <p>{tool.error.message}</p>
              {tool.error.recovery && <p><b>{t({ zh: "下一步：", en: "Next:" })}</b> {tool.error.recovery}</p>}
              {trace && <p>{t({ zh: "原始事件载荷在下面的「完整」一段里。", en: "Raw event payloads are available in the Trace section below." })}</p>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function StepSection({
  step,
  density,
  onToggle,
}: {
  step: StepProjection;
  density: Density;
  onToggle: (id: string, wasOpen: boolean) => void;
}) {
  const t = useT();
  const open = step.fold !== "collapsed_auto" && step.fold !== "manual_closed";

  return (
    <section className={cn("execution-step", open && "is-open")}>
      <button
        type="button"
        className="execution-step-heading"
        aria-expanded={open}
        onClick={() => onToggle(step.id, open)}
      >
        {step.status === "running" ? <Sparkles size={16} /> : step.status === "error" ? <AlertCircle size={16} /> : <Check size={16} />}
        <span>
          <strong>{step.title}</strong>
          {step.summary && <small>{step.summary}</small>}
        </span>
        {step.durationMs !== undefined && <time>{formatDuration(step.durationMs)}</time>}
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </button>
      {open && density !== "summary" && (
        <div className="execution-step-detail">
          {step.tools.map((tool) => (
            <ToolRow key={tool.id} tool={tool} trace={density === "trace"} onToggle={onToggle} />
          ))}
          {step.tools.length === 0 && (
            <p className="execution-muted">{t({ zh: "这一步没有发出任何工具活动。", en: "No tool activity was emitted for this step." })}</p>
          )}
        </div>
      )}
    </section>
  );
}

function DecisionSection({
  payload,
  readOnly = false,
  meta,
}: {
  payload: EventPayloadView;
  readOnly?: boolean;
  meta?: string;
}) {
  const t = useT();
  const [selected, setSelected] = useState<string | null>(null);
  const decision = payload.decision;
  if (!decision) return null;

  return (
    <section className="execution-decision">
      <span className="execution-eyebrow">{t({ zh: "需要你拍板", en: "Decision required" })}</span>
      <h2>{payload.title ?? t({ zh: "研究方向需要你定", en: "Research direction needs input" })}</h2>
      {decision.reason && <p>{decision.reason}</p>}
      {decision.recommendation && (
        <p><strong>{t({ zh: "推荐：", en: "Recommended:" })}</strong> {decision.recommendation}</p>
      )}
      {decision.evidence && <p className="execution-decision-evidence">Basis: {decision.evidence}</p>}
      <div className="execution-decision-options">
        {decision.options.map((option) => readOnly ? (
          <div
            key={option.id}
            className={cn("execution-decision-option", option.recommended && "is-recommended")}
          >
            <span>{option.label}{option.recommended && <small>{t({ zh: "推荐", en: "Recommended" })}</small>}</span>
            {option.consequence && <p>{option.consequence}</p>}
          </div>
        ) : (
          <button
            type="button"
            key={option.id}
            className={cn("execution-decision-option", option.recommended && "is-recommended", selected === option.id && "is-selected")}
            onClick={() => setSelected(option.id)}
          >
            <span>{option.label}{option.recommended && <small>{t({ zh: "推荐", en: "Recommended" })}</small>}</span>
            {option.consequence && <p>{option.consequence}</p>}
          </button>
        ))}
      </div>
      {readOnly ? (
        meta && <p className="execution-decision-meta">{meta} · Read-only current Decision</p>
      ) : (
        <p className="execution-fixture-note">
          {selected
            ? t({ zh: "演示模式：选择只记在本地，没有发出任何指令。", en: "Fixture selection recorded locally; no command was sent." })
            : t({ zh: "演示模式 · 选一项看看决策状态长什么样。", en: "Fixture mode · choose an option to preview the decision state." })}
        </p>
      )}
    </section>
  );
}

function ResultSection({ payload }: { payload: EventPayloadView }) {
  const t = useT();
  const artifact = payload.artifact;
  if (!artifact) return null;

  return (
    <section className="execution-result">
      <div className="execution-result-heading">
        <FileText size={18} />
        <div>
          <span className="execution-eyebrow">{t({ zh: "结果", en: "Result" })}</span>
          <h2>{artifact.name}</h2>
        </div>
        {artifact.version !== undefined && <span>v{artifact.version}</span>}
      </div>
      {artifact.preview && <p>{artifact.preview}</p>}
      <div className="execution-result-actions">
        <button type="button">{t({ zh: "打开结果", en: "Open result" })}</button>
        <button type="button">{t({ zh: "看出处", en: "View provenance" })}</button>
      </div>
    </section>
  );
}

function TraceTable({ events }: { events: ReconcilerSnapshot["events"] }) {
  const t = useT();
  return (
    <section className="execution-trace">
      <div className="execution-section-label">{t({ zh: "正式事件 · schemaVersion 1", en: "Canonical events · schemaVersion 1" })}</div>
      <div className="execution-trace-table" role="table" aria-label={t({ zh: "权威执行事件", en: "Canonical execution events" })}>
        {events.map((event) => (
          <details key={event.id} className="execution-trace-row">
            <summary>
              <code>#{event.sequence}</code>
              <strong>{event.kind}</strong>
              <span>{event.runId ?? "session"}</span>
              <time>{new Date(event.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time>
            </summary>
            <dl>
              <div><dt>eventId</dt><dd><code>{event.id}</code></dd></div>
              <div><dt>origin</dt><dd>{event.origin}</dd></div>
              <div><dt>visibility</dt><dd>{event.visibility}</dd></div>
              {event.parentRunId && <div><dt>parentRunId</dt><dd><code>{event.parentRunId}</code></dd></div>}
            </dl>
            <pre>{JSON.stringify(event.payload, null, 2)}</pre>
          </details>
        ))}
      </div>
    </section>
  );
}

function ChildRunGroupSection({ group }: { group: ChildRunGroup }) {
  const t = useT();
  return (
    <details
      className={`execution-child-run execution-child-run-${group.status}`}
      open={group.status === "running"}
    >
      <summary>
        <span className="execution-child-node">{group.nodeType}</span>
        <span className="execution-child-status">
          {group.status === "running" ? t({ zh: "进行中", en: "In progress" })
            : group.status === "failed" ? t({ zh: "失败", en: "Failed" }) : t({ zh: "完成", en: "Done" })}
        </span>
        <span className="execution-child-count">{group.events.length} 个动作</span>
      </summary>
      <ol className="execution-child-actions">
        {group.events.map((event) => (
          <li key={event.id}>
            <span className="execution-child-kind">{event.kind}</span>
            {typeof (event.payload as Record<string, unknown>)?.displayName === "string" && (
              <span className="execution-child-detail">
                {String((event.payload as Record<string, unknown>).displayName)}
              </span>
            )}
          </li>
        ))}
      </ol>
    </details>
  );
}

/**
 * 一条按 sequence 归并的执行流：消息、子节点组、顶层 step、结果，全部按
 * **发生顺序**排回一条线。
 *
 * 之前是按**类型**硬分区（user 全部 → assistant 第一句 → agent 叙述一坨 →
 * 子节点组一坨 → ledger → 收尾），于是"literature 完了、调度器说了句话、
 * hypothesis 开始"这条真实时间线在页面上被打散成四个远隔的区块 ——
 * 2026-08-17 用户（对着主会话页同款问题）：「这个显示非常的不合理」。
 * 事件流本来就带 sequence，这里只是把顺序还给渲染层（同
 * run-activity-timeline 的处理；2026-08-11 分区注释里的诉求本来就是
 * "一坨结束→调度器说话→再开新的一坨"，按 sequence 归并才是它的完整形态）。
 */
function ExecutionEventStream({
  projection,
  childRunGroups,
  density,
  onToggleFold,
}: {
  projection: SessionProjection;
  childRunGroups: ChildRunGroup[];
  density: Density;
  onToggleFold: (id: string, wasOpen: boolean) => void;
}) {
  const t = useT();
  const items: { key: string; sequence: number; node: ReactNode }[] = [];
  for (const { event, payload } of projection.messages) {
    if (payload.role === "user") {
      items.push({
        key: event.id,
        sequence: event.sequence,
        node: <div className="execution-user-message">{payload.text}</div>,
      });
    } else if (payload.role === "agent") {
      items.push({
        key: event.id,
        sequence: event.sequence,
        node: (
          <div className="execution-agent-narration">
            <p className="execution-agent-line">
              {typeof payload.turn === "number" && payload.turn > 0 && (
                <span className="execution-agent-turn">第 {payload.turn} 轮</span>
              )}
              {payload.text}
              {payload.previewOnly === true && (
                /* 老 transcript 只存了硬截 500 字的 preview，且不留截断标记 ——
                   完不完整我们不知道，就如实说"预览"，不加假省略号。 */
                <span className="execution-agent-preview" title={t({ zh: "这条来自旧版 transcript 的截断预览，可能不完整", en: "A truncated preview from an older transcript; may be incomplete" })}>{t({ zh: "预览", en: "Preview" })}</span>
              )}
            </p>
          </div>
        ),
      });
    } else if (payload.role === "assistant") {
      items.push({
        key: event.id,
        sequence: event.sequence,
        node: <p className="execution-assistant-copy">{payload.text}</p>,
      });
    } else {
      // 未知 role：如实显示文本，别静默吞掉。
      items.push({
        key: event.id,
        sequence: event.sequence,
        node: <p className="execution-assistant-copy">{payload.text}</p>,
      });
    }
  }
  for (const group of childRunGroups) {
    items.push({
      key: `child-${group.runId}`,
      sequence: group.events[0]?.sequence ?? 0,
      node: <ChildRunGroupSection group={group} />,
    });
  }
  for (const step of projection.steps) {
    items.push({
      key: `step-${step.id}`,
      sequence: step.startedAt,
      node: (
        <div className="execution-ledger">
          <StepSection step={step} density={density} onToggle={onToggleFold} />
        </div>
      ),
    });
  }
  for (const { event, payload } of projection.results) {
    items.push({
      key: `result-${event.id}`,
      sequence: event.sequence,
      node: <ResultSection payload={payload} />,
    });
  }
  items.sort((a, b) => a.sequence - b.sequence);
  return <>{items.map(({ key, node }) => <Fragment key={key}>{node}</Fragment>)}</>;
}

function durableDecisionPayload(decision: DurableDecision): EventPayloadView {
  const recommended = decision.choices.find(
    (choice) => choice.choiceId === decision.recommendedChoiceId,
  );
  return {
    outputLines: [],
    correlationId: decision.id,
    decision: {
      id: decision.id,
      subtype: decision.subtype,
      prompt: decision.prompt,
      reason: decision.prompt,
      recommendation: recommended?.label,
      options: decision.choices.map((choice) => ({
        id: choice.choiceId,
        label: choice.label,
        consequence: choice.consequence ?? choice.description ?? undefined,
        recommended: choice.choiceId === decision.recommendedChoiceId,
      })),
    },
  };
}

function apiStateFromSnapshot(snapshot: ReconcilerSnapshot): ApiViewState {
  for (let index = snapshot.events.length - 1; index >= 0; index -= 1) {
    const kind = snapshot.events[index].kind;
    if (kind === "connection.recovered") return "online";
    if (kind === "connection.lost" || kind === "run.status_unknown") return "stale";
  }
  return "online";
}

// 纯函数收 t，不自己够 hook —— 见 shared/i18n/useT.ts 的「纯函数不许用它」。
function describeApiProblem(error: unknown, t: (phrase: Phrase) => string) {
  if (error instanceof ExecutionReadApiError) {
    if (error.status === 401 || error.status === 403) {
      return t({ zh: "当前账号读不了这条执行记录。", en: "Your current account cannot read this execution record." });
    }
    if (error.status === 404) {
      return t({ zh: "这条执行记录在当前项目里已经没有了。", en: "This execution record is no longer available in the current Project." });
    }
    return t({ zh: "执行服务没能给出一条确认过的记录。", en: "The execution service could not return a confirmed record." });
  }
  if (error instanceof ExecutionStreamInterruptedError) {
    return t({ zh: "执行服务正在重启，正从最后一条确认过的事件接上。", en: "The execution service is restarting. Reconnecting from the last confirmed event." });
  }
  if (error instanceof ExecutionReadContractError) {
    return t({ zh: "执行服务给回的数据，这个版本的应用没法安全地显示。", en: "The execution service returned data that this version of the app cannot safely display." });
  }
  return t({ zh: "连不上执行服务。", en: "The execution service could not be reached." });
}

function FixtureSessionExecutionView({ projectId, sessionId }: Omit<SessionExecutionViewProps, "mode">) {
  const t = useT();
  const lang = useLanguage();
  const { settings: interfaceSettings } = useInterfaceSettings();
  const [fixtureId, setFixtureId] = useState<FixtureId>("active");
  const fixture = EXECUTION_FIXTURES[fixtureId];
  const reconcilerRef = useRef<LiveEventReconciler | null>(null);
  if (!reconcilerRef.current) reconcilerRef.current = initializeReconciler(fixture);

  const [snapshot, setSnapshot] = useState<ReconcilerSnapshot>(() => reconcilerRef.current!.snapshot());
  const [sourceCount, setSourceCount] = useState(fixture.events.length);
  const [playing, setPlaying] = useState(false);
  const [density, setDensity] = useState<Density>(interfaceSettings.execution_detail);
  const [manualOpenIds, setManualOpenIds] = useState<Set<string>>(() => new Set());
  const [manualClosedIds, setManualClosedIds] = useState<Set<string>>(() => new Set());
  const [lastBackfill, setLastBackfill] = useState<number | null>(null);

  useEffect(() => setDensity(interfaceSettings.execution_detail), [interfaceSettings.execution_detail]);

  useEffect(() => {
    if (!playing) return;
    const timer = window.setInterval(() => {
      setSourceCount((current) => {
        if (current >= fixture.events.length) {
          setPlaying(false);
          return current;
        }
        const next = current + 1;
        const reconciler = reconcilerRef.current as LiveEventReconciler;
        if (reconciler.snapshot().phase !== "offline") {
          reconciler.accept([fixture.events[current]]);
          setSnapshot(reconciler.snapshot());
        }
        return next;
      });
    }, 720);
    return () => window.clearInterval(timer);
  }, [fixture, playing]);

  const projection = useMemo(
    () => projectExecution(snapshot.events, {
      manualOpenIds,
      manualClosedIds,
      autoCollapseCompletedTools: interfaceSettings.auto_collapse_completed_tools,
      autoCollapseCompletedSteps: interfaceSettings.auto_collapse_completed_steps,
      lang,
    }),
    [interfaceSettings.auto_collapse_completed_steps, interfaceSettings.auto_collapse_completed_tools, lang, manualClosedIds, manualOpenIds, snapshot.events],
  );
  // 子节点各成一组：跑完收起、还在跑的展开。分组键是后端修好的
  // run_id / parent_run_id 归属；组在流里的位置 = 它第一个事件的 sequence。
  const childRunGroups = groupByChildRun(snapshot.events);
  const resources = useMemo(
    () => summarizeSessionResources(snapshot.events),
    [snapshot.events],
  );

  function changeFixture(nextFixtureId: FixtureId) {
    const nextFixture = EXECUTION_FIXTURES[nextFixtureId];
    const reconciler = reconcilerRef.current as LiveEventReconciler;
    reconciler.reset();
    reconciler.accept(nextFixture.events);
    setFixtureId(nextFixtureId);
    setSourceCount(nextFixture.events.length);
    setSnapshot(reconciler.snapshot());
    setPlaying(false);
    setManualOpenIds(new Set());
    setManualClosedIds(new Set());
    setLastBackfill(null);
  }

  function resetPlayer() {
    const reconciler = reconcilerRef.current as LiveEventReconciler;
    reconciler.reset();
    reconciler.accept(fixture.events.slice(0, 1));
    setSnapshot(reconciler.snapshot());
    setSourceCount(1);
    setPlaying(false);
    setManualOpenIds(new Set());
    setManualClosedIds(new Set());
    setLastBackfill(null);
  }

  function togglePlayback() {
    if (playing) {
      setPlaying(false);
      return;
    }
    if (sourceCount >= fixture.events.length) {
      const reconciler = reconcilerRef.current as LiveEventReconciler;
      reconciler.reset();
      reconciler.accept(fixture.events.slice(0, 1));
      setSnapshot(reconciler.snapshot());
      setSourceCount(1);
      setManualOpenIds(new Set());
      setManualClosedIds(new Set());
    }
    setPlaying(true);
  }

  function simulateGap() {
    const reconciler = reconcilerRef.current as LiveEventReconciler;
    reconciler.setOffline();
    setSnapshot(reconciler.snapshot());
    setSourceCount((current) => Math.min(fixture.events.length, current + 2));
    setPlaying(false);
  }

  async function reconnect() {
    const reconciler = reconcilerRef.current as LiveEventReconciler;
    const pending = reconciler.reconnect(async (afterSequence) => {
      setLastBackfill(afterSequence);
      const items = fixture.events.filter(
        (event) => event.sequence > afterSequence && event.sequence <= sourceCount,
      );
      return {
        items,
        afterSequence,
        nextAfterSequence: items.at(-1)?.sequence ?? afterSequence,
        hasMore: false,
      };
    });
    setSnapshot(reconciler.snapshot());
    setSnapshot(await pending);
  }

  function toggleFold(id: string, wasOpen: boolean) {
    setManualOpenIds((current) => {
      const next = new Set(current);
      if (wasOpen) next.delete(id);
      else next.add(id);
      return next;
    });
    setManualClosedIds((current) => {
      const next = new Set(current);
      if (wasOpen) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  return (
    <div className="execution-page">
      <header className="execution-session-header">
        <div>
          <span className="execution-breadcrumb">{projectId} / Research</span>
          <h1>{fixture.label}</h1>
          <p>{sessionId} · Local UI demo fixture · model unavailable</p>
        </div>
        <div className="execution-session-status">
          <ConnectionStatus snapshot={snapshot} />
          <span>{snapshot.lastSequence} events</span>
          <span>
            {resources.totalTokens === null
              ? t({ zh: "读不到 token 数", en: "Tokens unavailable" })
              : `${resources.totalTokens.toLocaleString()} tokens`}
          </span>
          <span>
            {resources.cost === null
              ? t({ zh: "读不到花费", en: "Cost unavailable" })
              : formatCost(resources.cost, resources.currency)}
          </span>
          {resources.coverage && <span>{t({ zh: "花费覆盖 {coverage}", en: "{coverage} usage coverage" }, { coverage: resources.coverage })}</span>}
          <span>{resources.observedRetries} observed {resources.observedRetries === 1 ? "retry" : "retries"}</span>
          <DensityControl value={density} onChange={setDensity} />
        </div>
      </header>

      <FixtureControls
        fixture={fixture}
        fixtureId={fixtureId}
        snapshot={snapshot}
        sourceCount={sourceCount}
        playing={playing}
        lastBackfill={lastBackfill}
        onFixtureChange={changeFixture}
        onTogglePlay={togglePlayback}
        onReset={resetPlayer}
        onSimulateGap={simulateGap}
        onReconnect={reconnect}
      />

      <main className="execution-document">
        <ExecutionEventStream
          projection={projection}
          childRunGroups={childRunGroups}
          density={density}
          onToggleFold={toggleFold}
        />

        {/* 决策是**动作**，不是历史：无论发生在流的哪个位置，等人回答的东西
            不能被埋进滚动历史里（同主会话页 pause 前置的理由）。 */}
        {projection.decisions.map(({ event, payload }) => (
          <DecisionSection key={event.id} payload={payload} />
        ))}

        {density === "trace" && <TraceTable events={snapshot.events} />}

        {snapshot.events.length === 0 && (
          <div className="execution-empty">
            <Search size={18} />
            <p>{t({ zh: "重放夹具，开始这份执行记录。", en: "Replay the fixture to begin the execution record." })}</p>
          </div>
        )}
      </main>
    </div>
  );
}

function ApiSessionExecutionView({
  projectId,
  sessionId,
}: Omit<SessionExecutionViewProps, "mode">) {
  const t = useT();
  const lang = useLanguage();
  const { settings: interfaceSettings } = useInterfaceSettings();
  const reader = useMemo(() => new ExecutionSessionReader({
    client: createExecutionReadClient({
      baseUrl: API_BASE_URL,
      fetchImpl: (input, init) => api.fetchWithAuth(input, init),
    }),
    sessionId,
    expectedProjectId: projectId,
  }), [projectId, sessionId]);
  const [snapshot, setSnapshot] = useState<ReconcilerSnapshot>(() => reader.reconciler.snapshot());
  const [decisions, setDecisions] = useState<DurableDecision[]>([]);
  const [decisionsVerified, setDecisionsVerified] = useState(false);
  const [apiState, setApiState] = useState<ApiViewState>("loading");
  const [apiProblem, setApiProblem] = useState<string | null>(null);
  const [density, setDensity] = useState<Density>(interfaceSettings.execution_detail);
  const [manualOpenIds, setManualOpenIds] = useState<Set<string>>(() => new Set());
  const [manualClosedIds, setManualClosedIds] = useState<Set<string>>(() => new Set());
  const hasConfirmedRead = useRef(false);

  useEffect(() => setDensity(interfaceSettings.execution_detail), [interfaceSettings.execution_detail]);

  useEffect(() => startExecutionSessionPolling({
    reader,
    onRead: (read) => {
      hasConfirmedRead.current = true;
      setSnapshot(read.snapshot);
      setDecisions(read.decisions);
      setDecisionsVerified(true);
      setApiProblem(null);
      setApiState(apiStateFromSnapshot(read.snapshot));
    },
    onError: (error) => {
      reader.reconciler.setOffline();
      setSnapshot(reader.reconciler.snapshot());
      setDecisionsVerified(false);
      setApiProblem(describeApiProblem(error, t));
      if (error instanceof ExecutionReadApiError || error instanceof ExecutionReadContractError) {
        setApiState("error");
      } else {
        setApiState(hasConfirmedRead.current ? "stale" : "offline");
      }
    },
  }), [reader]);

  const projection = useMemo(
    () => projectExecution(snapshot.events, {
      manualOpenIds,
      manualClosedIds,
      autoCollapseCompletedTools: interfaceSettings.auto_collapse_completed_tools,
      autoCollapseCompletedSteps: interfaceSettings.auto_collapse_completed_steps,
      lang,
    }),
    [interfaceSettings.auto_collapse_completed_steps, interfaceSettings.auto_collapse_completed_tools, lang, manualClosedIds, manualOpenIds, snapshot.events],
  );
  // 子节点各成一组：跑完收起、还在跑的展开。分组键是后端修好的
  // run_id / parent_run_id 归属；组在流里的位置 = 它第一个事件的 sequence。
  const childRunGroups = groupByChildRun(snapshot.events);
  const resources = useMemo(
    () => summarizeSessionResources(snapshot.events),
    [snapshot.events],
  );

  function toggleFold(id: string, wasOpen: boolean) {
    setManualOpenIds((current) => {
      const next = new Set(current);
      if (wasOpen) next.delete(id);
      else next.add(id);
      return next;
    });
    setManualClosedIds((current) => {
      const next = new Set(current);
      if (wasOpen) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  const notice = apiState === "loading"
    ? t({ zh: "正在加载执行记录和当前的决策。", en: "Loading the execution record and current Decisions." })
    : apiState === "offline"
      ? `${apiProblem ?? t({ zh: "平台没有确认到任何执行数据。", en: "The platform has not confirmed any execution data." })} ${t({ zh: "先刷新再往下操作。", en: "Refresh before taking further action." })}`
      : apiState === "stale"
        ? `${t({ zh: "最新的运行状态没能确认。", en: "The latest run status could not be confirmed." })}${apiProblem ? ` ${apiProblem}` : ""} ${t({ zh: "先刷新再把请求发出去。", en: "Refresh before sending the request again." })}`
        : apiState === "error"
          ? `${apiProblem ?? t({ zh: "这条执行记录显示不出来。", en: "The execution record could not be displayed." })} ${t({ zh: "等服务恢复之后再打开 Trace。", en: "Open Trace only after the service is available again." })}`
          : null;

  return (
    <div className="execution-page">
      <header className="execution-session-header">
        <div>
          <span className="execution-breadcrumb">{projectId} / Research</span>
          <h1>{t({ zh: "会话执行", en: "Session execution" })}</h1>
          <p>{sessionId} · Research App Server</p>
        </div>
        <div className="execution-session-status">
          <ApiConnectionStatus state={apiState} />
          <span>{snapshot.lastSequence} events</span>
          <span>
            {resources.totalTokens === null
              ? t({ zh: "读不到 token 数", en: "Tokens unavailable" })
              : `${resources.totalTokens.toLocaleString()} tokens`}
          </span>
          <span>
            {resources.cost === null
              ? t({ zh: "读不到花费", en: "Cost unavailable" })
              : formatCost(resources.cost, resources.currency)}
          </span>
          <span>{resources.coverage
            ? t({ zh: "花费覆盖 {coverage}", en: "{coverage} usage coverage" }, { coverage: resources.coverage })
            : t({ zh: "读不到花费覆盖", en: "Usage coverage unavailable" })}</span>
          <span>
            {snapshot.events.length === 0
              ? t({ zh: "读不到重试记录", en: "Retry history unavailable" })
              : `${resources.observedRetries} observed ${resources.observedRetries === 1 ? "retry" : "retries"}`}
          </span>
          <DensityControl value={density} onChange={setDensity} />
        </div>
      </header>

      {(notice || !decisionsVerified) && (
        <div
          className={cn("execution-api-notice", `execution-api-notice-${apiState}`)}
          role={apiState === "loading" ? "status" : "alert"}
        >
          <AlertCircle size={14} aria-hidden="true" />
          <span>
            {notice}
            {!decisionsVerified && apiState !== "loading" && (
              <>{t({ zh: "当前决策状态没能核实，留下的决策可能已经过期。", en: "Current Decision state could not be verified; retained Decisions may be stale." })}</>
            )}
          </span>
        </div>
      )}

      <main className="execution-document">
        <ExecutionEventStream
          projection={projection}
          childRunGroups={childRunGroups}
          density={density}
          onToggleFold={toggleFold}
        />

        {decisions.map((decision) => (
          <DecisionSection
            key={decision.id}
            payload={durableDecisionPayload(decision)}
            readOnly
            meta={`${decision.status.replaceAll("_", " ")} · ${decision.acceptedResponseCount}/${decision.authority.requiredApprovalCount} approvals`}
          />
        ))}


        {density === "trace" && <TraceTable events={snapshot.events} />}

        {snapshot.events.length === 0 && decisions.length === 0 && (
          <div className="execution-empty">
            <Search size={18} />
            <p>{apiState === "loading"
              ? t({ zh: "正在连接执行接口…", en: "Connecting to the execution API…" })
              : t({ zh: "没有确认过的执行事件。", en: "No confirmed execution events are available." })}</p>
          </div>
        )}
      </main>
    </div>
  );
}

export function SessionExecutionView({
  projectId,
  sessionId,
  mode,
}: SessionExecutionViewProps) {
  const viewKey = `${projectId}:${sessionId}`;
  if (mode === "demo") {
    return <FixtureSessionExecutionView key={viewKey} projectId={projectId} sessionId={sessionId} />;
  }
  return <ApiSessionExecutionView key={viewKey} projectId={projectId} sessionId={sessionId} />;
}
