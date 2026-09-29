import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

const view = source("../components/ProjectOutputsView.tsx");
const shell = source("../../../shared/layout/AppShell.tsx");
const api = source("../../../lib/api.ts");

test("只有工作过程默认收起 —— 用户来这一页是找那篇论文的", () => {
  /**
   * 2026-09-09 之前论文被埋在 206 件 compression_log / latex_build_receipt
   * 中间（真项目实测）。
   *
   * 「哪些档默认展开」「分组是不是排序而不是过滤」两件事的判据在
   * `group-by-tier.test.ts` —— 那里用**数据**测，不靠比对源码。这一条只
   * 剩下"渲染时用的是那个纯函数"这一句。
   */
  assert.match(view, /tier !== "working"/, "只有工作过程默认收起");
  assert.match(view, /groupByTier\(catalog\.data\?\.entries \?\? \[\]\)/);
  assert.match(view, /TIER_ORDER\.map/, "档的顺序也只有一处定义");
});

test("每一档都有自己的标题 —— 工作过程照样列得出来", () => {
  assert.match(view, /TIER_LABEL/);
  assert.match(view, /working: { zh: "工作过程"/);
});

test("一行就是一件产物，点得开的文件挂在那一行上", () => {
  // 让用户去目录树里翻、还得知道它叫 writing/latex_build/…/main_clean.pdf，
  // 正是这次要修的毛病。
  assert.match(view, /entry\.files\.map/);
  assert.match(view, /onOpenFile\(path\)/);
  assert.match(view, /onOpenFile\(entry\.recordPath\)/, "那份 JSON 记录也要能打开");
});

test("判决不在前端重算 —— 只读 harness 给的 tier", () => {
  /**
   * 重算就是第七个各说各话的入口。前端不许自己判"什么算交付物"。
   */
  assert.doesNotMatch(view, /frozen\s*&&\s*permanent/, "交付物判据归 core/catalog.py");
  assert.doesNotMatch(view, /retention/, "策略表的事不该出现在前端");
  // 分档只读后端给的 tier —— 断言落在真的读它的那个文件上。
  assert.match(source("./group-by-tier.ts"), /entry\.tier/);
  assert.doesNotMatch(source("./group-by-tier.ts"), /frozen|permanent|kind/);
});

test("类型名取自共用的那张表 —— 表本身的判据在 kind-label.test.ts", () => {
  assert.match(view, /artifactKindLabel\(entry\.kind, lang\)/);
});

test("导航里「研究产出」是这个问题的唯一入口", () => {
  assert.match(shell, /\$\{root\}\/outputs`, label: \{ zh: "研究产出"/);
});

test("目录是一次请求，不是前端把几个来源拼起来", () => {
  // 拼接就是把分裂往下挪一层：几个来源就有几份各自演化的答案。
  assert.match(api, /getProjectCatalog/);
  assert.match(api, /\/catalog\$\{query\}/);
});
