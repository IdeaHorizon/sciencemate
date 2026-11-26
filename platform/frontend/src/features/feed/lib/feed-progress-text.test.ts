import { test } from "node:test";
import assert from "node:assert/strict";
import type { FeedProgress, FeedToday } from "../../../lib/api.ts";
import { feedEmptyState } from "./feed-progress-text.ts";

const all = { domain: true, journal: true, scholar: true, project: true };
const onlyProject = { ...all, domain: false, journal: false, scholar: false };

function page(fields: Partial<FeedProgress> = {}): FeedToday {
  return {
    feed: [], has_more: false, onboarded: true, personalized: true, empty_reason: "", refreshing: false,
    progress: {
      phase: "settled", collected: 0, prepared: 0, pending: 0, recommendable: 0, translated: 0,
      translation_pending: 0, source_groups: [], project_error: false, ...fields,
    },
  };
}

const desktop = page({
  recommendable: 1986, translated: 1985, translation_pending: 1,
  source_groups: [
    { sources: ["domain"], total: 1160, translated: 1159 },
    { sources: ["journal"], total: 826, translated: 826 },
  ],
});
desktop.refreshing = true;

test("只勾选项目时，其他来源的后台活动和待译数都不会显示为项目进度", () => {
  assert.deepEqual(feedEmptyState(desktop, onlyProject, "zh"), {
    title: "还没有推荐内容", hint: "当前勾选的来源暂无推荐内容。",
  });
  assert.match(feedEmptyState(desktop, onlyProject, "en").hint, /selected sources/);
});

test("多项勾选时，每张等待译文的卡片只算一次，且只计算命中的来源", () => {
  const mixed = page({ recommendable: 8, translated: 0, translation_pending: 8, source_groups: [
    { sources: ["domain", "project"], total: 2, translated: 0 },
    { sources: ["journal"], total: 6, translated: 0 },
  ] });
  const selection = { ...onlyProject, domain: true };
  assert.match(feedEmptyState(mixed, selection, "zh").hint, /有 2 条推荐内容的中文译文/);
  assert.doesNotMatch(feedEmptyState(mixed, selection, "en").hint, /translation|No content matches/);
});

test("全选且所有卡片待译时，中文说明等待译文", () => {
  assert.match(feedEmptyState(page({ recommendable: 306, translation_pending: 306 }), all, "zh").hint, /306 条推荐内容的中文译文/);
});

test("有可展示卡片时，空页不会归因到另外一条待译或没有匹配内容", () => {
  assert.match(feedEmptyState(desktop, all, "zh").hint, /重新加载/);
  assert.match(feedEmptyState(desktop, all, "en").hint, /reload/);
});

test("未勾选来源、整个推荐池为空和服务端给出具体原因时各有相应说明", () => {
  assert.match(feedEmptyState(desktop, { ...onlyProject, project: false }, "zh").hint, /请勾选/);
  assert.match(feedEmptyState(page(), all, "zh").hint, /暂时没有与你的订阅和项目相关的内容/);
  const empty = { ...page(), empty_reason: "Collection is disabled." };
  assert.equal(feedEmptyState(empty, onlyProject, "en").hint, empty.empty_reason);
});
