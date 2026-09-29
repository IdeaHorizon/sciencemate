import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

/**
 * 「留空 = 用平台默认（N）」里的 N 是**框架的兜底常量**，在进程边界两侧各写
 * 一次。这里钉住相等：核心把默认从 120000 提到 256000 而占位文案没跟上，
 * 界面就会理直气壮地告诉人一个假数 —— 而这一栏的全部意义就是"不填会怎样"。
 */
test("上下文窗口占位文案里的默认值 = core/harness.py 的 DEFAULT_MAX_CONTEXT_TOKENS", () => {
  const core = readFileSync(
    new URL("../../../../../../core/harness.py", import.meta.url),
    "utf8",
  );
  const match = core.match(/^DEFAULT_MAX_CONTEXT_TOKENS\s*=\s*(\d+)/m);
  assert.ok(match, "core/harness.py 里没找到 DEFAULT_MAX_CONTEXT_TOKENS");

  const page = readFileSync(
    // 表单在本机页和组织页共用的那一块里（`ModelConnections`）。
    new URL("../components/ModelConnections.tsx", import.meta.url),
    "utf8",
  );
  const shown = page.match(/留空 = 用平台默认（(\d+)）/);
  assert.ok(shown, "models 设置页没找到那句占位文案");
  assert.equal(shown[1], match[1]);
});
