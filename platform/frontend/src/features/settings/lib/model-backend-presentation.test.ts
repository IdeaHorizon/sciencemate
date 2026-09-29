import assert from "node:assert/strict";
import test from "node:test";
import type { ModelBackend } from "@/lib/api";
import { deduplicateModelBackends, modelBackendDisplayName, modelBackendModelLabel } from "./model-backend-presentation.ts";

function backend(overrides: Partial<ModelBackend>): ModelBackend {
  return {
    id: "id",
    provider: "demo",
    display_name: "Legacy product demo",
    model: "atrium-demo-v1",
    base_url: null,
    status: "ready",
    is_default: false,
    scope: "platform",
    has_api_key: false,
    editable: false,
    ...overrides,
  };
}

test("demo aliases with the same model collapse to one stable connection", () => {
  const items = deduplicateModelBackends([
    backend({ id: "legacy", provider: "demo", base_url: null }),
    backend({ id: "local", provider: "local_demo", base_url: "local://demo", is_default: true }),
  ]);
  assert.equal(items.length, 1);
  assert.equal(items[0].id, "local");
  assert.equal(modelBackendDisplayName(items[0]), "Local demonstration model");
  assert.equal(modelBackendModelLabel(items[0]), "Deterministic local runtime");
  assert.equal(`${modelBackendDisplayName(items[0])} ${modelBackendModelLabel(items[0])}`.toLowerCase().includes("atrium"), false);
});

test("real endpoints remain distinct when provider and model match", () => {
  const items = deduplicateModelBackends([
    backend({ id: "one", provider: "openai", model: "model-a", base_url: "https://one.example/v1" }),
    backend({ id: "two", provider: "openai", model: "model-a", base_url: "https://two.example/v1" }),
  ]);
  assert.equal(items.length, 2);
});
