import { API_BASE_URL, ApiError, api, type Project } from "@/lib/api";

export type ProjectMemberRole = "viewer" | "researcher" | "reviewer" | "lead";

export interface ProjectMember {
  userId: string;
  displayName: string;
  email: string | null;
  role: ProjectMemberRole;
  joinedAt: string | null;
}

export interface ProjectMembersResult {
  items: ProjectMember[];
  capabilities: string[];
  canonical: boolean;
}

class ProjectMembersUnavailableError extends Error {}

async function requestMembers<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body) headers.set("Content-Type", "application/json");
  const response = await api.fetchWithAuth(`${API_BASE_URL}${path}`, { ...init, headers });
  if (!response.ok) {
    if (response.status === 404 || response.status === 405) throw new ProjectMembersUnavailableError();
    const body = await response.json().catch(() => ({}));
    throw new ApiError(body.detail || `API error: ${response.status}`, response.status);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export async function listProjectMembers(projectId: string, project?: Project): Promise<ProjectMembersResult> {
  try {
    const payload = await requestMembers<ProjectMember[] | { items: ProjectMember[]; capabilities?: string[] }>(
      `/projects/${encodeURIComponent(projectId)}/members`,
    );
    return Array.isArray(payload)
      ? { items: payload, capabilities: project?.capabilities ?? [], canonical: true }
      : { items: payload.items, capabilities: payload.capabilities ?? project?.capabilities ?? [], canonical: true };
  } catch (error) {
    if (!(error instanceof ProjectMembersUnavailableError)) throw error;
    const owner = project?.owner;
    return {
      items: owner ? [{
        userId: owner.id,
        displayName: owner.display_name,
        email: null,
        role: "lead",
        joinedAt: project.created_at,
      }] : [],
      capabilities: [],
      canonical: false,
    };
  }
}

export function addProjectMember(projectId: string, email: string, role: ProjectMemberRole) {
  return requestMembers<ProjectMember>(`/projects/${encodeURIComponent(projectId)}/members`, {
    method: "POST",
    body: JSON.stringify({ email, role }),
  });
}

export function changeProjectMemberRole(projectId: string, userId: string, role: ProjectMemberRole) {
  return requestMembers<ProjectMember>(`/projects/${encodeURIComponent(projectId)}/members/${encodeURIComponent(userId)}`, {
    method: "PATCH",
    body: JSON.stringify({ role }),
  });
}

export function removeProjectMember(projectId: string, userId: string) {
  return requestMembers<void>(`/projects/${encodeURIComponent(projectId)}/members/${encodeURIComponent(userId)}`, {
    method: "DELETE",
  });
}
