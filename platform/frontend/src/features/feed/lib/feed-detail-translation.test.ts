import assert from "node:assert/strict";
import test from "node:test";
import { retainDetailTranslation } from "./feed-detail-translation.ts";

const original = { id: "a", title: "Title", summary: "Abstract", titleZh: "标题", summaryZh: "摘要" };

test("read demotion drops the translation lookup but the open detail keeps Chinese", () => {
  const opened = retainDetailTranslation(null, original);
  const refreshed = retainDetailTranslation(opened, { id: "a", title: "Title", summary: "Abstract" });
  assert.strictEqual(refreshed, opened);
  assert.strictEqual(retainDetailTranslation(refreshed, { ...original, titleZh: " ", summaryZh: "" }), opened);
});

test("new translations replace retained text independently", () => {
  const next = retainDetailTranslation(original, { ...original, titleZh: "新标题", summaryZh: undefined });
  assert.equal(next.titleZh, "新标题");
  assert.equal(next.summaryZh, "摘要");
});

test("switching articles cannot reuse translations even with identical source text", () => {
  const next = retainDetailTranslation(original, { id: "b", title: "Title", summary: "Abstract" });
  assert.equal(next.titleZh, undefined);
  assert.equal(next.summaryZh, undefined);
});

test("changed source invalidates only its own translation", () => {
  const next = retainDetailTranslation(original, { id: "a", title: "Title", summary: "New abstract" });
  assert.equal(next.titleZh, "标题");
  assert.equal(next.summaryZh, undefined);
  const changedTitle = retainDetailTranslation(original, { id: "a", title: "New title", summary: "Abstract" });
  assert.equal(changedTitle.titleZh, undefined);
  assert.equal(changedTitle.summaryZh, "摘要");
});
