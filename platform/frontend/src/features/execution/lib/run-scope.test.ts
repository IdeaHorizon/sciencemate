import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { belongsToRun } from "./run-scope.ts";

test("一轮 = 这条 run 和它派出去的子节点", () => {
  assert.equal(belongsToRun({ runId: "r" }, "r"), true);
  assert.equal(belongsToRun({ runId: "child", parentRunId: "r" }, "r"), true);
  assert.equal(belongsToRun({ runId: "someone-else" }, "r"), false,
    "放宽 ≠ 取消：别人家的 run 混进来说明后端过滤错了，要吵");
  assert.equal(belongsToRun({ runId: "grandchild", parentRunId: "child" }, "r"), false,
    "只认一层 —— UI 上也没有更深的层级");
});

test("判据只有一处 —— 没有第七个地方自己判", () => {
  /**
   * 2026-08-12：「一轮包含子节点」这个定义前端有六处各写一遍，我改的时候
   * 一次只找到两三处，同一个下午撞了三轮，每轮症状都是整页执行记录空白、
   * 报错指向读取代码。
   *
   * 判据散着放，就不存在"改对"这回事，只存在"这次改到了几处"。
   * 这条**扫盘**：任何比较 runId 的地方都必须走 `belongsToRun`。
   */
  const dir = new URL(".", import.meta.url);
  const offenders: string[] = [];
  for (const file of readdirSync(dir)) {
    if (!file.endsWith(".ts") || file.endsWith(".test.ts") || file === "run-scope.ts") continue;
    const source = readFileSync(new URL(file, dir), "utf8");
    const lines = source.split("\n");
    lines.forEach((line, index) => {
      if (line.trim().startsWith("*") || line.trim().startsWith("//")) return;
      // 例外要**写出理由**：帧级检查（token / run.end）判的不是事件归属，
      // 严格比较才对。给例外一个出口，但必须留下 `run-scope:frame` 标记 ——
      // 第七处想绕过就得先解释自己为什么不一样。
      if (lines.slice(Math.max(0, index - 3), index).some((l) => l.includes("run-scope:frame"))) return;
      // 直接拿 runId 做相等/不等比较 = 自己判了一遍
      if (/\brunId\s*[!=]==\s/.test(line) && !line.includes("belongsToRun")) {
        offenders.push(`${file}:${index + 1}: ${line.trim()}`);
      }
    });
  }
  assert.deepEqual(offenders, [],
    "这些地方自己判了 run 归属，绕过 belongsToRun：\n  " + offenders.join("\n  "));
});
