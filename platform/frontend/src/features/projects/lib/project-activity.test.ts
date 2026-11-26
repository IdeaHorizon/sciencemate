import test from "node:test";
import assert from "node:assert/strict";
import { projectActivity } from "./project-activity.ts";

type Session = NonNullable<Parameters<typeof projectActivity>[1]>[number];
function session(phase: Session["execution"]["phase"], waiting: Session["execution"]["waitingOn"] = null): Session {
  return {
    lifecycleStatus: "active",
    execution: { phase, waitingOn: waiting, outcome: phase === "ended" ? "ok" : null,
      error: null, canStop: phase === "alive", label: "", runId: "run", since: null,
      answer: { via: "composer" } },
  };
}

test("a live session stays running even with an older completed session", () => {
  assert.equal(projectActivity("active", [session("ended"), session("alive")]).tone, "running");
  assert.equal(projectActivity("active", [session("alive", { kind: "compute" })]).tone, "running");
});
test("human and permission waits match the sidebar waiting state", () => {
  assert.equal(projectActivity("active", [session("alive", { kind: "human" })]).tone, "waiting");
  assert.equal(projectActivity("active", [session("alive", { kind: "permission" })]).tone, "waiting");
  assert.equal(projectActivity("active", [session("alive", { kind: "human" }), session("alive")]).tone, "running");
});
test("unarchived does not mean running or completed", () => {
  assert.equal(projectActivity("active", []).tone, "idle");
  assert.equal(projectActivity("active", [session("ended")]).tone, "idle");
  assert.equal(projectActivity("active", [session("interrupted")]).tone, "idle");
  assert.equal(projectActivity("active", [{ ...session("alive"), lifecycleStatus: "archived" }]).tone, "idle");
});
test("loading, failed refresh and archived project do not report success", () => {
  assert.equal(projectActivity("active", undefined).tone, "unknown");
  assert.equal(projectActivity("active", [session("alive")], true).tone, "unknown");
  assert.equal(projectActivity("archived", [session("alive")], true).tone, "archived");
});
test("running then waiting then idle tracks refreshed execution data", () => {
  const tones = [session("alive"), session("alive", { kind: "permission" }), session("ended")]
    .map((s) => projectActivity("active", [s]).tone);
  assert.deepEqual(tones, ["running", "waiting", "idle"]);
});
