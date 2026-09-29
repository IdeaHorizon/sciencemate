import test from "node:test";
import assert from "node:assert/strict";
import { protectsLastLead } from "./member-policy.ts";

const member = (userId: string, role: "viewer" | "researcher" | "reviewer" | "lead") => ({
  userId,
  displayName: userId,
  email: `${userId}@example.edu`,
  role,
  joinedAt: null,
});

test("the final Project Lead cannot be demoted or removed", () => {
  const lead = member("lead-a", "lead");
  assert.equal(protectsLastLead(lead, [lead, member("viewer", "viewer")]), true);
  assert.equal(protectsLastLead(lead, [lead, member("lead-b", "lead")]), false);
  assert.equal(protectsLastLead(member("researcher", "researcher"), [lead]), false);
});
