import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join, relative } from "node:path";

/**
 * 界面上每一句用户读得到的话，都必须**两种语言并排写着**。
 *
 * ## 这条判据存在的理由
 *
 * 2026-09-15 之前，「界面语言」是一个动了没反应的开关：全仓只有资讯流读
 * `useLanguage()`，其余的字是写死的英文。我第一版的处理是**把开关删掉** ——
 * wangd：「我觉得还应该保留这个按钮，只是让它真的有用才行啊。你删了按钮
 * 这不是糊弄吗」。对的：开关没用是缺陷，删掉开关是把缺陷藏起来。
 *
 * 所以判据不是"有没有这个开关"，是"切了之后界面是不是真的变了"。
 *
 * ## 两个方向，缺一边就等于没有
 *
 * 第一版只问「有中文的字符串缺不缺英文」。于是**反过来那半边它看不见**：
 * 整片执行界面、设置页的说明段落是英文硬字符串，中文界面下原样露出来 ——
 * 正是 wangd 说的「中英混杂」。判据只覆盖一个方向时，另一个方向的漏，
 * 在护栏眼里不存在。
 *
 * 所以这里是**两条**判据：
 *   1. 任何中文字符串，必须写在 `{ zh, en }` 里；
 *   2. 界面上任何一句英文（JSX 文本节点、以及 title/hint/label 这类展示属性），
 *      也必须写在 `{ zh, en }` 里。
 *
 * 漏翻因此在语法上就写不出来：`Phrase` 两个字段都是必填。
 */

const SRC = fileURLToPath(new URL("../..", import.meta.url));
const HAN = /[一-鿿]/;

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) walk(path, out);
    else if (/\.tsx?$/.test(path) && !path.includes(".test.")) out.push(path);
  }
  return out;
}

