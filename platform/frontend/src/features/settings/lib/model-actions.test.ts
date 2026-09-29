import test from "node:test";
import assert from "node:assert/strict";
import {
  managedScopeLabel,
  modelAccessDescription,
  modelBackendActions,
  modelBackendScopeLabel,
  modelDefaultImpact,
} from "./model-actions.ts";

const researcher = {
  id: "researcher",
  email: "researcher@atrium.local",
  display_name: "Researcher",
  is_active: true,
  role: "researcher" as const,
  permissions: ["model_backends.select"],
};

test("editable 是后端的答案，前端不再拿角色权限二次否决", () => {
  // 研究员建在个人 scope 下的连接：后端 can_edit_backend() 为真、PUT 会通过。
  // 前端曾经额外要求 model_backends.manage（研究员没有），于是自己的连接
  // 在界面上不可编辑，也没有任何入口新建。
  assert.deepEqual(modelBackendActions(researcher, {
    editable: true,
    is_default: false,
    status: "ready",
  }), { canEdit: true, canSetDefault: true });
  // 别人管的 scope（机构 / 课题组）后端会回 editable: false，界面照样只读。
  assert.equal(modelBackendActions(researcher, {
    editable: false,
    is_default: false,
    status: "ready",
  }).canEdit, false);
  assert.match(modelAccessDescription(researcher, "en"), /Your own connections can be added and edited/);
  assert.match(modelAccessDescription(researcher), /自己的连接/);
});

test("selection is unavailable for an unready backend or without select permission", () => {
  assert.equal(modelBackendActions(researcher, {
    editable: false,
    is_default: false,
    status: "unreachable",
  }).canSetDefault, false);
  assert.equal(modelBackendActions({ ...researcher, permissions: [] }, {
    editable: false,
    is_default: false,
    status: "ready",
  }).canSetDefault, false);
});

test("object scopes and default impact are explained without internal IDs", () => {
  assert.equal(modelBackendScopeLabel("personal", "en"), "Personal connection");
  assert.equal(modelDefaultImpact(researcher, {
    editable: false,
    scope: { kind: "institution", id: "institution-a" },
  }, "en"), "Changes only your model for newly created Sessions; existing Sessions keep their model.");
  assert.match(modelDefaultImpact(researcher, {
    editable: false,
    scope: { kind: "institution", id: "institution-a" },
  }), /只改你新建会话时用的模型/);

  const administrator = { ...researcher, role: "institution_admin" as const, permissions: ["model_backends.manage"] };
  assert.equal(modelDefaultImpact(administrator, {
    editable: true,
    scope: { kind: "institution", id: "institution-a" },
  }, "en"), "Sets the Organisation default and your model for newly created Sessions.");
});

test("新建连接的落点取后端给的治理 scope，不按角色再算一遍", () => {
  assert.equal(managedScopeLabel(researcher, "en"), "Personal connection");
  assert.equal(managedScopeLabel(researcher), "个人的连接");
  assert.equal(managedScopeLabel({ ...researcher, governance_scope: { kind: "institution", id: "i", name: "I" } }, "en"), "Organisation connection");
  assert.equal(managedScopeLabel({ ...researcher, governance_scope: { kind: "institution", id: "i", name: "I" } }, "en"), "Organisation connection");
  assert.equal(managedScopeLabel(null, "en"), "Personal connection");
});
