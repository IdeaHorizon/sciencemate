import test from "node:test";
import assert from "node:assert/strict";
import {
  buildChildTimeline,
  buildRunTimeline,
  collapseFinishedToolRuns,
  conversationOnly,
  withoutReplyEcho,
} from "./run-activity-timeline.ts";
import type {
  RunActivityNarration,
  RunActivityStep,
  RunActivityTool,
} from "./run-activity-detail";

/**
 * 时间线守的是**发生顺序**：一段话，紧跟它引出的那次调用。
 *
 * wangd 2026-08-13：「输出的文字都在上面，然后执行的指令、子节点记录啥的都在
 * 最下面」—— 之前叙述和动作各攒一坨，"它说了要干什么"和"它干了什么"隔一整屏。
 */

const ROOT = "run_root";
const CHILD = "run_child";

function narration(sequence: number, runId: string, text: string): RunActivityNarration {
  return { id: `n${sequence}`, runId, sequence, turn: 1, text, previewOnly: false };
}

function tool(sequence: number, title: string): RunActivityTool {
  return {
    id: `t${sequence}`, sequence, title, status: "completed",
    retryCount: 0, notices: [],
  };
}

function step(partial: Partial<RunActivityStep> & Pick<RunActivityStep, "id" | "kind" | "sequence">): RunActivityStep {
  return { status: "completed", tools: [], ...partial };
}

test("叙述和工具按 sequence 交错，而不是各自攒成一坨", () => {
  const timeline = buildRunTimeline({
    narration: [narration(1, ROOT, "先探查环境"), narration(5, ROOT, "找到了，验证一下")],
    steps: [step({
      id: "s1", kind: "tool_group", sequence: 2,
      tools: [tool(2, "检查 LAMMPS"), tool(6, "运行验证")],
    })],
  }, ROOT);
  assert.deepEqual(
    timeline.map((item) =>
      item.kind === "narration" ? `说:${item.narration.text}`
      : item.kind === "tool" ? `做:${item.tool.title}`
      : item.kind === "workspace" ? `改:${item.change.id}`
      : item.kind === "said" ? `告知:${item.said.id}`
      : item.kind === "child" ? `组:${item.step.id}`
      : item.kind),
    ["说:先探查环境", "做:检查 LAMMPS", "说:找到了，验证一下", "做:运行验证"],
  );
});

test("顶层工具组的壳被拆掉，工具各自站在自己发生的位置", () => {
  const timeline = buildRunTimeline({
    narration: [],
    steps: [step({
      id: "unassociated-tools", kind: "tool_group", sequence: 1,
      tools: [tool(1, "a"), tool(9, "b")],
    })],
  }, ROOT);
  assert.deepEqual(timeline.map((item) => item.sequence), [1, 9]);
  assert.ok(timeline.every((item) => item.kind === "tool"));
});

test("子节点仍是一个组，插在它开始的位置；它的叙述不上主线", () => {
  const timeline = buildRunTimeline({
    narration: [narration(1, ROOT, "启动 hypothesis"), narration(4, CHILD, "子节点自述")],
    steps: [
      step({ id: "s-child", kind: "child", runId: CHILD, sequence: 3, tools: [tool(4, "查文献")] }),
    ],
  }, ROOT);
  assert.deepEqual(timeline.map((item) => item.kind), ["narration", "child"]);
});

test("别人家 run 的叙述不混进来", () => {
  const timeline = buildRunTimeline({
    narration: [narration(1, "run_someone_else", "外人")],
    steps: [],
  }, ROOT);
  assert.equal(timeline.length, 0);
});

test("子节点组内部同样按发生顺序交错", () => {
  const child = step({
    id: "s-child", kind: "child", runId: CHILD, sequence: 1,
    tools: [tool(3, "查文献"), tool(7, "写预注册")],
  });
  const inner = buildChildTimeline(child, [
    narration(2, CHILD, "先摸底"),
    narration(5, CHILD, "换精准词"),
    narration(9, ROOT, "顶层的话不属于这个组"),
  ]);
  assert.deepEqual(
    inner.map((item) =>
      item.kind === "narration" ? `说:${item.narration.text}`
      : item.kind === "workspace" ? `改:${item.change.id}`
      : `做:${item.tool.title}`),
    ["说:先摸底", "做:查文献", "说:换精准词", "做:写预注册"],
  );
});

