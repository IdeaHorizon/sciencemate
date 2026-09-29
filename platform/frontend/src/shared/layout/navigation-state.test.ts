import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join, relative } from "node:path";
import { GLOBAL_NAVIGATION, workspaceDisplayName } from "./navigation-state.ts";

test("personal workspace naming never repeats the signed-in user identity", () => {
  assert.equal(workspaceDisplayName({ kind: "personal", id: "u", name: "Mia Zhang" }, "en"), "Personal workspace");
  assert.equal(workspaceDisplayName({ kind: "personal", id: "u", name: "Mia Zhang" }), "个人工作区");
  assert.equal(workspaceDisplayName({ kind: "individual" as never, id: "u", name: "Mia Zhang" }, "en"), "Personal workspace");
  // 机构/课题组的名字是后端给的真名 —— 两种语言里都原样显示，不翻译。
  assert.equal(workspaceDisplayName({ kind: "group", id: "g", name: "Robotics Lab" }), "Robotics Lab");
  assert.equal(workspaceDisplayName({ kind: "group", id: "g", name: "Robotics Lab" }, "en"), "Robotics Lab");
});

test("global rail is Project-level only: no Session entry point, no Session list", () => {
  // 2026-09-12 核查后砍掉三个：Inbox（唯一写入方零调用 → 永远空）、学术搜索
  // （孤岛，那套端点只有它在用）、Artifacts（与项目级「研究产出」同问题不同
  // 数据源，且分叉不报错）。理由逐条写在 navigation-state.ts 的表上面。
  assert.deepEqual(GLOBAL_NAVIGATION.primary.map((item) => item.label.zh), ["资讯", "项目"]);
  assert.deepEqual(GLOBAL_NAVIGATION.primary.map((item) => item.label.en), ["Feed", "Projects"]);
  // 专业版只多**一个**侧栏入口（导航盘点 RFC 09-12）：「组织」。09-17 之前它散在
  // 三处（设置里一页、侧栏的组织知识、算力页一段），现在收成一个。
  // 「组织」不在核心的表上：它由专业版装配时登记（registerNavigation），判据在
  // src/pro/features/organisation/the-pro-edition-adds-one-entry.test.ts。核心的表一个
  // 组织概念都没有 —— 个人版就是这张表的全部。
  assert.deepEqual(GLOBAL_NAVIGATION.more.map((item) => item.label.zh), ["算力"]);
  assert.deepEqual(GLOBAL_NAVIGATION.more.filter((item) => "needsAny" in item), [],
    "核心的导航表里有带能力闸的项 —— 那是专业版的东西，该走 registerNavigation");

  const destinations = [
    ...GLOBAL_NAVIGATION.primary,
    ...GLOBAL_NAVIGATION.more,
    GLOBAL_NAVIGATION.settings,
  ];
  // Session 只在 Project 里才有：全局导航不许出现任何 Project 外的 Session 目的地。
  const hrefs: string[] = destinations.map((item) => item.href);
  assert.equal(hrefs.filter((href) => href.includes("session") || href === "/chat").length, 0);
  assert.equal(new Set(hrefs).size, destinations.length);
  assert.equal(destinations.filter((item) => item.label.zh === "设置").length, 1);
});

/**
 * 导航表和它的呈现（图标 + 高亮判据）是两个文件里的两份清单。从前 AppShell
 * 那份是**按下标**展开导航表的（`primary[0]` / `primary[1]`），于是往导航表里
 * 加一项而不改那边，会静默错位：图标和标签配错人，或者新项根本不出现。
 *
 * 所以这条判据不是"Feed 有没有图标"，而是**每一个**目的地都在呈现表里有一份。
 * 下一个人加导航项时，忘了配图标会在这里红，而不是在用户屏幕上。
 */
test("every global destination has an icon and a highlight rule", () => {
  const shell = readFileSync(new URL("./AppShell.tsx", import.meta.url), "utf8");
  const table = shell.slice(
    shell.indexOf("const NAV_PRESENTATION"),
    shell.indexOf("function decorate"),
  );
  assert.ok(table.length > 0, "NAV_PRESENTATION must exist");
  for (const item of [...GLOBAL_NAVIGATION.primary, ...GLOBAL_NAVIGATION.more]) {
    assert.ok(
      table.includes(`"${item.href}":`),
      `${item.href} has no entry in NAV_PRESENTATION`,
    );
  }
});

