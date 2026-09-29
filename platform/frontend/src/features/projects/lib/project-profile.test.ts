import test from "node:test";
import assert from "node:assert/strict";
import {
  canManageProjectSettings,
  projectProfileChanged,
  projectProfileDraft,
  projectProfilePatch,
} from "./project-profile.ts";

const project = {
  name: "Materials Lab",
  research_domain: "Materials science",
  description: null,
  capabilities: ["view", "manage_settings"],
};

test("Project profile editing is granted only by the canonical capability", () => {
  assert.equal(canManageProjectSettings(project), true);
  assert.equal(canManageProjectSettings({ capabilities: ["manage_members"] }), false);
  assert.equal(canManageProjectSettings({ capabilities: undefined }), false);
});

test("Project profile interaction saves only persisted metadata", () => {
  const draft = { ...projectProfileDraft(project), description: "  Shared project context.  " };

  assert.equal(projectProfileChanged(project, draft), true);
  assert.deepEqual(projectProfilePatch(draft), {
    name: "Materials Lab",
    research_domain: "Materials science",
    description: "Shared project context.",
  });
  assert.deepEqual(Object.keys(projectProfilePatch(draft)).sort(), [
    "description",
    "name",
    "research_domain",
  ]);
});
