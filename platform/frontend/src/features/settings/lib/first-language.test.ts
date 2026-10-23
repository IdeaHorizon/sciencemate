import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { languageForASystem } from "../../../shared/i18n/language.ts";
import { languageToSettleOnFirstOpen } from "./first-language.ts";

/**
 * 第一次打开时的界面语言（wangd 2026-10-19）：系统语言列表里有中文就中文，否则英文；
 * 选过之后只认他选的。以及：换语言的钮用**目标语言**写着，第一屏、侧栏、登录页都有。
 */

test("系统语言列表里有中文就中文 —— 不只看第一项", () => {
  // 2026-10-19 这台 Mac：英文系统、中文排第二。只看第一项会把它判成英文。
  assert.equal(languageForASystem(["en-GB", "zh-Hans-CN"]), "zh");
  assert.equal(languageForASystem(["zh-CN"]), "zh");
  assert.equal(languageForASystem(["zh_TW"]), "zh");
  assert.equal(languageForASystem(["ZH-cn"]), "zh");
});

test("中英之外的语言、以及什么都不知道的时候，给英文", () => {
  assert.equal(languageForASystem(["de-DE", "fr-FR"]), "en");
  assert.equal(languageForASystem(["ja-JP"]), "en");
  assert.equal(languageForASystem([]), "en");
  assert.equal(languageForASystem(["zha"]), "en", "壮语（zha）不是中文");
});

test("选过的就不再猜", () => {
  assert.equal(languageToSettleOnFirstOpen({ chosen: true, system_languages: ["de-DE"], chosen_at_install: "zh" }, null, ["de"]), null);
});

test("没选过：登录页上点过的 > 后端读到的系统语言 > 浏览器给的", () => {
  const fresh = (system_languages: string[], chosen_at_install: "zh" | "en" | null = null) =>
    ({ chosen: false, system_languages, chosen_at_install });
  // 在安装器上点过的，排在系统语言前面；登录页上点过的又排在它前面。
  assert.equal(languageToSettleOnFirstOpen(fresh(["en-GB", "zh-Hans-CN"], "en"), null, ["en-GB"]), "en");
  assert.equal(languageToSettleOnFirstOpen(fresh(["de-DE"], "zh"), null, ["de-DE"]), "zh");
  assert.equal(languageToSettleOnFirstOpen(fresh(["de-DE"], "zh"), "en", ["de-DE"]), "en");
  assert.equal(languageToSettleOnFirstOpen(fresh(["zh-Hans-CN"]), "en", ["zh-CN"]), "en");
  // 桌面版：WKWebView 只给 en-GB，后端读到整张表里有中文。
  assert.equal(languageToSettleOnFirstOpen(fresh(["en-GB", "zh-Hans-CN"]), null, ["en-GB"]), "zh");
  // 组织服务器：后端不在他电脑上（空列表），用浏览器的。
  assert.equal(languageToSettleOnFirstOpen(fresh([]), null, ["de-DE"]), "en");
  assert.equal(languageToSettleOnFirstOpen(fresh([]), null, ["zh-CN", "en"]), "zh");
});

const strip = (s: string) => s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
const read = (path: string) => strip(readFileSync(new URL(path, import.meta.url), "utf8"));

test("换语言的钮写的是目标语言自己的名字，不是当前语言里的一个词", () => {
  const SWITCH = read("../components/LanguageSwitch.tsx");
  assert.match(SWITCH, /LANGUAGE_LABELS\[other\]/, "侧栏 / 登录页那个钮没写目标语言的名字");
  assert.match(SWITCH, /LANGUAGE_LABELS\[language\]/, "开场那一排没写每种语言自己的名字");
});

test("第一屏、侧栏、登录页、注册页都摆着它", () => {
  assert.match(read("../../onboarding/FirstRun.tsx"), /<LanguageSwitch variant="both"/, "开场第一张卡片上没有语言");
  const SHELL = read("../../../shared/layout/AppShell.tsx");
  const bottom = SHELL.slice(SHELL.indexOf('className="app-sidebar-bottom"'));
  assert.match(bottom.slice(0, 600), /<LanguageSwitch\b/, "侧栏底部没有常驻的语言钮");
  // 登录、注册只在专业版里有：公开树（个人版导出）删掉了这两个目录（2026-10-19 public-tree job 撞出）。
  for (const page of ["../../../app/login/page.tsx", "../../../app/register/page.tsx"]) {
    if (!existsSync(new URL(page, import.meta.url))) continue;
    assert.match(read(page), /<LanguageSwitch\b/, `${page} 上没有语言钮`);
  }
});

test("第一次打开真的去问了，并且把定下的存进账号", () => {
  const PROVIDER = read("../InterfaceSettingsProvider.tsx");
  const refresh = PROVIDER.slice(PROVIDER.indexOf("const refresh = useCallback"));
  assert.match(refresh, /api\.getInterfaceLanguageHint\(\)/, "没问后端选过没有");
  assert.match(refresh, /languageToSettleOnFirstOpen\(/, "没走那条判据");
  assert.match(refresh, /api\.saveInterfaceSettings\(\{ \.\.\.current, language \}\)/,
    "定下了却没存进账号 —— 后端的文案还按默认语言出");
});