/**
 * 判据落在渲染全局侧栏那段源码上，不落在导航表上：上一版的「Active」列表根本
 * 不走 GLOBAL_NAVIGATION —— 它自己去查 runs 再拼 /projects/x/sessions/y 的链接。
 * 只断言导航表干净，这类新增的旁路一条都拦不住。
 */
test("the global sidebar renders no Session rows of its own", () => {
  const shell = readFileSync(new URL("./AppShell.tsx", import.meta.url), "utf8");
  const global = shell.slice(
    shell.indexOf("function GlobalNavigation"),
    shell.indexOf("function ProjectNavigation"),
  );
  assert.ok(global.length > 0, "GlobalNavigation must exist");
  for (const forbidden of [/sessions\//, /useVisibleRuns/, /useProjectSessions/, /app-recent-item/]) {
    assert.doesNotMatch(global, forbidden);
  }
});

const SRC = fileURLToPath(new URL("../..", import.meta.url));

function walk(dir: string, hit: (path: string) => void) {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) walk(path, hit);
    else hit(path);
  }
}

function sourceFiles(): string[] {
  const files: string[] = [];
  walk(SRC, (path) => {
    if (/\.(ts|tsx)$/.test(path) && !path.endsWith(".test.ts") && !path.endsWith(".test.tsx")) {
      files.push(path);
    }
  });
  return files;
}


/**
 * API 路径不是页面地址。
 *
 * 后端端点里也有 `/resources`、`/compute/inventory`、`/repository/files` 这些
 * 名字 —— 和被删掉的前端路由同名而已。它们只出现在 API 客户端与各 feature 的
 * api/ 目录里，按构造排除掉；剩下的每一个 `"/…"` 字面量都是要把人送去的地方。
 */
