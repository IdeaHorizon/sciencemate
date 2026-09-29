import test from "node:test";
import assert from "node:assert/strict";
import { diagnosticsFailureMessage, diagnosticsFilename } from "./diagnostics.ts";

test("服务器给的文件名原样用", () => {
  assert.equal(
    diagnosticsFilename('attachment; filename="session-diagnostics-f4a5d198-20260923-163302.zip"', "f4a5d198-d838"),
    "session-diagnostics-f4a5d198-20260923-163302.zip",
  );
});

test("没给文件名、或给的名字带路径，就自己拼一个，不照着路径存", () => {
  assert.equal(diagnosticsFilename(null, "f4a5d198-d838"), "session-diagnostics-f4a5d198.zip");
  assert.equal(
    diagnosticsFilename('attachment; filename="../../evil.zip"', "f4a5d198-d838"),
    "session-diagnostics-f4a5d198.zip",
  );
});

test("旧的组织服务器没有这个接口：说该升级哪一边，而不是一句出错了", () => {
  const message = diagnosticsFailureMessage(404, "Not Found", "zh");
  assert.match(message, /升级/);
  assert.match(message, /组织服务器/);
});

test("会话不在了就照后端的话说，不冒充成版本问题", () => {
  const message = diagnosticsFailureMessage(404, "Session not found", "zh");
  assert.doesNotMatch(message, /升级/);
  assert.match(message, /Session not found/);
});

test("后端没给原因时至少说出状态码", () => {
  assert.match(diagnosticsFailureMessage(500, { weird: true }, "en"), /HTTP 500/);
});
