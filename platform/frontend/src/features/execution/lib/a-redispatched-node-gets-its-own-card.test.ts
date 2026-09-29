import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { projectRunActivity as projectRunActivityRaw } from "./run-activity-detail.ts";

// 下面的判据断言的是**英文**那一份文案，所以这里显式说英文 —— 默认语言已经
// 是中文了。换成默认值就等于把「这些句子长什么样」这件事交给默认语言，
// 判据会跟着默认值一起漂。
const projectRunActivity = (events: readonly ExecutionEvent[], parentInterruptedHint?: boolean) =>
  projectRunActivityRaw(events, parentInterruptedHint, "en");
import {
  buildRunTimeline,
  conversationOnly,
  withoutMessageAnchoredSaids,
  withoutReplyEcho,
} from "./run-activity-timeline.ts";
import type { ExecutionEvent } from "./execution-event.ts";

/**
 * 同一个节点被派第二次，是**新的一件事** —— 另起一张卡，各戴各的结局。
 *
 * 2026-09-02 实拍（课题二，会话 c7168ec6，父 run run_3f0d…）：主聊天里
 * `→ experiment 失败 · 112 actions` 出现了两次。不是重复渲染：seq 1440 首派、
 * seq 2140 按修订后的预注册重派，是两句真实的派发语；子 run 沿用同一个确定性
 * run id，事件流里有两条 `run.started`。首派的真实结局是 seq 1904
 * `run.incomplete`（报 blocker 收工），第二次才是 seq 2295 `run.failed`（上游
 * 503）。投影按 run id 并成一张卡、状态取末事件，折叠又把这一张挂到每句派发语
 * 上 —— 首派那句戴上了第二次的"失败"。
 *
 * 判据来自库里的形状（全库回放）：死亡续跑永远是 `run.started` 紧跟
 * `run.resumed`，是同一件事，继续归并（2026-08-18 的"一条子 run = 一张卡"不动）；
 * 不带 `run.resumed` 的第二条 `run.started` 才是重派。
 *
 * fixture 是那一页真实事件（父 run 与 experiment 子 run，seq 1421–2297 里的
 * 生命周期段 + 各段头几个动作），不是手造样本。
 */

const ROOT = "run_3f0d5a99baa9461dbcd71b7d88a565b7";
const EXPERIMENT = `${ROOT}::_orchestrator->experiment@d1`;

function realEvents(): ExecutionEvent[] {
  const url = new URL("./fixtures/child-redispatched-twice.json", import.meta.url);
  return JSON.parse(readFileSync(url, "utf8")) as ExecutionEvent[];
}

/** 与 CanonicalRunActivity 的链一致（尾窗，不按 sequence 过滤）。 */
function asTheChatSeesIt(events: ExecutionEvent[]) {
  const activity = projectRunActivity(events, undefined);
  const timeline = withoutMessageAnchoredSaids(
    conversationOnly(withoutReplyEcho(buildRunTimeline(activity, ROOT), undefined)),
  );
  return { activity, timeline };
}

test("同一节点被重派两次 → 两张卡，各戴各的结局（真实事件页）", () => {
  const { activity, timeline } = asTheChatSeesIt(realEvents());

  // 右栏「研究进程」读的就是 activity.steps：两次派发，两张卡。
  const cards = activity.steps.filter((s) => s.kind === "child" && s.runId === EXPERIMENT);
  assert.deepEqual(
    cards.map((c) => [c.dispatch, c.status]),
    [[1, "completed"], [2, "failed"]],
    "首派的结局是 incomplete（收工），重派才是 failed —— 不能一张卡两句话都戴'失败'",
  );
  assert.ok(cards[0].tools.length > 0 && cards[1].tools.length > 0, "动作按派发段分到各自的卡上");

  // 主聊天：每句派发语折进**自己派出的那一次**。
  const lines = timeline.flatMap((item) =>
    item.kind === "said" && item.said.aboutNodeType === "experiment" ? [item] : []);
  assert.deepEqual(
    lines.map((line) => [line.sequence, line.child?.dispatch, line.child?.status]),
    [[1440, 1, "completed"], [2140, 2, "failed"]],
  );
  assert.equal(timeline.filter((item) => item.kind === "child").length, 0, "子卡都该折进派发语，不该游离");
});