function isApiCallSite(file: string): boolean {
  const rel = relative(SRC, file);
  if (rel === "lib/api.ts" || rel === "pro/lib/api.ts" || /(^|\/)api\//.test(rel)) return true;
  const text = readFileSync(file, "utf8");
  // 拼后端地址的那些模块：它们要么自己带着 API 基址，要么导出的就是一个
  // `…Path()`。两者都由构造决定，不是我一个个数出来的名单。
  return text.includes("API_BASE_URL") || /export function [A-Za-z]+Path\(/.test(text);
}

/** app/ 下真实存在的路由，`[seg]` 记成通配。 */
function realRoutes(): string[][] {
  const appDir = join(SRC, "app");
  const routes: string[][] = [];
  walk(appDir, (path) => {
    if (!/[/\\]page\.tsx$/.test(path)) return;
    const segments = relative(appDir, path)
      .split(/[/\\]/)
      .slice(0, -1)
      // (workspace) 这类分组段不出现在地址里。
      .filter((seg) => !/^\(.*\)$/.test(seg))
      .map((seg) => (/^\[.*\]$/.test(seg) ? "*" : seg));
    routes.push(segments);
  });
  return routes;
}

function segmentMatches(routeSeg: string, seg: string): boolean {
  if (routeSeg === "*" || seg === "*") return true;
  if (seg === routeSeg) return true;
  // `/login${query}` 归一成 "login*" —— 查询串在编译期看不见，按前缀算数。
  if (seg.endsWith("*")) return routeSeg.startsWith(seg.slice(0, -1));
  return false;
}

function routeExists(segments: string[], routes: string[][]): boolean {
  return routes.some((route) =>
    route.length === segments.length
    && route.every((routeSeg, at) => segmentMatches(routeSeg, segments[at])));
}

/**
 * 源码里说得出的每一个页面地址，都必须真的存在。
 *
 * ## 为什么不是一张「删掉的路由」名单
 *
 * 上一版就是名单，而**名单里漏了 `/feed`** —— 我删了那条路由，却没把它写进
 * 自己的名单里。于是 `resolveDefaultLanding()` 继续 return `"/feed"`，用户打开
 * 软件第一眼是 404，597 条测试全绿（feedback_guardrails_must_scan_not_list：
 * 硬编码枚举 = 新东西默认漏过，而这次"新东西"就是我自己删的那一条）。
 *
 * 现在判据不问"它在不在名单里"，问"它指的那一页在不在盘上"。谁再删一条路由
 * 而漏了某个跳转，都会在这里红。
 */
test("源码里说得出的每一个页面地址都真的存在", () => {
  const routes = realRoutes();
  assert.ok(routes.length > 3, "没找到任何路由 —— 这条判据自己坏了");
  const offenders: string[] = [];
  for (const file of sourceFiles()) {
    if (isApiCallSite(file)) continue;
    const text = readFileSync(file, "utf8")
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/^\s*\/\/.*$/gm, "");
    // 先把插值整段记成通配，再找地址字面量。不这么做的话，
    // `/projects/${encodeURIComponent(id)}/artifacts/${…}` 里的括号会让匹配落空 ——
    // 而那正是真实的死链长的样子（它整整躲过了上一版判据）。
    const normalized = text.replace(/\$\{[^{}]*\}/g, "*");
    // 地址可以**以插值开头**：`${root}/resources` 归一后是 `*/resources`。
    // 第一版只认以 `/` 开头的字面量，于是项目设置里那两条链向已删路由的行
    // 整个漏过去了 —— 点了就是 404，而判据是绿的。
    for (const match of normalized.matchAll(/["'`]((?:\/|\*\/)[A-Za-z0-9_\-*./]*)["'`]/g)) {
      const raw = match[1];
      if (raw === "/" || raw === "*/") continue;
      // 以 `/` 结尾的是**前缀判据**（`path.startsWith(`${root}/sessions/`)`），
      // 不是要把人送去的地方。它天然对不上任何一条完整路由。
      if (raw.endsWith("/")) continue;
      // 合法的非页面地址，显式命名出来；剩下一律当成"要把人送去的地方"。
      if (raw.startsWith("/api/")) continue;   // API 基址
      if (raw === "/dev/null") continue;       // git diff 里的那个哨兵，不是地址
      const path = raw.split("?")[0].split("#")[0].replace(/\/$/, "");
      if (!path) continue;
      if (path.startsWith("*/")) {
        // 以插值开头（`${root}/resources`）：前缀在编译期看不见，但**后面那几段
        // 是字面量**，它们必须是某条真实路由的结尾。
        //
        // 不能把开头那个 `*` 当成万能通配去和整条路由对：那样 `*/resources`
        // 会匹配上 `/projects/[id]`（两段、每段都有一侧是 `*`），于是一条
        // 真死链被判成合法 —— 项目设置里那两行就是这么漏过去的。
        const tail = path.slice(2).split("/");
        const ok = routes.some((route) =>
          // 前缀至少占一段（`${root}` 展开出来的东西不可能是空的），所以
          // 严格大于。否则 `${root}/compute` 会匹配上顶层的 `/compute`。
          route.length > tail.length
          && tail.every((seg, at) => {
            const routeSeg = route[route.length - tail.length + at];
            // 尾巴上的字面量必须落在路由的**字面量**段上。允许它落在动态段上的话，
            // 任何以 `[id]` 结尾的路由都会吸收任何尾巴：`*/resources` 就是这么
            // 匹配上 `/projects/[id]` 的，一条真死链被判成合法。
            return seg === "*" ? routeSeg === "*" : routeSeg === seg;
          }));
        if (ok) continue;
        offenders.push(`${relative(SRC, file)} → ${raw}`);
        continue;
      }
      const segments = path.slice(1).split("/");
      if (segments.some((seg) => seg === "")) continue;
      if (routeExists(segments, routes)) continue;
      offenders.push(`${relative(SRC, file)} → ${raw}`);
    }
  }
  assert.deepEqual(
    offenders,
    [],
    `这些地址在 app/ 下没有对应的页面，点了就是 404：\n${offenders.join("\n")}`,
  );
});
