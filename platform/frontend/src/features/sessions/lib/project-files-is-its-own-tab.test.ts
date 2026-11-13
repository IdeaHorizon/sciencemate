import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

/** Project files open from the workspace toolbar, without embedding the tree in a conversation. */
test("Project files 从工作区顶部打开，不再内联在会话里", () => {
  const shell = source("../../projects/components/ProjectToolbar.tsx");
  const page = source("../../../app/(workspace)/projects/[id]/files/page.tsx");
  const view = source("../components/ProjectFilesView.tsx");
  const workspace = source("../components/SessionWorkspace.tsx");

  assert.match(shell, /id: "files", label: \{ zh: "文件"/);
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
