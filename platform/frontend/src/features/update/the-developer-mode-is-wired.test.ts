/**
 * 开发者模式（桌面版从自己的源码目录跑）：关于页上有入口、三个口都接到后端、每种局面有一句话、
 * 重启走更新那条同一个 restart。
 *
 * 接线的部分扫源码（组件写了没挂、方法写了没调，渲染测试拿 mock 一样绿）；
 * 「现在」那一行说什么按行为测（纯函数，喂状态看它说什么）。
 */
import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";
import type { DeveloperSource } from "../../lib/api.ts";
import { sayWhereItRuns } from "./say-where-it-runs.ts";

const strip = (s: string) => s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
const ABOUT = strip(readFileSync("src/features/update/AboutPanel.tsx", "utf8"));
const PANEL = strip(readFileSync("src/features/update/DeveloperSourcePanel.tsx", "utf8"));
const API = strip(readFileSync("src/lib/api.ts", "utf8"));
const SAYS = strip(readFileSync("src/features/update/say-where-it-runs.ts", "utf8"));

test("the about page mounts the developer-mode section", () => {
  assert.match(ABOUT, /<DeveloperSourcePanel phase=\{phase\} restart=\{restart\} \/>/,
    "关于页没挂开发者模式 —— 组件写了但没人渲染它，或者没把同一个 restart 传下去");
});

test("the three doors really reach the backend, with the right verbs", () => {
  assert.match(API, /async getDeveloperSource\(\) \{\s*return this\.request<DeveloperSource>\("\/update\/developer"\)/);
  assert.match(API, /async setDeveloperSource\(sourceRoot: string\)[\s\S]*?"\/update\/developer", \{\s*method: "PUT",\s*body: JSON\.stringify\(\{ source_root: sourceRoot \}\)/,
    "写声明不是 PUT /update/developer + source_root");
  assert.match(API, /async clearDeveloperSource\(\)[\s\S]*?"\/update\/developer", \{ method: "DELETE" \}/);
  assert.match(PANEL, /api\.getDeveloperSource\(\)/, "面板没真去问后端");
  assert.match(PANEL, /api\.setDeveloperSource\(path\.trim\(\)\)/, "「使用这个目录」没接上后端");
  assert.match(PANEL, /api\.clearDeveloperSource\(\)/, "「停用」没接上后端");
});

test("restart is the one from useUpdate, not a second implementation", () => {
  assert.match(PANEL, /onClick=\{\(\) => void restart\(\)\}/, "「现在重启」没接上传下来的 restart");
  assert.doesNotMatch(PANEL, /api\.restartForUpdate/, "面板自己又实现了一遍重启 —— 「即将重启」那句话会分叉");
});

test("an organisation server draws nothing instead of a section that always errors", () => {
  assert.match(PANEL, /error\.status === 403\) \{ setUnavailable\(true\); return; \}/);
  assert.match(PANEL, /if \(unavailable\) return null;/);
});

test("saving an invalid directory shows the backend's sentence, not a generic one", () => {
  assert.match(PANEL, /setProblem\(error instanceof Error \? error\.message : String\(error\)\)/,
    "后端已按这一屏的语言说了为什么用不了，面板得照显示");
  assert.match(PANEL, /<p className="settings-inline-error" role="alert">\{problem\}<\/p>/);
});

const zh = (p: { zh: string }) => p.zh;
const en = (p: { en: string }) => p.en;
const told = (over: Partial<DeveloperSource> = {}): DeveloperSource => ({
  declared: null, active: false, restart_needed: false, problem: null, harness: null, backend_app: null,
  static_ui: null, git: null, running_from: null, ...over,
});

test("the 'now' row really says where it runs", () => {
  assert.match(PANEL, /: sayWhereItRuns\(told, t\)\}<\/small>/, "「现在」那一行没接上 sayWhereItRuns");
  assert.match(SAYS, /export function sayWhereItRuns\(/);
});

test("every situation has its own sentence", () => {
  assert.equal(sayWhereItRuns(null, zh), "正在读…");
  assert.equal(sayWhereItRuns(told(), zh), "没有开启。跑的是安装包或自更新装上的那份。");
  assert.equal(sayWhereItRuns(told({ declared: "/src/hf", restart_needed: true }), zh),
    "已指向 /src/hf，重启后生效 —— 现在跑的还是安装的版本。");
  assert.equal(sayWhereItRuns(told({ declared: "/src/hf", active: true, git: { branch: "main", commit: "abc1234" } }), zh),
    "正在从源码目录 /src/hf (main @ abc1234) 跑。");
  assert.equal(sayWhereItRuns(told({ declared: "/src/hf", active: true }), zh), "正在从源码目录 /src/hf 跑。");
  assert.equal(sayWhereItRuns(told({ declared: "/gone", problem: "目录不存在：/gone", restart_needed: false }), zh),
    "指向 /gone，但这个目录用不了：目录不存在：/gone 跑的是安装的版本。");
  assert.equal(sayWhereItRuns(told({ restart_needed: true, running_from: "/src/hf" }), zh),
    "已停用，重启后回到安装的版本 —— 这个进程还在从 /src/hf 跑。");
  assert.equal(sayWhereItRuns(told({ declared: "/src/hf", restart_needed: true }), en),
    "Set to /src/hf; it takes effect after a restart — the installed version is still running.");
});

test("'set but not restarted' is never shown as 'running from source'", () => {
  // 这一条是这一段存在的理由：看起来开着、跑的还是安装的那份，是最容易骗人的局面。
  const said = sayWhereItRuns(told({ declared: "/src/hf", active: false, restart_needed: true }), zh);
  assert.doesNotMatch(said, /正在从源码目录/);
  assert.match(said, /重启后生效/);
});
