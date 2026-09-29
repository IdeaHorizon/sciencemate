import test from "node:test";
import assert from "node:assert/strict";

import { TIER_ORDER, groupByTier } from "./group-by-tier.ts";
import type { CatalogEntry, CatalogTier } from "../../../lib/api.ts";

/** 形状照真产物（`core/catalog.py` 的 `as_dict()` 出来的那一份）。 */
function entry(kind: string, tier: CatalogTier, name = kind): CatalogEntry {
  return {
    artifactId: `${kind}__${name}`,
    kind, name, ownerNode: "writing", version: 5,
    frozen: tier === "deliverable", permanent: tier === "deliverable",
    tier, isDeliverable: tier === "deliverable",
    recordPath: `paper/artifacts/${kind}__${name}.json`,
    files: tier === "deliverable" ? ["paper/latex_build/sci/main.pdf"] : [],
    createdAt: "2026-09-09T08:00:00+00:00",
    frozenAt: tier === "deliverable" ? "2026-09-09T08:01:00+00:00" : "",
    producedByRunId: "1788160373-daa391",
  };
}

test("交付物排在最前 —— 那是用户来这一页要找的东西", () => {
  assert.deepEqual(TIER_ORDER, ["deliverable", "output", "working"]);
});

test("工作过程照样在分组里 —— 分层是排序不是过滤", () => {
  /**
   * 真项目实测：264 件产物里 206 件落在这一档。它最容易被顺手滤掉，而滤掉
   * 之后用户就会以为那些东西不存在。
   */
  const rows = [
    entry("manuscript", "deliverable"),
    entry("survey_report", "output"),
    ...Array.from({ length: 26 }, (_, i) => entry("compression_log", "working", `turn_${i}`)),
  ];

  const grouped = groupByTier(rows);

  assert.equal(grouped.deliverable.length, 1);
  assert.equal(grouped.output.length, 1);
  assert.equal(grouped.working.length, 26, "26 条压缩日志一条都不能丢");
  const total = TIER_ORDER.reduce((n, tier) => n + grouped[tier].length, 0);
  assert.equal(total, rows.length, "分组前后条数必须相等：这是排序不是过滤");
});

test("三个键恒在 —— 空的也在（「这一档是空的」本身是个答案）", () => {
  const grouped = groupByTier([]);
  assert.deepEqual(Object.keys(grouped).sort(), ["deliverable", "output", "working"]);
});

test("没见过的 tier 落进最保守那一档，而不是被丢掉", () => {
  const rogue = { ...entry("something_new", "output"), tier: "brand_new" as CatalogTier };
  const grouped = groupByTier([rogue]);
  assert.equal(grouped.working.length, 1);
  assert.equal(grouped.deliverable.length, 0);
});
