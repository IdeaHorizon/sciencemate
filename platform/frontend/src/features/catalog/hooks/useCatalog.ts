"use client";

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { qk } from "@/lib/query/keys";

/**
 * 「这个项目产出了什么」。
 *
 * 目录按**最近一个会话**的工作区取：Project = Git 仓库，会话是它的 worktree，
 * 正在进行的工作只在会话分支上看得到。不传 sessionId 时后端回落到项目主干。
 */
export function useProjectCatalog(projectId: string, sessionId?: string, enabled = true) {
  return useQuery({
    queryKey: qk.projectCatalog(projectId, sessionId ?? ""),
    queryFn: () => api.getProjectCatalog(projectId, sessionId || undefined),
    enabled: enabled && !!projectId,
    // 产出发生在一轮结束时，不是每秒都在变。
    refetchInterval: 30_000,
  });
}