let seq = 0;
function event(partial: Partial<ExecutionEvent> & { kind: string }): ExecutionEvent {
  seq += 1;
  return {
    id: `e${seq}`, sequence: seq, at: `2026-09-02T00:00:${String(seq).padStart(2, "0")}Z`,
    schemaVersion: 1, workspaceId: "w", projectId: "p", sessionId: "s",
    origin: "raw_transcript", source: {}, visibility: "standard", payload: {},
    ...partial,
  } as ExecutionEvent;
}

test("续跑不切卡：run.started 紧跟 run.resumed 仍是同一件事", () => {
  seq = 0;
  const child = "run_root::_orchestrator->literature@d1";
  const events = [
    event({ kind: "run.started", runId: "run_root", payload: { nodeType: "project_chat", owningRun: true } }),
    event({ kind: "run.started", runId: child, parentRunId: "run_root", payload: { nodeType: "literature" } }),
    event({ kind: "step.started", runId: child, parentRunId: "run_root", payload: { stepId: "st-1", title: "literature activity" } }),
    event({ kind: "tool.started", runId: child, parentRunId: "run_root", payload: { stepId: "st-1", toolCallId: "t1", tool: "search_papers" } }),
    event({ kind: "tool.completed", runId: child, parentRunId: "run_root", payload: { stepId: "st-1", toolCallId: "t1" } }),
    // 死亡续跑：库里的形状（如 seq 3620/3621）—— started 紧跟 resumed
    event({ kind: "run.started", runId: child, parentRunId: "run_root", payload: { nodeType: "literature" } }),
    event({ kind: "run.resumed", runId: child, parentRunId: "run_root", payload: {} }),
    event({ kind: "step.started", runId: child, parentRunId: "run_root", payload: { stepId: "st-2", title: "literature activity" } }),
    event({ kind: "tool.started", runId: child, parentRunId: "run_root", payload: { stepId: "st-2", toolCallId: "t2", tool: "search_papers" } }),
    event({ kind: "tool.completed", runId: child, parentRunId: "run_root", payload: { stepId: "st-2", toolCallId: "t2" } }),
    event({ kind: "run.completed", runId: child, parentRunId: "run_root", payload: { status: "completed" } }),
  ];
  const cards = projectRunActivity(events).steps.filter((s) => s.kind === "child");
  assert.equal(cards.length, 1, "续跑被切成了两张卡（08-18 的归并被破坏）");
  assert.equal(cards[0].tools.length, 2);
  assert.equal(cards[0].status, "completed");
  assert.equal(cards[0].dispatch, 1);
  assert.equal(cards[0].resumed, true);
});

test("重派把旧派发顶掉、旧段又没收终态 → 已中断，不是进行中", () => {
  seq = 0;
  const child = "run_root::_orchestrator->experiment@d1";
  const events = [
    event({ kind: "run.started", runId: "run_root", payload: { nodeType: "project_chat", owningRun: true } }),
    event({ kind: "run.started", runId: child, parentRunId: "run_root", payload: { nodeType: "experiment" } }),
    event({ kind: "step.started", runId: child, parentRunId: "run_root", payload: { stepId: "st-1", title: "experiment activity" } }),
    event({ kind: "tool.started", runId: child, parentRunId: "run_root", payload: { stepId: "st-1", toolCallId: "t1", tool: "run_python" } }),
    event({ kind: "tool.completed", runId: child, parentRunId: "run_root", payload: { stepId: "st-1", toolCallId: "t1" } }),
    // 没有终态、没有 resumed，直接又 started：重派
    event({ kind: "run.started", runId: child, parentRunId: "run_root", payload: { nodeType: "experiment" } }),
    event({ kind: "step.started", runId: child, parentRunId: "run_root", payload: { stepId: "st-2", title: "experiment activity" } }),
    event({ kind: "tool.started", runId: child, parentRunId: "run_root", payload: { stepId: "st-2", toolCallId: "t2", tool: "run_python" } }),
  ];
  const cards = projectRunActivity(events).steps.filter((s) => s.kind === "child");
  assert.deepEqual(cards.map((c) => [c.dispatch, c.status]), [[1, "interrupted"], [2, "running"]]);
});
