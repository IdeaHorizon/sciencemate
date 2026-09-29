import test from "node:test";
import assert from "node:assert/strict";

import { groupByChildRun } from "./child-run-grouping.ts";

/**
 * 子节点的动作按它自己的 run 分组，跑完就收起。
 *
 * 现场（wangd 2026-08-11 试用）：
 * > 「literature 都结束了，然后开始 hypothesis 了，它还是在下面显示一大坨。
 * >   按理说 literature 这一大坨运行的内容就应该是 over 了，然后可以把它
 * >   弄成一大坨，然后调度器说话，然后接下来那再开始个新的 hypothesis。」
 *
 * 后端已经把子节点事件归到各自的 run（parent_run_id 指向 orchestrator）。
 * 这里按 runId 分组，并从 run.completed / run.failed 判"跑完没"。
 */
function ev(id: string, kind: string, runId: string, at: string, extra: object = {}) {
  return { id, kind, runId, parentRunId: "orc-1", at: at, payload: extra } as never;
}

test("按子节点 run 分组，顶层的不进组", () => {
  const groups = groupByChildRun([
    ev("1", "tool.started", "lit-1", "2026-08-11T10:00:00Z", { nodeType: "literature" }),
    ev("2", "tool.completed", "lit-1", "2026-08-11T10:00:05Z"),
    ev("3", "tool.started", "hyp-1", "2026-08-11T10:10:00Z", { nodeType: "hypothesis" }),
    { id: "4", kind: "session.message", runId: "orc-1", parentRunId: undefined,
      at: "2026-08-11T10:05:00Z", payload: {} } as never,
  ]);

  assert.equal(groups.length, 2, "两个子节点 → 两组；顶层消息不该成组");
  assert.equal(groups[0].runId, "lit-1");
  assert.equal(groups[0].events.length, 2);
  assert.equal(groups[1].runId, "hyp-1");
});

test("组按开始时间排序 —— 先 literature 后 hypothesis", () => {
  const groups = groupByChildRun([
    ev("3", "tool.started", "hyp-1", "2026-08-11T10:10:00Z"),
    ev("1", "tool.started", "lit-1", "2026-08-11T10:00:00Z"),
  ]);
  assert.deepEqual(groups.map((g) => g.runId), ["lit-1", "hyp-1"]);
});

test("跑完的组标记为 done —— 前端据此收起", () => {
  const groups = groupByChildRun([
    ev("1", "tool.started", "lit-1", "2026-08-11T10:00:00Z"),
    ev("2", "run.completed", "lit-1", "2026-08-11T10:05:00Z"),
    ev("3", "tool.started", "hyp-1", "2026-08-11T10:10:00Z"),
  ]);
  assert.equal(groups[0].status, "done");
  assert.equal(groups[1].status, "running", "还在跑的不能收起 —— 那正是要看的");
});

test("失败的组也是终态，但要能一眼看出来", () => {
  const groups = groupByChildRun([
    ev("1", "tool.started", "lit-1", "2026-08-11T10:00:00Z"),
    ev("2", "run.failed", "lit-1", "2026-08-11T10:05:00Z"),
  ]);
  assert.equal(groups[0].status, "failed");
});

test("组名取节点类型，取不到就退回 run id", () => {
  const named = groupByChildRun([
    ev("1", "tool.started", "lit-1", "2026-08-11T10:00:00Z", { nodeType: "literature" }),
  ]);
  assert.equal(named[0].nodeType, "literature");

  const unnamed = groupByChildRun([ev("1", "tool.started", "x-1", "2026-08-11T10:00:00Z")]);
  assert.equal(unnamed[0].nodeType, "x-1", "没有节点名时别显示空白");
});

test("没有子节点时不造空组", () => {
  assert.deepEqual(groupByChildRun([]), []);
});
