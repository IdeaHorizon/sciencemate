import test from "node:test";
import assert from "node:assert/strict";
import {
  clearStaleSessionPublishError,
  runFreshSessionPublish,
} from "./publish-error-policy.ts";

test("a fresh Publish clears the prior mutation error before sending", () => {
  const calls: string[] = [];
  runFreshSessionPublish(
    () => calls.push("reset"),
    () => calls.push("publish"),
  );
  assert.deepEqual(calls, ["reset", "publish"]);
});

test("successful conflict resolution clears the stale Publish error", () => {
  let resets = 0;
  clearStaleSessionPublishError(() => { resets += 1; });
  assert.equal(resets, 1);
});
