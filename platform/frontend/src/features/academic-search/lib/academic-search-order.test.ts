import assert from "node:assert/strict";
import test from "node:test";

import { compareAcademicPapers } from "./academic-search-order.ts";

const paper = (ranking_score: number, score: number) => ({
  ranking_score, score, score_breakdown: { raw: { relevance: score } },
  pub_date: null, year: 2025,
});

test("default recommendation order preserves the backend ranking score", () => {
  const intentMatch = paper(0.9, 0.5);
  const broadHighQuality = paper(0.7, 0.95);
  assert.equal(
    [broadHighQuality, intentMatch].sort((a, b) => compareAcademicPapers(a, b, "recommendation"))[0],
    intentMatch,
  );
});

test("recommendation keeps federated positions while explicit sorts use their own fields", () => {
  const publicPaper = { ...paper(0.9, 0.9), federated_rank: 5, pub_date: "2025-01-01" };
  const schoolPaper = { ...paper(0, 0), federated_rank: 4, pub_date: "2026-01-01" };
  assert.equal(compareAcademicPapers(schoolPaper, publicPaper, "recommendation") < 0, true);
  assert.equal(compareAcademicPapers(schoolPaper, publicPaper, "relevance") > 0, true);
  assert.equal(compareAcademicPapers(schoolPaper, publicPaper, "published") < 0, true);
});
