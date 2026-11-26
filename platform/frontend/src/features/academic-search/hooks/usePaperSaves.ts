"use client";

import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type LiteraturePaper } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { useT } from "@/shared/i18n";
import { pushError, pushSuccess } from "@/stores/notification";
import { indexSavedItems, paperToSave, savedItemIdFor } from "../lib/saved-papers";

/**
 * 文献检索里的「收藏」：收下一篇 = 进科研资讯的「收藏」页签；再点一次就取消。
 *
 * 之前它只是页面里的一个 `useState`，刷新就没了，也从来进不了「收藏」页签。
 */
export function usePaperSaves() {
  const t = useT();
  const queryClient = useQueryClient();
  const saved = useQuery({
    queryKey: qk.feedSavedIndex(),
    queryFn: () => api.listSavedFeedItems(),
  });
  const index = useMemo(() => indexSavedItems(saved.data ?? []), [saved.data]);
  // 资讯那边的「收藏」页签和推荐卡片上的星标都读 ["feed", …]，一起刷新。
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["feed"] });

  const save = useMutation({
    mutationFn: ({ paper, folderId }: { paper: LiteraturePaper; folderId: string | null }) =>
      api.savePaperToFeedFolder(paperToSave(paper), folderId),
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
    savedFolderId: (paper: Pick<LiteraturePaper, "doi" | "url">) => {
      const itemId = savedItemIdFor(index, paper);
      return saved.data?.find((item) => item.id === itemId)?.saved_folder_id;
    },
    // The idempotent endpoint also moves an existing save, without replacing
    // its record/date, and returns the actual location even if this index raced.
    saveToFolder: (paper: LiteraturePaper, folderId: string | null) => save.mutateAsync({ paper, folderId }),
    remove: (paper: LiteraturePaper) => {
      const itemId = savedItemIdFor(index, paper);
      return itemId ? unsave.mutateAsync(itemId) : Promise.resolve();
    },
    toggle: (paper: LiteraturePaper) => {
      const itemId = savedItemIdFor(index, paper);
      if (itemId) unsave.mutate(itemId);
      else save.mutate({ paper, folderId: null });
    },
    busy: save.isPending || unsave.isPending,
  };
}
