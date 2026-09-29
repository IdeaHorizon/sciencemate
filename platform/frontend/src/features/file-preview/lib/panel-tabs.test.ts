import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { resolveActiveTab } from "./panel-tabs.ts";
import {
  fileTabId,
  fileTabsForProject,
  RESEARCH_TAB_ID,
  useWorkspacePanel,
} from "../../../stores/workspace-panel.ts";

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

const tab = (path: string, projectId = "p1", sessionId = "s1") => ({ projectId, sessionId, path });

test.beforeEach(() => {
  useWorkspacePanel.setState({ open: false, activeTabId: RESEARCH_TAB_ID, files: [] });
});

// ── 该显示哪个 Tab ──────────────────────────────────────────────────────

test("文件页没有「研究进程」这一栏时落到第一个文件，而不是空面板", () => {
  /**
   * store 的初值是 research，而 Project files 页根本不提供那一栏。不处理的话
   * 从文件页打开一个文件，右栏会停在一个不存在的 Tab 上 —— 表现为"点了文件
   * 右栏是空的"。
   */
  const ids = [fileTabId(tab("paper/main.pdf"))];
  assert.equal(resolveActiveTab(RESEARCH_TAB_ID, { hasResearch: false, fileIds: ids }), ids[0]);
  assert.equal(
    resolveActiveTab(RESEARCH_TAB_ID, { hasResearch: true, fileIds: ids }),
    RESEARCH_TAB_ID,
  );
});

test("记着的那个 Tab 属于别的项目时不显示它", () => {
  // 换项目时上一个项目的文件标签不在 fileIds 里 —— 不回退的话右栏会去读一个
  // 当前项目里不存在的路径，然后报 404。
  const stale = fileTabId(tab("figures/fig1.png", "other-project"));
  assert.equal(resolveActiveTab(stale, { hasResearch: true, fileIds: [] }), RESEARCH_TAB_ID);
  const here = fileTabId(tab("data/a.csv"));
  assert.equal(resolveActiveTab(stale, { hasResearch: false, fileIds: [here] }), here);
});

test("什么都没有时不报错", () => {
  assert.equal(resolveActiveTab("file:x", { hasResearch: false, fileIds: [] }), RESEARCH_TAB_ID);
});

// ── store ───────────────────────────────────────────────────────────────

test("同一个文件打开两次不会多出一个标签", () => {
  const { openFile } = useWorkspacePanel.getState();
  openFile(tab("paper/main.pdf"));
  openFile(tab("figures/fig1.png"));
  openFile(tab("paper/main.pdf"));
  const state = useWorkspacePanel.getState();
  assert.equal(state.files.length, 2);
  // 重复打开 = 切过去，不是什么都不做（否则点了没反应）。
  assert.equal(state.activeTabId, fileTabId(tab("paper/main.pdf")));
  assert.equal(state.open, true);
});

test("同名文件来自不同会话是两个标签", () => {
  /**
   * `figures/fig1.png` 在两个会话的 worktree 里是两份不同的内容。只按 path
   * 做 id 的话，从会话 B 打开会命中会话 A 的标签，显示的是另一份文件 ——
   * 而且看起来完全正常。
   */
  const { openFile } = useWorkspacePanel.getState();
  openFile(tab("figures/fig1.png", "p1", "session-a"));
  openFile(tab("figures/fig1.png", "p1", "session-b"));
  assert.equal(useWorkspacePanel.getState().files.length, 2);
});

test("关掉当前标签时落到邻居，不是弹回研究进程", () => {
  // 连着关几个标签会在中间反复跳回研究进程 —— 每关一个都要再点回来。
  const { openFile, closeTab } = useWorkspacePanel.getState();
  openFile(tab("a.md"));
  openFile(tab("b.md"));
  openFile(tab("c.md"));
  closeTab(fileTabId(tab("b.md")));
  // 关的不是当前那个（当前是 c），当前不该变。
  assert.equal(useWorkspacePanel.getState().activeTabId, fileTabId(tab("c.md")));

  closeTab(fileTabId(tab("c.md")));
  // 关掉的就是当前那个，右边没有邻居了 → 取左边的。
  assert.equal(useWorkspacePanel.getState().activeTabId, fileTabId(tab("a.md")));

  closeTab(fileTabId(tab("a.md")));
  assert.equal(useWorkspacePanel.getState().activeTabId, RESEARCH_TAB_ID);
  assert.equal(useWorkspacePanel.getState().files.length, 0);
});

test("文件标签按项目隔离", () => {
  const files = [tab("a.md", "p1"), tab("b.md", "p2"), tab("c.md", "p1")];
  assert.deepEqual(
    fileTabsForProject(files, "p1").map((file) => file.path),
    ["a.md", "c.md"],
  );
});

// ── 接线 ────────────────────────────────────────────────────────────────

