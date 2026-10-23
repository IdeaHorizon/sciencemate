export type AcademicSortMode = "recommendation" | "relevance" | "published";

type AcademicPaperOrderFields = {
  score: number;
  ranking_score: number;
  federated_rank?: number | null;
  score_breakdown: Record<string, unknown>;
  pub_date: string | null;
  year: number | string | null;
};

function relevanceScore(paper: AcademicPaperOrderFields): number {
  const raw = (paper.score_breakdown?.raw ?? {}) as Record<string, unknown>;
  const value = Number(raw.relevance);
  return Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0;
}

function publicationOrder(paper: AcademicPaperOrderFields): number {
  const parts = String(paper.pub_date || paper.year || "").match(/\d+/g)?.map(Number) ?? [];
  const year = parts[0];
  if (!year || !Number.isFinite(year)) return 0;
  const month = Math.min(12, Math.max(1, parts[1] || 12));
  const day = Math.min(31, Math.max(1, parts[2] || 31));
  return Date.UTC(year, month - 1, day);
}

export function compareAcademicPapers(
  left: AcademicPaperOrderFields,
  right: AcademicPaperOrderFields,
  mode: AcademicSortMode,
): number {
  if (mode === "recommendation" && left.federated_rank != null && right.federated_rank != null) {
    return left.federated_rank - right.federated_rank;
  }
  // ranking_score 是后端最终推荐顺序，包含原问题要素覆盖与误匹配惩罚。
  const recommendationDiff = Number(right.ranking_score || 0) - Number(left.ranking_score || 0);
  const qualityDiff = Number(right.score || 0) - Number(left.score || 0);
  const relevanceDiff = relevanceScore(right) - relevanceScore(left);
  const publicationDiff = publicationOrder(right) - publicationOrder(left);
  if (mode === "relevance") return relevanceDiff || recommendationDiff || qualityDiff || publicationDiff;
  if (mode === "published") return publicationDiff || recommendationDiff || qualityDiff || relevanceDiff;
  return recommendationDiff || qualityDiff || relevanceDiff || publicationDiff;
}