test("workspace changes interleave at their recorded position", () => {
  const timeline = buildRunTimeline({
    narration: [narration(1, ROOT, "先写预注册")],
    steps: [{
      id: "s-tools", kind: "tool_group", sequence: 2, status: "completed",
      tools: [tool(2, "保存产物")],
    }],
    workspaceChanges: [{
      id: "wc-1", runId: ROOT, sequence: 3, tool: "save_artifact",
      filesChanged: 1, additions: 17, deletions: 0,
      files: [{ path: "plan/pre_registration.md", additions: 17, deletions: 0 }],
      patchTruncated: false,
    }],
  }, ROOT);
  assert.deepEqual(
    timeline.map((item) =>
      item.kind === "narration" ? "说"
      : item.kind === "tool" ? "做"
      : item.kind === "workspace" ? "改"
      : item.kind === "said" ? "告知"
      : "组"),
    ["说", "做", "改"],
  );
});

function said(sequence: number, runId: string, aboutNodeType: string) {
  return { id: `sd${sequence}`, runId, sequence, text: `派 ${aboutNodeType}`, aboutNodeType };
}

test("子节点派发下一个节点前说的话，照样上主线（不能被 childRunIds 吞掉）", () => {
  // 会话 2276bce7 的真实形状：hypothesis 派 literature，那句「先做文献调研…」
  // 由 hypothesis 这条子 run 发出。旧代码 continue 掉它，而子节点组不渲染
  // said —— 整句消失，用户只看到两个并列的组，没有任何解释。
  const timeline = buildRunTimeline({
    narration: [],
    said: [said(12, ROOT, "hypothesis"), said(45, CHILD, "literature")],
    steps: [
      step({ id: "s-h", kind: "child", runId: CHILD, sequence: 15, tools: [tool(16, "写预注册")] }),
      step({ id: "s-l", kind: "child", runId: "run_grandchild", sequence: 49, tools: [tool(50, "查文献")] }),
    ],
  }, ROOT);
  assert.deepEqual(
    timeline.map((item) =>
      item.kind === "said" ? `告知:${item.said.aboutNodeType}`
      : item.kind === "child" ? `组:${item.step.id}`
      : item.kind),
    ["告知:hypothesis", "组:s-h", "告知:literature", "组:s-l"],
  );
});

test("别人家 run 的告知不混进来", () => {
  const timeline = buildRunTimeline({
    narration: [],
    said: [said(3, "run_someone_else", "外人")],
    steps: [],
  }, ROOT);
  assert.equal(timeline.length, 0);
});

test("归属不明的旧记录（没有 runId）仍然上主线", () => {
  const timeline = buildRunTimeline({
    narration: [],
    said: [{ id: "sd-legacy", sequence: 2, text: "旧记录", aboutNodeType: "literature" }],
    steps: [],
  }, ROOT);
  assert.deepEqual(timeline.map((item) => item.kind), ["said"]);
});

test("与回复正文一字不差的顶层叙述是回声，不再上时间线", () => {
  const reply = "这是一个新研究开题。我直接起 hypothesis 节点。";
  const timeline = buildRunTimeline({
    narration: [
      narration(4, ROOT, reply),
      narration(9, ROOT, "中间轮次的叙述与回复不同，保留"),
    ],
    steps: [],
  }, ROOT);
  const deduped = withoutReplyEcho(timeline, `  ${reply}  `);
  assert.deepEqual(
    deduped.map((item) => item.kind === "narration" ? item.narration.text : item.kind),
    ["中间轮次的叙述与回复不同，保留"],
  );
});

test("回复缺失时叙述原样保留 —— 那是这段话的唯一记录", () => {
  const timeline = buildRunTimeline({
    narration: [narration(4, ROOT, "唯一记录")],
    steps: [],
  }, ROOT);
  assert.equal(withoutReplyEcho(timeline, undefined).length, 1);
  assert.equal(withoutReplyEcho(timeline, "").length, 1);
  assert.equal(withoutReplyEcho(timeline, "不同的回复").length, 1);
});

test("连着说同一句话收成一条 ×N；隔了别的事再说不合并", () => {
  const timeline = buildRunTimeline({
    narration: [],
    said: [
      said(10, ROOT, "writing"), said(11, ROOT, "writing"), said(12, ROOT, "writing"),
      said(20, ROOT, "literature"),
      said(30, ROOT, "writing"),
    ],
    steps: [],
  }, ROOT);
  assert.deepEqual(
    timeline.map((item) => item.kind === "said"
      ? `${item.said.aboutNodeType}${item.repeats && item.repeats > 1 ? `×${item.repeats}` : ""}`
      : item.kind),
    ["writing×3", "literature", "writing"],
  );
});

// ── 「Ran N commands」的三档（wangd 2026-08-18 指着参考图纠正）────────────

