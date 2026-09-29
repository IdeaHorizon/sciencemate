import test from "node:test";
import assert from "node:assert/strict";
import { projectRunActivity as projectRunActivityRaw } from "./run-activity-detail.ts";

// 下面的判据断言的是**英文**那一份文案，所以这里显式说英文 —— 默认语言已经
// 是中文了。换成默认值就等于把「这些句子长什么样」这件事交给默认语言，
// 判据会跟着默认值一起漂。
const projectRunActivity = (events: readonly ExecutionEvent[], parentInterruptedHint?: boolean) =>
  projectRunActivityRaw(events, parentInterruptedHint, "en");
import type { ExecutionEvent } from "./execution-event.ts";

/**
 * 「这一轮干了什么」包含子节点做的事。
 *
 * 现场（wangd 2026-08-11 试用）：
 *
 *   「这 literature 都结束了，然后开始 hypothesis 了，它还是在下面显示一大坨…
 *     按理说 literature 这一大坨运行的内容就应该是 over 了，然后可以把它弄成
 *     一大坨，然后调度器说话，然后接下来那再开始个新的 hypothesis」
 *
 * 实测（会话 bc1c7343）：会话视图按 runId 精确过滤，一轮里只看得到编排器自己
 * 的 197 条工具调用，平铺成一坨 "Research activity"；子节点各 180 / 58 / 38 条
 * 事件一条都取不回来 —— 它们是各自独立的 run。
 *
 * 按 run 切是**数据模型的内部划分**，不该原样泄漏成 UX。
 */

let seq = 0;
function event(partial: Partial<ExecutionEvent> & { kind: string }): ExecutionEvent {
  seq += 1;
  return {
    id: `e${seq}`, sequence: seq, at: `2026-08-12T00:00:${String(seq).padStart(2, "0")}Z`,
    schemaVersion: 1, workspaceId: "w", projectId: "p", sessionId: "s",
    origin: "raw_transcript", source: {}, visibility: "standard", payload: {},
    ...partial,
  } as ExecutionEvent;
}

test("子节点跑的那一步认得出是谁，不叫 Research step", () => {
  seq = 0;
  const events = [
    event({ kind: "run.started", runId: "parent", payload: { nodeType: "_orchestrator" } }),
    event({ kind: "step.started", runId: "parent", payload: { stepId: "s-parent" } }),
    event({ kind: "tool.started", runId: "parent", payload: { stepId: "s-parent", toolCallId: "t1", title: "Run node", technicalName: "run_node" } }),
    event({ kind: "tool.completed", runId: "parent", payload: { stepId: "s-parent", toolCallId: "t1" } }),
    // ↓ 子节点自己的 run
    event({ kind: "run.started", runId: "child-lit", parentRunId: "parent", payload: { nodeType: "literature" } }),
    event({ kind: "step.started", runId: "child-lit", parentRunId: "parent", payload: { stepId: "s-lit" } }),
    event({ kind: "tool.started", runId: "child-lit", parentRunId: "parent", payload: { stepId: "s-lit", toolCallId: "t2", title: "Search literature", technicalName: "search_papers" } }),
    event({ kind: "tool.completed", runId: "child-lit", parentRunId: "parent", payload: { stepId: "s-lit", toolCallId: "t2" } }),
    event({ kind: "step.completed", runId: "child-lit", parentRunId: "parent", payload: { stepId: "s-lit" } }),
  ];

  const activity = projectRunActivity(events);
  const child = activity.steps.find((step) => step.kind === "child");

  assert.ok(child, "子节点那一步没被认出来 —— 它的动作会混进编排器那一坨里");
  assert.equal(child.title, "literature", "组标题要说得出是谁；'Research step' 回答不了「轮到谁了」");
  assert.equal(child.status, "completed", "跑完了要看得出来（跑完的默认收起靠它）");
  assert.equal(child.tools.length, 1);
});

test("判据是 run 归属，不是某个 raw 事件名", () => {
  // 老判据是 `start.source.rawEvent === "subagent_call_start"` —— 依赖一个
  // 恰好没变过的名字。run_id / parent_run_id 是库里的事实。
  seq = 0;
  const events = [
    event({ kind: "run.started", runId: "parent" }),
    event({ kind: "step.started", runId: "child-x", parentRunId: "parent", payload: { stepId: "s-x" } }),
    event({ kind: "tool.started", runId: "child-x", parentRunId: "parent", payload: { stepId: "s-x", toolCallId: "t1", title: "Do", technicalName: "run_node" } }),
  ];
  const activity = projectRunActivity(events);
  assert.equal(activity.steps.find((s) => s.id === "s-x")?.kind, "child");
});

