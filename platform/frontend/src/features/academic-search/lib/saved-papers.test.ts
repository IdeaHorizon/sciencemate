import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import type { FeedItem } from "../../../lib/api.ts";
import { indexSavedItems, paperToSave, savedItemIdFor } from "./saved-papers.ts";

function item(id: string, fields: Partial<FeedItem>): FeedItem {
  return {
    id, kind: "paper", title: "t", url: null, summary: null, authors: [], venue: null, published_at: null,
    domains: [], domain_labels: [], source_name: null, author_display_name: null, saved: true, viewed: false,
    extra: {}, ...fields,
  };
}

test("检索结果按 DOI 认出资讯「收藏」里那一条，大小写和 doi.org 链接都算", () => {
  const index = indexSavedItems([
    item("by-extra", { extra: { doi: "10.1234/abc.DEF" } }),
    item("by-doi-url", { url: "https://doi.org/10.5555/XYZ" }),
    item("by-link", { url: "https://arxiv.org/abs/2401.00001/" }),
    item("not-saved", { saved: false, extra: { doi: "10.9999/gone" } }),
  ]);
  assert.equal(savedItemIdFor(index, { doi: "10.1234/ABC.def", url: null }), "by-extra");
  assert.equal(savedItemIdFor(index, { doi: "10.5555/xyz", url: "https://publisher.test/x" }), "by-doi-url");
  assert.equal(savedItemIdFor(index, { doi: null, url: "https://arxiv.org/abs/2401.00001" }), "by-link");
  assert.equal(savedItemIdFor(index, { doi: "10.9999/gone", url: null }), null, "取消收藏的那条还被当成收过");
  assert.equal(savedItemIdFor(index, { doi: null, url: null }), null);
});

test("交给后端的是一篇论文的书目信息，年份不是数就不交", () => {
  const paper = {
    title: "A", title_zh: null, abstract_zh: null, authors: Array.from({ length: 60 }, (_, i) => `P${i}`),
    year: "2024", pub_date: "2024-05-01", venue: "PRB", doi: "10.1/x", url: null, abstract: "abs",
  } as unknown as Parameters<typeof paperToSave>[0];
  const sent = paperToSave(paper);
  assert.equal(sent.year, 2024);
  assert.equal(sent.authors.length, 50);
  assert.equal(sent.published, "2024-05-01");
  assert.equal(paperToSave({ ...paper, year: "n.d." } as typeof paper).year, null);
});

test("检索页的「收藏」接到资讯的收藏，「点赞」没有了", () => {
  const screen = readFileSync("src/features/academic-search/components/AcademicSearch.tsx", "utf8");
  // 以前两个按钮都只是页面里的 useState：刷新就没了，也进不了「收藏」页签。
  assert.doesNotMatch(screen, /setLiked|setSaved|AC\.like/, "点赞 / 收藏又退回成了页面里的一个状态");
  assert.match(screen, /usePaperSaves\(\)/);
  const hook = readFileSync("src/features/academic-search/hooks/usePaperSaves.ts", "utf8");
  assert.match(hook, /api\.savePaperToFeed\(/);
  assert.match(hook, /api\.undoFeedEngagement\(itemId, "save"\)/);
  const client = readFileSync("src/lib/api.ts", "utf8");
  assert.match(client, /"\/feed\/saved-papers"/);
});
