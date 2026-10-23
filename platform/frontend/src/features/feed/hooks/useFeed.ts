"use client";

import { keepPreviousData, useInfiniteQuery, useMutation, useQuery, useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { api, type FeedEngagementAction, type FeedSubscriptions, type FeedToday } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { useT } from "@/shared/i18n";
import { pushError, pushSuccess } from "@/stores/notification";

export function useFeedToday(enabled = true, sources = "domain,journal,scholar,project", progressEnabled = true) {
  const pageSize = 21;
  const progress = useQuery({
    queryKey: ["feed", "progress"], enabled: enabled && progressEnabled,
    queryFn: () => api.getFeedProgress(),
    refetchInterval: (query) => query.state.data?.pending || query.state.data?.translation_pending
      || query.state.data?.source_progress?.some((row) => ["searching", "preparing", "translating", "waiting"].includes(row.phase))
      || query.state.data?.phase === "searching"
      || query.state.data?.domain_progress?.some((row) => ["searching", "preparing", "translating", "not_started"].includes(row.phase)) ? 5000 : 60000,
  });
  const list = useInfiniteQuery({
    queryKey: [...qk.feedToday(), sources],
    enabled,
    initialPageParam: 0,
    placeholderData: keepPreviousData,
    queryFn: ({ pageParam }) => api.getFeedToday({ limit: pageSize, offset: pageParam, ready_only: true, sources }),
    getNextPageParam: (lastPage, _pages, lastPageParam) =>
      lastPage.has_more ? lastPageParam + pageSize : undefined,
    // 在线采集运行时才短轮询；完成后的最后一次响应会把它自动关掉。
    refetchInterval: (query) => {
      if (!enabled) return false;
      const first = query.state.data?.pages[0];
      return first?.refreshing ? 15000 : first?.progress?.pending || first?.progress?.translation_pending || first?.progress?.project_error ? 15000 : 60000;
    },
  });
  return { ...list, data: list.data && { ...list.data, pages: list.data.pages.map((page, index) =>
    index === 0 && progressEnabled && progress.data ? { ...page, progress: progress.data } : page) } };
}

export function useRefreshFeedToday() {
  const qc = useQueryClient();
  const t = useT();
  return useMutation({
    mutationFn: async () => {
      const queryKey = qk.feedToday();
      await api.refreshFeedRecommendations();
      await qc.invalidateQueries({ queryKey: ["feed", "progress"] });
      await qc.cancelQueries({ queryKey });
      // Reset clears all loaded pages; the next request starts at offset 0.
      await qc.resetQueries({ queryKey }, { throwOnError: true });
    },
    onSuccess: () => pushSuccess(t({ zh: "已按最新规则刷新推荐", en: "Recommendations refreshed with the latest rules" })),
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "刷新失败，请重试", en: "Refresh failed. Please try again." })),
  });
}

export function useFeedItems(filters: {
  domain?: string;
  kind?: string;
  saved?: boolean;
  limit?: number;
}) {
  return useQuery({
    queryKey: qk.feedItems(filters),
    queryFn: () => api.listFeedItems(filters),
  });
}

export function useFeedSubscriptions() {
  return useQuery({
    queryKey: qk.feedSubscriptions(),
    queryFn: () => api.getFeedSubscriptions(),
    refetchInterval: (query) => query.state.data?.scholars.some((entry) => entry.identity_status === "unresolved") ? 5000 : query.state.data?.scholars.some((entry) => entry.identity_status === "unavailable") ? 300000 : false,
  });
}

export function useFeedSubscriptionSearch(kind: "journal" | "scholar", query: string) {
  return useQuery({
    queryKey: qk.feedSubscriptionSearch(kind, query),
    queryFn: () => api.searchFeedSubscriptions(kind, query),
    enabled: query.trim().length > 0,
  });
}

export function useFeedSubscriptionItems(kind: "domain" | "journal" | "scholar" | "all" = "all") {
  return useInfiniteQuery({
    queryKey: [...qk.feedSubscriptionItems(), kind],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => api.getFeedSubscriptionItems(pageParam, 100, kind),
    getNextPageParam: (lastPage, _pages, lastPageParam) =>
      lastPage.length === 100 ? lastPageParam + 100 : undefined,
  });
}

