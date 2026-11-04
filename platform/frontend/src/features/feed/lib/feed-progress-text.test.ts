import { test } from "node:test";
import assert from "node:assert/strict";
import type { FeedProgress } from "../../../lib/api.ts";
import { feedProgressText } from "./feed-progress-text.ts";

const settled = (fields: Partial<FeedProgress>): FeedProgress => ({
  phase: "settled", collected: 0, prepared: 0, pending: 0, recommendable: 0, translated: 0,
  translation_pending: 0, source_groups: [], project_error: false, ...fields,
});

test("有可推荐的内容、只是译文还没好时，如实说，不说成没有相关内容", () => {
  // 11-04 实测：306 条可推荐、0 条译好，页面上写的是「暂时没有与你的订阅和项目相关的内容」。
  const text = feedProgressText(settled({ recommendable: 306, translation_pending: 306 }), "zh");
  assert.match(text, /306 条推荐内容的中文译文还没准备好/);
  assert.doesNotMatch(text, /暂时没有/);
});

test("真的什么都没有时，照旧说没有相关内容", () => {
  assert.match(feedProgressText(settled({}), "zh"), /暂时没有与你的订阅和项目相关的内容/);
});

test("英文界面不等中文译文，这句话不出现", () => {
  assert.doesNotMatch(feedProgressText(settled({ recommendable: 3, translation_pending: 3 }), "en"), /translation/);
});
