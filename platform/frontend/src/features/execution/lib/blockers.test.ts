import assert from "node:assert/strict";
import test from "node:test";

import { adaptBlockers, blockerCategoryLabel, openBlockers } from "./blockers.ts";

const PAYLOAD = {
  schemaVersion: 1,
  blockers: [
    {
      id: "blk_1", runId: "run-1", sessionId: "s1", reportingNode: "experiment",
      category: "missing_resource", summary: "GPU 队列没有权限",
      requestedAction: "给 GPU 队列授权", suggestedOwner: "user",
      retryableAfterChange: true, evidencePaths: ["experiments/logs/queue.txt"],
      reportedAt: "2026-08-07T00:00:00Z", runStatus: "incomplete", stale: false,
    },
    {
      id: "blk_2", runId: "run-2", sessionId: "s1", reportingNode: "data",
      category: "missing_input", summary: "缺 dataset", requestedAction: "",
      suggestedOwner: "", retryableAfterChange: false, evidencePaths: [],
      reportedAt: "2026-08-06T00:00:00Z", runStatus: "completed", stale: true,
    },
  ],
};

test("解析阻塞列表", () => {
  const rows = adaptBlockers(PAYLOAD);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].requestedAction, "给 GPU 队列授权");
  assert.deepEqual(rows[0].evidencePaths, ["experiments/logs/queue.txt"]);
  assert.equal(rows[1].retryableAfterChange, false);
});

test("stale 的不该催用户去处理", () => {
  assert.deepEqual(openBlockers(adaptBlockers(PAYLOAD)).map((b) => b.id), ["blk_1"]);
});

test("形状不对返回空数组，不抛", () => {
  assert.deepEqual(adaptBlockers(undefined), []);
  assert.deepEqual(adaptBlockers({}), []);
  assert.deepEqual(adaptBlockers({ blockers: "nope" }), []);
  assert.deepEqual(adaptBlockers({ blockers: [null, {}, { id: "" }] }), []);
});

test("retryableAfterChange 缺省视为 true（保守：假定还能救）", () => {
  const rows = adaptBlockers({ blockers: [{ id: "x" }] });
  assert.equal(rows[0].retryableAfterChange, true);
});

test("类别标签：认识的翻译，不认识的也读得通", () => {
  assert.equal(blockerCategoryLabel("missing_resource"), "Missing resource");
  assert.equal(blockerCategoryLabel("brand_new_category"), "Brand New Category");
});
