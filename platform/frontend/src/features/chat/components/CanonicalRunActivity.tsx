"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  AlertCircle,
  Check,
  ChevronRight,
  FileText,
  Loader2,
  RotateCw,
} from "lucide-react";
import type { RunDetailResponse } from "@/lib/api";
import {
  isOpenableChange,
  useWorkspaceFileOpener,
} from "@/features/file-preview/components/WorkspaceFileOpener";
import { useInterfaceSettings } from "@/features/settings/InterfaceSettingsProvider";
import { useRunEvents } from "@/features/execution/hooks/useRuns";
import {
  projectRunActivity,
  type RunActivityNarration,
  type RunActivityStep,
  type RunActivityTool,
  type RunActivitySaid,
  type RunActivityWorkspaceChange,
} from "@/features/execution/lib/run-activity-detail";
import {
  buildChildTimeline,
  buildRunTimeline,
  collapseFinishedToolRuns,
  isDispatchLine,
  saidsAnchoredTo,
  withoutMessageAnchoredSaids,
  conversationOnly,
  withoutReplyEcho,
} from "@/features/execution/lib/run-activity-timeline";
import { parseRunLineage } from "@/features/execution/lib/research-map";
import { shouldShowFailureDrawer } from "@/features/execution/lib/failure-surface";
import { GitDiffViewer, type GitDiffStat } from "@/shared/ui";
import { RichText } from "./RichText";
import { canonicalRunAttention, canonicalRunPause } from "../lib/canonical-run-activity";
import { PausedRecord } from "./PausedRecord";
import { useT, useLanguage, type Phrase } from "@/shared/i18n";

const NODE_STATUS_LABELS: Record<RunActivityStep["status"], Phrase> = {
  running: { zh: "进行中", en: "In progress" },
  completed: { zh: "完成", en: "Completed" },
  failed: { zh: "失败", en: "Failed" },
  interrupted: { zh: "已中断", en: "Interrupted" },
};

function toolStatusLabel(tool: RunActivityTool) {
  if (tool.status === "failed") return tool.declined ? "Declined" : "Failed";
  if (tool.status === "completed") return "Completed";
  if (tool.status === "interrupted") return "Interrupted";
  if (tool.status === "retrying") return "Retrying";
  return "Running";
}

function ToolRecord({ tool, trace }: { tool: RunActivityTool; trace: boolean }) {
  const t = useT();
  const { settings } = useInterfaceSettings();
  const shouldOpen = tool.status === "running" || tool.status === "retrying"
    || (tool.status === "completed" && !settings.auto_collapse_completed_tools);
  const [open, setOpen] = useState(shouldOpen);
  useEffect(() => setOpen(shouldOpen), [shouldOpen]);
  return (
    <details
      className={`chat-run-tool state-${tool.declined ? "declined" : tool.status}`}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span className="chat-run-tool-heading">
          <strong>{tool.title}</strong>
          {trace && tool.technicalName && <code>{tool.technicalName}</code>}
          {tool.status !== "completed" && <em>{toolStatusLabel(tool)}</em>}
        </span>
        <ChevronRight className="chat-run-tool-chevron" size={11} />
      </summary>
      <div className="chat-run-tool-body">
        {tool.input && <p><b>{t({ zh: "输入", en: "Prompt" })}</b><span>{tool.input}</span></p>}
        {tool.code && (
          <div className="chat-run-tool-code">
            <b>{t({ zh: "代码", en: "Code" })}</b>
            <pre><code>{tool.code}</code></pre>
          </div>
        )}
        {tool.output && (
          <div className={`chat-run-tool-output ${tool.outputFormat === "terminal" ? "terminal" : "text"}`}>
            <b>{t({ zh: "输出", en: "Completion" })}</b>
            {tool.outputFormat === "terminal"
              ? <pre>{tool.output}</pre>
              : <span>{tool.output}</span>}
          </div>
        )}
        {tool.error && (
          <p className="chat-run-tool-error">
            <b>{tool.error.title}</b>
            <span>{tool.error.message}</span>
            <small>{tool.error.recovery}</small>
          </p>
        )}
        {(tool.retryCount > 0 || tool.notices.length > 0) && (
          <div className="chat-run-tool-notices">
            {tool.retryCount > 0 && <span><RotateCw size={9} /> {tool.retryCount} recorded {tool.retryCount === 1 ? "retry" : "retries"}</span>}
            {tool.notices.map((notice) => <span key={notice}>{notice}</span>)}
          </div>
        )}
      </div>
    </details>
  );
}

/**
 * agent 这一轮写的人话。
 *
 * wangd 试用后的原话：「每一步具体在干啥，我感觉看的一头雾水，它没有告诉这个
 * 用户，我现在干了啥？」—— UI 上原来只有一串工具名（`Search literature —
 * "Christmas pudding"`），而模型第 3 轮就写了"前两轮查得太宽泛，换精准词"。
 * 那句话正好回答"它为什么突然搜圣诞布丁"，只是从来没送到过。
 */
