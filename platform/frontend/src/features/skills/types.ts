export type LibrarySkill = {
  owner_id: string;
  name: string;
  description: string;
  revision: string;
  revisions: string[];
  enabled: boolean;
  tools_used: string[];
  files: string[];
  revision_details?: Record<string, Pick<LibrarySkill, "name" | "description" | "revision" | "tools_used" | "files">>;
};

export type SkillBinding = Pick<LibrarySkill, "owner_id" | "name" | "revision" | "enabled"> & {
  nodes: string[];
};

export type SkillNode = { id: string; tools: string[] };
export type SkillsLibrary = { skills: LibrarySkill[]; nodes: SkillNode[] };
export type ProjectSkills = SkillsLibrary & { bindings: SkillBinding[]; revision: string };
export type SkillPreview = LibrarySkill & {
  content: string;
  asset: string;
  binary: boolean;
  truncated: boolean;
};
