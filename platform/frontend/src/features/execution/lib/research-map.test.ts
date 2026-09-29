import test from "node:test";
import assert from "node:assert/strict";
import type { ExecutionEvent } from "./execution-event";
import { parseRunLineage, projectResearchMap } from "./research-map.ts";

/**
 * 事件形状照抄真实会话 2276bce7 的库内数据：子 run 的 runId 末段是
 * `派发者->节点@d深度`，nodeType 在 run.started 的 payload 里。
 */
const ROOT = "run_f2cb43980cdf40ec893ae7aa75997f0a";

let seq = 0;
function reset() { seq = 0; }

function event(partial: Partial<ExecutionEvent> & { kind: ExecutionEvent["kind"] }): ExecutionEvent {
  seq += 1;
  return {
    schemaVersion: 1,
    id: `ev-${seq}`,
    sequence: seq,
    at: "2026-08-17T10:00:00Z",
    workspaceId: "ws",
    projectId: "proj",
    sessionId: "sess",
    origin: "raw_transcript",
    source: { rawEvent: "run_start", fileRef: "f", byteOffset: 0 },
    visibility: "summary",
    payload: {},
    ...partial,
  };
}

function childStart(dispatcher: string, nodeType: string, depth: number): ExecutionEvent {
  return event({
    kind: "run.started",
    runId: `${ROOT}::${dispatcher}->${nodeType}@d${depth}`,
    parentRunId: ROOT,
    payload: { nodeType, attemptNo: 1, owningRun: false },
  });
}

function childEnd(dispatcher: string, nodeType: string, depth: number, kind: ExecutionEvent["kind"] = "run.completed"): ExecutionEvent {
  return event({
    kind,
    runId: `${ROOT}::${dispatcher}->${nodeType}@d${depth}`,
    parentRunId: ROOT,
    payload: { status: "completed", owningRun: false },
  });
}

test("runId 末段解析出真实血缘（平台 parentRunId 被拍平，回答不了谁派的）", () => {
  assert.deepEqual(
    parseRunLineage(`${ROOT}::hypothesis->literature@d2`),
    { dispatcher: "hypothesis", nodeType: "literature" },
  );
  assert.deepEqual(
    parseRunLineage(`${ROOT}::_orchestrator->hypothesis@d1`),
    { dispatcher: "_orchestrator", nodeType: "hypothesis" },
  );
  assert.equal(parseRunLineage(ROOT), null);
});

test("真实会话形状：analysis 在跑，literature 是它的卫星 —— 没有幽灵车站", () => {
  reset();
  const model = projectResearchMap([
    event({ kind: "run.started", runId: ROOT, payload: { nodeType: "project_chat", owningRun: true } }),
    childStart("_orchestrator", "hypothesis", 1),
    childStart("hypothesis", "literature", 2),
  ]);
  assert.deepEqual(model.stations, [{
    nodeType: "hypothesis", label: "analysis", caption: "hypothesis", visits: 1, running: true,
  }]);
  assert.deepEqual(model.edges, []);
  assert.deepEqual(model.satellites, [
    { nodeType: "literature", station: "hypothesis", count: 1, running: true },
  ]);
});

test("迭代回路：去程回程分开计数，正在跑的那条转移是 active", () => {
  reset();
  const model = projectResearchMap([
    childStart("_orchestrator", "hypothesis", 1),
    childEnd("_orchestrator", "hypothesis", 1),
    childStart("hypothesis", "experiment", 2),
    childEnd("hypothesis", "experiment", 2),
    childStart("_orchestrator", "hypothesis", 3),
    childEnd("_orchestrator", "hypothesis", 3),
    childStart("hypothesis", "experiment", 4),
  ]);
  assert.deepEqual(model.stations.map((s) => [s.label, s.visits, s.running]), [
    ["analysis", 2, false],
    ["experiment", 2, true],
  ]);
  assert.deepEqual(model.edges, [
    { from: "hypothesis", to: "experiment", count: 2, active: true },
    { from: "experiment", to: "hypothesis", count: 1, active: false },
  ]);
});

test("writing 到访后才出现；postprocess 挂在 writing 旁", () => {
  reset();
  const model = projectResearchMap([
    childStart("_orchestrator", "hypothesis", 1),
    childEnd("_orchestrator", "hypothesis", 1),
    childStart("_orchestrator", "writing", 2),
    childStart("writing", "postprocess", 3),
    childEnd("writing", "postprocess", 3),
    childEnd("_orchestrator", "writing", 2),
  ]);
  assert.deepEqual(model.stations.map((s) => s.label), ["analysis", "writing"]);
  assert.deepEqual(model.satellites, [
    { nodeType: "postprocess", station: "writing", count: 1, running: false },
  ]);
});

test("调度器直接派的服务，按时间挂在最近开始的车站；一个车站都没有就浮动", () => {
  reset();
  const floating = projectResearchMap([
    childStart("_orchestrator", "literature", 1),
  ]);
  assert.deepEqual(floating.satellites, [
    { nodeType: "literature", station: null, count: 1, running: true },
  ]);

  reset();
  const attached = projectResearchMap([
    childStart("_orchestrator", "hypothesis", 1),
    childStart("_orchestrator", "literature", 2),
  ]);
  assert.equal(attached.satellites[0].station, "hypothesis");
});

test("同型接续（重试）计入 visits 不算转移；同型多次服务聚合计数", () => {
  reset();
  const model = projectResearchMap([
    childStart("_orchestrator", "hypothesis", 1),
    childEnd("_orchestrator", "hypothesis", 1, "run.failed"),
    childStart("_orchestrator", "hypothesis", 2),
    childStart("hypothesis", "literature", 3),
    childEnd("hypothesis", "literature", 3),
    childStart("hypothesis", "literature", 4),
  ]);
  assert.deepEqual(model.stations, [{
    nodeType: "hypothesis", label: "analysis", caption: "hypothesis", visits: 2, running: true,
  }]);
  assert.deepEqual(model.edges, []);
  assert.deepEqual(model.satellites, [
    { nodeType: "literature", station: "hypothesis", count: 2, running: true },
  ]);
});

