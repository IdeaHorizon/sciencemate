import assert from "node:assert/strict";
import test from "node:test";
import { feedCardTranslation } from "./feed-card-translation.ts";

const saved = {id: "A", title: "Settling", summary: "Methods", title_zh: "沉降", abstract_zh: "方法"};

test("saved list and detail can render both translations after reloading without today's recommendation", () => {
  assert.deepEqual(feedCardTranslation("zh", saved), {titleZh: "沉降", summaryZh: "方法"});
  assert.deepEqual(feedCardTranslation("en", saved), {});
});

test("missing saved fields can reuse current translations only for the same item and original text", () => {
  const live = {titleZh: "刚译的标题", abstractZh: "刚译的摘要"};
  const row = {...saved, abstract_zh: " "};
  assert.deepEqual(feedCardTranslation("zh", row, saved, live), {titleZh: "沉降", summaryZh: "刚译的摘要"});
  assert.deepEqual(feedCardTranslation("zh", {...saved, title_zh: null}, saved, live),
    {titleZh: "刚译的标题", summaryZh: "方法"});
  assert.equal(feedCardTranslation("zh", {...row, summary: "Changed"}, saved, live).summaryZh, undefined);
  assert.equal(feedCardTranslation("zh", {...row, id: "B"}, saved, live).summaryZh, undefined);
  assert.deepEqual(feedCardTranslation("en", row, saved, live), {});
});
