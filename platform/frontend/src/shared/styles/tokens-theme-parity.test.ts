import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

/**
 * 主题有三态：显式 dark、显式 light、以及没标 data-theme 的 system。
 * 三处映射必须声明**同一套** token 名字——漏一个不会报错，只会让那个 token
 * 回落到另一个主题的取值。
 *
 * 这条护栏是有实据的：修之前 system 那一份漏掉了全部 5 个
 * `--color-score-*`，于是「跟随系统 + 系统是亮色」的用户在白底上拿到的是
 * 暗色主题的分数配色，一直没人发现。
 *
 * 扫盘而不是写名单：新增任何 --color-* / --shadow-* 都自动纳入检查。
 */
const CSS = readFileSync(new URL("./tokens.css", import.meta.url), "utf8");

/** 抠出一个 selector 块的内容（只取第一层，够用：本文件没有嵌套）。 */
function block(startSelector: string): string {
  const at = CSS.indexOf(startSelector);
  assert.ok(at > -1, `找不到选择器：${startSelector}`);
  const open = CSS.indexOf("{", at);
  let depth = 0;
  for (let i = open; i < CSS.length; i += 1) {
    if (CSS[i] === "{") depth += 1;
    if (CSS[i] === "}") {
      depth -= 1;
      if (depth === 0) return CSS.slice(open + 1, i);
    }
  }
  throw new Error(`选择器 ${startSelector} 的块没闭合`);
}

function declaredTokens(body: string): Set<string> {
  return new Set([...body.matchAll(/^\s*(--[a-z0-9-]+)\s*:/gm)].map((m) => m[1]));
}

const themed = (names: Set<string>) =>
  new Set([...names].filter((n) => n.startsWith("--color-") || n.startsWith("--shadow-")));

test("三个主题态声明同一套 token —— 漏一个不会报错，只会静默回落到另一主题的取值", () => {
  const dark = themed(declaredTokens(block(':root,\n:root[data-theme="dark"]')));
  const light = themed(declaredTokens(block(':root[data-theme="light"]')));
  const system = themed(declaredTokens(block(":root:not([data-theme]),")));

  assert.ok(dark.size >= 25, `暗色映射只声明了 ${dark.size} 个 token，太少了`);
  assert.deepEqual([...light].sort(), [...dark].sort(), "亮色映射与暗色映射的 token 集合不一致");
  assert.deepEqual([...system].sort(), [...dark].sort(), "system 态映射与暗色映射的 token 集合不一致");
});

test("取值只在调色板里出现一次：映射段不许写字面色值", () => {
  for (const selector of [
    ':root,\n:root[data-theme="dark"]',
    ':root[data-theme="light"]',
    ":root:not([data-theme]),",
  ]) {
    const body = block(selector);
    const literals = [...body.matchAll(/:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\))/g)].map((m) => m[1]);
    assert.deepEqual(
      literals,
      [],
      `${selector} 里出现了字面色值 ${literals.join(", ")} —— 取值应该只在 --dark-* / --light-* 调色板里写一次`,
    );
  }
});

test("每个映射引用的调色板变量都真的存在", () => {
  const palette = declaredTokens(block(":root {"));
  for (const selector of [
    ':root,\n:root[data-theme="dark"]',
    ':root[data-theme="light"]',
    ":root:not([data-theme]),",
  ]) {
    for (const [, ref] of block(selector).matchAll(/var\((--(?:dark|light)-[a-z0-9-]+)\)/g)) {
      assert.ok(palette.has(ref), `${selector} 引用了不存在的调色板变量 ${ref}`);
    }
  }
});

test("减少动效不许把状态信号一起关掉", () => {
  // 原实现是 `animation-duration: .01ms !important` + `animation-iteration-count: 1
  // !important`，把 spinner 和「正在跑」的脉冲点一并停掉——那砍掉的是功能信号。
  assert.ok(
    !/animation-iteration-count:\s*1\s*!important/.test(CSS),
    "把 animation-iteration-count 砍成 1 会让循环状态动画永久停住",
  );
  assert.ok(
    !/animation-duration:\s*0?\.0*1m?s\s*!important/.test(CSS),
    "把 animation-duration 归零等于关掉动画，而不是减少动效",
  );
  // 两个来源（应用内开关 / 系统偏好）都必须接上。
  assert.match(CSS, /:root\[data-reduce-motion="true"\]\s*\{[^}]*--motion-shift:\s*0/);
  assert.match(CSS, /@media \(prefers-reduced-motion: reduce\)/);
});
