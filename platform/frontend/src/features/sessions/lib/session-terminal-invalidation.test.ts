import test from "node:test";
import assert from "node:assert/strict";
import {
  invalidateSessionAfterTerminal,
  invalidateSessionLifecycle,
  sessionLifecycleInvalidationTargets,
  sessionTerminalInvalidationTargets,
  type QueryInvalidationTarget,
} from "./session-terminal-invalidation.ts";

test("a successful Session terminal refreshes every canonical revision owner exactly", async () => {
  const targets = sessionTerminalInvalidationTargets("project-a", "session-a");

  assert.deepEqual(targets, [
    { queryKey: ["sessions", "project-a", "session-a", "api"], exact: true },
    { queryKey: ["sessions", "project-a", "session-a", "messages", "api"], exact: true },
    { queryKey: ["sessions", "project-a", "session-a", "change-set"], exact: true },
    // 树是**前缀**失效：它一层一个 query，精确失效只会命中根那一层，
    // 展开着的目录会停在跑之前的样子（而且不报错）。
    { queryKey: ["sessions", "project-a", "session-a", "project-tree"], exact: false },
    { queryKey: ["sessions", "project-a", "session-a", "conflicts"], exact: true },
    { queryKey: ["sessions", "project-a", "api"], exact: true },
    { queryKey: ["artifacts", "project-a"], exact: true },
  ]);

  const invalidated: QueryInvalidationTarget[] = [];
  await invalidateSessionAfterTerminal({
    invalidateQueries(target) {
      invalidated.push(target);
      return Promise.resolve();
    },
  }, "project-a", "session-a");

  assert.deepEqual(invalidated, targets);
});

test("a terminal with a Run id refreshes that exact canonical Run detail", () => {
  const targets = sessionTerminalInvalidationTargets("project-a", "session-a", "run-a");
  assert.deepEqual(targets.at(-1), { queryKey: ["run", "run-a"], exact: true });
  assert.equal(targets.filter((target) => target.queryKey[0] === "run").length, 1);
});

test("terminal invalidation never flushes unrelated Sessions or the global cache", () => {
  const targets = sessionTerminalInvalidationTargets("project-a", "session-a");

  assert.equal(targets.every((target) => target.queryKey.includes("project-a")), true);
  // 唯一一条前缀失效必须仍然钉在本会话上 —— 宽的是"哪一层"，不是"哪个会话"。
  assert.deepEqual(
    targets.filter((target) => !target.exact).map((target) => target.queryKey),
    [["sessions", "project-a", "session-a", "project-tree"]],
  );
  assert.equal(
    targets.some((target) => target.queryKey.length === 1),
    false,
  );
  assert.equal(
    targets.some((target) => target.queryKey.includes("project-b") || target.queryKey.includes("session-b")),
    false,
  );
});

test("run 起来（不是终态）也要刷新 session —— 否则顶栏挂着上一轮的终态", async () => {
  // 实测形状（本机 2026-08-19，session 78e7681a）：第一个 run 因模型 503 failed
  // → 终态路径刷了一次 session，存下 executionState=failed + 标题生成前的原始
  // prompt；换模型重发后新 run 起（不是终态）→ session 再没刷过 → 顶栏一直显示
  // 「Failed」、标题一直是那段长 prompt，而后端两样都早就对了。
  const targets = sessionLifecycleInvalidationTargets("project-a", "session-a");
  assert.deepEqual(targets, [
    { queryKey: ["sessions", "project-a", "session-a", "api"], exact: true },
    { queryKey: ["sessions", "project-a", "api"], exact: true },
  ]);

  const invalidated: QueryInvalidationTarget[] = [];
  await invalidateSessionLifecycle({
    invalidateQueries(target) {
      invalidated.push(target);
      return Promise.resolve();
    },
  }, "project-a", "session-a");
  assert.deepEqual(invalidated, targets);
});

test("生命周期刷新是终态刷新的子集 —— 两条路径不许对同一个对象各说各话", () => {
  // run.* 一轮里会来很多次，所以生命周期这套必须窄；但窄不等于可以跑偏：
  // 它刷的每一个 key，终态那套也必须刷，否则两条路径会让同一个 session 对象
  // 在不同时机停在不同版本上。
  const lifecycle = sessionLifecycleInvalidationTargets("project-a", "session-a");
  const terminal = sessionTerminalInvalidationTargets("project-a", "session-a");
  const key = (t: QueryInvalidationTarget) => JSON.stringify(t.queryKey);
  const terminalKeys = new Set(terminal.map(key));
  for (const target of lifecycle) {
    assert.ok(terminalKeys.has(key(target)), `终态路径漏了 ${key(target)}`);
  }
});

test("生命周期刷新不碰重家伙 —— run.* 很频繁，别让无关面板反复重取", () => {
  const keys = sessionLifecycleInvalidationTargets("project-a", "session-a").map((t) => t.queryKey.join("/"));
  for (const heavy of ["messages", "change-set", "project-tree", "conflicts", "artifacts"]) {
    assert.ok(!keys.some((k) => k.includes(heavy)), `生命周期刷新不该带上 ${heavy}`);
  }
});
