import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

/**
 * 高危审批卡上，被批的那条命令必须**默认可见**。
 *
 * 后端把命令原文（工具名、命中类别、前 300 字符 payload）放进 pause 的
 * `context`，一路送到组件 —— 链上每一环都对。但组件把它折进一个不起眼的
 * "Context" 折面，默认收起。2026-08-22 实测：排查的人（我）两次没发现它
 * 在场 —— innerText 不含折叠内容，肉眼也不会去点一个次要材料折面 ——
 * 差点当成"字段被管道吞了"去修后端。普通用户更不会点：他们看到的就是
 * 一个只有问题和两个按钮的框，被要求批一个看不见内容的高危操作。
 *
 * 后端 `_pending_approval` 的注释原话早就把判据写明了：
 * 「审批一个看不见内容的高危操作没有意义 —— 这一段必须原样送到人面前。」
 * 送到组件手里然后默认藏起来，等于没送。
 *
 * 修法：`kind === "permission"` 时 details 默认展开。其它类（decision /
 * human_input）的 context 是背景材料不是审批对象，维持折叠 —— 那个折叠
 * 决策有自己的理由（常驻 224px 占面板 39%），不推翻。
 *
 * （无 DOM runner，按本目录家规用源码断言锁接线 —— 同
 * pause-precedence.test.ts。）
 */

const HERE = dirname(fileURLToPath(import.meta.url));
const SOURCE = readFileSync(
  join(HERE, "..", "components", "HumanInputPrompt.tsx"),
  "utf8",
);

test("permission 类停靠的 context 默认展开", () => {
  assert.ok(
    /<details[^>]*open=\{pause\.kind === "permission"\}/.test(SOURCE),
    "context 的 <details> 必须带 open={pause.kind === \"permission\"} —— " +
      "高危审批的命令原文默认可见，其它类维持折叠",
  );
});

test("context 折面本身还在（没被顺手改成常驻展开）", () => {
  assert.ok(
    SOURCE.includes('<summary>{t({ zh: "背景"'),
    "非 permission 类的 context 仍应是可折叠的次要材料",
  );
});
