"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type FeedEngagementAction, type FeedSubscriptions } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { useT } from "@/shared/i18n";
import { pushError } from "@/stores/notification";

export function useFeedToday() {
  return useQuery({
    queryKey: qk.feedToday(),
    queryFn: () => api.getFeedToday(),
    // 在线采集运行时才短轮询；完成后的最后一次响应会把它自动关掉。
    refetchInterval: (query) => query.state.data?.refreshing ? 3000 : false,
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
  });
}

export function useFeedSubscriptionSearch(kind: "journal" | "scholar", query: string) {
  return useQuery({
    queryKey: qk.feedSubscriptionSearch(kind, query),
    queryFn: () => api.searchFeedSubscriptions(kind, query),
    enabled: query.trim().length > 0,
  });
}

export function useFeedSubscriptionItems() {
  return useQuery({
    queryKey: qk.feedSubscriptionItems(),
    queryFn: () => api.getFeedSubscriptionItems(),
  });
}

export function useSaveFeedSubscriptions() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.saveFeedSubscriptions.bind(api),
    // 乐观写入，而不是只在成功后再 invalidate（那是异步重取）。
    //
    // 这里防的是一个真实的丢更新：用户连点几个学者时，每次点击的载荷都是按
    // "渲染时看到的订阅"拼的，而上一发请求的响应还没回来 —— 于是后来那几次
    // 把先前加的东西一并覆盖掉。实测症状：先订阅了期刊 Water Research（界面
    // 显示已关注、内容也出来了），随后连点 5 位学者，服务端的 journals 变成
    // 了空数组，用户唯一的感受是"我的期刊订阅莫名其妙没了"。
    //
    // 把下一次状态**先写进缓存**，后续点击就能看到它，连点变成累加。
    onMutate: async (next: FeedSubscriptions) => {
      await qc.cancelQueries({ queryKey: qk.feedSubscriptions() });
      const previous = qc.getQueryData<FeedSubscriptions>(qk.feedSubscriptions());
      qc.setQueryData(qk.feedSubscriptions(), next);
      return { previous };
    },
    onError: (_error, _next, context) => {
      if (context?.previous !== undefined) {
        qc.setQueryData(qk.feedSubscriptions(), context.previous);
      }
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: ["feed"] }),
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
  const qc = useQueryClient();
  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["feed"] });
  };
  const record = useMutation({
    mutationFn: (vars: { itemId: string; action: FeedEngagementAction }) =>
      api.recordFeedEngagement(vars.itemId, vars.action),
    onSuccess: invalidate,
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
