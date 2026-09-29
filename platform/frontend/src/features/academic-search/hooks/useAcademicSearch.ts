"use client";

import { useCallback, useRef, useState } from "react";
import { api, LiteratureSearchResponse } from "@/lib/api";
import { say, useLanguage, type Phrase } from "@/shared/i18n";

/**
 * 进度阶段 → 文案。
 *
 * 后端只在部分事件里给 detail（那是后端自己的一句话）；没给的时候由界面按
 * `stage` 说，而不是让网络层写死一句中文 —— 网络层拿不到语言，写死的那句在
 * 英文界面上会原样露出来。
 */
const STAGE_PHRASES: Record<string, Phrase> = {
  strategy: { zh: "正在准备学术检索", en: "Preparing the academic search" },
  connection: { zh: "已连接检索服务，等待检索进度", en: "Connected to the search service; waiting for progress" },
  search: { zh: "正在检索", en: "Searching" },
};

export function useAcademicSearch() {
  const lang = useLanguage();
  const [data, setData] = useState<LiteratureSearchResponse>();
  const [error, setError] = useState<Error>();
  const [progress, setProgress] = useState<{ stage: string; detail: string; percent: number }>();
  const [isPending, setPending] = useState(false);
  const cancelRef = useRef<(() => void) | null>(null);

  const start = useCallback((input: { query: string; limit?: number }) => {
    cancelRef.current?.();
    setPending(true);
    setError(undefined);
    setData(undefined);
    setProgress({ stage: "strategy", detail: say(STAGE_PHRASES.strategy, lang), percent: 1 });
    cancelRef.current = api.streamLiteratureSearch(
      input.query,
      input.limit ?? 200,
      (event) => setProgress((current) => {
        // 来源级进度只用于后端诊断。多个来源并发完成时顺序不固定，直接展示
        // 会让主进度文案来回跳；百分比也必须在前端做最后一道单调保护。
        if (String(event.stage || "") === "source") return current;
        const incoming = Math.max(
          0,
          Math.min(100, Number(event.progress_percent) || current?.percent || 1),
        );
        const stage = String(event.stage || current?.stage || "search");
        return {
          stage,
          detail: String(event.detail || "") || say(STAGE_PHRASES[stage] ?? STAGE_PHRASES.search, lang),
          percent: Math.max(current?.percent || 1, incoming),
        };
      }),
      (result) => { setData(result); setPending(false); setProgress(undefined); },
      (message) => { setError(new Error(message)); setPending(false); setProgress(undefined); },
    );
  }, [lang]);

  return { data, error, progress, isPending, isError: Boolean(error), start };
}
