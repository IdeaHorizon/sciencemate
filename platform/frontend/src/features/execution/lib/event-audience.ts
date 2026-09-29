import { EXECUTION_EVENT_KINDS, type ExecutionEventKind } from "./execution-event.ts";

/**
 * 每种事件**说给谁听** —— 声明出来的数据，不是各渲染点各猜一遍。
 *
 * ## 为什么要有这层（2026-08-18，一晚栽三次之后）
 *
 * 左栏是对话，右栏是过程。这个划分原来靠每个渲染点自己写判断：
 * 「narration 不显示」「tool 不显示」「said 显示」…… 于是：
 *
 *   · 待命轮的答复被 ingest 映射成 `agent.message`（节点独白），而左栏刚把
 *     独白整体搬去右栏 —— 用户问"现在跑的怎么样了"，调度器实实在在答了三段，
 *     左栏一个字都不显示；
 *   · 派发前那句话曾被按 runId 过滤掉，而子节点组根本不渲染它 —— 整句消失；
 *   · 同一句宣告在闸门前发出，说了 17 遍只做成 1 次。
 *
 * 三次都是同一个形状：**"这条该不该进对话"是从 kind 现推的，而推导散在多处**。
 * 加一个来源、挪一次位置，就有某处的判断是错的，且错了不报错 —— 文字要么
 * 消失要么泛滥，两个方向都很难被发现。
 *
 * 所以受众成为**事件自身的属性**：
 *
 *   · `conversation` —— 人在对话里该看到的：用户的话、调度器说的话、
 *     需要人回答的事、这一轮的结论、对工作区的改动。
 *   · `process` —— 怎么做到的：节点内部独白、每一次工具调用、生命周期、
 *     计量与诊断。右栏和执行历史完整呈现。
 *
 * 判据是**穷尽**的：`EXECUTION_EVENT_KINDS` 里每一种都必须在这张表里，
 * 少一个 `audienceOf` 就抛。新增事件类型时你**必须**回答"这说给谁听"，
 * 而不是默认漏进（或涌入）对话（"护栏要扫盘，不要写名单"）。
 */
export type EventAudience = "conversation" | "process";

const AUDIENCE: Record<ExecutionEventKind, EventAudience> = {
  // ── 对话：人该看到的 ──────────────────────────────────────────────────
  "session.started": "conversation",
  "session.message": "conversation",
  "session.completed": "conversation",
  // 调度器对用户说的话：派发前那句、待命轮的答复。
  "orchestrator.said": "conversation",
  // 插话送达回执 —— 用户刚说了话，回执必须出现在对话里。
  "interrupt.acknowledged": "conversation",
  // 需要人做的事 —— 永远不能被降级成"过程"。
  "decision.required": "conversation",
  "decision.resolved": "conversation",
  "permission.required": "conversation",
  "permission.resolved": "conversation",
  // 对 Project 文件的改动：研究的**后果**，不是过程。
  "workspace.changed": "conversation",
  // 产物是交付物。
  "artifact.created": "conversation",
  "artifact.versioned": "conversation",
  "artifact.frozen": "conversation",

  // ── 过程：怎么做到的 ──────────────────────────────────────────────────
  // 独白与工具调用的受众**取决于是谁的 run**：调度器自己的进对话（它在跟你
  // 干活），子节点内部的进过程。而"是谁的"由 buildRunTimeline 的 run 作用域
  // 已经分好了 —— 到得了主线的只有顶层那条。所以这张表对它们记的是**默认
  // 归属**：不经过 run 作用域时（右栏、执行历史）按过程完整呈现。
  "agent.message": "process",
  "step.started": "process",
  "step.progress": "process",
  "step.completed": "process",
  "step.failed": "process",
  "tool.started": "process",
  "tool.progress": "process",
  "tool.retrying": "process",
  "tool.long_running": "process",
  "tool.completed": "process",
  "tool.failed": "process",
  "task.created": "process",
  "task.started": "process",
  "task.updated": "process",
  "task.completed": "process",
  "task.blocked": "process",
  "review.completed": "process",
  "knowledge.updated": "process",
  "knowledge.conflict_detected": "process",
  "run.queued": "process",
  "run.started": "process",
  "run.resumed": "process",
  "run.retrying": "process",
  "run.paused": "process",
  "run.completed": "process",
  "run.incomplete": "process",
  "run.failed": "process",
  "run.cancelled": "process",
  "run.status_unknown": "process",
  // 档位被这台机器降回逐步 / 资源墙弱：运行态的自述，状态行里显示，不进对话。
  "autonomy.downgraded": "process",
  "autonomy.weak_walls": "process",
  // 用户交来的文件落进了工作区 / 新后端接回了在飞的那一轮：运行态记账。
  "material.added": "process",
  "run.rejoined": "process",
  "run.injected": "process",
  "run.recovering": "process",
  "run.blocked": "process",
  // 「我在睡，睡到几点」是运行态记账，不是说给人听的话 —— 人要看的是
  // 那条实时状态行（它会显示停靠到几点），不是流水里多一行事件。
  "run.parked": "process",
  // 回执是**说给人听的**：它回答"我那句话到了吗、会怎么处理"。
  "interject.queued": "conversation",
  "usage.updated": "process",
  // 窗口占用是运行态记账：chip 上常显，不进对话流。
  "context.updated": "process",
  "redaction.warning": "process",
  // 预算告警是**给人**的：它要人决定加不加钱 / 缩不缩范围。
  "budget.warning": "conversation",
  "record.rejected": "process",
  "connection.lost": "process",
  "connection.recovered": "process",
};

export function audienceOf(kind: string): EventAudience {
  const audience = (AUDIENCE as Record<string, EventAudience | undefined>)[kind];
  if (!audience) {
    // fail loud：新事件类型没声明受众，就不该有人替它猜。
    throw new Error(
      `事件类型 ${kind} 没有声明受众 —— 在 event-audience.ts 里写清它说给谁听`,
    );
  }
  return audience;
}

/** 穷尽性自检的入口（测试用）。 */
export const DECLARED_KINDS = Object.keys(AUDIENCE);
export const ALL_KINDS: readonly string[] = EXECUTION_EVENT_KINDS;
