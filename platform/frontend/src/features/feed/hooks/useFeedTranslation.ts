"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { FeedItem } from "@/lib/api";
import { FeedTranslationQueue, needsTranslation } from "../lib/feed-translation-queue";

export interface FeedTranslation {
  titleZh?: string;
  abstractZh?: string;
}

/** Shared index translations render immediately; only missing fields enter queues. */
export function useFeedTranslation(items: FeedItem[], retry = 0, autoTranslate = true): Record<string, FeedTranslation> {
  const [translations, setTranslations] = useState<Record<string, FeedTranslation>>({});
  const sources = useRef(new Map<string, { text: string; key: string }>());
  const sequence = useRef(0);
  const queues = useRef<{ titles: FeedTranslationQueue; abstracts: FeedTranslationQueue } | null>(null);
  const output: Record<string, FeedTranslation> = {};
  const papers = items.map((item) => {
    const text = JSON.stringify([item.title, item.summary]);
    let source = sources.current.get(item.id);
    if (!source || source.text !== text) {
      source = { text, key: `${item.id}:${++sequence.current}` };
      sources.current.set(item.id, source);
    }
    const translated = translations[source.key];
    output[item.id] = {
      titleZh: item.title_zh || translated?.titleZh,
      abstractZh: item.abstract_zh || translated?.abstractZh,
    };
    return {
      key: source.key,
      title: item.title.slice(0, 1000),
      abstract: item.summary?.slice(0, 50000) ?? null,
      doi: typeof item.extra?.doi === "string" ? item.extra.doi : null,
      url: item.url,
      authors: (item.authors ?? []).slice(0, 10),
      year: item.published_at ? new Date(item.published_at).getFullYear() : null,
      venue: item.venue?.slice(0, 1000) ?? null,
    };
  });
  const pageKey = JSON.stringify(items.map((item, index) => [papers[index].key, item.title_zh, item.abstract_zh]));

  useEffect(() => {
    const publish = (updates: Awaited<ReturnType<typeof api.translateLiteraturePage>>["translations"]) => setTranslations((current) => {
      const next = { ...current };
      for (const item of updates) {
        next[item.key] = {
          titleZh: item.title_zh || current[item.key]?.titleZh,
          abstractZh: item.abstract_zh || current[item.key]?.abstractZh,
        };
      }
      return next;
    });
    const titles = new FeedTranslationQueue(
      (batch) => api.translateLiteraturePage(batch.map((paper) => ({ ...paper, abstract: null })), false, { translate_abstracts: false }),
      publish, 2000, ["title"],
    );
    const abstracts = new FeedTranslationQueue(
      (batch) => api.translateLiteraturePage(batch, false, { translate_titles: false }),
      publish, 2000, ["abstract"],
    );
    queues.current = { titles, abstracts };
    return () => { titles.dispose(); abstracts.dispose(); queues.current = null; };
  }, [retry]);

  useEffect(() => {
    if (!autoTranslate) return;
    void queues.current?.titles.enqueue(papers.filter((paper, index) =>
      needsTranslation(paper.title) && !output[items[index].id]?.titleZh));
    void queues.current?.abstracts.enqueue(papers.filter((paper, index) =>
      needsTranslation(paper.abstract) && !output[items[index].id]?.abstractZh));
  }, [pageKey, retry, autoTranslate]); // eslint-disable-line react-hooks/exhaustive-deps

  return output;
}
