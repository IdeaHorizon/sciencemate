"use client";

import { useProjectSessions } from "@/features/sessions/hooks/useSessions";

/**
 * 项目"当前真相"所在的 session。
 *
 * Project v2 里节点的 checkpoint 提交在 **session 分支**上，publish 之前
 * main 分支没有这些内容。项目级视图（Research state / Memory）如果不带
 * sessionId 去读 repository API，读到的是 main —— 于是明明存在的
 * research_state / MEMORY.md 显示"还没有"（E2E v11 实测：v1 就躺在
 * session 分支的 plan/ 里，页面说 No research state yet）。
 *
 * 判据：最近活动的 session 即当前真相；一个 session 都没有 → undefined
 * （读 main —— 已 publish 的历史真相）。
 */
export function useProjectTruthSession(projectId: string): {
  sessionId: string | undefined;
  isLoading: boolean;
} {
  const sessions = useProjectSessions(projectId);
  const items = [...(sessions.data ?? [])].sort((a, b) =>
    String(b.updatedAt ?? "").localeCompare(String(a.updatedAt ?? "")),
  );
  return { sessionId: items[0]?.id, isLoading: sessions.isLoading };
}
