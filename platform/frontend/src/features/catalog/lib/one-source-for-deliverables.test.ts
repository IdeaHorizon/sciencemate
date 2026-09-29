import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { readdirSync, statSync } from "node:fs";
import { join } from "node:path";

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

function everySourceFile(root: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(root)) {
    const full = join(root, name);
    if (statSync(full).isDirectory()) out.push(...everySourceFile(full));
    else if (/\.tsx?$/.test(name)) out.push(full);
  }
  return out;
}

const SRC = new URL("../../../", import.meta.url).pathname;

test("「这个项目交付了什么」只剩一个数据源", () => {
  /**
   * 2026-09-09 之前有两个：会话里的交付物条读 `/deliverables`（那条路由读的是
   * `<state>/…/deliverables/` 下的**复制品**），页面读别的。同一篇论文因此可能
   * 一处有、另一处没有，而分叉不报错。
   *
   * 判据是**扫盘**不是查名单：谁再去调那条老路由都会在这里转红，包括以后新写
   * 的组件。写名单的话，新调用点默认漏过。
   */
  // 排除的只有**这个文件自己**（它的正则里就写着那个模式），不是"所有测试
  // 文件" —— 后者会让真正写在测试里的调用点藏进来。
  const self = new URL(import.meta.url).pathname;
  const offenders = everySourceFile(SRC).filter((file) => {
    if (file === self) return false;
    const code = readFileSync(file, "utf8")
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/^\s*\/\/.*$/gm, "");
    return /listDeliverables|["'`][^"'`]*\/deliverables/.test(code);
  });
  assert.deepEqual(
    offenders.map((f) => f.slice(SRC.length)),
    [],
    "这些文件还在读那条已经退役的交付物路由",
  );
});

test("交付物条和「研究产出」页读的是同一个目录", () => {
  const bar = source("../../sessions/components/SessionDeliverables.tsx");
  const view = source("../components/ProjectOutputsView.tsx");
  for (const [name, code] of [["交付物条", bar], ["研究产出页", view]] as const) {
    assert.match(code, /useProjectCatalog\(/, `${name} 应该读目录`);
  }
  // 交付物判据只有一处：后端算好的 isDeliverable，前端不自己重算。
  assert.match(bar, /entry\.isDeliverable/);
});