function runningTool(sequence: number, title: string): RunActivityTool {
  return { id: `t${sequence}`, sequence, title, status: "running", retryCount: 0, notices: [] };
}

test("跑完的连续动作收成一行；只有一条时保留它自己的标题", () => {
  const items = collapseFinishedToolRuns([
    { kind: "tool", sequence: 1, tool: tool(1, "a") },
    { kind: "tool", sequence: 2, tool: tool(2, "b") },
    { kind: "tool", sequence: 3, tool: tool(3, "c") },
    { kind: "narration", sequence: 4, narration: narration(4, ROOT, "接下来推 PR") },
    { kind: "tool", sequence: 5, tool: tool(5, "Merged PR 485") },
  ]);
  assert.deepEqual(
    items.map((item) =>
      item.kind === "tools" ? `收:${item.tools.length}`
      : item.kind === "tool" ? `单:${item.tool.title}`
      : item.kind),
    ["收:3", "narration", "单:Merged PR 485"],
    "「Ran 1 commands」比原标题信息还少，不该出现",
  );
});

test("正在跑的动作不收束 —— 收束是'这批干完了'的总结", () => {
  const items = collapseFinishedToolRuns([
    { kind: "tool", sequence: 1, tool: tool(1, "done a") },
    { kind: "tool", sequence: 2, tool: tool(2, "done b") },
    { kind: "tool", sequence: 3, tool: runningTool(3, "Waiting for CI on PR 492") },
  ]);
  assert.deepEqual(
    items.map((item) =>
      item.kind === "tools" ? `收:${item.tools.length}`
      : item.kind === "tool" ? `单:${item.tool.title}`
      : item.kind),
    ["收:2", "单:Waiting for CI on PR 492"],
    "正在跑的被收起来 = 把当下正在发生的事藏了",
  );
});

test("中间夹了别的东西就断开重新计数 —— 那句话正是它们的分组理由", () => {
  const items = collapseFinishedToolRuns([
    { kind: "tool", sequence: 1, tool: tool(1, "a") },
    { kind: "tool", sequence: 2, tool: tool(2, "b") },
    { kind: "child", sequence: 3, step: step({ id: "s", kind: "child", sequence: 3 }) },
    { kind: "tool", sequence: 4, tool: tool(4, "c") },
    { kind: "tool", sequence: 5, tool: tool(5, "d") },
  ]);
  assert.deepEqual(
    items.map((item) => item.kind === "tools" ? `收:${item.tools.length}` : item.kind),
    ["收:2", "child", "收:2"],
  );
});

// ── 派发语与它引出的子节点合成一条（wangd 2026-08-19）────────────────────────
//
// 「先是一个蓝色的一段话，表示我为什么要启动这个 hypothesis 节点。然后下面又有
//   一个这个卡片……这三个是重复的，可以把下面那个删掉，就直接缩成在上面那段话
//   的最后面那个，点那里就能展开右边就行了。」

function dispatchLine(sequence: number, id: string, text: string, aboutNodeType = "") {
  return { id, runId: ROOT, sequence, text, aboutNodeType };
}

test("派发语把它引出的子节点折进话尾，不再另起一张卡", () => {
  const timeline = buildRunTimeline({
    narration: [],
    said: [dispatchLine(1, "d1", "我先启动 Analysis 节点来开题", "hypothesis")],
    steps: [step({
      id: "c1", kind: "child", sequence: 2, status: "running",
      runId: "run_root::_orchestrator->hypothesis@d1",
      title: "hypothesis", tools: [tool(3, "read_file")],
    })],
  }, ROOT);

  assert.equal(timeline.length, 1, "子节点卡应该被折进派发语，不该另占一行");
  const [item] = timeline;
  assert.equal(item.kind, "said");
  if (item.kind !== "said") return;
  assert.equal(item.child?.id, "c1");
  assert.equal(item.child?.status, "running");
});

test("没有派发语的子节点照常自己站一行", () => {
  const timeline = buildRunTimeline({
    narration: [],
    said: [],
    steps: [step({
      id: "c1", kind: "child", sequence: 2,
      runId: "run_root::_orchestrator->hypothesis@d1", title: "hypothesis",
    })],
  }, ROOT);
  assert.deepEqual(timeline.map((i) => i.kind), ["child"]);
});

test("派发语说的是别的节点时不折叠 —— 判据是声明的关系，不是相邻", () => {
  const timeline = buildRunTimeline({
    narration: [],
    said: [dispatchLine(1, "d1", "我先起 literature", "literature")],
    steps: [step({
      id: "c1", kind: "child", sequence: 2,
      runId: "run_root::_orchestrator->hypothesis@d1", title: "hypothesis",
    })],
  }, ROOT);
  assert.deepEqual(timeline.map((i) => i.kind), ["said", "child"]);
});