test("编排器自己的步骤不会被误判成子节点", () => {
  seq = 0;
  const events = [
    event({ kind: "run.started", runId: "parent" }),
    event({ kind: "step.started", runId: "parent", payload: { stepId: "s-own" } }),
    event({ kind: "tool.started", runId: "parent", payload: { stepId: "s-own", toolCallId: "t1", title: "Read", technicalName: "read_file" } }),
  ];
  const activity = projectRunActivity(events);
  assert.equal(activity.steps.find((s) => s.id === "s-own")?.kind, "tool_group");
});

test("叙述按 run 归到它所属的那一组 —— 不是按 stepId（它没有 stepId）", () => {
  // 这条盯的是一个真实的坑：叙述事件来自 `llm_response`，payload 里只有
  // text / turn / nodeType，**没有 stepId**。按 stepId 匹配的话，渲染出来
  // 永远是空的 —— "写了没人调" 的同款形状，只是换成了"接了但永远不成立"。
  seq = 0;
  const events = [
    event({ kind: "run.started", runId: "parent" }),
    event({ kind: "step.started", runId: "child-lit", parentRunId: "parent", payload: { stepId: "s-lit" } }),
    event({ kind: "tool.started", runId: "child-lit", parentRunId: "parent", payload: { stepId: "s-lit", toolCallId: "t1", title: "Search", technicalName: "search_papers" } }),
    event({ kind: "agent.message", runId: "child-lit", parentRunId: "parent", payload: { text: "前两轮查得太宽泛，换更精准的词。", turn: 3 } }),
  ];
  const activity = projectRunActivity(events);

  assert.equal(activity.narration.length, 1, "叙述没进投影");
  assert.equal(activity.narration[0].runId, "child-lit", "没带 run 归属，前端就归不了组");
  assert.equal(activity.narration[0].stepId, undefined, "叙述本来就没有 stepId");
  assert.equal(activity.steps.find((s) => s.id === "s-lit")?.runId, "child-lit",
    "step 得说得出自己属于哪条 run，叙述才接得上");
});

test("子节点的终态从它的 run 生命周期现算 —— literature 完成后不再标'进行中'", () => {
  seq = 0;
  const child = "run_root::hypothesis->literature@d2";
  const events = [
    event({ kind: "run.started", runId: "run_root", payload: { nodeType: "project_chat", owningRun: true } }),
    event({ kind: "run.started", runId: child, parentRunId: "run_root", payload: { nodeType: "literature", owningRun: false } }),
    event({ kind: "step.started", runId: child, parentRunId: "run_root", payload: { stepId: "st-lit", title: "literature activity" } }),
    event({ kind: "tool.started", runId: child, parentRunId: "run_root", payload: { stepId: "st-lit", toolCallId: "t1", tool: "search_papers" } }),
    event({ kind: "tool.completed", runId: child, parentRunId: "run_root", payload: { stepId: "st-lit", toolCallId: "t1" } }),
    // 子节点 transcript 没有 step.completed —— 终态只写在 run 生命周期里。
    event({ kind: "run.completed", runId: child, parentRunId: "run_root", payload: { status: "completed", owningRun: false } }),
  ];
  const detail = projectRunActivity(events);
  const step = detail.steps.find((s) => s.kind === "child");
  assert.equal(step?.status, "completed");
});

test("子节点 run.failed → failed；没有终态事件才是进行中", () => {
  seq = 0;
  const failed = "run_root::hypothesis->experiment@d2";
  const live = "run_root::hypothesis->literature@d3";
  const events = [
    event({ kind: "run.started", runId: "run_root", payload: { nodeType: "project_chat", owningRun: true } }),
    event({ kind: "run.started", runId: failed, parentRunId: "run_root", payload: { nodeType: "experiment", owningRun: false } }),
    event({ kind: "step.started", runId: failed, parentRunId: "run_root", payload: { stepId: "st-exp", title: "experiment activity" } }),
    event({ kind: "run.failed", runId: failed, parentRunId: "run_root", payload: { status: "failed", owningRun: false } }),
    event({ kind: "run.started", runId: live, parentRunId: "run_root", payload: { nodeType: "literature", owningRun: false } }),
    event({ kind: "step.started", runId: live, parentRunId: "run_root", payload: { stepId: "st-lit", title: "literature activity" } }),
  ];
  const detail = projectRunActivity(events);
  const byStep = new Map(detail.steps.map((s) => [s.id, s.status]));
  assert.equal(byStep.get("st-exp"), "failed");
  assert.equal(byStep.get("st-lit"), "running");
});

