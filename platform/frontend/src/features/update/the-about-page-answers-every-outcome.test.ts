/**
 * 「关于」页：能走到、说得出版本、**每一种检查结果都有一句话**。
 *
 * 扫源码而不是渲染：这几条问的是"接没接上"和"有没有漏掉一种局面" —— 渲染测试
 * 拿一个 mock status 跑一遍，漏掉的那几种局面根本不会被触发，照样全绿。
 */
import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";
import type { UpdateStatus } from "../../lib/api.ts";
import { sayWhatTheCheckFound } from "./say-what-the-check-found.ts";

const strip = (s: string) => s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
const PANEL = strip(readFileSync("src/features/update/AboutPanel.tsx", "utf8"));
const PAGE = strip(readFileSync("src/app/(workspace)/settings/about/page.tsx", "utf8"));
const NAV = strip(readFileSync("src/features/settings/components/SettingsShell.tsx", "utf8"));
const HOOK = strip(readFileSync("src/features/update/useUpdate.ts", "utf8"));
/** 「更新」那一行说的话住在这里（纯函数，能按行为测）；面板调它。 */
const SAYS = strip(readFileSync("src/features/update/say-what-the-check-found.ts", "utf8"));

test("there is a way in: the route renders the panel and settings navigation links to it", () => {
  assert.match(PAGE, /<AboutPanel \/>/, "/settings/about 这个路由没渲染关于页");
  assert.match(NAV, /href: "\/settings\/about"/,
    "设置导航里没有「关于」—— 页面做了但没有入口，等于没做");
});

test("it says which version is installed, and where that version came from", () => {
  assert.match(PANEL, /status\.installed_version/, "没显示装着的版本 —— 这是这一页存在的理由");
  assert.match(PANEL, /status\.installed_from/, "没说这一版从哪来（自更新载荷 / 安装包自带 / 源码目录）");
});

test("checking again is a button, and it really asks the backend", () => {
  assert.match(PANEL, /onClick=\{\(\) => void check\(\)\}/, "「检查更新」没接上 check()");
  assert.match(HOOK, /api\.getUpdateStatus\(\)/, "check() 没有真去问后端");
});

/**
 * 这一条是这一页和横幅的**分水岭**。
 *
 * 横幅查不到更新源时一个字都不说（它是提醒，不该在界面上留一条红）。但人按了
 * 「检查更新」之后，静默就变成了谎：「没检查」和「检查过了、已经是最新」在屏幕上
 * 长得一模一样，而这两件事该做的完全相反。
 */
test("every outcome of a check has something to say — including the ones that failed", () => {
  for (const [needle, what] of [
    ["已是最新", "查到了、没有新版本"],
    ["有新版本", "查到了、有新版本"],
    ["已经下好", "更新下好了等重启"],
    ["需要重新安装|要重新安装", "这次改了壳、得重装"],
    ["连不上更新源", "源不可达（离线 / 地址到不了）"],
    ["没查成", "后端自己报了错"],
    ["还在发布", "更新的一版还在发布页上传着（still_publishing）"],
    ["不走自更新", "这是源码目录直接跑的"],
  ] as const) {
    assert.match(PANEL + SAYS, new RegExp(needle), `「${what}」这种结果没有对应的一句话`);
  }
  assert.match(PANEL, /查不到更新：/, "请求本身失败了（离线、后端没起）时没有一句话");
});

test("the update row really says what the check found", () => {
  assert.match(PANEL, /<small>\{sayWhatTheCheckFound\(status, phase, lastChecked, t\)\}<\/small>/,
    "「更新」那一行没接上 sayWhatTheCheckFound —— 那一页的话说给谁听");
});

/**
 * 2026-09-27：0.5.5 在发布页上一个文件一个文件地传了大半天。后端答的是「现在装得上的
 * 最新一版」，更新的那版列在 `still_publishing`。「已是最新」「有新版本 X」之后不跟一句，
 * 就把「新版就在路上」藏掉了。按行为测：喂状态，看它说什么。
 */
const zh = (p: { zh: string }) => p.zh;
const checked = (over: Partial<UpdateStatus> = {}): UpdateStatus => ({
  installed_version: "0.5.4", installed_from: "payload", self_updatable: true, available_version: null,
  staged_version: null, source: "https://h/m", reachable: true, error: null, apply_error: null,
  checked_at: "", notes: "", shell: null, shell_update: null, reinstall_url: null, still_publishing: [], ...over,
});

test("a newer release that is still uploading is said after 'up to date' and after 'available'", () => {
  assert.match(sayWhatTheCheckFound(checked({ still_publishing: ["v0.5.5"] }), "idle", null, zh),
    /^已是最新。v0\.5\.5 还在发布，文件没传完 —— 过一会儿再查。$/);
  assert.match(sayWhatTheCheckFound(checked({ installed_version: "0.5.3", available_version: "0.5.4",
                                              still_publishing: ["v0.5.5"] }), "idle", null, zh),
    /^有新版本 0\.5\.4。v0\.5\.5 还在发布/);
  assert.equal(sayWhatTheCheckFound(checked(), "idle", null, zh), "已是最新。",
    "没有在传的版本时多说了一句");
});

test("a failed update that was already staged is surfaced, not swallowed", () => {
  assert.match(PANEL, /status\.apply_error/,
    "上一次更新没装上（apply_error）时这一页不说 —— 那条信息后端算了没人用");
});

test("the busy states disable the buttons, so a second click cannot start a second install", () => {
  assert.match(PANEL, /const busy = phase === "checking" \|\| phase === "installing" \|\| phase === "restarting";/,
    "没有一个统一的「正在忙」判据");
  assert.ok((PANEL.match(/disabled=\{busy\}/g) || []).length >= 2, "按钮在忙的时候还能点");
});
