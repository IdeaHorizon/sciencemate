import test from "node:test";
import assert from "node:assert/strict";
import { projectIdentity } from "./project-identity.ts";

test("project identity uses authoritative project metadata instead of the URL id", () => {
  const identity = projectIdentity({
    id: "4de02626-4911-4f90-9900-4af28644d1aa",
    name: "Robust interatomic potentials",
    description: null,
    status: "active",
    research_domain: "Materials science",
    entry_type: null,
    created_at: "2026-08-03T00:00:00Z",
    updated_at: "2026-08-03T00:00:00Z",
  }, { loading: false, error: false });

  assert.deepEqual(identity, {
    name: "Robust interatomic potentials",
    meta: "进行中 · Materials science",
    state: "ready",
  });
});

test("project identity has honest loading and error states", () => {
  assert.equal(projectIdentity(undefined, { loading: true, error: false }).name, "载入中…");
  assert.deepEqual(projectIdentity(undefined, { loading: false, error: true }), {
    name: "读不到这个项目",
    meta: "项目信息没读出来",
    state: "error",
  });
});

