import type { LibrarySkill, SkillBinding, SkillNode } from "../types.ts";

export function bindingFor(skill: LibrarySkill): SkillBinding {
  return { owner_id: skill.owner_id, name: skill.name, revision: skill.revision,
    nodes: [], enabled: true };
}

export function missingTools(skill: LibrarySkill, node: SkillNode): string[] {
  return skill.tools_used.filter((tool) => !node.tools.includes(tool));
}

export function bindingKey(skill: Pick<LibrarySkill, "owner_id" | "name">): string {
  return `${skill.owner_id}/${skill.name}`;
}

export function sameBindings(left: SkillBinding[], right: SkillBinding[]): boolean {
  const canonical = (bindings: SkillBinding[]) => JSON.stringify(bindings
    .map((b) => ({ owner_id: b.owner_id, name: b.name, revision: b.revision,
      enabled: b.enabled, nodes: [...b.nodes].sort() }))
    .sort((a, b) => bindingKey(a).localeCompare(bindingKey(b))));
  return canonical(left) === canonical(right);
}