test("未知 node_type 不消失 —— 按卫星渲染（护栏要扫盘，不要写名单）", () => {
  reset();
  const model = projectResearchMap([
    childStart("_orchestrator", "hypothesis", 1),
    childStart("hypothesis", "figure_service", 2),
  ]);
  assert.deepEqual(model.satellites, [
    { nodeType: "figure_service", station: "hypothesis", count: 1, running: true },
  ]);
});

test("身份解析不出来时退回 payload 的 nodeType，事件乱序也不影响", () => {
  reset();
  const start = event({
    kind: "run.started",
    runId: `${ROOT}::legacy-child`,
    parentRunId: ROOT,
    payload: { nodeType: "experiment", owningRun: false },
  });
  const end = event({
    kind: "run.completed",
    runId: `${ROOT}::legacy-child`,
    parentRunId: ROOT,
    payload: { owningRun: false },
  });
  const model = projectResearchMap([end, start]);
  assert.deepEqual(model.stations.map((s) => [s.label, s.running]), [["experiment", false]]);
});

test("流程图的车站 = 治理型产出节点，observation 不再是小卫星", () => {
  // wangd 2026-08-21：「observation 这种节点也算是生产节点了，为啥在流程图里
  // 是小的呢？」判据在节点自己的 harness.yaml（post_run_flow != none），
  // 这里只钉住前端这份抄件的内容 —— 分叉由
  // tests/test_research_map_stations_match_node_roles.py 扫盘拦住。
  const model = projectResearchMap([
    event({ kind: "run.started", runId: "run_a::_orchestrator->observation@d1",
            parentRunId: "run_a", payload: { nodeType: "observation" } }),
    event({ kind: "run.completed", runId: "run_a::_orchestrator->observation@d1",
            parentRunId: "run_a", payload: { nodeType: "observation" } }),
    event({ kind: "run.started", runId: "run_a::_orchestrator->literature@d1",
            parentRunId: "run_a", payload: { nodeType: "literature" } }),
  ]);
  assert.deepEqual(model.stations.map((s) => s.nodeType), ["observation"]);
  assert.deepEqual(model.satellites.map((s) => s.nodeType), ["literature"],
    "服务型节点（post_run_flow: none）仍然是卫星");
});

test("同一个 run id 被派第二次 = 第二次到访：车站重新亮起来，visits 也跟着涨", () => {
  // 2026-08-22 现场（会话 41ac6a66 真实事件流）：literature 派了 3 次、data 3
  // 次、hypothesis 2 次，图上一律 ×1，而且一个亮着的车站都没有。
  //
  // 病根是 `collectChildRuns` 里的 `!runs.has(event.runId)` 守卫 —— 复派的
  // `run.started` 被整个丢掉。上面那条「同型接续」测试用的是 `@d1`/`@d2`
  // 两个**不同的 runId**，所以它绿着，而线上复派用的是同一个 id。
  reset();
  const model = projectResearchMap([
    childStart("_orchestrator", "hypothesis", 1),
    childEnd("_orchestrator", "hypothesis", 1),
    childStart("_orchestrator", "observation", 1),
    childEnd("_orchestrator", "observation", 1),
    // ← 同一个 runId（同 depth）第二次开跑
    childStart("_orchestrator", "hypothesis", 1),
  ]);

  assert.deepEqual(model.stations, [
    { nodeType: "hypothesis", label: "analysis", caption: "hypothesis", visits: 2, running: true },
    { nodeType: "observation", label: "observation", caption: undefined, visits: 1, running: false },
  ]);
  // 回程那一条是"正在跑的这一次"，要画成流动的。
  assert.deepEqual(model.edges, [
    { from: "hypothesis", to: "observation", count: 1, active: false },
    { from: "observation", to: "hypothesis", count: 1, active: true },
  ]);
});

test("复派之后真结束了就熄灭 —— 一次到访的终态关不掉更早的那次", () => {
  // 反向变异：光把守卫删掉、让终态回头去关"第一条"，这条会红。
  reset();
  const model = projectResearchMap([
    childStart("_orchestrator", "hypothesis", 1),
    childEnd("_orchestrator", "hypothesis", 1),
    childStart("_orchestrator", "hypothesis", 1),
    childEnd("_orchestrator", "hypothesis", 1, "run.failed"),
  ]);
  assert.deepEqual(model.stations, [{
    nodeType: "hypothesis", label: "analysis", caption: "hypothesis", visits: 2, running: false,
  }]);
});

test("同一次到访连发两个终态（cancelled + incomplete）不算两次结束", () => {
  // 现场就有：data@d1 一次到访依次发 run.cancelled / run.blocked / run.incomplete。
  reset();
  const model = projectResearchMap([
    childStart("_orchestrator", "hypothesis", 1),
    childStart("_orchestrator", "observation", 1),
    childEnd("_orchestrator", "observation", 1, "run.cancelled"),
    childEnd("_orchestrator", "observation", 1, "run.incomplete"),
  ]);
  // hypothesis 那次到访还开着 —— 后面那个 incomplete 不该殃及它。
  assert.equal(model.stations.find((s) => s.nodeType === "hypothesis")?.running, true);
  assert.equal(model.stations.find((s) => s.nodeType === "observation")?.running, false);
  assert.equal(model.stations.find((s) => s.nodeType === "observation")?.visits, 1);
});
