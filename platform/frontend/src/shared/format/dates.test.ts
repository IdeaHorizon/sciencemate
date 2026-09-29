import test from "node:test";
import assert from "node:assert/strict";
import { formatRelativeTime } from "./dates.ts";

test("解析不出的时间戳走「没有时间」那一档，不画 NaN", () => {
  // 判据落在渲染出来的字上：每一档的比较都是 `NaN < x` → 全 false → 一路掉到
  // 最后一档，界面上出现「NaNy ago」。所以这里断言的是"和缺时间戳一个样"，
  // 不是"没抛异常"。
  const absent = formatRelativeTime(null);
  for (const broken of ["", "not-a-date", "2026-13-45T99:99:99Z", "—"]) {
    assert.equal(formatRelativeTime(broken), absent, JSON.stringify(broken));
    assert.doesNotMatch(formatRelativeTime(broken), /NaN/, JSON.stringify(broken));
  }
  // 能解析的照常走档位，别把上面那条写成"永远返回占位符"。
  assert.match(formatRelativeTime(new Date(Date.now() - 5000).toISOString()), /^\d+ 秒前$/);
});
