import test from "node:test";
import assert from "node:assert/strict";
import {
  adaptSessionChangeSet,
  adaptSessionConflicts,
  buildSessionCandidateRequest,
  SessionRevisionContractError,
} from "./change-set-adapter.ts";

/**
 * ⚠️ 这个文件里的样本是**手写的**，只用来钉边界情形（大小写混写的字段名、身份
 * 交叉、KB 被排除）。「后端真实发的东西读不读得懂」不在这里问 —— 手写样本是照着
 * 适配器自己的假设造的，两边自洽，证明不了中间那条真实响应（cuib 2026-09-14 那条
 * `ChangeSet.items must be an array` 就是这么漏过去的）。
 *
 * 那个问题的判据在 `change-set-contract.test.ts`：读后端真实产出的样本。
 */
const changeSet = {
  projectId: "project-a",
  sessionId: "session-a",
  gitBranch: "session/session-a",
  gitBaseCommitSha: "1111111111111111111111111111111111111111",
  gitHeadCommitSha: "2222222222222222222222222222222222222222",
  aheadBy: 2,
  behindBy: 0,
  patch: "diff --git a/reports/evidence-map b/reports/evidence-map\n-old\n+new",
  additions: 1,
  deletions: 1,
  filesChanged: 1,
  worktreeClean: true,
  patchTruncated: false,
  changedPaths: ["reports/evidence-map"],
  conflicts: [],
};

test("ChangeSet adapter keeps the git answer: paths, counters, patch", () => {
  const adapted = adaptSessionChangeSet(changeSet, { projectId: "project-a", sessionId: "session-a" });
  assert.equal(adapted.changeCount, 1);
  assert.deepEqual(adapted.changedPaths, ["reports/evidence-map"]);
  assert.equal(adapted.conflictCount, 0);
  assert.equal(adapted.gitBranch, "session/session-a");
  assert.equal(adapted.gitHeadCommitSha?.slice(0, 8), "22222222");
  assert.deepEqual([adapted.additions, adapted.deletions, adapted.filesChanged], [1, 1, 1]);
  assert.equal(adapted.worktreeClean, true);
});

test("ChangeSet adapter accepts a wrapped snake-case boundary but rejects crossed identity", () => {
  const wrapped = {
    changeSet: {
      ...changeSet,
      projectId: undefined,
      sessionId: undefined,
      changedPaths: undefined,
      gitBranch: undefined,
      project_id: "project-a",
      session_id: "session-a",
      changed_paths: ["reports/evidence-map"],
      git_branch: "session/session-a",
    },
  };
  assert.deepEqual(
    adaptSessionChangeSet(wrapped, { projectId: "project-a", sessionId: "session-a" }).changedPaths,
    ["reports/evidence-map"],
  );
  assert.throws(
    () => adaptSessionChangeSet(changeSet, { projectId: "project-b", sessionId: "session-a" }),
    /identity/,
  );
});

test("A missing changedPaths is a broken contract, not an empty Session", () => {
  const { changedPaths: _dropped, ...without } = changeSet;
  assert.throws(
    () => adaptSessionChangeSet(without, { projectId: "project-a", sessionId: "session-a" }),
    SessionRevisionContractError,
  );
});

test("KB is mechanically excluded from Session candidates", () => {
  assert.throws(
    () => buildSessionCandidateRequest({
      resourceType: "kb",
      resourceKey: "kb/claim-a",
      name: "Claim",
      content: "Not revision content",
    } as never),
    /exclude KB/,
  );
});

test("Conflict adapter maps Project theirs and Session ours without hiding truncation", () => {
  const [conflict] = adaptSessionConflicts({ items: [{
    id: "conflict-a",
    resourceType: "project_doc",
    resourceKey: "project_doc/protocol.md",
    baseVersionId: "version-base",
    projectVersionId: "version-project",
    proposedVersionId: "version-session",
    status: "open",
    resolution: null,
    basePreview: "base",
    theirsPreview: "current Project text",
    oursPreview: "Session candidate text",
    previewTruncated: { base: false, theirs: true, ours: false },
  }] });

  assert.equal(conflict.theirsPreview, "current Project text");
  assert.equal(conflict.oursPreview, "Session candidate text");
  assert.deepEqual(conflict.previewTruncated, { base: false, theirs: true, ours: false });
});
