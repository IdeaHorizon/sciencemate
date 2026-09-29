import test from "node:test";
import assert from "node:assert/strict";
import {
  sessionChangeDetailExpanded,
  sessionUnpublishedCount,
  visibleSessionChangePaths,
} from "./session-change-projection.ts";
import type { SessionMergeConflict } from "../types.ts";

const conflict = (
  id: string,
  resourceKey: string,
  status: "open" | "resolved",
): SessionMergeConflict => ({
  id,
  resourceType: "artifact",
  resourceKey,
  baseVersionId: "base",
  projectVersionId: "project",
  proposedVersionId: "proposed",
  status,
  resolution: status === "resolved" ? { choice: "use_proposed" } : null,
  basePreview: null,
  theirsPreview: "theirs",
  oursPreview: "ours",
  previewTruncated: { base: false, theirs: false, ours: false },
});

test("open conflict owns its path presentation and suppresses the plain listing", () => {
  const paths = ["reports/shared", "reports/independent"];
  const conflicts = [conflict("conflict-a", "reports/shared", "open")];

  assert.deepEqual(visibleSessionChangePaths(paths, conflicts), ["reports/independent"]);
});

test("resolved conflicts no longer suppress the surviving changed path", () => {
  const paths = ["reports/shared"];
  const conflicts = [conflict("conflict-a", "reports/shared", "resolved")];

  assert.deepEqual(visibleSessionChangePaths(paths, conflicts), paths);
});

test("an open conflict forces details visible regardless of the ordinary fold state", () => {
  assert.equal(sessionChangeDetailExpanded(false, 1), true);
  assert.equal(sessionChangeDetailExpanded(false, 0), false);
  assert.equal(sessionChangeDetailExpanded(true, 0), true);
});

// ── 「有多少未发布改动」只能有一个答案（2026-08-17 node20 实测）──────────────

test("git 侧有改动而 ChangeItem 为 0 时，仍然如实报有改动", () => {
  // 现场：英国饮食那个会话，session 分支相对 main 有 34 files/+1122，
  // 而 session.unpublishedChangeCount（数 ChangeItem）是 0 —— 研究产出是以
  // 节点 checkpoint 直接提交的，不产生 ChangeItem。chip 因此显示"无未发布
  // 改动"，用户据此可能把一整个会话的产出当空的丢掉。
  assert.equal(
    sessionUnpublishedCount({ changedPaths: [], filesChanged: 26 }, 0),
    26,
    "说'没有'的那个源更容易被相信，不能让它单独决定",
  );
});

test("change-set 还没到时退回 session 字段，不谎称 0", () => {
  assert.equal(sessionUnpublishedCount(undefined, 3), 3);
  assert.equal(sessionUnpublishedCount(null, 0), 0);
});

test("两个口径都非零时取大的 —— 两者各数了一半，漏报比多报危险", () => {
  assert.equal(sessionUnpublishedCount({ changedPaths: ["a", "b"], filesChanged: 5 }, 2), 5);
  assert.equal(
    sessionUnpublishedCount({ changedPaths: ["a", "b", "c", "d"], filesChanged: 1 }, 4),
    4,
  );
});
