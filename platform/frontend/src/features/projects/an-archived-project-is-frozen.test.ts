/**
 * 归档 = 冻结（服务器那一半：`platform/backend/tests/test_an_archived_project_is_frozen.py`）。
 *
 * 界面这一半钉的是：归档的项目一进来就说清楚只能看、能恢复的人（服务器答的 `can_archive`）在那儿
 * 恢复；清单里收在最下面折起来；归档按钮只给负责人 / 组织管理员；组织页那一问点名组织；状态只剩两种。
 */
import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";

const shown = (path: string) => readFileSync(path, "utf8")
  .replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

test("归档的项目一进来就说清楚，恢复按钮只给能做主的人", () => {
  const banner = shown("src/features/projects/components/ArchivedBanner.tsx");
  const shell = shown("src/features/projects/components/ProjectShellRoute.tsx");
  assert.match(banner, /if \(project\.data\?\.status !== "archived"\) return null;/);
  assert.match(banner, /\{project\.data\.can_archive && \(/, "恢复按钮没以 can_archive 为条件");
  assert.match(banner, /api\.restoreProject\(projectId\)/);
  assert.match(shell, /<ArchivedBanner projectId=\{projectId\} \/>/, "横条没挂在项目页上");
});

test("归档在项目设置里，只给负责人 / 组织管理员、只对进行中的", () => {
  const page = shown("src/app/(workspace)/projects/[id]/settings/page.tsx");
  assert.match(page, /\{project\.can_archive && project\.status === "active" && <ArchiveThisProject/);
  assert.match(page, /api\.archiveProject\(projectId\)/);
});

test("清单里归档的收在最下面、折起来", () => {
  const list = shown("src/features/projects/components/ProjectsList.tsx");
  assert.match(list, /const live = projects\.filter\(\(p\) => p\.status !== "archived"\)/);
  assert.match(list, /<details className="project-archived-group">/);
  assert.match(list, /byHome\(live\)/, "归档的还混在分组里");
});

// 组织页那一半（归档 / 恢复点名组织、OrganisationProjects 里没有并掉的状态）：
// src/pro/features/organisation/an-archived-project-is-frozen-there-too.test.ts。

test("项目状态只剩两种", () => {
  const api = readFileSync("src/lib/api.ts", "utf8");
  assert.match(api, /export type ProjectStatus = "active" \| "archived";/);
  assert.doesNotMatch(shown("src/features/projects/lib/project-identity.ts"), /paused|completed:/, "project-identity 里还有并掉的状态");
});

test("开会话、收起会话的按钮跟着这个项目的 drive 能力走，不只看账号角色", () => {
  const index = shown("src/features/sessions/components/SessionIndex.tsx");
  assert.match(index, /const drivesThisProject = project\.data\?\.capabilities\?\.includes\("drive"\) \?\? false;/);
  assert.match(index, /const canCreate = canCreateSessions\(user\) && mode === "api" && drivesThisProject;/,
    "归档的项目（和只读进来的人）还画着一个点了就 403 的「新建会话」");
  assert.match(index, /const canArchive = canCreateSessions\(user\) && mode === "api" && drivesThisProject;/);
});
