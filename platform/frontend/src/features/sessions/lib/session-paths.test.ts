import test from "node:test";
import assert from "node:assert/strict";
import { projectSessionsCollectionPath } from "./session-paths.ts";

test("session index explicitly requests archived sessions", () => {
  assert.equal(
    projectSessionsCollectionPath("project/a", { includeArchived: true }),
    "/projects/project%2Fa/sessions?includeArchived=true",
  );
});

test("session creation keeps the collection path free of list filters", () => {
  assert.equal(projectSessionsCollectionPath("project-a"), "/projects/project-a/sessions");
});