test("四个入口都接到同一个 openFile 上", () => {
  /**
   * 「打开一个文件」发起自四处：文件树、对话里的产出卡、右栏自己的标签条、
   * 以及消息正文里模型自己写的引用（`![](path)` / `[](path)`）。
   * 只查名字出现是不够的 —— 要查它**被调用**。这里查的是调用点。
   */
  const filesView = source("../../sessions/components/ProjectFilesView.tsx");
  assert.match(filesView, /onOpenFile=\{\(path\) => openFile\(\{ projectId, sessionId, path \}\)\}/);

  const tree = source("../../sessions/components/ProjectFileTree.tsx");
  assert.match(tree, /onClick=\{\(\) => onOpenFile\(entry\.path\)\}/);

  const workspace = source("../../sessions/components/SessionWorkspace.tsx");
  assert.match(workspace, /openPanelFile\(\{ projectId, sessionId, path \}\)/);
  assert.match(workspace, /<WorkspaceFileOpenerProvider[\s\S]{0,200}onOpenFile=\{openWorkspaceFile\}/);

  const activity = source("../../chat/components/CanonicalRunActivity.tsx");
  assert.match(activity, /openFile\(path\)/);

  // 正文引用：图和链接都落到同一个 openFile，不各走一套。
  const rich = source("../../chat/components/RichText.tsx");
  assert.match(rich, /onOpen=\{workspace\.openFile\}/);
  assert.match(rich, /onClick=\{\(\) => workspace\.openFile\(path\)\}/);
});

test("内联图和点开的文件必须来自同一个 worktree", () => {
  /**
   * 正文里的图自己去取字节，需要 projectId + sessionId。如果它和「点开」各取
   * 各的会话，某天其中一处改了口径（比如按"最近会话"重算），正文画的是 A 会话
   * 的图、点开看到的是 B 会话的文件 —— **两边都不会报错**。
   *
   * 判据：语境只有一个来源（同一个 context 值），不是两处各拿。
   */
  const opener = source("../components/WorkspaceFileOpener.tsx");
  assert.match(opener, /projectId: string;\s*sessionId: string;\s*openFile:/);
  const rich = source("../../chat/components/RichText.tsx");
  assert.equal(
    rich.split("useWorkspaceFiles()").length - 1, 1,
    "语境只该取一次",
  );
  assert.match(rich, /projectId=\{workspace\.projectId\}/);
  assert.match(rich, /sessionId=\{workspace\.sessionId\}/);
});

test("树和预览读同一个会话的工作区", () => {
  /**
   * 树按「最近一个会话」取，预览要是自己再算一次"哪个会话"，两边排序稍有
   * 不同就会出现"树里列着、点开 404"，而且报错指向后端。
   *
   * 判据：文件页里那个 sessionId 只有一个来源，openFile 用的就是它。
   */
  const filesView = source("../../sessions/components/ProjectFilesView.tsx");
  const occurrences = filesView.split("const sessionId").length - 1;
  assert.equal(occurrences, 1, "sessionId 只该在这里算一次");
  assert.match(filesView, /projectId=\{projectId\}/);
  assert.match(filesView, /sessionId=\{sessionId\}/);
  assert.match(filesView, /openFile\(\{ projectId, sessionId, path \}\)/);
});

test("已删除的文件不给点", () => {
  // 点开必然 404，而那个报错看起来像平台坏了。
  const opener = source("../components/WorkspaceFileOpener.tsx");
  assert.match(opener, /status !== "deleted"/);
  const activity = source("../../chat/components/CanonicalRunActivity.tsx");
  assert.match(activity, /isOpenableChange\(status\)/);
});

test("右栏关着的模式下，文件引用一律退回纯文本", () => {
  /**
   * fixture/demo 模式把右栏关了（`enabled={mode === "api"}`）。如果"可点"那一
   * 半还开着，就会得到最难查的状态：文件名长得可点，点下去 store 记下了要
   * 打开谁，而承载它的右栏根本没渲染 —— **点了没反应，且没有任何一层报错**。
   *
   * 判据：这两个开关取同一个值。分开写迟早只改一处。
   */
  const workspace = source("../../sessions/components/SessionWorkspace.tsx");
  // 必须钉在 **provider 这一处**。数"文件里有几个 enabled={mode === "api"}"
  // 是废断言：别的组件本来就有好几处，把 provider 那处删掉照样数得够。
  const at = workspace.indexOf("<WorkspaceFileOpenerProvider");
  assert.ok(at > 0, "provider 应该在");
  const providerTag = workspace.slice(at, workspace.indexOf(">", workspace.indexOf("onOpenFile", at)));
  assert.match(providerTag, /enabled=\{mode === "api"\}/, "provider 要和右栏用同一个开关");
  assert.match(
    workspace.slice(workspace.indexOf("<WorkspacePanelHost")),
    /enabled=\{mode === "api"\}/,
  );

  const opener = source("../components/WorkspaceFileOpener.tsx");
  // 关掉时 context 值是 null —— 消费方本来就按"没有 provider"处理。
  assert.match(opener, /enabled \? \{ projectId, sessionId, openFile: onOpenFile \} : null/);
});
