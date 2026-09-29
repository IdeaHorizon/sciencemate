/**
 * 核心永远不 import 专业版 —— 前端这一半。
 *
 * 个人版开源、专业版不开源，源码是同一份。专业版的界面住在几个固定位置（`src/pro/`、
 * 组织功能目录、登录 / 注册页、auth 里两个只有登录才用得上的文件），公开仓库是删掉
 * 它们之后的快照。这只在一个前提下成立：核心的任何文件都不 import 它们。判据落在
 * import 语句上（TS 的 import 是字面量，扫得出来），不落在名字上。
 *
 * 后端那一半：platform/backend/tests/test_the_core_never_imports_the_pro_edition.py。
 *
 * 棘轮 `BASELINE`：今天还允许的核心→专业版 import，逐条写着谁来清。只许减不许增；
 * 一条不再成立时必须从表上删掉。
 */
import { readdirSync, readFileSync, statSync, existsSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { test } from "node:test";
import assert from "node:assert/strict";

const SRC = resolve(import.meta.dirname);

/** 专业版的位置（相对 src/）。目录以 / 结尾；单个文件写全名（不带扩展名也匹配）。 */
const PRO_PATHS = [
  "pro/",
  "app/(workspace)/organisation/",
  "app/login/",
  "app/register/",
];

/** 核心找专业版的唯一一处：`src/edition.ts`。公开树的导出脚本把它换成空的 `wire`。 */
const THE_SEAM = "edition.ts";

/** 棘轮。键 = 核心文件，值 = 它今天还 import 着的专业版文件 → 谁来清。 */
const BASELINE: Record<string, string[]> = {};
// 棘轮已清零（PR③）。从现在起核心 → 专业版的任何一个 import 都是红。

function isPro(file: string): boolean {
  return PRO_PATHS.some((p) => (p.endsWith("/") ? file.startsWith(p) : file === p || file.replace(/\.tsx?$/, "") === p.replace(/\.tsx?$/, "")));
}

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (/\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name) && !name.endsWith(".d.ts")) out.push(full);
  }
  return out;
}

const IMPORT = /(?:import|export)\s[^'"]*?\sfrom\s+['"]([^'"]+)['"]|import\(\s*['"]([^'"]+)['"]\s*\)|^import\s+['"]([^'"]+)['"]/gm;

function resolveImport(spec: string, from: string): string | null {
  let base: string;
  if (spec.startsWith("@/")) base = join(SRC, spec.slice(2));
  else if (spec.startsWith(".")) base = resolve(dirname(from), spec);
  else return null;
  for (const cand of [base, `${base}.ts`, `${base}.tsx`, join(base, "index.ts"), join(base, "index.tsx")]) {
    if (existsSync(cand) && statSync(cand).isFile() && /\.tsx?$/.test(cand)) return relative(SRC, cand);
  }
  return null;
}

function coreImportsOfPro(): Record<string, string[]> {
  const found: Record<string, string[]> = {};
  for (const full of walk(SRC)) {
    const file = relative(SRC, full);
    if (isPro(file) || file === THE_SEAM) continue;
    const text = readFileSync(full, "utf8");
    for (const m of text.matchAll(IMPORT)) {
      const spec = m[1] ?? m[2] ?? m[3];
      const target = spec ? resolveImport(spec, full) : null;
      if (target && isPro(target)) (found[file] ??= []).push(target);
    }
  }
  for (const k of Object.keys(found)) found[k] = [...new Set(found[k])].sort();
  return found;
}

test("核心永远不 import 专业版（超出棘轮的一律拒绝）", () => {
  const found = coreImportsOfPro();
  const fresh: Record<string, string[]> = {};
  for (const [file, targets] of Object.entries(found)) {
    const allowed = new Set(BASELINE[file] ?? []);
    const extra = targets.filter((t) => !allowed.has(t));
    if (extra.length) fresh[file] = extra;
  }
  assert.deepEqual(fresh, {},
    "核心 import 了专业版。公开仓库没有那些文件 —— 要用它的功能，在核心开一个插槽 / 注册表，由 src/pro 在装配时填进去。");
});

test("棘轮只许往下走：表上不再成立的例外必须删掉", () => {
  const found = coreImportsOfPro();
  const stale: Record<string, string[]> = {};
  for (const [file, targets] of Object.entries(BASELINE)) {
    const still = new Set(found[file] ?? []);
    const gone = targets.filter((t) => !still.has(t));
    if (gone.length) stale[file] = gone;
  }
  assert.deepEqual(stale, {}, "这些例外已经不成立了，从 BASELINE 删掉");
});

test("专业版的位置都还在（搬走了就更新这张表）", (t) => {
  // 公开树里 src/pro 整个不在 —— 那是导出的结果，不是表烂了。
  if (!existsSync(join(SRC, "pro"))) return t.skip("公开树：没有专业版");
  const missing = PRO_PATHS.filter((p) => !existsSync(join(SRC, p)));
  assert.deepEqual(missing, [], "PRO_PATHS 里这些位置不存在了");
});

test("接缝只有一处，且它只是转发 wire（公开树里是空桩）", () => {
  const seam = readFileSync(join(SRC, THE_SEAM), "utf8").replace(/\/\*[\s\S]*?\*\//g, "").trim();
  const forwarding = 'export { wire } from "@/pro/wire";';
  const stub = "export function wire(): void {}";
  assert.ok(seam === forwarding || seam === stub,
    `edition.ts 只许是这两种之一：内部树转发 wire，公开树空桩。现在是：${seam}`);
  assert.equal(existsSync(join(SRC, "pro")) ? forwarding : stub, seam, "接缝的形态和这棵树里有没有专业版对不上");
});
