"use client";

import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type LiteraturePaper } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { useT } from "@/shared/i18n";
import { pushError, pushSuccess } from "@/stores/notification";
import { indexSavedItems, paperToSave, savedItemIdFor } from "../lib/saved-papers";

/** 拿来对「收过没有」的那份资讯收藏。后端一页最多 100 条。 */
const SAVED = { saved: true, limit: 100 } as const;

/**
 * 文献检索里的「收藏」：收下一篇 = 进科研资讯的「收藏」页签；再点一次就取消。
 *
 * 之前它只是页面里的一个 `useState`，刷新就没了，也从来进不了「收藏」页签。
 */
export function usePaperSaves() {
  const t = useT();
  const queryClient = useQueryClient();
  const saved = useQuery({
    queryKey: qk.feedItems(SAVED),
    queryFn: () => api.listFeedItems(SAVED),
  });
  const index = useMemo(() => indexSavedItems(saved.data ?? []), [saved.data]);
  // 资讯那边的「收藏」页签和推荐卡片上的星标都读 ["feed", …]，一起刷新。
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["feed"] });

  const save = useMutation({
    mutationFn: (paper: LiteraturePaper) => api.savePaperToFeed(paperToSave(paper)),
    onSuccess: async () => {
      await refresh();
      pushSuccess(t({ zh: "已收藏，可在「收藏」页签中查看", en: "Saved. You can find it under the Saved tab." }));
    },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "收藏失败", en: "Could not save" })),
  });
  const unsave = useMutation({
    mutationFn: (itemId: string) => api.undoFeedEngagement(itemId, "save"),
    onSuccess: () => refresh(),
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "取消收藏失败", en: "Could not remove it from Saved" })),
  });

  return {
    /** 这一篇收过没有：收过就是那条资讯条目的 id。 */
    savedItemId: (paper: Pick<LiteraturePaper, "doi" | "url">) => savedItemIdFor(index, paper),
    toggle: (paper: LiteraturePaper) => {
      const itemId = savedItemIdFor(index, paper);
      if (itemId) unsave.mutate(itemId);
      else save.mutate(paper);
    },
    busy: save.isPending || unsave.isPending,
  };
}
