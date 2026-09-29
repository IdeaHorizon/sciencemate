import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

/**
 * 帮助函数写对了不等于有人调它。这条守的是**接线**：run 生命周期事件那一支
 * 必须真的把 session 刷掉。
 *
 * 之前的缺陷正是接线缺失 —— `invalidateSessionAfterTerminal` 一直存在且正确，
 * 但只挂在 onTerminal 上；run 起来那一支只刷了 runs 列表。于是顶栏的
 * executionState / title 在一轮跑起来之后永远停在上一轮的终态。
 */
const SOURCE = readFileSync(new URL("../hooks/useRuns.ts", import.meta.url), "utf8");

/** 抠出 `if (…startsWith("run.")…) { … }` 这一支的函数体。 */
function runLifecycleBranch(): string {
  // 判断只算一次（sawRunLifecycle），两处共用 —— 抠的是**用它**的那一支。
  const at = SOURCE.indexOf("if (sawRunLifecycle) {");
  assert.ok(at > -1, "找不到 run.* 生命周期分支 —— 它被改名或删了");
  const open = SOURCE.indexOf("{", at);
  let depth = 0;
  for (let i = open; i < SOURCE.length; i += 1) {
    if (SOURCE[i] === "{") depth += 1;
    if (SOURCE[i] === "}") {
      depth -= 1;
      if (depth === 0) return SOURCE.slice(open, i + 1);
    }
  }
  throw new Error("run.* 分支没闭合");
}

test("run 生命周期事件那一支真的刷了 session，不只是 runs 列表", () => {
  const branch = runLifecycleBranch();
  assert.match(branch, /invalidateSessionLifecycle\(/, "这一支没有刷新 session");
  assert.match(branch, /qk\.sessionRunsPrefix\(/, "这一支也应继续刷 runs 列表");
});

test("刷 session 用的是窄的那个，别在 run.* 里调终态那套", () => {
  // run.* 一轮里来很多次；终态那套会连带刷消息/变更集/文件树/冲突/产物。
  const branch = runLifecycleBranch();
  assert.equal(
    branch.includes("invalidateSessionAfterTerminal("),
    false,
    "run.* 分支不该调终态刷新 —— 太重",
  );
});

test("终态路径仍然刷 session —— 这条修复是补一条路径，不是搬走原来那条", () => {
  assert.match(SOURCE, /invalidateSessionAfterTerminal\(/);
});
