import test from "node:test";
import assert from "node:assert/strict";
import {
  sessionDriverLabel,
  sessionFrozenModelLabel,
  sessionHasActiveDriver,
  sessionHasAdvancedBase,
  sessionIsDrivenBy,
  sessionRevisionLabel,
} from "./session-presentation.ts";

const session = (
  source: "canonical" | "legacy" | "fixture",
  headCommitSha: string | null,
  behindBy = 0,
) => ({ source, headCommitSha, behindBy });

test("the version line names a commit you can actually go look at", () => {
  // 从前是 `main · r6` —— 一个只在 project_revisions 表里有意义的号码。
  // 现在是 main 的短 sha：能拿去 `git show`（RFC X1）。
  assert.equal(
    sessionRevisionLabel(session("canonical", "abcdef1234567890" + "0".repeat(24)) as never),
    "main · abcdef12",
  );
  assert.equal(
    sessionRevisionLabel(session("canonical", null) as never),
    "读不到版本信息",
  );
});

test("base divergence is how far main moved since this session branched", () => {
  assert.equal(sessionHasAdvancedBase(session("canonical", "a".repeat(40), 2) as never), true);
  assert.equal(sessionHasAdvancedBase(session("canonical", "a".repeat(40), 0) as never), false);
});

test("driver labels distinguish archived records from active unclaimed sessions", () => {
  assert.equal(sessionDriverLabel({ lifecycleStatus: "archived", primaryDriverUserId: null, driverLabel: "Unassigned" } as never), null);
  assert.equal(sessionDriverLabel({ lifecycleStatus: "active", primaryDriverUserId: null, driverLabel: "Unassigned" } as never), "没人在开车");
  assert.equal(sessionDriverLabel({ lifecycleStatus: "active", primaryDriverUserId: "user-a", driverLabel: "Mia Zhang" } as never), "Mia Zhang · 驾驶者");
});

test("who is driving comes from the server, and it never expires", () => {
  // 驾驶权租约 2026-09-05 删除。后端现算「谁在开」= 最近一条用户消息的作者，
  // 所以前端这里只剩身份比对：没有到期时间可读，也就没有第二份判据会和后端
  // 分叉。「我能不能开」是另一个问题，只有后端答（execution.answer.via）。
  const driven = {
    source: "canonical",
    lifecycleStatus: "active",
    capabilities: ["drive"],
    primaryDriverUserId: "mia",
  };
  const unclaimed = { ...driven, primaryDriverUserId: null };

  assert.equal(sessionHasActiveDriver(driven as never), true);
  assert.equal(sessionHasActiveDriver(unclaimed as never), false);
  assert.equal(sessionIsDrivenBy(driven as never, "mia"), true);
  assert.equal(sessionIsDrivenBy(driven as never, "chen"), false);
  assert.equal(sessionIsDrivenBy(unclaimed as never, "mia"), false);
  assert.equal(sessionDriverLabel({ ...unclaimed, driverLabel: "Unassigned" } as never), "没人在开车");
});

/**
 * 这里曾经有两条 `sessionComposerAccess` 的用例（"未认领的会话可以打字即接管"
 * / "驾驶者与协作者的区别"）。2026-09-01 连同那个函数一起删除 ——
 *
 * 它在客户端**又算了一遍**驾驶权租约，而后端同时在算能力与归档。两半的交集
 * 才是真的"能不能驱动"，却没有任何一层持有那个交集：`canSend=true` 与一个灰着
 * 的输入框可以同时成立，反过来也可以。
 *
 * 意图没有丢，判据搬去了它该在的那一层，落在真 HTTP 响应上：
 * `platform/backend/tests/test_local_runtime_api.py::test_canonical_session_membership_and_driver_contract`
 * —— 接管之后，被接管走的那个人拿到 `answer.via === "none"` + 原因 + 到期时刻。
 */

test("the frozen Session model label removes credential provenance before linking", () => {
  assert.equal(sessionFrozenModelLabel("DeepSeek (environment)", "en"), "DeepSeek · fixed for this Session");
  assert.equal(sessionFrozenModelLabel("DeepSeek (environment)"), "DeepSeek · 这个会话固定用它");
  assert.equal(sessionFrozenModelLabel("GPT-5", "en"), "GPT-5 · fixed for this Session");
  assert.equal(sessionFrozenModelLabel(null, "en"), "Project model · fixed for this Session");
});
