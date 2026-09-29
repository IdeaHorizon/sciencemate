import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

/**
 * 「暗色模式下有些白底小块，字也是白的」（2026-09-02）的根因：
 * misc.css 写的是 `var(--surface-muted, #f3f4f6)`，而这套 token 库里根本没有
 * `--surface-muted`。回落值是亮色的灰，两个主题下都生效；字色则从全局 `pre`
 * 那条继承来近白 —— 于是暗色主题里出现白底白字。
 *
 * 全库一共 21 个这样的幽灵名字（--fg / --fg-muted / --border / --accent /
 * --text-subtle …），200 多处引用。没带回落的那批会让声明在计算值阶段失效：
 * color 静默继承、background 变透明、border 变 currentColor —— 页面「看起来
 * 还行」全是碰巧。
 *
 * 这条闸扫盘不写名单：src 下所有 css / tsx / ts 里 `var(--x` 引用的名字，必须
 * 在某处有 `--x:` 声明；css 里也不许再给 token 拖一个字面色值回落——token
 * 在 :root 永远有值，回落是死代码，而它正是这次事故的形状。
 */
const SRC = new URL("../../", import.meta.url).pathname;

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.(css|tsx?|)$/.test(name) && /\.(css|tsx|ts)$/.test(name)) out.push(p);
  }
  return out;
}

const files = walk(SRC).filter((p) => !p.endsWith(".test.ts"));
const sources = files.map((p) => [relative(SRC, p), readFileSync(p, "utf8")] as const);

const declared = new Set<string>();
for (const [, text] of sources) {
  for (const m of text.matchAll(/(?:^|[\s;{"'`])(--[a-zA-Z0-9-]+)\s*:/g)) declared.add(m[1]);
}

test("每个 var(--x) 引用的自定义属性都在某处声明过 —— 未声明不会报错，只会静默失效或吃到回落值", () => {
  const missing = new Map<string, string[]>();
  for (const [file, text] of sources) {
    for (const m of text.matchAll(/var\(\s*(--[a-zA-Z0-9-]+)/g)) {
      if (declared.has(m[1])) continue;
      const line = text.slice(0, m.index).split("\n").length;
      const list = missing.get(m[1]) ?? [];
      list.push(`${file}:${line}`);
      missing.set(m[1], list);
    }
  }
  assert.ok(declared.size > 100, `只找到 ${declared.size} 个声明，扫描本身可能坏了`);
  const report = [...missing].map(([n, at]) => `  ${n} (${at.length} 处) 例如 ${at[0]}`).join("\n");
  assert.equal(missing.size, 0, `引用了从未声明的自定义属性：\n${report}`);
});

test("css 里的 token 引用不许拖字面色值回落 —— 回落只在 token 不存在时生效，那正是要被上一条抓住的情形", () => {
  const offenders: string[] = [];
  for (const [file, text] of sources) {
    if (!file.endsWith(".css")) continue;
    for (const m of text.matchAll(/var\(\s*--[a-zA-Z0-9-]+\s*,\s*(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)|white|black)\s*\)/g)) {
      offenders.push(`${file}:${text.slice(0, m.index).split("\n").length} ${m[0]}`);
    }
  }
  assert.deepEqual(offenders, []);
});
