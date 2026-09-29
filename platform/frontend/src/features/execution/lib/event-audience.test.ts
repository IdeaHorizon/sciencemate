import test from "node:test";
import assert from "node:assert/strict";
import { ALL_KINDS, DECLARED_KINDS, audienceOf } from "./event-audience.ts";

/**
 * 受众必须**穷尽**声明 —— 这是这层存在的全部意义。
 *
 * 2026-08-18 一晚栽三次，都是同一形状：「这条该不该进对话」从 kind 现推，
 * 而推导散在多处。加一个来源或挪一次位置，就有某处判断是错的，且**错了
 * 不报错** —— 文字要么消失（用户问了没人答）要么泛滥（满屏日志）。
 */

test("每一种事件都声明了受众，一个都不能漏", () => {
  const undeclared = ALL_KINDS.filter((kind) => !DECLARED_KINDS.includes(kind));
  assert.deepEqual(undeclared, [], "新事件类型必须回答'这说给谁听'");
});

test("没声明的事件类型 fail loud，不许被谁替它猜", () => {
  assert.throws(() => audienceOf("some.brand.new.kind"), /没有声明受众/);
});

test("要人做的事永远是对话受众 —— 降级成'过程'就等于把人晾在那", () => {
  for (const kind of [
    "decision.required", "permission.required", "orchestrator.said",
    "session.message", "budget.warning",
  ]) {
    assert.equal(audienceOf(kind), "conversation", kind);
  }
});

test("节点内部独白与工具调用是过程受众", () => {
  for (const kind of ["agent.message", "tool.started", "step.started", "usage.updated"]) {
    assert.equal(audienceOf(kind), "process", kind);
  }
});