function Narration({ items }: { items: RunActivityNarration[] }) {
  const t = useT();
  if (items.length === 0) return null;
  return (
    <div className="chat-run-narration">
      {items.map((item) => (
        <div key={item.id}>
          {item.turn > 0 && <span className="chat-run-narration-turn">第 {item.turn} 轮</span>}
          <RichText text={item.text} />
          {item.previewOnly && (
            /* 老 transcript 只存了硬截 500 字的预览且不留截断标记 —— 完不完整
               我们不知道，就如实说"预览"，不加假省略号。 */
            <span className="chat-run-narration-preview" title={t({ zh: "来自旧版 transcript 的截断预览，可能不完整", en: "Truncated preview from an older transcript; may be incomplete" })}>{t({ zh: "预览", en: "Preview" })}</span>
          )}
        </div>
      ))}
    </div>
  );
}

const CHANGE_VERBS: Record<string, string> = {
  added: "Created",
  deleted: "Deleted",
  renamed: "Renamed",
};

/**
 * "Edited research_state.py +19 -4" —— 一次工具调用对 Project 文件的改动，
 * 内联在它发生的位置（2026-08-17 用户以 Claude Code 的内联卡为范本点名要的
 * 形态），点开是这一次改动的 diff 正文。
 *
 * 卡片只说"这一次"。它此前报的是整个节点目录对 HEAD 的累计改动 —— 写了 6 个
 * 文件的那一步会显示 "Edited 14 files +1869"，因为上一步的 8 个文件仍然是脏
 * 的、被整个又数了一遍。行数与正文现在同一个口径（采集端 tree↔tree），不会
 * 出现"数字说 14 个文件、正文只有 6 个"这种两份真相。
 */
/**
 * 调度器对用户说的话 —— **对话级分量**，和节点内部独白明确分开。
 *
 * 2026-08-17 实测一个真会话：assistant 消息 0 条，调度器 8 句话和子节点
 * 51 句内部独白用同一个 `Narration` 组件、同一套样式渲染，再加 190 张工具卡
 * —— 用户原话「就没有看到调度器的任何的输出…完全是混乱的」。数据里两者本来
 * 就分得开（runId 不同），只是显示层没把这个区别用出来。
 */
function OrchestratorSaid({ said, repeats, child, onOpen }: {
  said: RunActivitySaid;
  repeats?: number;
  /** 这句话引出的子节点（时间线已折进来）。带上它，话尾那个 `→ x` 就是它的入口。 */
  child?: RunActivityStep;
  onOpen?: (target: { nodeType: string; stepId?: string }) => void;
}) {
  const t = useT();
  // 两种话共用一个事件 kind，但**不是一种东西**：派发旁注（accent 竖线）vs
  // 对插话的答复（对话消息样式）。2026-08-18 用户看着同一条蓝边问"为什么有个
  // 蓝边" —— 答复套旁注的皮，读起来就不像在回答他。
  /**
   * 节点入口**另起一行**放在正文下面（wangd 2026-08-20）：
   *
   *   「感觉这块应该改成调度器正常输出，然后那个就只保留最后的那个节点，
   *     然后一个箭头那里，然后把它放在就是下面，就表示现在正在进行一个
   *     子节点的运算就可以了。」
   *
   * 原来它挂在正文末尾同一行 —— 一句话读到句号，后面还跟着个胶囊，读者得
   * 先判断"这是这句话的一部分吗"。它不是话的一部分，它是这句话引出的**那件
   * 正在跑的事**，所以给它自己一行。
   */
  // 画不画 `→ node` 由**一个**判据说了算（`isDispatchLine`）—— 答复不是派发，
  // 它没有子节点卡可折，画出来就是一段灰字、点不开的假 affordance（#776）。
  const nodeEntry = isDispatchLine(said, child)
    ? (child && onOpen ? (
        <button
          type="button"
          className={`chat-said-node state-${child.status}`}
          // 带上**这一次**派发的 step —— 只给 nodeType 的话，右栏会滚到
          // 最后一张同类卡（同一个节点派三次时，点第一个箭头也跳到第三张）。
          onClick={() => onOpen({ nodeType: said.aboutNodeType!, stepId: child.id })}
          aria-label={t({ zh: `查看 ${said.aboutNodeType} 的运行细节`, en: `See the run detail for ${said.aboutNodeType}` })}
        >
          {/* 静态点，不是转圈 —— 一段里唯一该动的是那条实时状态行。 */}
          {child.status === "running" ? <i className="chat-said-node-dot" aria-hidden /> : null}
          → {said.aboutNodeType}
          <small>
            {child.resumed ? t({ zh: "续跑 · ", en: "Resumed · " }) : ""}
            {t(NODE_STATUS_LABELS[child.status])}
            {child.tools.length > 0
              ? ` · ${child.tools.length} ${child.tools.length === 1 ? "action" : "actions"}`
              : ""}
          </small>
          <ChevronRight size={10} />
        </button>
      ) : (
        /* 子节点还没折进时间线（刚派发那一瞬 / 历史窗口）：同一个形状，
           只是点不动。 */
        <span className="chat-said-node is-static">→ {said.aboutNodeType}</span>
      ))
    : null;
  return (
    <>
      {/* 调度器说的话与 assistant 消息正文是**同一种东西**，只是到达通道不同。
          所以渲染也必须是同一份：`[论文](path)` / `![](图)` 在这里同样要变成
          可点文件 / 内联图（RichText 里那三条安全策略跟着一起生效）。
          从前这里是 `{said.text}` 裸文本 —— PR#642 的能力只接到了 ChatMessages
          一处，调度器在跑动过程中说的每一句话里的链接都是死的
          （wangd 2026-08-25：「那个链接也没法点」）。 */}
      <div className={said.repliesToMessageId ? "chat-interject-answer" : "chat-orchestrator-said"}>
        <RichText text={said.text} />
        {repeats && repeats > 1 ? <em>×{repeats}</em> : null}
      </div>
      {nodeEntry && <p className="chat-said-node-row">{nodeEntry}</p>}
    </>
  );
}

