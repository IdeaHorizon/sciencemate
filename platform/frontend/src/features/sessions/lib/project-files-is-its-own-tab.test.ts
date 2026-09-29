import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

/**
 * Project files 从会话内联面板挪成左侧导航的一个页面。
 *
 * 现场（wangd，2026-08-11 试用）：「你不说那个 Project Files 我都没注意…
 * 现在在那上面感觉点开就很占地方」。
 *
 * 它和 Artifacts / Research state / Memory 是同一层级的东西 —— 那几个都在
 * 左侧栏，只有它内联在会话里，本来就不一致；展开还把对话挤下去。
 *
 * 会话内保留的是 **SessionChangesPanel**（本轮改了什么）—— 那才是你打开会话
 * 想知道的；「项目里一共有哪些文件」是另一个问题，去它自己的页面看。
 */
test("Project files 是左侧导航的一页，不再内联在会话里", () => {
  const shell = source("../../../shared/layout/AppShell.tsx");
  const page = source("../../../app/(workspace)/projects/[id]/files/page.tsx");
  const view = source("../components/ProjectFilesView.tsx");
  const workspace = source("../components/SessionWorkspace.tsx");

  assert.match(shell, /\/files`, label: \{ zh: "项目文件"/);
  assert.match(page, /ProjectFilesView/);
  assert.match(view, /ProjectFileTree/, "页面 → View → 树 这条链要接上");
  assert.doesNotMatch(
    workspace,
    /<ProjectFileTree/,
    "会话里不该再内联整棵树 —— 展开就把对话挤下去",
  );

  // 整页只有这一个东西，却默认收起 —— 用户点进来看到一行计数和一个"再点一次"
  // 的按钮（2026-08-12 实测）。折叠是布局的事，树是内容；揉在一起，内容就被
  // 上一个使用场景的布局决定绑架了。
  const tree = source("../components/ProjectFileTree.tsx");
  assert.doesNotMatch(tree, /useState\(false\)/, "树不该自带折叠状态");
  assert.doesNotMatch(tree, /aria-expanded/, "树不该自带折叠壳");
});

test("会话里保留的是「本轮改了什么」，不是「一共有什么」", () => {
  const workspace = source("../components/SessionWorkspace.tsx");
  assert.match(workspace, /SessionChangesPanel/);
});