test("被打断又续跑的同一条 run 只画一张卡（不是两张）", () => {
  // 2026-08-18 实测：literature 跑到第 8 步被杀，下一条消息自动续跑同一条
  // run（同 run_id、transcript 追加）。但恢复时会重发一条 root step ——
  // UI 于是显示「literature 已中断 · 8 actions」+「literature 进行中 · 5
  // actions」两张卡，读起来还是"它又新开了一个"，正是要消灭的那个观感。
  seq = 0;
  const child = "run_root::_orchestrator->literature@d1";
  const events = [
    event({ kind: "run.started", runId: "run_root", payload: { nodeType: "project_chat", owningRun: true } }),
    event({ kind: "run.started", runId: child, parentRunId: "run_root", payload: { nodeType: "literature", owningRun: false } }),
    // 第一段：被打断
    event({ kind: "step.started", runId: child, parentRunId: "run_root", payload: { stepId: "st-1", title: "literature activity" } }),
    event({ kind: "tool.started", runId: child, parentRunId: "run_root", payload: { stepId: "st-1", toolCallId: "t1", tool: "search_papers" } }),
    event({ kind: "tool.completed", runId: child, parentRunId: "run_root", payload: { stepId: "st-1", toolCallId: "t1" } }),
    // 第二段：续跑（新 root step，同一条 run）
    event({ kind: "step.started", runId: child, parentRunId: "run_root", payload: { stepId: "st-2", title: "literature activity" } }),
    event({ kind: "tool.started", runId: child, parentRunId: "run_root", payload: { stepId: "st-2", toolCallId: "t2", tool: "search_papers" } }),
    event({ kind: "tool.completed", runId: child, parentRunId: "run_root", payload: { stepId: "st-2", toolCallId: "t2" } }),
    event({ kind: "run.completed", runId: child, parentRunId: "run_root", payload: { status: "completed", owningRun: false } }),
  ];
  const detail = projectRunActivity(events);
  const children = detail.steps.filter((s) => s.kind === "child");
  assert.equal(children.length, 1, "同一条 run 被画成了多张卡");
  assert.equal(children[0].tools.length, 2, "两段的动作要接起来");
  assert.equal(children[0].status, "completed", "状态取最后那段（run 的现状）");
  // 动作按发生顺序，不是按段拼接
  assert.deepEqual(
    children[0].tools.map((t) => t.sequence),
    [...children[0].tools.map((t) => t.sequence)].sort((a, b) => a - b),
  );
});

test("不同 run 各自一张卡 —— 归并判据是 runId，不是节点名", () => {
  seq = 0;
  const first = "run_root::_orchestrator->literature@d1";
  const second = "run_root::hypothesis->literature@d2";
  const events = [
    event({ kind: "run.started", runId: "run_root", payload: { nodeType: "project_chat", owningRun: true } }),
    event({ kind: "run.started", runId: first, parentRunId: "run_root", payload: { nodeType: "literature", owningRun: false } }),
    event({ kind: "step.started", runId: first, parentRunId: "run_root", payload: { stepId: "st-a", title: "literature activity" } }),
    event({ kind: "run.completed", runId: first, parentRunId: "run_root", payload: { status: "completed", owningRun: false } }),
    event({ kind: "run.started", runId: second, parentRunId: "run_root", payload: { nodeType: "literature", owningRun: false } }),
    event({ kind: "step.started", runId: second, parentRunId: "run_root", payload: { stepId: "st-b", title: "literature activity" } }),
  ];
  const detail = projectRunActivity(events);
  assert.equal(detail.steps.filter((s) => s.kind === "child").length, 2);
});

test("续跑的 run 带 resumed 标记 —— UI 说得出这是接着上次跑", () => {
  seq = 0;
  const child = "run_turn2::_orchestrator->literature@d1";
  const detail = projectRunActivity([
    event({ kind: "run.started", runId: "run_turn2", payload: { nodeType: "project_chat", owningRun: true } }),
    event({ kind: "run.started", runId: child, parentRunId: "run_turn2", payload: { nodeType: "literature", owningRun: false } }),
    event({ kind: "run.resumed", runId: child, parentRunId: "run_turn2", payload: { resumedFromTurn: 3, owningRun: false } }),
    event({ kind: "step.started", runId: child, parentRunId: "run_turn2", payload: { stepId: "st-r", title: "literature activity" } }),
    event({ kind: "tool.started", runId: child, parentRunId: "run_turn2", payload: { stepId: "st-r", toolCallId: "t1", tool: "search_papers" } }),
  ]);
  const step = detail.steps.find((s) => s.kind === "child");
  assert.equal(step?.resumed, true);
});

