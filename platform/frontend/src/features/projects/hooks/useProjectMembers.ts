"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Project } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import {
  addProjectMember,
  changeProjectMemberRole,
  listProjectMembers,
  removeProjectMember,
  type ProjectMemberRole,
} from "../api/project-members";
import { pushError } from "@/stores/notification";
import { useT } from "@/shared/i18n";

export function useProjectMembers(projectId: string, project?: Project) {
  const t = useT();
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: qk.projectMembers(projectId),
    queryFn: () => listProjectMembers(projectId, project),
    enabled: !!project,
  });
  const invalidate = () => queryClient.invalidateQueries({ queryKey: qk.projectMembers(projectId) });
  const add = useMutation({
    mutationFn: ({ email, role }: { email: string; role: ProjectMemberRole }) => addProjectMember(projectId, email, role),
    onSuccess: () => void invalidate(),
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "没能把这个人加进项目", en: "This person could not be added to the Project" })),
  });
  const changeRole = useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: ProjectMemberRole }) => changeProjectMemberRole(projectId, userId, role),
    onSuccess: () => void invalidate(),
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "角色没能改", en: "The role could not be changed" })),
  });
  const remove = useMutation({
    mutationFn: (userId: string) => removeProjectMember(projectId, userId),
    onSuccess: () => void invalidate(),
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "没能把这个人移出项目", en: "This person could not be removed from the Project" })),
  });
  return { query, add, changeRole, remove };
}
