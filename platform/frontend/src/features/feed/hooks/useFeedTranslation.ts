"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { FeedItem } from "@/lib/api";

/** 一条内容的译文。两个字段都可能缺：标题本来是中文、或后端只翻出了其中一条。 */
export interface FeedTranslation {
  titleZh?: string;
  abstractZh?: string;
}

/**
 * 按需后台翻译 feed 卡片的**标题与摘要** —— 复用学术搜索同一套 `/literature/translate`。
 *
 * 策略与学术搜索一致：只翻译「纯英文」内容（含字母、不含中文），中文条目直接跳过；
 * 翻译是独立后台请求，不阻塞资讯流首屏 —— 先显示原文，译文回来后再补上。标题译文
 * 补在原文下一行，摘要译文**替换**原文（与学术搜索结果卡片同一处理，也避免卡片高度
 * 翻倍把瀑布流撑乱）。
 *
 * 结果存组件内 state、不落库 —— 每次打开重翻一次，与学术搜索一致；翻错的那位下次
 * 还是会错，但总比在采集层多存一份译文、还得保证两者同步要省心。
 */
export function useFeedTranslation(items: FeedItem[]): Record<string, FeedTranslation> {
  const [translations, setTranslations] = useState<Record<string, FeedTranslation>>({});
  const requested = useRef<Set<string>>(new Set());
  const requestId = useRef(0);

  // 用整组 item id 当 effect 的触发键：列表一变（切 pane、刷新）就重算缺失项。
  const pageKey = items.map((item) => item.id).join("|");

  useEffect(() => {
    if (items.length === 0) return;

    /** 纯英文才算「需要翻译」：中文内容没有可翻的东西，混排内容也不动。 */
    const needsTranslation = (text: string | null | undefined) =>
      Boolean(text) &&
      /[A-Za-z]/.test(text as string) &&
      !/[\u4e00-\u9fff]/.test(text as string);

    const targets = items.filter((item) => {
      if (requested.current.has(item.id)) return false;
      // 一条内容一次请求就把标题和摘要都带回来，所以拿到过就够。
      if (translations[item.id]) return false;
      return needsTranslation(item.title) || needsTranslation(item.summary);
    });
    if (targets.length === 0) return;

    for (const item of targets) requested.current.add(item.id);
    const id = ++requestId.current;

    void api
      .translateLiteraturePage(
        targets.map((item) => ({
          key: item.id.slice(0, 500),
          title: item.title.slice(0, 1000),
          // 摘要既是翻译目标，也帮后端消歧（同名不同领域的标题）。
          abstract: item.summary?.slice(0, 4000) ?? null,
          authors: (item.authors ?? []).slice(0, 10),
          year: item.published_at
            ? new Date(item.published_at).getFullYear()
            : null,
          venue: item.venue?.slice(0, 1000) ?? null,
        })),
      )
      .then((response) => {
        if (requestId.current !== id) return;
        setTranslations((current) => {
          const next = { ...current };
          for (const item of response.translations) {
            next[item.key] = {
              titleZh: item.title_zh || undefined,
              abstractZh: item.abstract_zh || undefined,
            };
          }
          return next;
        });
      })
      .catch(() => {
        // 失败时撤回 requested 标记，下次有机会再试。
        for (const item of targets) requested.current.delete(item.id);
      });
  }, [pageKey]); // eslint-disable-line react-hooks/exhaustive-deps

  return translations;
}