test("同一节点第二次派发另起一行 —— 重跑是一件新的事", () => {
  const timeline = buildRunTimeline({
    narration: [],
    said: [dispatchLine(1, "d1", "起 hypothesis", "hypothesis"), dispatchLine(3, "d2", "再起一次 hypothesis", "hypothesis")],
    steps: [
      step({ id: "c1", kind: "child", sequence: 2, runId: "run_root::_orchestrator->hypothesis@d1", title: "hypothesis" }),
      step({ id: "c2", kind: "child", sequence: 4, runId: "run_root::_orchestrator->hypothesis@d1", title: "hypothesis" }),
    ],
  }, ROOT);
  assert.deepEqual(timeline.map((i) => i.kind), ["said", "said"]);
  assert.equal(timeline[0].kind === "said" && timeline[0].child?.id, "c1");
  assert.equal(timeline[1].kind === "said" && timeline[1].child?.id, "c2");
});

test("卡只有一张时，第二次派发那句话也要挂上它 —— 不许留个死标签", () => {
  // ⚠️ 上一条测试构造了 c1/c2 两张卡，而**线上不长这样**：复派用的是同一个
  // run id，`projectRunActivity` 按 runId 把所有片段并成一张卡。于是第二、
  // 第三次派发的 `→ literature` 找不到 child，退化成 `is-static`：一段灰字、
  // 点不开、既不说进行中也不说完成（2026-08-22 截图里最新那几行全是这样）。
  //
  // 挂同一张卡是如实的 —— 它们本来就是同一条 run，点进去也该看到同一份细节。
  const only = step({
    id: "c1", kind: "child", sequence: 2, status: "running",
    runId: "run_root::_orchestrator->literature@d1",
    title: "literature", tools: [tool(3, "fetch_url")],
  });
  const timeline = buildRunTimeline({
    narration: [],
    said: [
      dispatchLine(1, "d1", "先做文献调研", "literature"),
      dispatchLine(4, "d2", "再让 literature 去抓官方数据", "literature"),
    ],
    steps: [only],
  }, ROOT);

  assert.deepEqual(timeline.map((i) => i.kind), ["said", "said"]);
  for (const item of timeline) {
    assert.equal(item.kind === "said" && item.child?.id, "c1");
    assert.equal(item.kind === "said" && item.child?.status, "running");
  }
});

test("挂的是同名节点的卡，不是随便抓一张", () => {
  const timeline = buildRunTimeline({
    narration: [],
    said: [
      dispatchLine(1, "d1", "起 hypothesis", "hypothesis"),
      dispatchLine(4, "d2", "起 data", "data"),
    ],
    steps: [step({
      id: "c1", kind: "child", sequence: 2,
      runId: "run_root::_orchestrator->hypothesis@d1", title: "hypothesis",
    })],
  }, ROOT);

  assert.equal(timeline[0].kind === "said" && timeline[0].child?.id, "c1");
  // data 这一次还没有任何 run 事件到达 —— 没有就是没有，不许借别人的卡。
  assert.equal(timeline[1].kind === "said" && timeline[1].child, undefined);
});

test("调度器自己的独白穿过 conversationOnly 之后还在 —— 左栏不许是哑巴", () => {
  // 2026-08-24 现场（英国饮食会话 014ed0ad）：调度器一轮里 narration + said
  // 说了一路，左栏一个字都没有。buildRunTimeline 把它们全放上了主线，
  // conversationOnly 又拿 audience 表的**默认归属**（"agent.message": "process"，
  // 那是给右栏用的）把 narration 恒 false 滤掉 —— 同一个 bug 的 tool 分支
  // 2026-08-18 修对了，narration 没跟上。这条测试必须**穿过** conversationOnly，
  // 只打在 buildRunTimeline 上的断言看不见这层（全绿了整整六天）。
  const timeline = conversationOnly(buildRunTimeline({
    narration: [narration(1, ROOT, "我来帮你做这个研究，先起 hypothesis 开题。")],
    steps: [step({
      id: "c1", kind: "child", sequence: 3,
      runId: "run_root::_orchestrator->hypothesis@d1", title: "hypothesis",
    })],
  }, ROOT));
  assert.deepEqual(
    timeline.map((item) => item.kind),
    ["narration", "child"],
    "调度器的独白被 conversationOnly 吞了 —— 左栏又成哑巴了",
  );
});
