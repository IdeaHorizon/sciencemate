/**
 * 文献检索里的「收藏」= 科研资讯的「收藏」。
 *
 * 检索结果来自外部检索，没有资讯条目；收下一篇时后端按 DOI / 链接找到（或建出）那一条再记
 * save（`POST /feed/saved-papers`）。所以「这一篇收过没有」要拿检索结果去对资讯「收藏」里
 * 那几条：先按 DOI，没有 DOI 再按链接。纯函数，按行为测（saved-papers.test.ts）。
 */
import type { FeedItem, LiteraturePaper, SavedPaperRequest } from "@/lib/api";

const DOI_IN_URL = /^https?:\/\/(?:dx\.)?doi\.org\/(.+)$/i;

function doiKey(doi: string | null | undefined): string | null {
  const value = (doi ?? "").trim();
  if (!value) return null;
  try {
    return `doi:${decodeURIComponent(value).toLowerCase()}`;
  } catch {
    return `doi:${value.toLowerCase()}`;
  }
}

function urlKey(url: string | null | undefined): string | null {
  const value = (url ?? "").trim().replace(/\/+$/, "");
  return value ? `url:${value.toLowerCase()}` : null;
}

/** 一条资讯条目能被认出来的身份：DOI（后端存在 `extra.doi`，或藏在 doi.org 链接里）和链接。 */
function savedItemKeys(item: FeedItem): string[] {
  const keys = new Set<string>();
  const extraDoi = typeof item.extra?.doi === "string" ? item.extra.doi : null;
  const fromUrl = item.url ? DOI_IN_URL.exec(item.url.trim())?.[1] : undefined;
  for (const key of [doiKey(extraDoi), doiKey(fromUrl), urlKey(item.url)]) {
    if (key) keys.add(key);
  }
  return [...keys];
}

/** 资讯「收藏」里那几条，按身份建一张索引：身份 → 资讯条目 id。 */
export function indexSavedItems(items: readonly FeedItem[]): Map<string, string> {
  const index = new Map<string, string>();
  for (const item of items) {
    if (!item.saved) continue;
    for (const key of savedItemKeys(item)) index.set(key, item.id);
  }
  return index;
}

/** 这一篇检索结果收过没有：收过就是那条资讯条目的 id。先按 DOI，没有 DOI 再按链接。 */
export function savedItemIdFor(index: ReadonlyMap<string, string>, paper: Pick<LiteraturePaper, "doi" | "url">): string | null {
  const byDoi = doiKey(paper.doi);
  if (byDoi && index.has(byDoi)) return index.get(byDoi) ?? null;
  const byUrl = urlKey(paper.url);
  return byUrl ? index.get(byUrl) ?? null : null;
}

/** 交给后端的那几样：标题、DOI、链接、作者、期刊、日期、摘要。 */
export function paperToSave(paper: LiteraturePaper): SavedPaperRequest {
  const year = typeof paper.year === "number" ? paper.year : Number.parseInt(String(paper.year ?? ""), 10);
  return {
    title: paper.title,
    doi: paper.doi,
    url: paper.url,
    authors: paper.authors.slice(0, 50),
    venue: paper.venue,
    published: paper.pub_date,
    year: Number.isFinite(year) && year >= 1000 && year <= 3000 ? year : null,
    abstract: paper.abstract,
  };
}