/**
 * 插话处理中 —— 动态 thinking，不是一行写死的状态文案（wangd 2026-08-18：
 * 「改成一个动态的 thinking」）。它仍然只承载机械事实（话已取走、待命轮
 * 在跑），措辞一个字都不冒充模型：模型自己的开场白/答复一到，这个指示器
 * 就让位（见调用处 —— 有 reply 就不渲染 receipt）。
 */
function InterjectThinking() {
  const t = useT();
  return (
    <p className="chat-interject-receipt" role="status" aria-label={t({ zh: "调度器正在处理", en: "The orchestrator is working on it" })}>
      <span className="chat-thinking-dots" aria-hidden="true">
        <i /><i /><i />
      </span>
    </p>
  );
}

/** deferred 变体：没有活跃待命轮在处理，动态 thinking 会撒谎 —— 安静的事实行。 */
function InterjectDeferred() {
  const t = useT();
  return (
    <p className="chat-interject-receipt" role="status">
      <Check size={12} />{t({ zh: "已送达 —— 调度器此刻在自己干活，下一轮开始就会读到这句话", en: "Delivered — the orchestrator is working right now and will read this at the next turn" })}</p>
  );
}

/**
 * 「Ran N commands」—— 跑完的连续动作收成一行，点开看每一条。
 *
 * 只用于**已经跑完**的一批：正在跑的动作各自展开（收束是"这批干完了"的
 * 总结，正在跑的东西没有总结可言）。只有一条时不用这个组件 —— 那条动作
 * 自己的标题比 "Ran 1 commands" 信息多。
 */
function RanCommands({ tools, trace }: { tools: RunActivityTool[]; trace: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="chat-run-commands"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span>Ran {tools.length} commands</span>
        <ChevronRight className="chat-run-tool-chevron" size={11} />
      </summary>
      <div className="chat-run-commands-body">
        {tools.map((tool) => <ToolRecord key={tool.id} tool={tool} trace={trace} />)}
      </div>
    </details>
  );
}

function WorkspaceChangeRecord({ change }: { change: RunActivityWorkspaceChange }) {
  const t = useT();
  const openFile = useWorkspaceFileOpener();
  const counts = (additions?: number, deletions?: number) => (
    <>
      {typeof additions === "number" && <em className="add">+{additions}</em>}
      {typeof deletions === "number" && <em className="del">-{deletions}</em>}
    </>
  );
  /**
   * 文件名从死文本变成可点 —— 这是"节点产出的图/论文在对话里打得开"的那一步。
   *
   * 产出发生在**它发生的位置**已经有一行记录（"Created fig1.png"），此前那行
   * 只是文本：一张刚画出来的图，界面上唯一的痕迹就是这个名字。现在点它就在
   * 右栏画出来。
   */
  const fileName = (path: string, status?: string, display: "basename" | "full" = "basename") => {
    // 单文件那行显示文件名（路径在 title 里），多文件清单显示完整路径 ——
    // 清单里一堆同名的 `main.tex` 分不出是哪个节点的。
    const label = <code title={path}>{display === "full" ? path : path.split("/").at(-1)}</code>;
    if (!openFile || !isOpenableChange(status)) return label;
    return (
      <button
        type="button"
        className="chat-run-file-open"
        onClick={(event) => {
          // details/summary 里：不拦的话点文件名会顺带把折叠块开合一次。
          event.preventDefault();
          event.stopPropagation();
          openFile(path);
        }}
        title={t({ zh: `在右栏打开 ${path}`, en: `Open ${path} in the side panel` })}
      >
        {label}
      </button>
    );
  };
  const single = change.files.length === 1 ? change.files[0] : undefined;
  const headline = single ? (
    <span>
      {CHANGE_VERBS[single.status ?? ""] ?? "Edited"}{" "}
      {fileName(single.path, single.status)}
      {counts(single.additions ?? change.additions, single.deletions ?? change.deletions)}
    </span>
  ) : (
    <span>
      Edited {change.filesChanged} files
      {counts(change.additions, change.deletions)}
    </span>
  );

  if (!change.patch) {
    // 这条事件是修复前落的库，没有正文。多文件仍可展开看文件清单（它有的
    // 全部），单文件就是一行 —— 绝不画一个点开是空的箭头。
    if (!single) {
      return (
        <details className="chat-run-workspace-change">
          <summary>{headline}<ChevronRight className="chat-run-tool-chevron" size={11} /></summary>
          <ul>
            {change.files.map((file) => (
              <li key={file.path}>
                {fileName(file.path, file.status, "full")}
                {counts(file.additions, file.deletions)}
              </li>
            ))}
          </ul>
        </details>
      );
    }
    return <div className="chat-run-workspace-change">{headline}</div>;
  }

  const stats: Record<string, GitDiffStat> = Object.fromEntries(
    change.files.map((file) => [
      file.path,
      { additions: file.additions, deletions: file.deletions, status: file.status },
    ]),
  );
  return (
    <details className="chat-run-workspace-change has-diff">
      <summary>{headline}<ChevronRight className="chat-run-tool-chevron" size={11} /></summary>
      <GitDiffViewer patch={change.patch} truncated={change.patchTruncated} stats={stats} compact />
    </details>
  );
}

