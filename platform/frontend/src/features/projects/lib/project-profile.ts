import type { Project, UpdateProjectRequest } from "@/lib/api";

export type ProjectProfileDraft = {
  name: string;
  researchDomain: string;
  description: string;
};

export function canManageProjectSettings(project: Pick<Project, "capabilities"> | undefined) {
  return project?.capabilities?.includes("manage_settings") ?? false;
}

export function projectProfileDraft(
  project: Pick<Project, "name" | "research_domain" | "description">,
): ProjectProfileDraft {
  return {
    name: project.name,
    researchDomain: project.research_domain ?? "",
    description: project.description ?? "",
  };
}

export function projectProfilePatch(draft: ProjectProfileDraft): UpdateProjectRequest {
  return {
    name: draft.name.trim(),
    research_domain: draft.researchDomain.trim() || null,
    description: draft.description.trim() || null,
  };
}

export function projectProfileChanged(
  project: Pick<Project, "name" | "research_domain" | "description">,
  draft: ProjectProfileDraft,
) {
  const current = projectProfilePatch(projectProfileDraft(project));
  const next = projectProfilePatch(draft);
  return current.name !== next.name
    || current.research_domain !== next.research_domain
    || current.description !== next.description;
}
