import assert from "node:assert/strict";
import test from "node:test";
import { shouldPreloadFeedPage } from "./feed-pagination.ts";

test("six translated cards do not exhaust a 21-index page", () => {
  assert.equal(shouldPreloadFeedPage(21, 6), true);
  assert.equal(shouldPreloadFeedPage(42, 6), true);
  assert.equal(shouldPreloadFeedPage(63, 6), false);
  assert.equal(shouldPreloadFeedPage(21, 21), true);
  assert.equal(shouldPreloadFeedPage(42, 21), false);
  assert.equal(shouldPreloadFeedPage(0, 0), false);
});
