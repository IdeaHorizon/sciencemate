import { api, ApiError, API_BASE_URL } from "@/lib/api";
import type { LibrarySkill, ProjectSkills, SkillBinding, SkillPreview, SkillsLibrary } from "./types";

const projectPath = (id: string) => `/projects/${encodeURIComponent(id)}/skills`;

export const skillsClient = {
  // The library belongs to this installation, regardless of the open project.
  list: () => api.request<SkillsLibrary>("/settings/skills", {}, {}),
  setEnabled: (name: string, enabled: boolean) => api.request<LibrarySkill>(
    `/settings/skills/${encodeURIComponent(name)}`, {
      method: "PATCH", body: JSON.stringify({ enabled }),
    }, {}),
  project: (id: string) => api.request<ProjectSkills>(projectPath(id)),
  saveProject: (id: string, revision: string, bindings: SkillBinding[]) =>
    api.request<ProjectSkills>(projectPath(id), {
      method: "PUT", body: JSON.stringify({ revision, bindings }),
    }),
  preview: (skill: Pick<LibrarySkill, "name" | "owner_id" | "revision">, asset = "", projectId?: string) => {
    const params = new URLSearchParams({ revision: skill.revision });
    if (asset) params.set("asset", asset);
    if (projectId) {
      params.set("name", skill.name);
      params.set("owner_id", skill.owner_id);
    }
    const path = projectId ? `${projectPath(projectId)}/preview` : `/settings/skills/${encodeURIComponent(skill.name)}`;
    return api.request<SkillPreview>(`${path}?${params}`, {}, projectId ? undefined : {});
  },
  import: async (files: File[], projectId?: string): Promise<LibrarySkill> => {
    const data = new FormData();
    for (const file of files) data.append("files", file, file.webkitRelativePath || file.name);
    const path = projectId ? `${projectPath(projectId)}/import` : "/settings/skills/import";
    const response = await api.fetchWithAuth(`${API_BASE_URL}${path}`, {
      method: "POST", body: data,
    }, projectId ? undefined : {});
    if (!response.ok) throw ApiError.fromBody(await response.json().catch(() => ({})), response.status);
    return response.json();
  },
};

export const skillsKeys = {
  library: ["skills", "library"] as const,
  project: (id: string) => ["skills", "project", id] as const,
};
