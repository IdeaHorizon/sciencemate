import type { ExecutionEvent } from "./execution-event";

/**
 * 把子节点的动作按它自己的 run 分组。
 *
 * ## 为什么（wangd 2026-08-11 试用）
 *
 * > 「literature 都结束了，然后开始 hypothesis 了，它还是在下面显示一大坨。
 * >   按理说 literature 这一大坨运行的内容就应该是 over 了，然后可以把它弄成
 * >   一大坨，然后调度器说话，然后接下来那再开始个新的 hypothesis。」
 *
 * 之前所有动作平铺成一坨 `Research activity — 124 recorded actions`：既看不出
 * 哪个动作属于谁，也看不出先后。根因在库里 —— 6 份子节点 transcript 都摄取了，
 * 但事件全挂在顶层那条 run 上。后端修好归属之后（每个子节点带自己的 run_id +
 * parent_run_id），这里就能按 runId 分组。
 *
 * 分组只做**分组**：谁跑完了、按什么顺序、叫什么名字。要不要收起、怎么渲染，
 * 是组件的事。
 */
export type ChildRunStatus = "running" | "done" | "failed";

export type ChildRunGroup = {
  runId: string;
  nodeType: string;
  status: ChildRunStatus;
  startedAt: string;
  events: ExecutionEvent[];
};

/** 终态事件 —— 见到就说明这个子节点这一趟结束了。 */
const DONE_KINDS = new Set(["run.completed", "run.incomplete"]);
const FAILED_KINDS = new Set(["run.failed", "run.cancelled"]);

function nodeTypeOf(events: ExecutionEvent[], fallback: string): string {
  for (const event of events) {
    const payload = event.payload as Record<string, unknown> | undefined;
    const value = payload?.nodeType ?? payload?.node_type;
    if (typeof value === "string" && value) return value;
  }
  // 取不到节点名时退回 run id —— 显示空白比显示一个丑名字更糟。
  return fallback;
}

export function groupByChildRun(events: readonly ExecutionEvent[]): ChildRunGroup[] {
  const buckets = new Map<string, ExecutionEvent[]>();
  for (const event of events) {
    // 顶层自己的事件不成组：它们是调度器说的话/做的事，按时间穿插在组之间。
    if (!event.runId || !event.parentRunId) continue;
    buckets.set(event.runId, [...(buckets.get(event.runId) ?? []), event]);
  }

  const groups: ChildRunGroup[] = [];
  for (const [runId, bucket] of buckets) {
    const ordered = [...bucket].sort((a, b) => a.at.localeCompare(b.at));
    let status: ChildRunStatus = "running";
    for (const event of ordered) {
      if (FAILED_KINDS.has(event.kind)) status = "failed";
      else if (DONE_KINDS.has(event.kind) && status !== "failed") status = "done";
    }
    groups.push({
      runId,
      nodeType: nodeTypeOf(ordered, runId),
      status,
      startedAt: ordered[0]?.at ?? "",
      events: ordered,
    });
  }
  return groups.sort((a, b) => a.startedAt.localeCompare(b.startedAt));
}
