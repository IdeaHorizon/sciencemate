import test from "node:test";
import assert from "node:assert/strict";
import { PROBE_STALE_AFTER_MS, probeFreshness, probeSummary } from "./probe-freshness.ts";

const NOW = new Date("2026-08-21T12:00:00Z").getTime();
const at = (msAgo: number) => new Date(NOW - msAgo).toISOString();

test("从没探过 ≠ 刚探过 —— 不能安静地当新鲜", () => {
  assert.deepEqual(probeFreshness({ last_probe_at: null }, NOW), { kind: "never" });
  assert.deepEqual(probeFreshness({ last_probe_at: undefined }, NOW), { kind: "never" });
  assert.equal(probeSummary({ last_probe_at: null, last_probe_ok: null, last_probe_detail: null }, NOW), "从未检测");
});

test("解析不出的时间戳算「不知道」，不算新鲜", () => {
  assert.deepEqual(probeFreshness({ last_probe_at: "不是时间" }, NOW), { kind: "never" });
});

test("刚探过是 fresh，超过一天是 stale", () => {
  assert.equal(probeFreshness({ last_probe_at: at(60_000) }, NOW).kind, "fresh");
  assert.equal(probeFreshness({ last_probe_at: at(PROBE_STALE_AFTER_MS - 1) }, NOW).kind, "fresh");
  assert.equal(probeFreshness({ last_probe_at: at(PROBE_STALE_AFTER_MS) }, NOW).kind, "stale");
  assert.equal(probeFreshness({ last_probe_at: at(30 * 24 * 3600_000) }, NOW).kind, "stale");
});

test("时钟偏移导致的「未来时间」当 0 岁，不显示负数", () => {
  const future = probeFreshness({ last_probe_at: new Date(NOW + 600_000).toISOString() }, NOW);
  assert.equal(future.kind, "fresh");
  assert.equal(future.kind === "fresh" ? future.ageMs : -1, 0);
  assert.equal(future.kind === "fresh" ? future.label : "", "刚刚");
});

test("陈旧的观测必须**明说**过期，而不是只显示一个 Ready", () => {
  const summary = probeSummary(
    { last_probe_at: at(3 * 24 * 3600_000), last_probe_ok: true, last_probe_detail: "ok" },
    NOW,
  );
  assert.match(summary, /3 天前/);
  assert.match(summary, /通过/);
  assert.match(summary, /已过期/, "陈旧却不吭声 = 让人继续把快照当事实");
});

test("三态：通过 / 被拒 / 未判定 —— 探不出结论不能说成通过", () => {
  const base = { last_probe_at: at(60_000), last_probe_detail: null };
  assert.match(probeSummary({ ...base, last_probe_ok: true }, NOW), /通过/);
  assert.match(probeSummary({ ...base, last_probe_ok: false }, NOW), /被拒/);
  assert.match(probeSummary({ ...base, last_probe_ok: null }, NOW), /未判定/);
});

test("相对时间的分档", () => {
  const label = (msAgo: number) => {
    const f = probeFreshness({ last_probe_at: at(msAgo) }, NOW);
    return f.kind === "never" ? "never" : f.label;
  };
  assert.equal(label(30_000), "刚刚");
  assert.equal(label(5 * 60_000), "5 分钟前");
  assert.equal(label(3 * 3600_000), "3 小时前");
  assert.equal(label(50 * 3600_000), "2 天前");
});
