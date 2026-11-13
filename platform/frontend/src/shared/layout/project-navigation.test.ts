import { test } from "node:test";
import assert from "node:assert/strict";
import { visibleSessions } from "./project-navigation.ts";
import type { ResearchSession } from "../../features/sessions/types.ts";

const sessions = Array.from({ length: 12 }, (_, index) => ({ id: String(index), title: `Research ${index}` }) as ResearchSession);

test("an older selected session remains visible before loading more", () => {
  assert.deepEqual(visibleSessions(sessions, "10", 6).map((session) => session.id), ["0", "1", "2", "3", "4", "5", "10"]);
  assert.equal(visibleSessions(sessions, "2", 6).length, 6);
});

test("search reaches older sessions and does not inject a nonmatching current session", () => {
  assert.deepEqual(visibleSessions(sessions, "0", 6, " RESEARCH 11 ").map((session) => session.id), ["11"]);
  assert.deepEqual(visibleSessions(sessions, "0", 6, "missing"), []);
});
