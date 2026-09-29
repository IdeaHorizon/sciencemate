import test from "node:test";
import assert from "node:assert/strict";
import { audienceOf } from "../../execution/lib/event-audience.ts";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

/**
 * 等人做决定时，决策面板必须排在活动日志**之前**。
 *
 * E2E 实测（v14 / v16）：一次 run 有 75~105 条活动记录，HumanInputPrompt 原本
 * 渲染在它们全部之后，于是在 1280x720 的正常窗口里被顶到视野外好几屏 ——
 * 顶部只显示一个 "Needs input"，用户看不到还有选项、也点不到 Submit。
 * 我自己驱动这轮 E2E 时把视口拉到 5200px 才够得着。
 *
 * 活动日志是**上下文**，决策是**动作**；纯按时间先后排，会让阻塞性动作永远
 * 排在历史记录后面，正好是反的。这条测试锁住顺序。
 */

const HERE = dirname(fileURLToPath(import.meta.url));
const SOURCE = readFileSync(
  join(HERE, "..", "components", "CanonicalRunActivity.tsx"),
  "utf8",
);

test("pending decision renders before the activity step list", () => {
  // 2026-09-01：能点的那张卡搬去了 SessionWorkspace（全前端唯一构造点）；
  // 这里剩下的是**只读记录**卡。排序的理由没变：它说的是"这一轮当时被什么
  // 挡住了"，排在 105 条活动记录之后等于没有。
  const pauseAt = SOURCE.indexOf("<PausedRecord");
  // 锚定**渲染**那一处 —— 2026-08-17 起时间线先窗口化再渲染（windowedTimeline）
  const stepsAt = SOURCE.indexOf("windowedTimeline.flatMap");
  assert.ok(pauseAt > -1, "HumanInputPrompt 没有被渲染");
  assert.ok(stepsAt > -1, "找不到活动列表的渲染处");
  assert.ok(
    pauseAt < stepsAt,
    "决策面板排在了活动日志之后 —— 长 run 下用户在正常窗口里够不着它",
  );
});

test("it is still gated on an actually pending pause", () => {
  assert.ok(SOURCE.includes("persistedPause && <PausedRecord"));
});

test("对话主线按**受众**过滤，不再各写一遍 kind 判断", () => {
  // wangd 2026-08-18：「左边不就应该是完全没有子节点的详细的运行信息，就是
  // 一些卡片吗？」→ 左栏 = 对话。但这条判断原来散在每个渲染点上（「narration
  // 不显示 / tool 不显示 / said 显示」），一晚栽三次：加一个来源或挪一次位置
  // 就有某处错了，且错了不报错。
  //
  // 现在受众是**事件自己声明的数据**（event-audience.ts，穷尽性由类型系统 +
  // 测试保证），渲染点只调 conversationOnly。
  assert.match(SOURCE, /conversationOnly\(/);
  assert.doesNotMatch(SOURCE, /if \(item\.kind === "narration"\) return \[\];/);
  assert.doesNotMatch(SOURCE, /if \(item\.kind === "tool"\) return \[\];/);
  // 调度器说的话和节点卡仍然要渲染出来
  assert.match(SOURCE, /<OrchestratorSaid/);
  assert.match(SOURCE, /<NodeStatusCard/);
});

test("调度器对用户说的话必须留在主线 —— 派发前那句和待命轮的答复都是", () => {
  // 2026-08-18 回归实录：待命轮真的答了（transcript 里三段实质回答），但
  // ingest 把它映射成 `agent.message`（节点独白），而同一天左栏刚把独白
  // 整体搬去右栏 —— 用户问"现在跑的怎么样了"，左栏一个字都不显示。
  //
  // 现在这两件事都由受众表定死：orchestrator.said = 对话，agent.message =
  // 过程。ingest 把待命轮答复映射成 orchestrator.said（后端测试锁着）。
  assert.equal(audienceOf("orchestrator.said"), "conversation");
  assert.equal(audienceOf("agent.message"), "process");
  assert.match(SOURCE, /if \(item\.kind === "said"\)[\s\S]{0,140}<OrchestratorSaid/);
});
