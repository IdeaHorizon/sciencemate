"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { pushError, pushSuccess } from "@/stores/notification";

/**
 * 我的项目 —— 自己建的、是成员的（服务器的 `GET /projects/` 只答这些，管理员也一样）。
 *
 * 组里别的项目在「组织 → 项目」里（RFC_ORGANISATION_PAGE §3.3：看得见 ≠ 在你的列表里 —— 从前
 * 管理员的侧栏被同事的项目填满）。`mine === false` 的这里再筛一道：一行不是我的却出现在清单里，
 * 只可能是清单的口径被人改宽了，那时侧栏不跟着被填满。旧服务器不发 `mine` = 当都是我的。
 */
export function useProjects() {
  return useQuery({
    queryKey: qk.projects(),
    queryFn: () => api.listProjects(),
    select: (projects) => projects.filter((project) => project.mine !== false),
  });
}

export function useProject(id: string | undefined) {
  return useQuery({
    queryKey: id ? qk.project(id) : ["project", "none"],
    queryFn: () => (id ? api.getProject(id) : Promise.reject(new Error("No id"))),
    enabled: !!id,
  });
}

export function useCreateProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: Parameters<typeof api.createProject>[0]) => api.createProject(data),
    onSuccess: (created) => {
      qc.setQueryData(qk.projects(), (prev: unknown) => {
        const current = Array.isArray(prev) ? prev : [];
        return [created, ...current];
      });
    },
    onError: (err) => pushError(err instanceof Error ? err.message : "Project creation failed"),
  });
}

export function useDeleteProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deleteProject(id),
    onSuccess: (_, id) => {
      qc.setQueryData(qk.projects(), (prev: unknown) => {
        if (!Array.isArray(prev)) return prev;
        return prev.filter((p: { id: string }) => p.id !== id);
      });
    },
    onError: (err) =>
      pushError(err instanceof Error ? err.message : "Delete failed"),
  });
}

export function useProjectArtifacts(projectId: string | undefined) {
  return useQuery({
    queryKey: projectId ? qk.artifacts(projectId) : ["artifacts", "none"],
    queryFn: () =>
      projectId
        ? api.listArtifacts(projectId)
        : Promise.reject(new Error("No id")),
    enabled: !!projectId,
  });
}
