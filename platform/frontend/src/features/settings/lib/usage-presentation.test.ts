import assert from "node:assert/strict";
import test from "node:test";
import { buildUsageSeries, formatKnownCost, usageIntensity } from "./usage-presentation.ts";

test("usage series fills missing calendar days without inventing recorded activity", () => {
  const series = buildUsageSeries(
    [{ date: "2026-08-03", total_tokens: 120, run_count: 2 }],
    3,
    new Date("2026-08-04T12:00:00Z"),
  );
  assert.deepEqual(series.map((day) => [day.date, day.total_tokens, day.recorded]), [
    ["2026-08-02", 0, false],
    ["2026-08-03", 120, true],
    ["2026-08-04", 0, false],
  ]);
});

test("usage intensity is derived only from recorded token totals", () => {
  assert.equal(usageIntensity(0, 100), 0);
  assert.equal(usageIntensity(1, 100), 1);
  assert.equal(usageIntensity(100, 100), 4);
});

test("known cost stays unavailable without a currency", () => {
  assert.equal(formatKnownCost(12.5, null), null);
  assert.match(formatKnownCost(12.5, "USD") ?? "", /12\.50/);
});
