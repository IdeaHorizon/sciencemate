/**
 * 侧栏底部的版本号：两种侧栏都有、读不联网的接口、点进关于页（wangd 10-19）。
 * 扫源码：这些都是「接没接上」的问题。
 */
import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";

// JSX 注释 `{/* … */}` 要先剥：先剥 `/* … */` 会留下一对空的 `{}`。
const strip = (s: string) => s.replace(/\{\/\*[\s\S]*?\*\/\}/g, "").replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
const SHELL = strip(readFileSync("src/shared/layout/AppShell.tsx", "utf8"));
const VERSION = strip(readFileSync("src/features/update/AppVersion.tsx", "utf8"));
const API = strip(readFileSync("src/lib/api.ts", "utf8"));

test("the version sits in the sidebar after either navigation, once", () => {
  assert.match(SHELL, /<GlobalNavigation pathname=\{pathname\} \/>\}\s*<AppVersion \/>\s*<\/aside>/,
    "版本号没挂在 aside 里、两种侧栏之后 —— 项目里或全局某一边会看不到");
  assert.equal(SHELL.match(/<AppVersion \/>/g)?.length, 1, "挂了不止一处：两处会各自演化");
});

test("it reads the endpoint that does not go to the network", () => {
  assert.match(VERSION, /api\.getInstalledVersion\(\)/);
  assert.doesNotMatch(VERSION, /getUpdateStatus/, "侧栏去查更新源了 —— 离线时每次渲染都要等超时");
  assert.match(API, /"\/version"/);
  assert.match(VERSION, /staleTime: Infinity/, "一次窗口里版本不会变，不该轮询");
  assert.doesNotMatch(VERSION, /retry: false/, "关了重试：启动时一次 500 就让版本号整个窗口都不见（预览里真撞到过）");
});

test("clicking it opens About, where updates are checked", () => {
  assert.match(VERSION, /href="\/settings\/about"/);
});