/** 注释里的中文是写给读代码的人的，不是界面上的字。 */
function withoutComments(raw: string): string {
  return raw
    .replace(/\/\*[\s\S]*?\*\//g, "")
    // 行尾注释也要剥：`if (!ready) return null; // 调用方据此显示"加载中"`
    // 里那句是写给读代码的人的。只剥整行注释会把它当成界面文案。
    .replace(/(^|[^:"'`\\])\/\/.*$/gm, "$1");
}

/** 把所有 `{ zh: "…", en: "…" }` 里的中文收集起来 —— 它们是**已经**双语的。 */
function translatedPhrases(source: string): Set<string> {
  const done = new Set<string>();
  for (const match of source.matchAll(/zh:\s*(["'`])((?:\\.|(?!\1).)*)\1/g)) {
    // 原样和去掉首尾空白各存一份：报到的那一头是 trim 过的，而词条里可能写着
    // `" · 已冻结"`（前面那个空格是排版的一部分）。只存原样的话，这类会被
    // 判成"只有中文"——判据自己造出一批假阳性。
    done.add(match[2]);
    done.add(match[2].trim());
  }
  return done;
}

/**
 * 豁免：**逐条写明理由**，默认一律不豁免。
 *
 * 这不是"名单式护栏"（那种是把要扫的目标写成名单，新东西默认漏过）——
 * 这里扫的是全盘，名单只记"这一条为什么不算界面文案"。加一条要写清理由，
 * 加不出理由的就该改成双语。
 */
const EXEMPT: readonly { file: string; text: string; why: string }[] = [
  {
    file: "features/execution/lib/event-audience.ts",
    text: "事件类型",
    why: "fail-loud 的开发者报错：新事件类型没声明受众时抛给写代码的人看，永远不进界面",
  },
  {
    file: "app/layout.tsx",
    text: "Agent-based Scientific Research Platform",
    why: "Next 的静态 metadata（服务端导出，取不到 hook）；它是页面描述，不是界面上的字",
  },
  {
    file: "features/auth/AuthProvider.tsx",
    text: "Authentication session ended",
    why: "AbortError 的 reason，给日志和调试看；用户界面上不出现",
  },
  {
    file: "features/auth/AuthProvider.tsx",
    text: "useAuth must be used within AuthProvider",
    why: "用错 hook 时抛给写代码的人看的报错",
  },
  {
    file: "features/settings/InterfaceSettingsProvider.tsx",
    text: "useInterfaceSettings must be used within InterfaceSettingsProvider",
    why: "同上：用错 hook 时抛给写代码的人看的报错",
  },
  {
    file: "features/feed/components/FeedItemCard.tsx",
    text: "button, a, input, select, textarea",
    why: "CSS 选择器，不是一句话",
  },
  {
    file: "features/feed/components/FeedMasonry.tsx",
    text: "(max-width: 1100px)",
    why: "CSS 媒体查询串（matchMedia 的判据），不是界面上的字；它必须和样式表里的断点逐字一致，翻译它等于让响应式列数和 CSS 断点对不上",
  },
  {
    file: "features/feed/components/FeedMasonry.tsx",
    text: "(max-width: 700px)",
    why: "同上：CSS 媒体查询串，与样式表断点逐字对应",
  },
];

function isExemptFile(rel: string): boolean {
  // i18n 自己（词表、判据）
  return rel.startsWith("shared/i18n/");
}

function isExempt(rel: string, text: string): boolean {
  return EXEMPT.some((entry) => rel === entry.file && text.includes(entry.text));
}

test("界面上每一句中文都必须配着英文一起写", () => {
  const offenders: string[] = [];
  for (const file of walk(SRC)) {
    const rel = relative(SRC, file);
    if (isExemptFile(rel)) continue;
    const source = withoutComments(readFileSync(file, "utf8"));
    const done = translatedPhrases(source);
    const seen = new Set<string>();
    const report = (text: string) => {
      const value = text.trim();
      if (!value || !HAN.test(value) || done.has(value) || seen.has(value)) return;
      if (isExempt(rel, value)) return;
      seen.add(value);
      offenders.push(`${rel} → ${value}`);
    };
    // JSX 文本节点：`>这句话<`
    for (const m of source.matchAll(/>\s*([^<>{}\n][^<>{}]*)\s*</g)) report(m[1]);
    /**
     * 任何**中文字符串字面量**。
     *
     * 前两版扫的是写法：先是 JSX 属性，然后补上对象属性 —— 每补一种，就又有
     * 一种没想到的写法漏过去（`{busy ? "新建中…" : "新建会话"}` 是 JSX 表达式
     * 里的字面量，`running: "正在跑"` 是另一个键名的表）。护栏扫写法，就永远
     * 落后于人怎么写。
     *
     * 所以判据落在**这件事本身**：源码里出现了中文，它要么在 `zh:` 里
     * （已经配好英文），要么就是一句切不动的话。合法的那条路只有一条，剩下
     * 一律违规。
     */
    // `keywords:` 是搜索索引，不是界面上的字 —— 中英混着写正是它该有的样子
    // （中文用户搜 "模型"、英文用户搜 "model"，都要能命中同一项）。
    const searchIndex = new Set(
      [...source.matchAll(/keywords\s*:\s*(["'`])((?:\\.|(?!\1).)*)\1/g)].map((m) => m[2]),
    );
    for (const m of source.matchAll(/(["'`])((?:\\.|(?!\1)[\s\S])*)\1/g)) {
      const text = m[2];
      if (!HAN.test(text) || searchIndex.has(text)) continue;
      // 一个本身套着 `t({ zh: … })` 的串，是几段**已经翻好**的东西拼起来的
      // （模板串里嵌了短语调用）。正则解析不了嵌套模板，所以这里按内容判：
      // 它含有短语调用，就不是一句漏翻的话。
      if (text.includes("zh:")) continue;
      report(text);
    }
  }
  assert.deepEqual(
    offenders,
    [],
    `这些字只有中文，切成英文界面时会原样露出来（${offenders.length} 处）：\n`
      + offenders.slice(0, 40).join("\n"),
  );
});

/**
 * 第三条：一个 `Phrase` 的 `en` 侧不许写中文。
 *
 * 上面两条判据合起来还留着一个缝：`{ zh: "协作", en: "协作" }` —— 它**在**一个
 * Phrase 里（第一条放行），它**不是**英文（第二条不看中文）。于是自动补全
 * 那一轮把中文原样填进 en 侧的五处，两条判据都是绿的，而英文界面上那几个
 * chip 一直写着中文。2026-09-16 在浏览器里切成英文才看见。
 *
 * 判据机械可判：en 侧出现汉字即违规。反过来（zh 侧写英文）不禁止 —— 专名、
 * 期刊名、"PDF" 这类词在中文里本来就这么写。
 */
test("Phrase 的英文那一侧不许写中文", () => {
  const offenders: string[] = [];
  for (const file of walk(SRC)) {
    const rel = relative(SRC, file);
    if (isExemptFile(rel)) continue;
    const source = withoutComments(readFileSync(file, "utf8"));
    for (const m of source.matchAll(/en:\s*(["'`])((?:\\.|(?!\1)[\s\S])*)\1/g)) {
      if (!HAN.test(m[2])) continue;
      offenders.push(`${rel} → en: ${m[2]}`);
    }
  }
  assert.deepEqual(
    offenders,
    [],
    `这些词条的英文侧写着中文，英文界面上会原样露出来（${offenders.length} 处）：\n`
      + offenders.slice(0, 40).join("\n"),
  );
});

/**
 * 反方向：界面上的英文也得配着中文。
 *
 * 第一版只扫 JSX 文本节点和 title/hint/label 这类展示属性 —— 于是
 * `{submitting ? "Signing in…" : "Sign in"}` 这种**表达式容器里的字面量**
 * 整类漏过（登录按钮因此在中文界面下一直写着 "Sign in"）。又一次"扫写法"。
 *
 * 所以这一条也落在字面量本身：一个 .tsx 里像**一句话**的英文（两个以上英文
 * 单词、中间有空格），要么已经在某个 `{ zh, en }` 里，要么就是一句切不动的话。
 * 不像句子的（class 名、事件名、单个词的标识符）不在这条判据的射程内 ——
 * 英文和代码在源码里长得太像，把它们全算上只会得到一堆假阳性，而假阳性会
 * 让人把整条判据关掉。
 */
const ENGLISH_WORD = /[A-Za-z][A-Za-z'’]+/g;
/** 这些不是文案：编译指令、路径、CSS class 名串、以及被正则夹到的代码片段。 */
const DIRECTIVES = new Set(["use client", "use strict", "use server"]);
const LOOKS_LIKE_PATH = /^(https?:|\/|\.\/|\.\.\/)/;
const LOOKS_LIKE_CODE = /[{}<>]|\bt\(/;
const CLASS_NAME_LIST = /^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*(?: [a-z][a-z0-9]*(?:[-_][a-z0-9]+)*)*$/;

/** 两个以上英文单词、并且中间有空格，才算"一句话"（`name@institution.edu` 不算）。 */
function looksLikeASentence(value: string): boolean {
  if (!value.includes(" ")) return false;
  return (value.match(ENGLISH_WORD) ?? []).length >= 2;
}

/** `{ zh: "…", en: "…" }` 两侧的值 —— 它们都**已经**在一个 Phrase 里了。 */
function phraseValues(source: string): Set<string> {
  const done = new Set<string>();
  for (const match of source.matchAll(/(?:zh|en):\s*(["'`])((?:\\.|(?!\1)[\s\S])*)\1/g)) {
    done.add(match[2]);
    done.add(match[2].trim());
  }
  return done;
}

test("界面上每一句英文也必须配着中文一起写", () => {
  const offenders: string[] = [];
  for (const file of walk(SRC)) {
    const rel = relative(SRC, file);
    if (isExemptFile(rel) || !file.endsWith(".tsx")) continue;
    const source = withoutComments(readFileSync(file, "utf8"));
    const done = phraseValues(source);
    const seen = new Set<string>();
    for (const m of source.matchAll(/(["'])((?:\\.|(?!\1)[^\n])*)\1/g)) {
      const value = m[2].trim();
      if (!value || seen.has(value) || done.has(value)) continue;
      if (HAN.test(value)) continue;
      if (!looksLikeASentence(value)) continue;
      if (DIRECTIVES.has(value) || LOOKS_LIKE_PATH.test(value) || LOOKS_LIKE_CODE.test(value)) continue;
      if (CLASS_NAME_LIST.test(value)) continue;
      // 一个 Phrase 的值可以是几段拼起来的（`en: "…" + "…"`）。前面那个 `+`
      // 就是"我是上一段的续"的标记 —— 它自己不是一句独立的漏翻。
      if (/\+\s*$/.test(source.slice(0, m.index).trimEnd() + "")) continue;
      if (isExempt(rel, value)) continue;
      seen.add(value);
      offenders.push(`${rel} → ${value}`);
    }
  }
  assert.deepEqual(
    offenders,
    [],
    `这些字只有英文，切成中文界面时会原样露出来（${offenders.length} 处）：\n`
      + offenders.slice(0, 40).join("\n"),
  );
});

/**
 * 第四条：同一个文件里声明成 `Phrase` 的字段，不许被拼进字符串。
 *
 * 前三条守的是"两种语言都写了、写对了"。它们守不住**忘了翻**：
 * `` `${provider.label} · ${model}` `` 里 `label` 是个 Phrase，模板字符串把它
 * 变成字面量 `[object Object]`。
 *
 * 2026-09-16 实测的代价不是"界面上难看一次"：那一串是首次运行向导拼出来的
 * **模型连接显示名，被存进了数据库**，此后资讯页的"挖掘模型"、会话输入框的
 * 模型徽标上一直挂着 `[object Object] · fake-model`。一次忘记 `t()`，脏数据
 * 就留在库里，改完代码也不会自己好。
 *
 * ## 判据为什么限定在"同一个文件里声明的"
 *
 * 第一版写成"凡 `${…label}` / `${…recovery}` 一律违规"，扫出 12 处 ——
 * **12 处全是误报**：`recovery` / `title` 那些是后端已经翻好、送过来的字符串。
 * 一道 13 次里错 12 次的闸，结局是被关掉或被绕过，比没有更糟。
 *
 * 所以判据只问它**静态答得出**的那件事：这个文件自己把某个字段声明成了
 * `{ zh: …, en: … }`，同一个文件又把它插进模板字符串 —— 那就一定是
 * `[object Object]`。声明在别处的（后端来的、别的模块导出的）这道闸不猜。
 */
test("同一个文件里声明成 Phrase 的字段，不许拼进字符串", () => {
  const offenders: string[] = [];
  for (const file of walk(SRC)) {
    const rel = relative(SRC, file);
    if (isExemptFile(rel)) continue;
    const source = withoutComments(readFileSync(file, "utf8"));
    // 这个文件把哪些字段名声明成了 Phrase 字面量：`label: { zh: "…", en: "…" }`
    const phraseFields = new Set<string>();
    for (const m of source.matchAll(/(\w+)\s*:\s*\{\s*zh\s*:\s*["'`][\s\S]*?en\s*:\s*["'`]/g)) {
      phraseFields.add(m[1]);
    }
    if (phraseFields.size === 0) continue;
    for (const m of source.matchAll(/\$\{\s*([A-Za-z_$][\w$.]*\.(\w+))\s*\}/g)) {
      if (phraseFields.has(m[2])) offenders.push(`${rel} → \${${m[1]}}`);
    }
  }
  assert.deepEqual(
    offenders,
    [],
    "这些地方把本文件里声明成 Phrase 的字段直接拼进了字符串，拼出来是 [object Object]"
      + `（${offenders.length} 处）。套一层 t(…) 再拼：\n`
      + offenders.join("\n"),
  );
});
