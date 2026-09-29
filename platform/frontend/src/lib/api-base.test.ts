import assert from "node:assert/strict";
import test from "node:test";

import { API_BASE_URL } from "./api.ts";

test("the production-safe API default is same-origin", () => {
  if (process.env.NEXT_PUBLIC_API_BASE_URL === undefined) {
    assert.equal(API_BASE_URL, "/api/v1");
  }
  assert.equal(API_BASE_URL.includes("localhost"), false);
});
