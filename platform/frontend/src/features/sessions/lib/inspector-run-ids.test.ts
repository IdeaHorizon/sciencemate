import test from "node:test";
import assert from "node:assert/strict";
import { inspectorRunIds } from "./inspector-run-ids.ts";

const ROOT = "run_186225bf26454f809dff33f9b2d45450";

test("新会话第一轮：runs 快照还是空的，消息流已经认领了 run —— 右栏不能空", () => {
  // 会话 c9deb4f2 的真实形状：runs 列表在 turn 开始前拍的快照（空），
  // 但用户消息已带 owning runId。
  assert.deepEqual(
    inspectorRunIds([{ runId: ROOT }], undefined, []),
    [ROOT],
  );
});

test("本轮 runId 只在 chat.terminal 上时也覆盖（消息 runId 回写有竞态）", () => {
  assert.deepEqual(inspectorRunIds([{ runId: null }], ROOT, undefined), [ROOT]);
});

test("三个来源去重，消息流顺序在前，runs 兜底在后", () => {
  const other = "run_older";
  assert.deepEqual(
    inspectorRunIds(
      [{ runId: other }, { runId: ROOT }],
      ROOT,
      [{ id: "run_legacy", parentRunId: null }, { id: ROOT, parentRunId: null }],
    ),
    [other, ROOT, "run_legacy"],
  );
});

test("子 run 永远不进列表 —— 它的事件已在 owning run 的读取里", () => {
  assert.deepEqual(
    inspectorRunIds([], undefined, [
      { id: `${ROOT}::_orchestrator->hypothesis@d1`, parentRunId: ROOT },
      { id: ROOT, parentRunId: null },
    ]),
    [ROOT],
  );
});