test("正常新开的 run 没有 resumed 标记", () => {
  seq = 0;
  const child = "run_turn1::_orchestrator->literature@d1";
  const detail = projectRunActivity([
    event({ kind: "run.started", runId: "run_turn1", payload: { nodeType: "project_chat", owningRun: true } }),
    event({ kind: "run.started", runId: child, parentRunId: "run_turn1", payload: { nodeType: "literature", owningRun: false } }),
    event({ kind: "step.started", runId: child, parentRunId: "run_turn1", payload: { stepId: "st-n", title: "literature activity" } }),
    event({ kind: "tool.started", runId: child, parentRunId: "run_turn1", payload: { stepId: "st-n", toolCallId: "t1", tool: "search_papers" } }),
  ]);
  assert.equal(detail.steps.find((s) => s.kind === "child")?.resumed, false);
});

test("同一个节点被派第二次 → 卡片重新变回「进行中」", () => {
  // 2026-08-22 现场（会话 41ac6a66）：调度器复用确定性 run id，同一个节点派
  // 两次还是 `…::_orchestrator->hypothesis@d1`。旧判据问的是「这条 run 出现过
  // 终态没有」，于是第一次 completed 之后，它再跑多少次都还是"已完成" ——
  // 用户盯着一个每几秒产出一条事件的会话，界面上一个动的东西都没有。
  //
  // ⚠️ 已有的「不同 run 各自一张卡」那条测试用了 `@d1` / `@d2` 两个不同深度，
  // runId 天然不同 —— 它构造的形状和线上复派不是一回事，所以这个缺陷在全绿的
  // 套件底下活了很久。这里刻意用**同一个 runId**。
  seq = 0;
  const child = "run_root::_orchestrator->hypothesis@d1";
  const detail = projectRunActivity([
    event({ kind: "run.started", runId: "run_root", payload: { nodeType: "project_chat", owningRun: true } }),
    event({ kind: "run.started", runId: child, parentRunId: "run_root", payload: { nodeType: "hypothesis" } }),
    event({ kind: "step.started", runId: child, parentRunId: "run_root", payload: { stepId: "st-1", title: "hypothesis activity" } }),
    event({ kind: "tool.started", runId: child, parentRunId: "run_root", payload: { stepId: "st-1", toolCallId: "t1", tool: "read_artifact" } }),
    event({ kind: "tool.completed", runId: child, parentRunId: "run_root", payload: { stepId: "st-1", toolCallId: "t1" } }),
    event({ kind: "run.completed", runId: child, parentRunId: "run_root", payload: { status: "completed" } }),
    // …别的节点跑了一阵，然后**同一个节点又被派出去**（同 runId）。
    event({ kind: "run.started", runId: child, parentRunId: "run_root", payload: { nodeType: "hypothesis" } }),
    event({ kind: "tool.started", runId: child, parentRunId: "run_root", payload: { stepId: "st-1", toolCallId: "t2", tool: "write_artifact" } }),
  ]);

  // 2026-09-02 起按派发段分卡：第一次那张保留自己的结局，**最新那张**在跑
  // （同一个 stepId 跨了派发段也按段切开）。
  const cards = detail.steps.filter((s) => s.kind === "child");
  assert.deepEqual(
    cards.map((c) => [c.dispatch, c.status]),
    [[1, "completed"], [2, "running"]],
    "复派之后它就是在跑 —— 界面必须动起来；而第一次的结局不许被改写",
  );
});

test("复派之后**真结束了**要如实回到终态 —— 不许一直亮着", () => {
  // 反向变异：上一条测试单独存在时，「永远 running」也能让它绿。
  seq = 0;
  const child = "run_root::_orchestrator->hypothesis@d1";
  const detail = projectRunActivity([
    event({ kind: "run.started", runId: "run_root", payload: { nodeType: "project_chat", owningRun: true } }),
    event({ kind: "run.started", runId: child, parentRunId: "run_root", payload: { nodeType: "hypothesis" } }),
    event({ kind: "step.started", runId: child, parentRunId: "run_root", payload: { stepId: "st-1", title: "hypothesis activity" } }),
    event({ kind: "tool.started", runId: child, parentRunId: "run_root", payload: { stepId: "st-1", toolCallId: "t1", tool: "read_artifact" } }),
    event({ kind: "run.completed", runId: child, parentRunId: "run_root", payload: { status: "completed" } }),
    event({ kind: "run.started", runId: child, parentRunId: "run_root", payload: { nodeType: "hypothesis" } }),
    event({ kind: "run.failed", runId: child, parentRunId: "run_root", payload: { status: "error" } }),
  ]);

  // 复派段一条 step 都没留下 —— 从生命周期合成一张 0-action 的卡，结局如实。
  const cards = detail.steps.filter((s) => s.kind === "child");
  assert.deepEqual(
    cards.map((c) => [c.dispatch, c.status, c.tools.length]),
    [[1, "completed", 1], [2, "failed", 0]],
  );
});
