import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import test from "node:test";

import { adaptSessionChangeSet } from "./change-set-adapter.ts";
import { sessionUnpublishedCount, visibleSessionChangePaths } from "./session-change-projection.ts";

/**
 * 改动面板读的那份响应，由**后端真实产出**的样本钉住。
 *
 * cuib 2026-09-14 报的 `ChangeSet.items must be an array`：后端把版本账连表删了、
 * 答案改由 git 给，而这个适配器还在要求 `items` / `id` / `status` —— 每个人、
 * 每个会话，展开面板就是那条红字。
 *
 * 它能红着活很久，是因为原来的适配器测试喂的是**手写 fixture**，而 fixture 是
 * 照着适配器自己的假设造的：两边都自洽，中间那条真实响应没人对过。所以这里读的
 * 不是手写的东西，是 `platform/backend/tests/test_change_set_contract_fixtures.py`
 * 用真实端点产出的那批字节。后端改了形状 → 那边红；这边读不懂 → 这边红。
 */
const FIXTURES = new URL(
  "../../../../../contracts/fixtures/session-change-set/",
  import.meta.url,
).pathname;

function load(name: string) {
  return JSON.parse(readFileSync(`${FIXTURES}${name}`, "utf8")) as Record<string, unknown>;
}

const names = readdirSync(FIXTURES).filter((name) => name.endsWith(".json")).sort();

test("后端真实产出的每一个样本，适配器都读得懂", () => {
  assert.ok(names.length >= 2, "一个真样本不够：空数组藏不住条目的形状");
  for (const name of names) {
    const payload = load(name);
    const adapted = adaptSessionChangeSet(payload, {
      projectId: payload.projectId as string,
      sessionId: payload.sessionId as string,
    });
    assert.equal(adapted.sessionId, payload.sessionId, name);
    assert.deepEqual(adapted.changedPaths, payload.changedPaths, name);
    assert.equal(adapted.changeCount, (payload.changedPaths as unknown[]).length, name);
    assert.equal(adapted.filesChanged, payload.filesChanged, name);
  }
});

test("有改动那个样本：路径、统计、冲突都到得了面板", () => {
  const payload = load("changed-with-conflict.json");
  const changeSet = adaptSessionChangeSet(payload, {
    projectId: payload.projectId as string,
    sessionId: payload.sessionId as string,
  });

  assert.deepEqual(changeSet.changedPaths, ["SHARED.md", "literature/notes.md"]);
  assert.equal(changeSet.conflictCount, 1);
  assert.equal(changeSet.aheadBy, 2);
  assert.equal(changeSet.behindBy, 1);
  assert.ok(changeSet.patch.includes("diff --git"));

  // 冲突的那个路径在冲突卡片里出现，正文列表就不再重复一遍。
  const visible = visibleSessionChangePaths(changeSet.changedPaths, [
    { id: "c1", resourceType: "artifact", resourceKey: "SHARED.md", status: "open" },
  ] as never);
  assert.deepEqual(visible, ["literature/notes.md"]);
});

test("干净会话是 0，不是「读不出来」", () => {
  const payload = load("clean-session.json");
  const changeSet = adaptSessionChangeSet(payload, {
    projectId: payload.projectId as string,
    sessionId: payload.sessionId as string,
  });
  assert.deepEqual(changeSet.changedPaths, []);
  assert.equal(sessionUnpublishedCount(changeSet, 0), 0);
});

test("缺了 changedPaths 才是契约破了 —— 那时要响亮地报出来", () => {
  const payload = load("clean-session.json");
  delete payload.changedPaths;
  assert.throws(
    () => adaptSessionChangeSet(payload, {
      projectId: payload.projectId as string,
      sessionId: payload.sessionId as string,
    }),
    /changedPaths must be an array/,
  );
});
