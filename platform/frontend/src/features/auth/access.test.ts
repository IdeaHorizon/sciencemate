import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { canManageModels, canSelectModels, governanceKindLabel, roleLabel } from "./access.ts";

test("role and governance labels remain explicit", () => {
  assert.equal(roleLabel("institution_admin"), "管理员");
  assert.equal(roleLabel("researcher"), "成员");
  assert.equal(governanceKindLabel("individual"), "个人");
});

test("model management is permission-driven rather than inferred from role", () => {
  const base = {
    id: "u1",
    email: "admin@example.edu",
    display_name: "Admin",
    is_active: true,
    role: "institution_admin" as const,
  };
  assert.equal(canManageModels({ ...base, permissions: [] }), false);
  assert.equal(canManageModels({ ...base, permissions: ["model_backends.manage"] }), true);
});

test("model selection is independent from connection management", () => {
  const researcher = {
    id: "u2",
    email: "researcher@example.edu",
    display_name: "Researcher",
    is_active: true,
    role: "researcher" as const,
    permissions: ["model_backends.select"],
  };
  assert.equal(canSelectModels(researcher), true);
  assert.equal(canManageModels(researcher), false);
  assert.equal(canSelectModels({ ...researcher, permissions: [] }), false);
});

test("sign-out stops app-lifetime Run observers before clearing cached identity", () => {
  const provider = readFileSync(new URL("./AuthProvider.tsx", import.meta.url), "utf8");
  assert.match(provider, /canonicalRunEventRegistry\.stopAll\("Authentication session ended"\)/);
  assert.match(provider, /queryClient\.clear\(\)/);
});
