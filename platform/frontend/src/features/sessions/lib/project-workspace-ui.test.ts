import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

test("Session changes render the live Git workspace instead of Artifact rows only", () => {
  const panel = source("../components/SessionChangesPanel.tsx");
  const projectFiles = source("../components/ProjectFileTree.tsx");
  const workspace = source("../components/SessionWorkspace.tsx");
  const hooks = source("../hooks/useSessions.ts");

  // 计数公式收进了 sessionUnpublishedCount（面板与 composer chip 共用一个
  // 答案，见该函数注释）；面板显示的仍是真实 Git 工作区计数，不是 Artifact 行数。
  assert.match(panel, /sessionUnpublishedCount\(changeSet, session\.unpublishedChangeCount\)/);
  assert.match(
    source("./session-change-projection.ts"),
    /changeSet\?\.filesChanged/,
  );
  assert.match(panel, /live workspace diff/);
  assert.match(panel, /GitDiffViewer patch=\{changeSet\.patch\}/);
  assert.match(panel, /Publish changes/);
  assert.match(workspace, /changeSet\.filesChanged > 0/);
  assert.match(workspace, /changeSet\.worktreeClean/);
  assert.match(workspace, /Publish \$\{changeSet\.filesChanged\} Session file change/);
  // 2026-08-11 翻转：整棵文件树挪去了左侧导航（见
  // project-files-is-its-own-tab.test.ts）。会话里只留「本轮改了什么」——
  // 内联展开整棵树会把对话挤下去，而且它和 Artifacts / Research state
  // 同层级，那几个都在左侧栏。
  assert.doesNotMatch(workspace, /<ProjectFileTree/);
  assert.match(projectFiles, /Project files/);
  // 目录行不展开也要说出里面有多少条没提交的改动 —— 否则"哪儿变了"要靠一层
  // 层点开找。
  assert.match(projectFiles, /entry\.changedCount/);
  assert.match(projectFiles, /entry\.owner/);
  assert.match(hooks, /refetchInterval: enabled \? 5000 : false/);
});
