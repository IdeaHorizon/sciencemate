/**
 * 自更新横幅：真的挂在壳上、只在有新版本时出现、装完再重启、重启时说清窗口会自己回来。
 *
 * 扫源码而不是渲染：这几条都是"接没接上"的问题 —— 组件写了没挂、方法写了没调，
 * 渲染测试用 mock 一样绿。
 */
import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";

const strip = (s: string) => s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
const SHELL = strip(readFileSync("src/shared/layout/AppShell.tsx", "utf8"));
const BANNER = strip(readFileSync("src/features/update/UpdateBanner.tsx", "utf8"));
// 查/装/重启那三步 2026-09-21 从横幅搬进了 useUpdate —— 关于页也要走同一条路。
// 这些断言问的还是同一件事（「接没接上」），只是那段代码现在住在这里。
const HOOK = strip(readFileSync("src/features/update/useUpdate.ts", "utf8"));
const API = strip(readFileSync("src/lib/api.ts", "utf8"));

test("the banner is mounted in the app shell, inside the main column", () => {
  assert.match(SHELL, /<section className="app-main"><UpdateBanner \/>/,
    "横幅没挂在 app-main 里 —— 组件写了但没人渲染它");
});

test("nothing is drawn unless there is something to do", () => {
  assert.match(BANNER, /if \(!available && !staged && phase === "idle"\) return null;/,
    "查不到更新源 / 已是最新 时也画了东西 —— 更新是方便，不是提醒");
});

test("install happens before restart, never the other way round", () => {
  const install = HOOK.indexOf("api.installUpdate()");
  const restart = HOOK.indexOf("api.restartForUpdate()");
  assert.ok(install > 0 && restart > install, "先重启后安装，或少了一步");
});

test("the restart copy says the window comes back by itself", () => {
  assert.match(BANNER, /窗口会自动回来/, "重启时用户看到的那句话没说清楚：端口会变、壳会重新指窗口");
});

test("a shell change the shell cannot apply itself is said out loud, with a place to go", () => {
  // #953 ③：这次更新动了应用本体、装着的壳不会自己换 → 别给「现在更新」按钮让人点完发现没变。
  assert.match(BANNER, /shell_update\?\.needs_reinstall/, "横幅没看 shell_update.needs_reinstall —— 后端算了没人用");
  assert.match(BANNER, /需要重新安装/, "需要重装时没有一句人话");
  assert.match(BANNER, /href=\{status\.reinstall_url\}/, "说了要重装却不给地址 —— 等于没提示");
  const reinstall = BANNER.indexOf("needsReinstall && phase");
  const updateButton = BANNER.indexOf("void install(");
  assert.ok(reinstall > 0 && reinstall < updateButton, "「需要重装」那条分支排在「现在更新」按钮之后 —— 按钮会先命中");
  assert.match(API, /shell_update: \{ changed: boolean \| null; needs_reinstall: boolean \} \| null;/, "ApiClient 的 UpdateStatus 没带 shell_update");
  assert.match(API, /reinstall_url: string \| null;/, "ApiClient 的 UpdateStatus 没带 reinstall_url");
});

test("the client hits exactly the three update endpoints", () => {
  for (const path of ['"/update"', '"/update/install"', '"/update/restart"']) {
    assert.ok(API.includes(path), `ApiClient 没有打 ${path}`);
  }
});
