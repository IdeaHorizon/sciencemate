import assert from "node:assert/strict";
import test from "node:test";
import { resolveDefaultLanding } from "./default-landing.ts";

test("explicit startup destinations do not query Run history", async () => {
  let calls = 0;
  const latest = async () => { calls += 1; return null; };
  assert.equal(await resolveDefaultLanding("projects", latest), "/projects");
  // launcher 已删；存量偏好值落回 Projects，不是 404。
  assert.equal(await resolveDefaultLanding("new_research", latest), "/projects");
  assert.equal(calls, 0);
});

test("last Session uses the latest recorded Run and safely falls back to Projects", async () => {
  const run = { projectId: "p/a", sessionId: "s 1" };
  assert.equal(
    await resolveDefaultLanding("last_session", async () => run as never),
    "/projects/p%2Fa/sessions/s%201",
  );
  assert.equal(await resolveDefaultLanding("last_session", async () => null), "/projects");
  assert.equal(await resolveDefaultLanding("last_session", async () => { throw new Error("offline"); }), "/projects");
});
