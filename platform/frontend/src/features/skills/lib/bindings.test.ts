import test from "node:test";
import assert from "node:assert/strict";
import { bindingFor, bindingKey, missingTools, sameBindings } from "./bindings.ts";
import type { LibrarySkill } from "../types.ts";

const skill: LibrarySkill = { owner_id: "alice", name: "method", description: "Research method",
  revision: "a".repeat(64), revisions: ["a".repeat(64)], enabled: true,
  tools_used: ["read_file", "external_tool"], files: ["SKILL.md"] };

test("adding a library skill requires an explicit node choice and preserves its revision", () => {
  const binding = bindingFor(skill);
  assert.deepEqual(binding.nodes, []);
  assert.equal(binding.revision, skill.revision);
  assert.equal(bindingKey(binding), "alice/method");
});

test("dependency warnings use the selected node's actual tools", () => {
  assert.deepEqual(missingTools(skill, { id: "experiment", tools: ["read_file"] }), ["external_tool"]);
  assert.deepEqual(missingTools(skill, { id: "writing", tools: [] }), skill.tools_used);
});

test("node order does not create unsaved changes; versions, owners and availability do", () => {
  const a = { ...bindingFor(skill), nodes: ["experiment", "writing"] };
  assert.ok(sameBindings([a], [{ ...a, nodes: ["writing", "experiment"] }]));
  for (const change of [{ enabled: false }, { revision: "b".repeat(64) }, { owner_id: "bob" }]) {
    assert.ok(!sameBindings([a], [{ ...a, ...change }]));
  }
});