export function ToolGroup({ step, trace, narration = [], workspaceChanges = [] }: { step: RunActivityStep; trace: boolean; narration?: RunActivityNarration[]; workspaceChanges?: RunActivityWorkspaceChange[] }) {
  const t = useT();
  const { settings } = useInterfaceSettings();
  // 在跑的子节点**也默认收起**（2026-08-17）。原来 running 强制展开，本意是
  // "让人看见进展"，实际后果是长任务期间满屏都是节点内部独白 —— 实测一次
  // run 51 条子节点叙述 + 190 张工具卡全部摊开，把调度器那 8 句话淹没。
  // 进展该由 summary 那一行的 live 状态承担（下面 status + 动作计数），
  // 展开是**用户主动要看细节**时才发生的事。
  const shouldOpen = step.status === "completed" && !settings.auto_collapse_completed_steps;
  const [open, setOpen] = useState(shouldOpen);
  useEffect(() => setOpen(shouldOpen), [shouldOpen]);
  const recordedTitle = step.title && !step.title.toLowerCase().endsWith(" activity")
    ? step.title
    : undefined;
  const title = recordedTitle
    ?? (step.kind === "child"
      ? t({ zh: "一步研究", en: "Research step" })
      : "Research activity");
  // 组内也按发生顺序交错：这句话解释的就是紧随其后的那次调用，隔开就白说了。
  const timeline = buildChildTimeline(step, narration, workspaceChanges);
  return (
    <details
      className={`chat-run-tool-group state-${step.status}`}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span className="chat-run-tool-group-heading">
          {step.status === "running" && <Loader2 className="spin" size={11} />}
          <strong>{title}</strong>
          <small>
            {/* 「完成 · Interrupted」自相矛盾 —— 被打断就是被打断，用同一张
                状态词表说话（2026-08-18 实测右栏显示成「完成 · Interrupted」）。*/}
            {t(NODE_STATUS_LABELS[step.status])}
            {` · ${step.tools.length} ${step.tools.length === 1 ? "action" : "actions"}`}
          </small>
        </span>
        <ChevronRight className="chat-run-tool-chevron" size={11} />
      </summary>
      <div className="chat-run-tool-group-body">
        {step.summary && <p>{step.summary}</p>}
        {timeline.map((item) => {
          if (item.kind === "narration") return <Narration key={item.narration.id} items={[item.narration]} />;
          if (item.kind === "workspace") return <WorkspaceChangeRecord key={item.change.id} change={item.change} />;
          return <ToolRecord key={item.tool.id} tool={item.tool} trace={trace} />;
        })}
      </div>
    </details>
  );
}

function StepSummaryRecord({ step }: { step: RunActivityStep }) {
  const t = useT();
  const recordedTitle = step.title && !step.title.toLowerCase().endsWith(" activity")
    ? step.title
    : undefined;
  return (
    <div className={`chat-run-step-summary state-${step.status}`}>
      {step.status === "running" && <Loader2 className="spin" size={10} />}
      {step.status === "failed" && <AlertCircle size={10} />}
      <span>
        <strong>{recordedTitle ?? (step.kind === "child" ? t({ zh: "一步研究", en: "Research step" }) : t({ zh: "研究活动", en: "Research activity" }))}</strong>
        <small>{step.summary ?? `${step.tools.length} recorded ${step.tools.length === 1 ? "action" : "actions"}${step.status === "completed" ? " completed" : ""}`}</small>
      </span>
    </div>
  );
}

