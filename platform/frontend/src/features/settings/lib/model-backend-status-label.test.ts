import test from "node:test";
import assert from "node:assert/strict";
import {
  MODEL_BACKEND_STATUSES,
  modelBackendStatusLabel,
} from "./model-backend-presentation.ts";

test("后端能吐出的每个状态都得有文案 —— 漏一个就掉进「Status not reported」", () => {
  for (const status of MODEL_BACKEND_STATUSES) {
    const label = modelBackendStatusLabel(status);
    assert.notEqual(
      label,
      "Status not reported",
      `${status} 没有配文案：界面会把一个平台**已经判定**的状态显示成「没数据」`,
    );
  }
});

test("credentials_rejected 必须说成「坏了」，不能说成「没上报」", () => {
  // 这是回归钉子：2026-08-21 本机实测，一条 key 已失效的连接界面上写着
  // "Status not reported" —— 整个凭证健康机制唯一的坏消息状态没被翻译。
  assert.equal(modelBackendStatusLabel("credentials_rejected"), "Credential rejected");
});

test("真·未知状态才走兜底", () => {
  assert.equal(modelBackendStatusLabel("something_invented_later"), "Status not reported");
});