export function useSaveFeedSubscriptions() {
  const t = useT();
  const qc = useQueryClient();
  const mutationKey = ["feed", "save-subscriptions"];
  return useMutation({
    mutationKey,
    scope: { id: "feed-subscriptions" },
    mutationFn: api.saveFeedSubscriptions.bind(api),
    onMutate: (next: FeedSubscriptions) => {
      // Publish synchronously so rapid clicks build on the preceding choice.
      void qc.cancelQueries({ queryKey: qk.feedSubscriptions(), exact: true });
      const previous = qc.getQueryData<FeedSubscriptions>(qk.feedSubscriptions());
      qc.setQueryData(qk.feedSubscriptions(), next);
      return { previous };
    },
    onError: (_error, next, context) => {
      if (context?.previous !== undefined && qc.getQueryData(qk.feedSubscriptions()) === next) {
        qc.setQueryData(qk.feedSubscriptions(), context.previous);
      }
      pushError(t({ zh: "关注保存失败，请重试", en: "Could not save subscriptions. Please try again." }));
    },
    onSuccess: (saved) => {
      if (qc.isMutating({ mutationKey }) === 1) {
        qc.setQueryData(qk.feedSubscriptions(), saved);
      }
    },
    onSettled: () => {
      if (qc.isMutating({ mutationKey }) !== 1) return;
      // Keep the page mounted and the search results stable. Recommendations
      // refresh on return to their tab, rather than interrupting subscriptions.
      qc.setQueryData<InfiniteData<FeedToday, number>>(qk.feedToday(), (current) =>
        current ? { ...current, pages: current.pages.slice(0, 1), pageParams: current.pageParams.slice(0, 1) } : current);
      void qc.invalidateQueries({ queryKey: qk.feedToday(), exact: true, refetchType: "none" });
      void qc.invalidateQueries({ queryKey: qk.feedSubscriptions(), exact: true });
      void qc.invalidateQueries({ queryKey: qk.feedSubscriptionItems() });
    },
  });
}

export function useFeedInterests() {
  return useQuery({
    queryKey: qk.feedInterests(),
    queryFn: () => api.getFeedInterests(),
  });
}

export function useFeedDomains(enabled: boolean) {
  return useQuery({
    queryKey: qk.feedDomains(),
    queryFn: () => api.getFeedDomainCatalog(),
    enabled,
    // 词表是 arXiv 分类骨架 —— 它不会在一次会话里变。
    staleTime: 60 * 60 * 1000,
  });
}

export function useFeedStatus() {
  return useQuery({
    queryKey: qk.feedStatus(),
    queryFn: () => api.getFeedStatus(),
  });
}

/**
 * 记一次互动。
 *
 * `dismiss` 会把这条从候选池里永久移除，所以它必须让今天的选摘失效重取 ——
 * 否则用户点了"不感兴趣"，那张卡还留在原地，看起来像是按钮坏了。
 */
export function useFeedImpression() {
  return useMutation({
    mutationFn: (itemId: string) => api.recordFeedEngagement(itemId, "impression"),
  });
}

export function useFeedEngagement() {
  const t = useT();
  const qc = useQueryClient();
  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["feed"] });
  };
  const record = useMutation({
    mutationFn: (vars: { itemId: string; action: FeedEngagementAction }) =>
      api.recordFeedEngagement(vars.itemId, vars.action),
    onSuccess: (_data, vars) => {
      if (vars.action === "open") {
        qc.setQueriesData<InfiniteData<FeedToday>>({ queryKey: qk.feedToday() }, (current) =>
          current && {
            ...current,
            pages: current.pages.map((page) => ({
              ...page,
              feed: page.feed.map((card) => card.item.id === vars.itemId
                ? { ...card, item: { ...card.item, viewed: true } }
                : card),
            })),
          });
      }
      if (vars.action === "dismiss") {
        qc.setQueriesData<InfiniteData<FeedToday>>({ queryKey: qk.feedToday() }, (current) =>
          current && {
            ...current,
            pages: current.pages.map((page) => ({
              ...page,
              feed: page.feed.filter((card) => card.item.id !== vars.itemId),
            })),
          });
      }
      invalidate();
    },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "操作失败，请重试", en: "Action failed. Please try again." })),
  });
  const undo = useMutation({
    mutationFn: (vars: { itemId: string; action: FeedEngagementAction }) =>
      api.undoFeedEngagement(vars.itemId, vars.action),
    onSuccess: invalidate,
  });
  return { record, undo };
}

export function useSaveInterests() {
  const t = useT();
  const qc = useQueryClient();
  return useMutation({
    mutationKey: ["feed", "save-interests"],
    mutationFn: (domains: string[]) => api.saveFeedInterests(domains),
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "研究方向没能保存", en: "This research direction could not be saved" })),
    onSuccess: () => {
      // 兴趣变了，今天的选摘在后端已经被作废重算，前端也得跟着重取 ——
      // 不然用户刚选完方向，日报还是老三条，看起来像设置没生效。
      void qc.invalidateQueries({ queryKey: ["feed"] });
    },
  });
}

export function useShareLink() {
  const t = useT();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: { url: string; comment?: string; visibility?: "platform" | "organization" }) =>
      api.shareFeedLink(data),
    // 分享失败最不能静默：用户会以为已经分享出去了。
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "这条链接没能分享出去", en: "This link could not be shared" })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["feed"] });
    },
  });
}

/**
 * 删掉一条推断出来的方向。
 *
 * 后端会**记住**这条被拒过，下次挖掘不再推 —— 否则用户每天删同一个，
 * 而系统看起来像没听见。
 */
export function useRejectInferred() {
  const t = useT();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (domain: string) => api.rejectInferredDomain(domain),
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "这个方向没能删掉", en: "This direction could not be removed" })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["feed"] });
    },
  });
}