/**
 * 「技术细节」只能有一个。
 *
 * 2026-08-12 实测：run 级失败的原文单独开了一个 `<details>`，紧挨着这个本来就
 * 叫「技术细节」的折叠区 —— 页面上**连着两个同名的抽屉**，用户得两个
 * 都点开才知道哪个装着自己要的东西。
 *
 * 出错时"细节在哪"必须只有一个答案：run 级的原因排最前（它是这次失败的**根**），
 * 失败的动作跟在后面（那是过程）。
 */
function FailedToolHistory({
  tools,
  steps,
  failure,
}: {
  tools: RunActivityTool[];
  steps: RunActivityStep[];
  failure?: { detail?: string; reference?: string };
}) {
  const t = useT();
  const count = tools.length + steps.length;
  if (count === 0 && !failure?.detail) return null;
  const label = count > 0
    ? `${count} failed ${count === 1 ? "action" : "actions"}`
    : "run failure";
  return (
    <details className="chat-run-failure-history">
      <summary>
        <span>{t({ zh: "技术细节", en: "Technical details" })}<small>{label}</small></span>
        <ChevronRight className="chat-run-tool-chevron" size={11} />
      </summary>
      <div>
        {failure?.detail && (
          <article className="chat-run-failure-raw">
            <strong>{t({ zh: "这一轮失败了", en: "Run failure" })}</strong>
            <pre>{failure.detail}</pre>
            {failure.reference && <small>Reference: <code>{failure.reference}</code></small>}
          </article>
        )}
        {tools.map((tool) => (
          <article key={tool.id}>
            <strong>{tool.title}</strong>
            {tool.error && <><p>{tool.error.message}</p><small>{tool.error.recovery}</small></>}
          </article>
        ))}
        {steps.map((step) => (
          <article key={step.id}>
            <strong>{step.title ?? t({ zh: "一步研究", en: "Research step" })}</strong>
            <p>{t({ zh: "这一步还没产出可用结果就结束了。", en: "This research step ended before producing a usable result." })}</p>
          </article>
        ))}
        <a href="?view=execution">{t({ zh: "去执行历史看原始记录", en: "Open Execution history for the raw trace" })}</a>
      </div>
    </details>
  );
}

/**
 * 子节点在对话主线上的形态：**一张紧凑状态卡，不再内联展开**。
 *
 * wangd 2026-08-17：「左边也显示子节点的内容，右边也显示子节点内容……左边
 * 就是聊天，然后一些状态栏，右边就是这个子节点的细节。」同一份细节两处
 * 展开就是重复；主线只回答"谁在跑、到哪了"，细节归右栏检查器。
 */
function NodeStatusCard({ step, onOpen }: {
  step: RunActivityStep;
  onOpen?: (target: { nodeType: string; stepId?: string }) => void;
}) {
  const t = useT();
  const nodeType = (step.runId && parseRunLineage(step.runId)?.nodeType) || step.title || "research";
  const openThisRun = onOpen ? () => onOpen({ nodeType, stepId: step.id }) : undefined;
  const body = (
    <>
      {step.status === "running" ? <Loader2 className="spin" size={11} /> : step.status === "failed" ? <AlertCircle size={11} /> : null}
      <strong>{step.title ?? nodeType}</strong>
      <small>
        {/* 续跑：如实说它接着上次跑，别让人以为又新开了一条。 */}
        {step.resumed ? t({ zh: "续跑 · ", en: "Resumed · " }) : ""}
        {t(NODE_STATUS_LABELS[step.status])}
        {step.tools.length > 0 ? ` · ${step.tools.length} ${step.tools.length === 1 ? "action" : "actions"}` : ""}
      </small>
      {onOpen && <ChevronRight className="chat-run-tool-chevron" size={11} />}
    </>
  );
  if (!onOpen) {
    return <div className={`chat-run-node-card state-${step.status}`}>{body}</div>;
  }
  return (
    <button
      type="button"
      className={`chat-run-node-card state-${step.status}`}
      onClick={openThisRun}
      aria-label={t({ zh: `查看 ${nodeType} 的运行细节`, en: `See the run detail for ${nodeType}` })}
    >
      {body}
    </button>
  );
}

