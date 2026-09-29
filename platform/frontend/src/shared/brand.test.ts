import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import test from "node:test";

import { PRODUCT_MARK, PRODUCT_NAME } from "./brand.ts";

// 产品名只有一个出处。扫盘不写名单：src 下任何文件（本文件与 brand.ts 除外）
// 都不许再写产品名字面量、旧名，或在 CSS 里写死短标 —— 改名只改 brand.ts。
const SRC = join(import.meta.dirname, "..");
const SELF = new Set([join(SRC, "shared", "brand.ts"), join(SRC, "shared", "brand.test.ts")]);
// 每次改名把旧名加进来 —— 扫盘闸挡的是"旧名在某个角落活下来"，那种残留不报错。
const OLD_NAMES = ["IEIT Research Platform", "Agent for Science"];

function* walk(dir: string): Generator<string> {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) yield* walk(p);
    else if (/\.(tsx?|css)$/.test(name)) yield p;
  }
}

test("产品名字面量只存在于 brand.ts", () => {
  const offenders: string[] = [];
  for (const file of walk(SRC)) {
    if (SELF.has(file)) continue;
    const text = readFileSync(file, "utf8");
    for (const literal of [PRODUCT_NAME, ...OLD_NAMES]) {
      if (text.includes(literal)) offenders.push(`${relative(SRC, file)}: "${literal}"`);
    }
    if (/content:\s*"[A-Za-z]{2,6}"/.test(text) && text.includes(".app-brand::after")) {
      offenders.push(`${relative(SRC, file)}: 折叠侧栏短标写死在 CSS，应走 attr(data-mark)`);
    }
  }
  assert.deepEqual(offenders, []);
});

test("短标短到放得进折叠侧栏", () => {
  assert.ok(PRODUCT_MARK.length >= 2 && PRODUCT_MARK.length <= 4, PRODUCT_MARK);
  assert.ok(PRODUCT_NAME.length > 0);
});
