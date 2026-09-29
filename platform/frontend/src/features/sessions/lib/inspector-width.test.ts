import assert from "node:assert/strict";
import test from "node:test";

import {
  CANVAS_MIN_WIDTH,
  clampInspectorWidth,
  INSPECTOR_DEFAULT_WIDTH,
  INSPECTOR_MAX_WIDTH,
  INSPECTOR_MIN_WIDTH,
} from "./inspector-width.ts";

test("a width inside the range is kept as-is", () => {
  assert.equal(clampInspectorWidth(560, 1920), 560);
});

test("dragging past either end stops at the bound", () => {
  assert.equal(clampInspectorWidth(40, 1920), INSPECTOR_MIN_WIDTH);
  assert.equal(clampInspectorWidth(5000, 3840), INSPECTOR_MAX_WIDTH);
});

test("the conversation column keeps its minimum width on a normal viewport", () => {
  // 1200 - 420 = 780，比 MAX(900) 小，所以是这一侧在管事。
  assert.equal(clampInspectorWidth(5000, 1200), 1200 - CANVAS_MIN_WIDTH);
});

test("a viewport too narrow for both columns still yields a usable inspector", () => {
  /**
   * 这条是这个模块存在的理由。窗口宽 600 时"上界"= 600 - 420 = 180，**低于**
   * 下界 320：天真的 `Math.min(Math.max(w, MIN), upper)` 在这里会返回 180，
   * 右栏塌成一条比最小值还窄的缝，而且怎么拖都拖不回来。
   *
   * 宁可挤对话列，也不要给出一个不可用的右栏。
   */
  assert.equal(clampInspectorWidth(500, 600), INSPECTOR_MIN_WIDTH);
  assert.equal(clampInspectorWidth(100, 600), INSPECTOR_MIN_WIDTH);
  // 连负数视口（测试环境 / 窗口最小化时确实会出现 0）也不许算出负宽度。
  assert.equal(clampInspectorWidth(500, 0), INSPECTOR_MIN_WIDTH);
});

test("a non-numeric width falls back to the default instead of producing NaN", () => {
  // localStorage 里存的是字符串，取出来 Number() 一下就可能是 NaN。
  // NaN 一路传到 flex-basis 上，右栏会直接不见。
  assert.equal(clampInspectorWidth(Number.NaN, 1920), INSPECTOR_DEFAULT_WIDTH);
  assert.equal(clampInspectorWidth(Number.POSITIVE_INFINITY, 1920), INSPECTOR_DEFAULT_WIDTH);
});

test("the result is always an integer", () => {
  // 拖拽给的是浮点指针坐标；亚像素宽度会让右栏边框忽隐忽现。
  assert.equal(clampInspectorWidth(560.7, 1920), 561);
  assert.ok(Number.isInteger(clampInspectorWidth(499.999, 1920)));
});

test("the default width itself survives a clamp on a normal viewport", () => {
  // 默认值必须落在合法区间里 —— 否则每次打开都会被夹一次，实际默认宽度
  // 就不是这个常数了。
  assert.equal(clampInspectorWidth(INSPECTOR_DEFAULT_WIDTH, 1440), INSPECTOR_DEFAULT_WIDTH);
  assert.ok(INSPECTOR_DEFAULT_WIDTH >= INSPECTOR_MIN_WIDTH);
  assert.ok(INSPECTOR_DEFAULT_WIDTH <= INSPECTOR_MAX_WIDTH);
});
