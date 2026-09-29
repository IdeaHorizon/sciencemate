import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";

/**
 * app/globals.css 在文件头 @import 全部模块，自身规则排在后面 —— 等特异性下
 * **globals 永远赢**。所以任何「同一个选择器在 globals 和某个模块里各写一份、
 * 且争同一批属性」的情况，都意味着模块里那几行是死的：照它改不会有任何效果，
 * 而且两边都不报错。
 *
 * 这条护栏扫盘，不写名单：新增任何一处冲突都会红。下面的 KNOWN 是当前存量，
 * 只允许变短。存量里模块那份**部分**生效，不能一删了之，得把 globals 的取值
 * 并进模块规则再删 globals 那条 —— 会动 cascade 位置，需要配一轮视觉核对，
 * 所以单独一个 PR 做。
 */
const DIR = new URL("./", import.meta.url);
const GLOBALS = new URL("../../app/globals.css", import.meta.url);

type Rule = { selector: string; props: Set<string>; file: string };

function parse(css: string, file: string): Rule[] {
  // @keyframes 内的 0% / 50% / 100% 不是选择器，整块摘掉
  const stripped = css
    .replace(/@keyframes[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}/g, "")
    .replace(/\/\*[\s\S]*?\*\//g, "");
  const rules: Rule[] = [];
  for (const m of stripped.matchAll(/([^{}@]+?)\{([^{}]*)\}/g)) {
    const props = new Set([...m[2].matchAll(/([a-z-]+)\s*:/g)].map((p) => p[1]));
    for (const one of m[1].split(",")) {
      const selector = one.trim().replace(/\s+/g, " ");
      if (selector) rules.push({ selector, props, file });
    }
  }
  return rules;
}

/** 当前存量：模块那份部分生效，需要合并而不是删除。只许变短。 */
const KNOWN = new Set([
  ".checkpoint-choice-btn", ".checkpoint-choices", ".conversation-item", ".conversation-list",
  ".conversation-sidebar", ".conversation-sidebar-header", ".event-content-preview",
  ".event-meta-line", ".event-readable p", ".memory-topic", ".page", ".page-header",
  ".settings-field select", ".timeline", ".timeline-content", ".timeline-detail",
  ".timeline-dot", ".timeline-header", ".timeline-item", ".timeline-summary",
]);

test("globals.css 和模块之间没有新的同选择器冲突", () => {
  const globals = parse(readFileSync(GLOBALS, "utf8"), "globals.css");
  const modules = readdirSync(DIR)
    .filter((f) => f.endsWith(".css"))
    .flatMap((f) => parse(readFileSync(new URL(f, DIR), "utf8"), f));

  const byGlobal = new Map<string, Set<string>>();
  for (const r of globals) {
    const acc = byGlobal.get(r.selector) ?? new Set<string>();
    for (const p of r.props) acc.add(p);
    byGlobal.set(r.selector, acc);
  }

  const collisions = new Map<string, string>();
  for (const r of modules) {
    const gp = byGlobal.get(r.selector);
    if (!gp) continue;
    const overlap = [...r.props].filter((p) => gp.has(p));
    if (overlap.length === 0) continue;
    collisions.set(r.selector, `${r.file} 的 ${overlap.sort().join(",")} 被 globals.css 覆盖`);
  }

  const fresh = [...collisions].filter(([sel]) => !KNOWN.has(sel));
  assert.deepEqual(
    fresh.map(([sel, why]) => `${sel} — ${why}`),
    [],
    "\n新增了 globals ↔ 模块的样式冲突。模块里那几行是死代码（globals 排在 @import 之后，" +
      "等特异性下必胜），照它改不会有效果。请把规则写进模块、或改 globals 那一份，别两边都写。\n",
  );

  // KNOWN 只许变短：修完一条就从名单里划掉，别让它变成永久豁免。
  const stale = [...KNOWN].filter((sel) => !collisions.has(sel));
  assert.deepEqual(
    stale,
    [],
    "\n这些选择器已经不冲突了，请从 KNOWN 名单里删掉 —— 名单只有会缩短才有意义。\n",
  );
});
