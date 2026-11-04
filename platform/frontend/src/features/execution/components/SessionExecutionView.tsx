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
import Link from "next/link";
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
import { eventKindLabel, nodeDisplayName } from "../lib/display-names";
import { EXECUTION_DETAIL_LEVELS } from "../lib/execution-detail-levels";
import { useProject } from "@/features/projects/hooks/useProjects";
import { useSession } from "@/features/sessions/hooks/useSessions";
import { useT, useLanguage, say, type Language, type Phrase } from "@/shared/i18n";
import { decisionChoiceText } from "@/features/chat/lib/decision-package";

type SessionExecutionViewProps = {
  projectId: string;
  sessionId: string;
  mode: "api" | "demo";
};

type ApiViewState = "loading" | "online" | "offline" | "stale" | "error";

// 时长单位跟着界面语言：中文「12 秒」「1 分 5 秒」，英文「12s」「1m 5s」。
function formatDuration(milliseconds: number | undefined, lang: Language) {
  if (milliseconds === undefined) return undefined;
  const seconds = Math.max(0, Math.round(milliseconds / 1000));
  if (seconds < 60) return say({ zh: "{s} 秒", en: "{s}s" }, lang, { s: seconds });
  return say({ zh: "{m} 分 {s} 秒", en: "{m}m {s}s" }, lang, { m: Math.floor(seconds / 60), s: seconds % 60 });
}

