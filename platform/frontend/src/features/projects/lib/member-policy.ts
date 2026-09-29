import type { ProjectMember } from "../api/project-members";

export function protectsLastLead(member: ProjectMember, members: ProjectMember[]) {
  return member.role === "lead" && members.filter((candidate) => candidate.role === "lead").length <= 1;
}
