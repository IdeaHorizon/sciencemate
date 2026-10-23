import assert from "node:assert/strict";
import test from "node:test";
import { layoutFeedCards } from "./feed-masonry.ts";

test("cards fill the shortest actual column and preserve a partial final row", () => {
  const result = layoutFeedCards([500, 100, 300, 90, 80], 3, 12);
  assert.deepEqual(result.positions, [
    { column: 0, top: 0 }, { column: 1, top: 0 }, { column: 2, top: 0 },
    { column: 1, top: 112 }, { column: 1, top: 214 },
  ]);
  assert.equal(result.height, 500);
});

test("late image height changes relocate subsequent cards without overlap", () => {
  const before = layoutFeedCards([100, 110, 120, 80], 3, 12);
  const after = layoutFeedCards([400, 110, 120, 80], 3, 12);
  assert.equal(before.positions[3].column, 0);
  assert.deepEqual(after.positions[3], { column: 1, top: 122 });
  const appended = layoutFeedCards([400, 110, 120, 80, 90], 3, 12);
  assert.deepEqual(appended.positions.slice(0, 4), after.positions);
  assert.deepEqual(appended.positions[4], { column: 2, top: 132 });
});

test("single column and empty results have correct container heights", () => {
  assert.deepEqual(layoutFeedCards([], 3, 12), { positions: [], height: 0 });
  assert.deepEqual(layoutFeedCards([100, 200], 1, 12), {
    positions: [{ column: 0, top: 0 }, { column: 0, top: 112 }], height: 312,
  });
});