function formatCost(cost: number, currency: string | undefined, lang: Language) {
  if (!currency) return say({ zh: "费用 {cost}", en: "Cost {cost}" }, lang, { cost: cost.toFixed(4) });
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
    live: { zh: "演示数据播放中", en: "Fixture playing" },
    offline: { zh: "离线", en: "Offline" },
    backfilling: { zh: "正在补全缺失的记录", en: "Filling in missed events" },
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
    online: { zh: "已连接", en: "Connected" },
    offline: { zh: "离线", en: "Offline" },
    stale: { zh: "状态未确认", en: "Status unconfirmed" },
    error: { zh: "无法加载记录", en: "Record unavailable" },
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
  return (
    <div className="execution-density" aria-label={t({ zh: "详细程度", en: "Detail level" })}>
      {EXECUTION_DETAIL_LEVELS.map((item) => (
        <button
          key={item.value}
          type="button"
          aria-pressed={value === item.value}
          className={value === item.value ? "is-active" : undefined}
          onClick={() => onChange(item.value)}
        >
          {t(item.label)}
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
    <section className="fixture-player" aria-label={t({ zh: "演示播放器", en: "Demo player" })}>
      <div className="fixture-player-primary">
        <label>
          <span>{t({ zh: "演示数据", en: "Demo data" })}</span>
          <select
            value={fixtureId}
            onChange={(event) => onFixtureChange(event.target.value as FixtureId)}
          >
            {Object.values(EXECUTION_FIXTURES).map((item) => (
              <option key={item.id} value={item.id}>{t(item.label)}</option>
            ))}
          </select>
        </label>
        <span className="fixture-player-description">{t(fixture.description)}</span>
        <span className="fixture-player-provenance">{t({ zh: "界面演示数据 · 不是真实记录", en: "UI demo data · not a real record" })}</span>
        <span className="fixture-player-sequence">
          {t({ zh: "序号 {last}/{source}/{total}", en: "sequence {last}/{source}/{total}" }, { last: snapshot.lastSequence, source: sourceCount, total: fixture.events.length })}
        </span>
      </div>
      <div className="fixture-player-actions">
        <button type="button" onClick={onTogglePlay}>
          {playing ? <Pause size={14} /> : <Play size={14} />}
          {playing
            ? t({ zh: "暂停", en: "Pause" })
            : sourceCount >= fixture.events.length ? t({ zh: "重新播放", en: "Replay" }) : t({ zh: "播放", en: "Play" })}
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
  const lang = useLanguage();
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
        {tool.durationMs !== undefined && <time>{formatDuration(tool.durationMs, lang)}</time>}
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </button>
      {open && (
        <div className="execution-activity-detail">
          {tool.detail && <p>{tool.detail}</p>}
          {progress?.mode === "elapsed_only" && (
            <p className="execution-progress-copy">
              {t({ zh: "已运行 {elapsed}", en: "Running for {elapsed}" }, { elapsed: formatDuration(progress.elapsedMs, lang) ?? "—" })}
            </p>
          )}
          {progress && progress.mode !== "elapsed_only" && (
            <p className="execution-progress-copy">
              {/* exact=false：这个数是推算出来的。 */}
              {t({ zh: "{completed}/{total} {unit}", en: "{completed} of {total} {unit}" }, { completed: progress.completed, total: progress.total, unit: progress.unit })}
              {!progress.exact && t({ zh: "（估算）", en: " (estimated)" })}
            </p>
          )}
          {tool.outputLines.length > 0 && (
            <ul className="execution-output-lines">
              {tool.outputLines.slice(0, 8).map((line) => <li key={line}>{line}</li>)}
            </ul>
          )}
          {tool.error && (
            <div className="execution-error" role="alert">
              <strong>{tool.error.impact ?? t({ zh: "操作失败", en: "This action did not complete" })}</strong>
              <p>{tool.error.message}</p>
              {tool.error.recovery && <p><b>{t({ zh: "下一步：", en: "Next:" })}</b> {tool.error.recovery}</p>}
              {trace && <p>{t({ zh: "原始记录见下方的事件表。", en: "See the event table below for the raw record." })}</p>}
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
  const lang = useLanguage();
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
        {step.durationMs !== undefined && <time>{formatDuration(step.durationMs, lang)}</time>}
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </button>
      {open && density !== "summary" && (
        <div className="execution-step-detail">
          {step.tools.map((tool) => (
            <ToolRow key={tool.id} tool={tool} trace={density === "trace"} onToggle={onToggle} />
          ))}
          {step.tools.length === 0 && (
            <p className="execution-muted">{t({ zh: "这一步没有调用工具。", en: "No tools were used in this step." })}</p>
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
  const lang = useLanguage();
  const [selected, setSelected] = useState<string | null>(null);
  const decision = payload.decision;
  if (!decision) return null;
  // 选项按 choice id 说人话（与聊天区那张决策卡同一张表）；表里没有的照上游原文。
  const said = (option: (typeof decision.options)[number]) =>
    decisionChoiceText(option.id, { label: option.label, description: option.consequence }, lang);
  const recommended = decision.options.find((option) => option.recommended);

  return (
    <section className="execution-decision">
      <span className="execution-eyebrow">{t({ zh: "需要你做决定", en: "Decision required" })}</span>
      <h2>{payload.title ?? t({ zh: "研究决策", en: "Research decision" })}</h2>
      {decision.reason && <p>{decision.reason}</p>}
      {decision.recommendation && (
        <p><strong>{t({ zh: "推荐：", en: "Recommended:" })}</strong> {recommended ? said(recommended).label : decision.recommendation}</p>
      )}
      <div className="execution-decision-options">
        {decision.options.map((option) => readOnly ? (
          <div
            key={option.id}
            className={cn("execution-decision-option", option.recommended && "is-recommended")}
          >
            <span>{said(option).label}{option.recommended && <small>{t({ zh: "推荐", en: "Recommended" })}</small>}</span>
            {said(option).description && <p>{said(option).description}</p>}
          </div>
        ) : (
          <button
            type="button"
            key={option.id}
            className={cn("execution-decision-option", option.recommended && "is-recommended", selected === option.id && "is-selected")}
            onClick={() => setSelected(option.id)}
          >
            <span>{said(option).label}{option.recommended && <small>{t({ zh: "推荐", en: "Recommended" })}</small>}</span>
            {said(option).description && <p>{said(option).description}</p>}
          </button>
        ))}
      </div>
      {readOnly ? (
        meta && <p className="execution-decision-meta">{meta}</p>
      ) : (
        <p className="execution-fixture-note">
          {selected
            ? t({ zh: "演示模式：你的选择不会提交。", en: "Demo mode: your choice is not submitted." })
            : t({ zh: "演示模式：选择一项查看效果。", en: "Demo mode: choose an option to preview it." })}
        </p>
      )}
    </section>
  );
}

/**
 * 一件成果的结果块。「打开结果」去成果详情页（和对话区那张成果卡同一个地址）；
 * 事件没带成果 id 时没有地方可去，就不画按钮。
 */
function ResultSection({ payload, projectId }: { payload: EventPayloadView; projectId: string }) {
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
      {artifact.id && (
        <div className="execution-result-actions">
          <Link href={`/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifact.id)}`}>
            {t({ zh: "打开结果", en: "Open result" })}
          </Link>
        </div>
      )}
    </section>
  );
}

function TraceTable({ events }: { events: ReconcilerSnapshot["events"] }) {
  const t = useT();
  const lang = useLanguage();
  return (
    <section className="execution-trace">
      <div className="execution-section-label">{t({ zh: "事件记录", en: "Event log" })}</div>
      <div className="execution-trace-table" role="table" aria-label={t({ zh: "事件记录", en: "Execution events" })}>
        {events.map((event) => (
          <details key={event.id} className="execution-trace-row">
            <summary>
              <code>#{event.sequence}</code>
              <strong title={event.kind}>{eventKindLabel(event.kind, lang)}</strong>
              <span>{event.runId ?? t({ zh: "会话", en: "Session" })}</span>
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
  const lang = useLanguage();
  return (
    <details
      className={`execution-child-run execution-child-run-${group.status}`}
      open={group.status === "running"}
    >
      <summary>
        <span className="execution-child-node">{nodeDisplayName(group.nodeType, lang)}</span>
        <span className="execution-child-status">
          {group.status === "running" ? t({ zh: "进行中", en: "In progress" })
            : group.status === "failed" ? t({ zh: "失败", en: "Failed" }) : t({ zh: "完成", en: "Done" })}
        </span>
        <span className="execution-child-count">{t({ zh: "{count} 个操作", en: "{count} action{s}" }, { count: group.events.length, s: group.events.length === 1 ? "" : "s" })}</span>
      </summary>
      <ol className="execution-child-actions">
        {group.events.map((event) => (
          <li key={event.id}>
            {/* 事件类型按表翻成人话（tool.started → 开始使用工具）；原值在 title 里。 */}
            <span className="execution-child-kind" title={event.kind}>{eventKindLabel(event.kind, lang)}</span>
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
  projectId,
  projection,
  childRunGroups,
  density,
  onToggleFold,
}: {
  projectId: string;
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
                <span className="execution-agent-turn">{t({ zh: "第 {turn} 步", en: "Step {turn}" }, { turn: payload.turn })}</span>
              )}
              {payload.text}
              {payload.previewOnly === true && (
                /* 老 transcript 只存了硬截 500 字的 preview，且不留截断标记 ——
                   完不完整我们不知道，就如实说"预览"，不加假省略号。 */
                <span className="execution-agent-preview" title={t({ zh: "只保存了开头部分，内容可能不完整", en: "Only the beginning was saved; it may be incomplete." })}>{t({ zh: "预览", en: "Preview" })}</span>
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
      node: <ResultSection payload={payload} projectId={projectId} />,
    });
  }
  items.sort((a, b) => a.sequence - b.sequence);
  return <>{items.map(({ key, node }) => <Fragment key={key}>{node}</Fragment>)}</>;
}

/**
 * 决策状态的显示名。执行历史页实际只会拿到 pending / partially_approved
 * （读接口只返回还能处理的决策，见 execution-read-client），其余几种照类型补齐。
 */
const DECISION_STATUS_LABELS: Record<DurableDecision["status"], Phrase> = {
  pending: { zh: "待决定", en: "Pending" },
  partially_approved: { zh: "部分已批准", en: "Partially approved" },
  resolved: { zh: "已决定", en: "Resolved" },
  cancelled: { zh: "已取消", en: "Cancelled" },
  superseded: { zh: "已被取代", en: "Superseded" },
  expired: { zh: "已过期", en: "Expired" },
};

/**
 * 页头的用量一栏。拿不到的数写「未知」；费用统计只在不完整时提一句；
 * 还没有任何事件时不说重试次数（下面已经有空状态）。
 */
function ResourceSummary({ eventCount, resources, hasEvents }: {
  eventCount: number;
  resources: ReturnType<typeof summarizeSessionResources>;
  hasEvents: boolean;
}) {
  const t = useT();
  const lang = useLanguage();
  return (
    <>
      <span>{t({ zh: "{n} 条事件", en: "{n} events" }, { n: eventCount })}</span>
      <span>
        {resources.totalTokens === null
          ? t({ zh: "token 数未知", en: "Tokens unavailable" })
          : `${resources.totalTokens.toLocaleString()} tokens`}
      </span>
      <span>
        {resources.cost === null
          ? t({ zh: "费用未知", en: "Cost unavailable" })
          : formatCost(resources.cost, resources.currency, lang)}
      </span>
      {resources.coverage === "partial" && <span>{t({ zh: "费用统计不完整", en: "Cost data incomplete" })}</span>}
      {hasEvents && (
        <span>{t({ zh: "重试 {n} 次", en: "{n} retr{ies}" }, { n: resources.observedRetries, ies: resources.observedRetries === 1 ? "y" : "ies" })}</span>
      )}
    </>
  );
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
      return t({ zh: "你没有权限查看这条执行记录。", en: "You don't have permission to view this execution record." });
    }
    if (error.status === 404) {
      return t({ zh: "当前项目中找不到这条执行记录。", en: "This execution record can't be found in the current Project." });
    }
    return t({ zh: "无法加载执行记录，请稍后重试。", en: "Couldn't load the execution record. Please try again later." });
  }
  if (error instanceof ExecutionStreamInterruptedError) {
    return t({ zh: "服务器正在重启，正在重新连接…", en: "The server is restarting. Reconnecting…" });
  }
  if (error instanceof ExecutionReadContractError) {
    return t({ zh: "当前版本的应用无法显示这条执行记录。", en: "This version of the app can't display this execution record." });
  }
  return t({ zh: "无法连接服务器。", en: "Couldn't connect to the server." });
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
          <span className="execution-breadcrumb">{projectId} / {t({ zh: "研究", en: "Research" })}</span>
          <h1>{t(fixture.label)}</h1>
          <p>{sessionId} · {t({ zh: "本机界面演示数据 · 不接模型", en: "Local UI demo fixture · model unavailable" })}</p>
        </div>
        <div className="execution-session-status">
          <ConnectionStatus snapshot={snapshot} />
          <ResourceSummary eventCount={snapshot.lastSequence} resources={resources} hasEvents />
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
          projectId={projectId}
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
            <p>{t({ zh: "点击播放开始演示。", en: "Press Play to start the demo." })}</p>
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
  // 页头说「哪个项目、哪个会话」用名字，不用 UUID。拿不到就不写，只留「执行历史」标题。
  const projectName = useProject(projectId).data?.name;
  const sessionTitle = useSession(projectId, sessionId).data?.title;

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

  // 中文句子之间不加空格，英文加一个。
  const join = (...parts: (string | null)[]) => parts.filter(Boolean).join(lang === "zh" ? "" : " ");
  const notice = apiState === "loading"
    ? t({ zh: "正在加载执行记录…", en: "Loading the execution record…" })
    : apiState === "offline"
      ? join(
        apiProblem ?? t({ zh: "无法加载执行记录。", en: "Couldn't load the execution record." }),
        t({ zh: "请刷新页面后重试。", en: "Refresh the page and try again." }),
      )
      : apiState === "stale"
        ? join(
          t({ zh: "无法获取最新的执行状态。", en: "Couldn't get the latest execution status." }),
          apiProblem,
          t({ zh: "请刷新页面。", en: "Refresh the page." }),
        )
        : apiState === "error"
          ? apiProblem ?? t({ zh: "无法显示这条执行记录。", en: "The execution record could not be displayed." })
          : null;
  // 决策的最新状态没确认：只在**有决策**的时候才值得说，而且单独成句。
  const decisionsStale = !decisionsVerified && decisions.length > 0 && apiState !== "loading";

  return (
    <div className="execution-page">
      <header className="execution-session-header">
        <div>
          {projectName && <span className="execution-breadcrumb">{projectName}</span>}
          <h1>{t({ zh: "执行历史", en: "Execution history" })}</h1>
          {sessionTitle && <p>{sessionTitle}</p>}
        </div>
        <div className="execution-session-status">
          <ApiConnectionStatus state={apiState} />
          <ResourceSummary
            eventCount={snapshot.lastSequence}
            resources={resources}
            hasEvents={snapshot.events.length > 0}
          />
          <DensityControl value={density} onChange={setDensity} />
        </div>
      </header>

      {(notice || decisionsStale) && (
        <div
          className={cn("execution-api-notice", `execution-api-notice-${apiState}`)}
          role={apiState === "loading" ? "status" : "alert"}
        >
          <AlertCircle size={14} aria-hidden="true" />
          <span>
            {join(
              notice,
              decisionsStale
                ? t({ zh: "无法确认决策的最新状态，下面显示的内容可能已过期。", en: "Couldn't confirm the latest Decision state; what's shown may be out of date." })
                : null,
            )}
          </span>
        </div>
      )}

      <main className="execution-document">
        <ExecutionEventStream
          projectId={projectId}
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
            meta={t({ zh: "{status} · 已批准 {n}/{m}", en: "{status} · {n}/{m} approved" }, {
              status: t(DECISION_STATUS_LABELS[decision.status]),
              n: decision.acceptedResponseCount,
              m: decision.authority.requiredApprovalCount,
            })}
          />
        ))}


        {density === "trace" && <TraceTable events={snapshot.events} />}

        {snapshot.events.length === 0 && decisions.length === 0 && (
          <div className="execution-empty">
            <Search size={18} />
            <p>{apiState === "loading"
              ? t({ zh: "正在加载…", en: "Loading…" })
              : t({ zh: "还没有执行记录。", en: "No execution records yet." })}</p>
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
