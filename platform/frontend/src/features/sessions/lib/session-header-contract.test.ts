/**
 * 会话顶栏的取舍 —— 钉住，别再长回去。
 *
 * 顶栏是每次打开会话第一眼看到的地方，也因此是最容易被"顺手加一个"的地方：
 * 它长到过 82px 高、三行元信息、四个并排按钮，而其中两个（Take control /
 * Review updates）在 ⋯ 菜单里还各有一个副本。真正每次都要看的只有两件事 ——
 * 这是哪个会话、它在干什么。
 *
 * 这组测试读源码。理由是这里要守的不是某个函数的行为，而是**哪些东西不许
 * 出现在顶栏**；行为测试验不了"没有多出一个按钮"。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";

const source = (relative: string) =>
  readFileSync(fileURLToPath(new URL(relative, import.meta.url)), "utf8");

const workspace = source("../components/SessionWorkspace.tsx");
const header = workspace.slice(
  workspace.indexOf("<header className=\"session-workspace-header\">"),
  workspace.indexOf("</header>"),
);
const menu = header.slice(header.indexOf("<details>"));
const persistent = header.slice(0, header.indexOf("<details>"));
/** 只取 import 段 —— 问的是"有没有接线"，不是"注释里有没有提到"。 */
const workspaceImports = workspace.slice(0, workspace.indexOf("function SessionCanonicalRunAccessory"));

test("the persistent header carries only identity and state", () => {
  assert.match(persistent, /SessionTitle/);
  assert.match(persistent, /sessionStateLabel/);
  // 版本号 / 基线 / 驾驶者 / 指令快照 —— 四样都搬进「会话信息」了。
  assert.doesNotMatch(persistent, /sessionRevisionLabel/);
  assert.doesNotMatch(persistent, /Based on r/);
  assert.doesNotMatch(persistent, /sessionDriverLabel/);
  assert.doesNotMatch(persistent, /SessionAboutDrawer/);
});

test("研究进程 stays a persistent toggle", () => {
  // 右栏开关是常驻的例外：它是会话里最常用的一个动作，藏进菜单等于每次
  // 多两步。
  assert.match(persistent, /session-inspector-toggle/);
  assert.match(persistent, /研究进程/);
});

test("Take control is gone from the whole header", () => {
  /**
   * 发消息本来就会自动接管驾驶权（`sendText` 里那段 acquireDriver），所以
   * 这个按钮从来没有独占的用途。删掉它的前提是菜单里那些 driver-only 的
   * 操作也得会自己接管 —— 见下一条。
   */
  assert.doesNotMatch(header, /Take control/);
});

test("driver-only menu actions acquire the lease themselves", () => {
  /**
   * 这条是删掉 Take control 的**代价**。没有它，一个没在开车的人打开菜单
   * 会看到一排灰的按钮，而界面上再没有任何地方能让他拿到驾驶权。
   */
  for (const action of ["undoMutation", "resetMutation", "stopMutation"]) {
    const at = menu.indexOf(action);
    assert.ok(at > 0, `${action} 应该在菜单里`);
    const clause = menu.slice(Math.max(0, at - 400), at + 200);
    assert.match(clause, /withDriver/, `${action} 必须走 withDriver 自己接管驾驶权`);
  }
  assert.match(workspace, /const withDriver = async/);
});

test("no action is rendered twice in the header", () => {
  // 曾经的 `session-mobile-action`：同一个功能在顶栏和菜单里各渲染一遍，
  // 靠 CSS 决定显示哪个。两份副本会各自演化。
  assert.doesNotMatch(header, /session-mobile-action/);
  for (const label of ["看改动", "归档会话", "研究进程"]) {
    const occurrences = header.split(label).length - 1;
    assert.equal(occurrences, 1, `${label} 在顶栏里应该只出现一次`);
  }
});

test("the project-level KB action left the session menu", () => {
  /**
   * `skipProjectDreaming(projectId)` 影响的是整个项目下所有会话。挂在会话
   * 菜单里，用的人无从知道自己刚刚影响了多大范围。
   */
  assert.doesNotMatch(workspaceImports, /skipProjectDreaming/);
  assert.doesNotMatch(menu, /skipDreaming/);
  assert.match(
    source("../../projects/components/ProjectDreamingPanel.tsx"),
    /skipProjectDreaming/,
  );
});

test("「停止这一轮」只在真停得掉的时候出现", () => {
  // 终态 run 上出现一个停不掉的按钮，比没有按钮更糟。
  const stopAt = menu.indexOf("停止这一轮");
  assert.ok(stopAt > 0);
  assert.match(menu.slice(Math.max(0, stopAt - 500), stopAt), /stoppableRunId/);
});

/**
 * 下面两条原来读 SessionWorkspace.tsx —— 那时候拖拽机械就长在会话页里。
 *
 * 右栏改成"研究进程只是其中一个 Tab"之后，这一百来行搬进了
 * `WorkspacePanelHost`（文件页也要同一栏，见该文件头注释）。要守的**不变量
 * 没变**：可拖、键盘可调、宽度记得住、存下来的值在挂载后才读。所以断言跟着
 * 实现走，钉住新的位置，而不是删掉。
 */
const panelHost = source("../../file-preview/components/WorkspacePanelHost.tsx");

test("the panel width is resizable and persisted", () => {
  assert.match(panelHost, /session-inspector-resizer/);
  assert.match(panelHost, /clampInspectorWidth/);
  assert.match(panelHost, /writeInspectorWidth/);
  // 键盘也要能调：拖拽是唯一入口的话，用不了鼠标的人只能接受写死的宽度。
  assert.match(panelHost, /ArrowLeft/);
  assert.match(panelHost, /role="separator"/);
  // 而且只能有一份 —— 会话页不该再自己留一套。
  assert.doesNotMatch(workspace, /session-inspector-resizer/);
  assert.doesNotMatch(workspace, /clampInspectorWidth/);
});

test("the stored width is read after mount, not during render", () => {
  /**
   * `useState(readInspectorWidth())` 会在服务端渲染时执行 —— 那里没有
   * window。初值必须是常量，存下来的宽度在 effect 里补。
   */
  assert.match(panelHost, /useState\(INSPECTOR_DEFAULT_WIDTH\)/);
  // readInspectorWidth 必须在 useEffect 里被调用，不能出现在 useState 的初值上。
  assert.match(panelHost, /useEffect\([\s\S]{0,120}readInspectorWidth\(\)/);
  assert.doesNotMatch(panelHost, /useState\([^)]*readInspectorWidth/);
});
