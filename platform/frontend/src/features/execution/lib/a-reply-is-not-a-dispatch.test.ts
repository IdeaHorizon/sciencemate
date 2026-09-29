import test from "node:test";
import assert from "node:assert/strict";
import { isDispatchLine } from "./run-activity-timeline.ts";
import type { RunActivitySaid, RunActivityStep } from "./run-activity-detail.ts";

/**
 * 插话槽位里的答复不该画 `→ node`（#776）。
 *
 * ## 现场（课题二 c7168ec6，2026-09-02）
 *
 * 插话答复槽位里那段「收到，这条插得对…我把这四条…注入到正在跑的 experiment
 * 节点」末尾有一个 `→ experiment`，class 是 `chat-said-node is-static`：
 * **灰字、点不开、不说进行中也不说完成**。同一会话主聊天里的三句派发语都正常
 * 折了卡（`→ experiment 完成 · 85` / `失败 · 27` / `进行中 · 50`）。
 *
 * ## 机制
 *
 * #766 把锚到插话的答复收进消息槽位，这条路直接渲染 `RunActivitySaid`，
 * **不经过** `buildRunTimeline → foldChildIntoItsDispatchLine`，所以
 * `aboutNodeType` 有值而 `child` 永远没有 → 退化成 `is-static`。
 *
 * ## 修法：答复不是派发
 *
 * `aboutNodeType` 在一条答复上说的是"这句话在讲哪个节点"，不是"这句话派了它"
 * —— 那个节点**本来就在跑**，答复只是往里注入了几条。给它画一个派发
 * affordance，是在承诺一个不存在的去处。
 *
 * 另一条修法（让槽位里的答复也去折一张卡）要靠"这句话之后最先开跑 / 最近的
 * 那张"去**猜**它指哪次派发 —— 猜出来的关系点开就是错的卡。不画才是如实的。
 *
 * 判据抽成纯函数，因为 JSX 里的判断谁都测不到 —— 这正是 #766 自己踩过的坑
 * （让位做了、接住没做，两头落空）。同一条规则现在只有一份：run 窗口的让位、
 * 折叠、和组件画不画胶囊，三处共用 `isDispatchLine`。
 */

function said(overrides: Partial<RunActivitySaid> = {}): RunActivitySaid {
  return {
    id: "said-1",
    sequence: 10,
    text: "我先启动 experiment 节点",
    ...overrides,
  } as RunActivitySaid;
}

const card = { id: "step-1", status: "completed" } as unknown as RunActivityStep;

test("锚到某条消息的答复不画 → node，哪怕它带着 aboutNodeType", () => {
  const reply = said({
    text: "收到，这条插得对 —— 我把这四条注入到正在跑的 experiment 节点",
    aboutNodeType: "experiment",
    repliesToMessageId: "msg-42",
  });
  assert.equal(isDispatchLine(reply), false);
});

test("答复即使碰巧折到了一张卡，也仍然不是派发", () => {
  const reply = said({ aboutNodeType: "experiment", repliesToMessageId: "msg-42" });
  assert.equal(isDispatchLine(reply, card), false);
});

test("真正的派发语照旧画 → node —— 否则这条修法把好的那半也删了", () => {
  assert.equal(isDispatchLine(said({ aboutNodeType: "experiment" })), true);
});

test("折着子节点卡的那句也算派发线（老数据没有 aboutNodeType）", () => {
  assert.equal(isDispatchLine(said(), card), true);
});

test("既不派发也没卡的普通叙述不画", () => {
  assert.equal(isDispatchLine(said()), false);
});
