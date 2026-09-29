"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { qk } from "@/lib/query/keys";

export function useAllArtifacts() {
  return useQuery({
    queryKey: qk.allArtifacts(),
    queryFn: () => api.listAllArtifacts(),
  });
}

export function useProjectArtifactsList(projectId: string | undefined) {
  return useQuery({
    queryKey: projectId ? qk.artifacts(projectId) : ["artifacts", "none"],
    queryFn: () =>
      projectId
        ? api.listArtifacts(projectId)
        : Promise.reject(new Error("No id")),
    enabled: !!projectId,
  });
}

export function useArtifactContent(projectId: string, id: string | undefined) {
  return useQuery({
    queryKey: id ? qk.artifactContent(projectId, id) : ["artifact-content", "none"],
    queryFn: () =>
      id
        ? api.getArtifactContent(projectId, id)
        : Promise.reject(new Error("No id")),
    enabled: !!id,
  });
}
