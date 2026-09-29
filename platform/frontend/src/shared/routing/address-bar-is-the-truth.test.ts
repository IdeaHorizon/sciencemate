/**
 * 静态导出下，动态段的取值只能从地址栏读。
 *
 * 2026-09-05 真机点验：`/projects/<真 id>/sessions/<真 id>` 硬刷新后，首帧发出的是
 *
 *     GET /api/v1/projects/_/sessions/_            → 404
 *     GET /api/v1/projects/_/sessions/_/messages   → 404
 *
 * 因为外壳 HTML 的 RSC 载荷里参数就是占位段 `_`，而 `useParams()` 在水合首帧
 * 交出来的正是它。`usePathname()` 读的是浏览器地址，第一帧就是真的。
 *
 * 所以这道闸：`src/app` 下（以及路由外壳组件里）不许出现 `useParams`。默认拒绝，
 * 例外写在下面并说明为什么那一处不会被烤进外壳。
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import assert from "node:assert/strict";

const ROOTS = ["src/app", "src/features/projects/components"];

/** 例外：写清楚为什么这一处读构建期参数是安全的。目前一个都没有。 */
const ALLOWED: string[] = [];

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else if (full.endsWith(".tsx") || full.endsWith(".ts")) out.push(full);
  }
  return out;
}

test("no route reads useParams — the address bar is the truth", () => {
  const offenders: string[] = [];
  for (const root of ROOTS) {
    for (const file of walk(root)) {
      if (ALLOWED.includes(file)) continue;
      const source = readFileSync(file, "utf8");
      // 注释里提到它是可以的（那些注释正是在解释为什么不用它）
      const code = source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
      if (/\buseParams\s*[(<]/.test(code)) offenders.push(file);
    }
  }
  assert.deepEqual(
    offenders,
    [],
    `这些文件用了 useParams：${offenders.join(", ")}。静态导出下它在水合首帧` +
      "返回占位段 `_`，于是首帧就用错误的 id 发请求。改用 " +
      "@/shared/routing/route-params 里的 useProjectId / useSessionId / useArtifactId。",
  );
});

test("every dynamic segment has a static shell layout", () => {
  const segments = [
    "src/app/(workspace)/projects/[id]/layout.tsx",
    "src/app/(workspace)/projects/[id]/sessions/[sessionId]/layout.tsx",
    "src/app/(workspace)/projects/[id]/artifacts/[aId]/layout.tsx",
  ];
  for (const file of segments) {
    const source = readFileSync(file, "utf8");
    assert.match(
      source,
      /generateStaticParams/,
      `${file} 没有 generateStaticParams —— output: export 会在构建时直接失败`,
    );
    assert.doesNotMatch(
      source,
      /await params|use\(params\)/,
      `${file} 在服务端读了 params —— 那会把占位段 _ 烤进外壳 HTML`,
    );
  }
});