export function CanonicalRunActivity({
  detail,
  loading,
  error,
  submitting = false,
  showStreamingDraft = false,
  sequenceWindow,
  interjectionMessageId,
  turnReplyText,
  onOpenNodeDetail,
}: {
  detail?: RunDetailResponse;
  loading: boolean;
  error?: unknown;
  submitting?: boolean;
  showStreamingDraft?: boolean;
  /**
   * 只渲染 (start, end) sequence 窗口内的活动段。同一个 run 会以多个窗口实例
   * 出现在它经过的每条消息下面（时间穿插），end=null 的尾窗实例负责渲染
   * run 级的"外壳"（pause/产物/失败横幅/状态行）。不传 = 整个 run 一个实例
   * （旧行为，standalone 恢复场景仍用它）。
   */
  sequenceWindow?: { start: number; end: number | null };
  /**
   * 插话锚定模式：只渲染回答这条消息的回执 + 待命轮答复，别的什么都不画
   * （活动照旧锚在开启这一轮的那条消息下面）。没有它，答复挂进 run 的活动
   * 窗口，渲染在提问**上面**（2026-08-18 实测）。
   */
  interjectionMessageId?: string;
  /**
   * 这一轮的回复正文（挂载消息的 text）。调度器收尾那段话走两个通道送达：
   * 回复契约 → 消息正文；transcript 摄取 → agent.message 叙述事件。单轮 turn
   * 里两者一字不差 —— 传进来让时间线滤掉那条回声（见 withoutReplyEcho）。
   */
  turnReplyText?: string;
  /** 点子节点状态卡时打开右栏检查器并定位到该节点。不传则卡片纯展示。 */
  onOpenNodeDetail?: (target: { nodeType: string; stepId?: string }) => void;
}) {
  const t = useT();
  const lang = useLanguage();
  const { settings } = useInterfaceSettings();
  const eventsQuery = useRunEvents(
    detail?.run.sessionId ?? "",
    detail?.run.projectId ?? "",
    detail?.run.id ?? "",
    Boolean(detail && detail.run.view.phase === "alive"),
    Boolean(detail),
  );
  const activity = useMemo(
    () => eventsQuery.data ? projectRunActivity(eventsQuery.data, detail ? (detail.run.view.phase === "interrupted" || detail.run.view.outcome === "failed" || detail.run.view.outcome === "cancelled") : undefined, lang) : undefined,
    [eventsQuery.data, detail?.run.view.phase, detail?.run.view.outcome, lang],
  );

  // 锚到这条消息的回执/答复（插话槽位）。判据在 saidsAnchoredTo（纯函数，
  // 拿真实事件页做变异测试）。加载/出错都保持沉默 —— 这个槽位是增强，不该在
  // 每条插话下面各转一个 spinner。
  //
  // #766：插话消息**同时**是 run 的挂载点（它切走自己那段窗口，之后的活动才
  // 渲染在它下面），所以这个块不能再是早返回 —— 它和窗口活动一起画：先答复，
  // 后活动。以前它只在"没有 segment 的用户消息"下面出现，而那种消息不存在。
  const anchored = interjectionMessageId && activity
    ? saidsAnchoredTo(activity.said, interjectionMessageId)
    : undefined;
  const runActive = Boolean(detail && detail.run.view.phase === "alive");
  const anchoredBlock = anchored && (anchored.replies.length || anchored.receipt)
    ? (
      <>
        {anchored.replies.map((item) => <OrchestratorSaid key={item.id} said={item} />)}
        {anchored.receipt?.receipt?.deferred && <InterjectDeferred />}
        {anchored.receipt && !anchored.receipt.receipt?.deferred && runActive && <InterjectThinking />}
      </>
    )
    : null;
  if (interjectionMessageId && !sequenceWindow) {
    // 独立槽位（没有窗口实例）：只画锚到这条消息的内容。
    return anchoredBlock ? <div className="chat-transcript-run">{anchoredBlock}</div> : null;
  }

  if (loading) {
    return (
      <div className="chat-run-canonical-state" aria-live="polite">
        <Loader2 className="spin" size={13} /> {t({ zh: "正在加载执行记录…", en: "Loading execution record…" })}
      </div>
    );
  }
  if (error || !detail) {
    return (
      <div className="chat-run-canonical-state failed" role="alert">
        <AlertCircle size={13} />{t({ zh: "读不到执行记录", en: "Execution record unavailable" })}</div>
    );
  }

  // 「要人管吗」「干净收尾了吗」：读后端那一次现算，不再按状态词猜。
  const view = detail.run.view;
  const attention = view.phase === "interrupted"
    || Boolean(view.waitingOn && view.waitingOn.kind !== "compute")
    || (view.outcome !== null && view.outcome !== "ok");
  const completed = view.outcome === "ok";
  const hasTools = Boolean(activity?.toolCount);
  const retryCount = Math.max(detail.run.retryCount, activity?.retryCount ?? 0);
  // 「N failed actions」只收**真失败**。框架按设计驳回的那些（参数不对、时机
  // 不对、护栏拦下）是 ReAct 循环的正常一步，模型下一轮就改对了 —— 人对此做
  // 不了任何事，摆出来只会让一次正常的研究读起来像出了事故。
  // 判据是 harness 盖的章（errorCode），不是显示层猜的（wangd 2026-08-21）。
  // 它们一条没删：仍在时间线的调用记录里，也仍在 ?view=execution。
  const failedTools = activity?.steps.flatMap((step) =>
    step.tools.filter((tool) => tool.status === "failed" && !tool.declined)) ?? [];
  const failedSteps = activity?.steps.filter((step) => step.status === "failed" && step.tools.length === 0) ?? [];
  const runAttention = canonicalRunAttention(detail, activity?.primaryFailure, lang);
  const showFailureDrawer = shouldShowFailureDrawer({
    view: detail.run.view,
    hasRunFailureDetail: Boolean(runAttention?.detail),
  });
  const persistedPause = canonicalRunPause(detail);
  const hasResults = Boolean(activity?.artifacts.length);

  // 窗口化：本实例只渲染 (start, end) 内的活动段。run 级"外壳"（pause/产物/
  // 失败横幅/状态行）只由尾窗实例（end=null）渲染 —— 它们说的是 run 的现状，
  // 不属于任何一个历史窗口。
  const isTailWindow = !sequenceWindow || sequenceWindow.end === null;
  const inWindow = (sequence: number) =>
    !sequenceWindow
    || (sequence > sequenceWindow.start
      && (sequenceWindow.end === null || sequence < sequenceWindow.end));
  const windowedSteps = (activity?.steps ?? []).filter((step) => inWindow(step.sequence));
  // 锚到某条插话的回执/答复在那条消息的槽位里渲染，不占 run 窗口 —— 判据与
  // 理由在 withoutMessageAnchoredSaids 上（抽成纯函数，才能拿真实事件页做变异测试）。
  const windowedTimeline = activity
    ? collapseFinishedToolRuns(
        withoutMessageAnchoredSaids(
          conversationOnly(
            withoutReplyEcho(buildRunTimeline(activity, detail.run.id), turnReplyText),
          ).filter((item) => inWindow(item.sequence)),
        ),
      )
    : [];

  if (sequenceWindow && !isTailWindow && windowedTimeline.length === 0 && windowedSteps.length === 0
      && !anchoredBlock) {
    return null;
  }
  // eventMismatch / retryCount 不再有对应的可见内容，就别再靠它们把一个
  // 空壳留在页面上。
  if (completed && activity && !hasTools && !hasResults && !anchoredBlock) return null;

  return (
    <div className="chat-transcript-run">
      {anchoredBlock}
      {isTailWindow && eventsQuery.isLoading && (
        <div className="chat-run-detail-state"><Loader2 className="spin" size={11} /> {t({ zh: "正在读已记录的活动…", en: "Reading recorded activity…" })}</div>
      )}
      {isTailWindow && eventsQuery.isError && (
        <div className="chat-run-detail-state failed" role="alert"><AlertCircle size={11} />{t({ zh: "读不到执行细节", en: "Execution details unavailable" })}</div>
      )}
      {/* 等人做决定时，被等的那个东西必须排在最前面。
          原来它跟在全部活动记录之后 —— E2E 实测一次 run 有 75~105 条记录，
          决策面板被顶到视野之外好几屏，正常窗口下用户根本看不到还有得选，
          只看到顶部一个 "Needs input" 就没有下文了。
          活动日志是**上下文**，决策是**动作**；按时间顺序排会让阻塞性动作
          永远排在历史后面，正好是反的。 */}
      {/* 只读记录。**能点的**那张卡不在这里 —— 它由会话级 `answer.via==="pause"`
          唯一决定（见 sessions/lib/answer-affordance.ts）。这里曾经也画一张
          可点的，于是"该不该能点"有两个判据，还得靠一个 `hidePausePrompt`
          把其中一个静音；两个判据分叉时全都不报错。 */}
      {isTailWindow && persistedPause && <PausedRecord pause={persistedPause.pause} />}
      {/* 按发生顺序渲染：一段话，紧跟它引出的那次调用/那个子节点。
          之前叙述全部提到顶上、动作全部折进底下的「Research activity」——
          "它说了要干什么"和"它干了什么"隔了一整屏（wangd 2026-08-13）。 */}
      {activity && settings.execution_detail === "summary" && windowedSteps.flatMap((step) => {
        const visibleTools = step.tools.filter((tool) => tool.status !== "failed" || tool.declined);
        const visibleStep = visibleTools.length === step.tools.length ? step : { ...step, tools: visibleTools };
        if (step.tools.length > 0 && visibleTools.length === 0) return [];
        if (step.status === "failed" && step.tools.length === 0) return [];
        return <StepSummaryRecord key={step.id} step={visibleStep} />;
      })}
      {activity && settings.execution_detail !== "summary" && windowedTimeline.flatMap((item) => {
        if (item.kind === "said") {
          return (
            <OrchestratorSaid
              key={item.said.id}
              said={item.said}
              repeats={item.repeats}
              child={item.child}
              onOpen={onOpenNodeDetail}
            />
          );
        }
        // 左栏 = 对话。哪些算对话见 conversationOnly；这里只管怎么画。
        if (item.kind === "workspace") {
          return <WorkspaceChangeRecord key={item.change.id} change={item.change} />;
        }
        if (item.kind === "narration") {
          // 主线上的独白只有**调度器自己的**（子节点的进它自己的组）。
          // 不带「第 N 轮」徽标：那是循环内部的记账，不是说给人听的。
          return (
            <div key={item.narration.id} className="chat-run-prose">
              <RichText text={item.narration.text} />
            </div>
          );
        }
        if (item.kind === "tools") {
          return (
            <RanCommands
              key={`cmds-${item.tools[0].id}`}
              tools={item.tools}
              trace={settings.execution_detail === "trace"}
            />
          );
        }
        if (item.kind === "tool") {
          // 真失败的细节归失败抽屉，不在主线重复一遍；被框架驳回的留在主线 ——
          // 它就是循环的一步，抽掉之后读者会看到模型莫名其妙换了个做法。
          if (item.tool.status === "failed" && !item.tool.declined) return [];
          return <ToolRecord key={item.tool.id} tool={item.tool} trace={settings.execution_detail === "trace"} />;
        }
        // 主线不再内联展开子节点 —— 细节的家在右栏检查器（点卡片过去）。
        if (item.kind === "child") {
          return <NodeStatusCard key={item.step.id} step={item.step} onOpen={onOpenNodeDetail} />;
        }
        return [];
      })}
      {/* 流式草稿是**最新**的一段话，按时间它排在已经发生的动作之后 ——
          原来它固定在最顶上，越新的内容离视线越远，顺序正好是反的。 */}
      {isTailWindow && showStreamingDraft && eventsQuery.assistantDraft && (
        <p className="chat-run-streaming-response" aria-live="polite">
          {eventsQuery.assistantDraft}
        </p>
      )}
      {isTailWindow && activity?.artifacts.map((artifact) => {
        const content = <><FileText size={11} /><span><strong>{artifact.name}</strong><small>{artifact.mediaType ? artifact.mediaType.replaceAll("_", " ") : t({ zh: "研究产出", en: "Research output" })}{artifact.version !== undefined ? ` · v${artifact.version}` : ""}</small></span></>;
        return artifact.linkable ? (
          <Link
            className="chat-run-result"
            key={artifact.id}
            href={`/projects/${encodeURIComponent(detail.run.projectId)}/artifacts/${encodeURIComponent(artifact.id)}`}
          >
            {content}
          </Link>
        ) : <div className="chat-run-result" key={artifact.id}>{content}</div>;
      })}
      {/* 「接着跑」那一类不是警报：不用 role="alert"、不用红图标、不占三行。
          用户什么都不用做，说一句就够 —— 拿红色报警去说"平台自己抖了一下，
          下一条接着跑"，是在为平台的问题吓用户（wangd 2026-08-18）。 */}
      {isTailWindow && runAttention && !persistedPause && runAttention.tone === "continue" && (
        <p className="chat-run-continue-note" role="status">
          {runAttention.title}
          {runAttention.recovery ? ` —— ${runAttention.recovery}` : ""}
        </p>
      )}
      {isTailWindow && runAttention && !persistedPause && runAttention.tone !== "continue" && (
        <div className={`chat-run-primary-error state-${runAttention.tone}`} role="alert">
          <AlertCircle size={12} />
          <span>
            <strong>{runAttention.title}</strong>
            <small>{runAttention.message}</small>
            {runAttention.recovery && <em>{runAttention.recovery}</em>}
            {/* 只在**真失败**的横幅里出现，而且说成一句话：一个裸计数
                （「1 recorded retry」）回答不了"它试过没有、还有没有救"。 */}
            {retryCount > 0 && <i>平台已自动重试 {retryCount} 次，仍未成功</i>}
            {/* 技术细节不在这里 —— 它和"失败的动作"一起进下面那个唯一的
                「技术细节」抽屉。正文只回答"出了什么事 / 现在能做
                什么"，用户不该被一条 SQL 转储劈头盖脸砸一脸。 */}
          </span>
        </div>
      )}
      {/* 失败抽屉不再常驻：跑着的时候（循环正在自己重试）和跑成功之后
          （那次失败没妨碍结果）都不摆出来。判据见 failure-surface.ts；
          记录一条没删，都在 `?view=execution` 里。 */}
      {isTailWindow && showFailureDrawer && (
        <FailedToolHistory tools={failedTools} steps={failedSteps} failure={runAttention} />
      )}
      {isTailWindow && detail.run.view.outcome === "cancelled" && (
        <div className="chat-transcript-status state-interrupted" role="status">{t({ zh: "这一轮被取消了，没做完的研究活动中断在这里。", en: "This run was cancelled. Unfinished research activity was interrupted." })}</div>
      )}
      {isTailWindow && !completed && !attention && activity?.currentStatus && (
        <div className="chat-transcript-status" role="status" aria-live="polite"><Loader2 className="spin" size={11} /> {activity.currentStatus}</div>
      )}
      {/* 「1 recorded retry」删掉：一个不带上下文的计数，读者做不了任何事。
          正在重试的时候，上面那条实时状态行会说「retry 2/5」（事件本来就带
          attempt/maxAttempts）；重试成功之后它属于执行历史，不属于对话。 */}
      {/* 「Execution record is still reconciling」删掉：这是我们自己的记账
          状态（事件条数和 run 记的对不上），读者既看不懂也做不了什么。 */}
      {isTailWindow && settings.execution_detail === "trace" && activity && (
        <Link className="chat-run-trace-link" href="?view=execution">{t({ zh: "打开原始执行记录", en: "Open raw execution trace" })}</Link>
      )}
    </div>
  );
}
