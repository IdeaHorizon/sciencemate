import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join, relative } from "node:path";
import ts from "typescript";

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

/**
 * 把字符串、模板字符串的文字部分、正则字面量都换成空白（换行留着），剩下代码与 JSX 文本。
 *
 * 不用一条正则剥：模板字符串会嵌套（`` `${a ? t({ zh: `…` }) : …}` ``），正则在
 * 里层那个反引号处就以为外层结束了，把后半截的中文当成 JSX 文本报出来；正则字面量
 * 里的引号（`/filename="?([^";]+)/`）又会让引号错配。所以按字符走一遍：模板的
 * `${…}` 里面是代码，按花括号深度回到模板；`/` 前面是运算符或开括号时才是正则。
 */
function outsideLiterals(source: string): string {
  const out: string[] = [];
  const templateDepths: number[] = [];
  let depth = 0;
  let prev = "";
  const blank = (from: number, to: number) => out.push(source.slice(from, to).replace(/[^\n]/g, " "));
  const templateText = (start: number): number => {
    let j = start;
    while (j < source.length) {
      if (source[j] === "\\") { j += 2; continue; }
      if (source[j] === "`") { blank(start, j + 1); return j + 1; }
      if (source[j] === "$" && source[j + 1] === "{") {
        blank(start, j + 2);
        templateDepths.push(depth);
        depth += 1;
        return j + 2;
      }
      j += 1;
    }
    blank(start, j);
    return j;
  };
  let i = 0;
  while (i < source.length) {
    const ch = source[i];
    if (ch === '"' || ch === "'") {
      let j = i + 1;
      while (j < source.length && source[j] !== ch && source[j] !== "\n") j += source[j] === "\\" ? 2 : 1;
      blank(i, j + 1);
      i = j + 1;
      prev = ch;
      continue;
    }
    if (ch === "`") { i = templateText(i + 1); prev = "`"; continue; }
    const regexCanStart = prev === "" || "(,=:[!&|?{};".includes(prev)
      || /\breturn\s*$/.test(source.slice(Math.max(0, i - 8), i));
    if (ch === "/" && source[i + 1] !== "/" && source[i + 1] !== "*" && regexCanStart) {
      let j = i + 1;
      let inClass = false;
      while (j < source.length && source[j] !== "\n") {
        if (source[j] === "\\") { j += 2; continue; }
        if (source[j] === "[") inClass = true;
        else if (source[j] === "]") inClass = false;
        else if (source[j] === "/" && !inClass) break;
        j += 1;
      }
      blank(i, j + 1);
      i = j + 1;
      prev = "/";
      continue;
    }
    if (ch === "{") depth += 1;
    if (ch === "}") {
      depth -= 1;
      if (templateDepths.length > 0 && depth === templateDepths[templateDepths.length - 1]) {
        templateDepths.pop();
        out.push(" ");
        i = templateText(i + 1);
        prev = "`";
        continue;
      }
    }
    out.push(ch);
    if (!/\s/.test(ch)) prev = ch;
    i += 1;
  }
  return out.join("");
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
    file: "features/feed/components/FeedHome.tsx",
    text: "button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex=",
    why: "焦点循环查询可聚焦元素的 CSS 选择器，不是用户文案",
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
  {
    file: "features/execution/lib/run-event-registry.ts",
    text: "Run event registry stopped",
    why: "和 AuthProvider 那条同一件事：停掉所有跟流时 AbortError 的 reason，给日志和调试看；被中止的读取不会报给人",
  },
  {
    file: "features/feed/lib/handoff.ts",
    text: "et al.",
    why: "西文作者列表的省略写法 —— 中文正文引西文文献也写 et al.（GB/T 7714）；资讯卡上的作者行（feed-presentation 的 authorLine）同一个口径",
  },
  {
    file: "features/feed/lib/feed-presentation.ts",
    text: "et al.",
    why: "同上：资讯卡作者行超过三个折成 et al.",
  },
  {
    file: "features/execution/lib/execution-read-client.ts",
    text: "Execution read failed with status",
    why: "ExecutionReadApiError 在后端没给 message 时记下的状态码；界面按 status 说一句（SessionExecutionView 的 describeApiProblem），不照念 message",
  },
  {
    file: "features/execution/lib/run-event-stream.ts",
    text: "Run event stream failed with status",
    why: "同上：跟流那条路上的 ExecutionReadApiError；跟流出错时界面只说一句固定的「读不到执行细节」",
  },
  {
    file: "features/execution/lib/execution-read-client.ts",
    text: "Run event stream was interrupted by a server shutdown",
    why: "ExecutionStreamInterruptedError 的 message；界面按类说「执行服务正在重启，正从最后一条确认过的事件接上」（describeApiProblem），不照念",
  },
  {
    file: "features/sessions/components/SessionWorkspace.tsx",
    text: "Session file change",
    why: "发布时的 git 提交说明：写进项目仓库的历史，和后端自己写的提交说明（project_repository.py 的 \"Take project version of …\" 等）同一种语言；不是界面上的字",
  },
  {
    file: "lib/api.ts",
    text: "Stream failed:",
    why: "和 presentChatFailure 之间的约定：它按「stream failed: 503」这个形状认出状态码（chat-error-presentation.ts 的 statusMatch），认出来按界面语言说，认不出不显示",
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
     * 字符串字面量**之外**的中文。
     *
     * 上面那条要求文本两头都是标签，于是 `<strong>只读 · {scope}</strong>` 这种
     * 紧挨着表达式的 JSX 文本两头不全是标签、它又不是一个带引号的字面量 ——
     * 两条判据都看不见（2026-10-19，英文界面的通知设置页上一直写着「只读」）。
     * 又一次扫写法。
     *
     * 落在这件事本身：注释已经剥掉了，再把字符串字面量剥掉，源码里**还剩下**
     * 的中文只能是 JSX 文本 —— 而 JSX 文本不可能在一个 Phrase 里。
     */
    for (const m of outsideLiterals(source).matchAll(/[^<>{}\n]*[一-鿿][^<>{}\n]*/g)) report(m[0]);
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
 * 所以这一条也落在字面量本身：一个像**一句话**的英文（两个以上英文
 * 单词、中间有空格），要么已经在某个 `{ zh, en }` 里，要么就是一句切不动的话。
 * 不像句子的（class 名、事件名、单个词的标识符）不在这条判据的射程内 ——
 * 英文和代码在源码里长得太像，把它们全算上只会得到一堆假阳性，而假阳性会
 * 让人把整条判据关掉。
 *
 * ## .ts 也扫（2026-10-20）
 *
 * 这条原来只扫 .tsx，于是 hook 和 lib 里 return 出去、push 出去的英文整片看不见：
 * 聊天页的 toast、聊天失败那五种说法、会话 mutation 失败时的兜底、阻塞类别、
 * 演示模型的名字……中文界面下原样露出来，闸一直是绿的。放开到 .ts 当天扫出
 * 161 处。.ts 里的英文比 .tsx 多出三类**不是界面文案**的东西，各按结构排除，
 * 不逐条登记：
 *
 *   1. 样例数据（`fixtures/` 目录）：`?mode=demo` 的界面演示和测试用的样例研究
 *      —— 会话标题、消息、待答的问题。它们是**内容**，和真会话里研究者自己起的
 *      标题一样，写成什么语言就是什么语言。
 *   2. 对面违约的诊断：继承 `ContractViolation`（shared/contract-violation.ts）的
 *      错误的 message，以及同一个文件里被拼进这种 message 的那个参数
 *      （`record(payload, "ChangeSet response")`）。**不是** "throw 出来的都放行"：
 *      界面有六十来处 `pushError(error.message)` 照念 message，裸 `new Error(…)`
 *      里那句 "Enter a new API key to save this backend." 就这样弹到了中文界面上。
 *      「这句是诊断」要在类的声明处说一次，裸 Error 仍要两种语言都写。
 *   3. 拿来比对的输入：`=== "Not Found"`、`.startsWith("diff --git ")`、`case "…":`
 *      —— 它们要和对面送来的字节逐字相等，翻译它就再也对不上了。
 */

/** 样例数据所在的目录 —— 见上面第 1 条。 */
function isSampleData(rel: string): boolean {
  return rel.split(/[\\/]/).includes("fixtures");
}

/** src 里所有（直接或间接）继承 `ContractViolation` 的类名，含它自己。 */
function contractViolationClasses(files: readonly string[]): Set<string> {
  const parentOf = new Map<string, string>();
  for (const file of files) {
    for (const m of readFileSync(file, "utf8").matchAll(/\bclass\s+(\w+)\s+extends\s+(\w+)/g)) {
      parentOf.set(m[1], m[2]);
    }
  }
  const found = new Set(["ContractViolation"]);
  for (let grew = true; grew;) {
    grew = false;
    for (const [child, parent] of parentOf) {
      if (found.has(parent) && !found.has(child)) {
        found.add(child);
        grew = true;
      }
    }
  }
  return found;
}

/**
 * 这个文件里哪些函数把自己的某个参数拼进了违约的 message：change-set-adapter 的
 * `record(value, path)` 抛的是 `new SessionRevisionContractError(模板串)`，模板串里
 * 嵌着 `${path}`。调用它时传进去的字面量是诊断的主语，不是一句话。只认 `function` 声明。
 */
function diagnosisHelpers(source: string, violations: ReadonlySet<string>): Set<string> {
  const helpers = new Set<string>();
  for (const m of source.matchAll(/\bfunction\s+(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)[^{;]*\{/g)) {
    let depth = 0;
    let end = m.index + m[0].length - 1;
    for (; end < source.length; end += 1) {
      if (source[end] === "{") depth += 1;
      else if (source[end] === "}" && --depth === 0) break;
    }
    const body = source.slice(m.index + m[0].length, end);
    const params = [...m[2].matchAll(/(?:^|,)\s*(\w+)\??\s*:/g)].map((p) => p[1]);
    const raises = params.some((param) => [...body.matchAll(
      new RegExp(`\\bnew\\s+(\\w+)\\(\\s*\`[^\`]*\\$\\{\\s*${param}\\s*\\}`, "g"),
    )].some((raise) => violations.has(raise[1])));
    if (raises) helpers.add(m[1]);
  }
  return helpers;
}

/** 这个字面量是不是一条违约诊断（或它的主语）—— 见上面第 2 条。 */
function isDiagnosis(before: string, violations: ReadonlySet<string>, helpers: ReadonlySet<string>): boolean {
  // 在构造参数里（`new X("…")`、`new X(detail ?? "…")`），中间没有别的调用套着。
  const raised = before.match(/\bnew\s+(\w+)\([^()]*$/);
  if (raised && violations.has(raised[1])) return true;
  const passed = before.match(/\b(\w+)\([^()]*$/);
  return Boolean(passed && helpers.has(passed[1]));
}

/** 这个字面量是不是拿来比对的输入 —— 见上面第 3 条。 */
function isMatchInput(before: string, after: string): boolean {
  return /(?:[!=]==?)\s*$/.test(before)
    || /^\s*(?:[!=]==?)/.test(after)
    || /\.(?:startsWith|endsWith|includes)\(\s*$/.test(before)
    || /\bcase\s+$/.test(before);
}
const ENGLISH_WORD = /[A-Za-z][A-Za-z'’]+/g;
/** 这些不是文案：编译指令、路径、CSS class 名串、以及被正则夹到的代码片段。 */
const DIRECTIVES = new Set(["use client", "use strict", "use server"]);
const LOOKS_LIKE_PATH = /^(https?:|\/|\.\/|\.\.\/)/;
const LOOKS_LIKE_CODE = /[{}<>]|\bt\(/;
const CLASS_NAME_LIST = /^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*(?: [a-z][a-z0-9]*(?:[-_][a-z0-9]+)*)*$/;

/**
 * CSS class 名串：全是 kebab / snake 记号，**而且至少一个带连字符**。
 *
 * 原来只要求「全小写、空格隔开」—— 于是全小写的一句话（"utilization unknown"、
 * "unknown file"、JSX 里的 `day longest streak`）也被当成 class 名串放走了
 * （2026-10-20 扫模板和 JSX 文本时撞出来）。真正的 class 名串在这个仓库里都带
 * 连字符；不带的（`className="muted small"`）按位置认 —— 见下一个。
 */
function looksLikeClassNames(value: string): boolean {
  return CLASS_NAME_LIST.test(value) && /[-_]/.test(value);
}

/** 字面量是 `className` / `rel` 的值 —— 两个都是记号列表，不是一句话。 */
const IN_A_TOKEN_ATTRIBUTE = /\b(?:className|rel)=(?:\{[^}]*)?$/;
const TOKEN_ATTRIBUTES = new Set(["className", "rel"]);
/** 字面量是一个 CSS 值：`el.style.colorScheme = … ? "light dark" : …`（JSX 的 `style={{…}}` 在下一条里按属性认）。 */
const IN_A_STYLE = /\.style\.\w+\s*=[^;\n]*$/;

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
  const files = walk(SRC);
  const violations = contractViolationClasses(files);
  for (const file of files) {
    const rel = relative(SRC, file);
    if (isExemptFile(rel) || isSampleData(rel)) continue;
    const source = withoutComments(readFileSync(file, "utf8"));
    const done = phraseValues(source);
    const helpers = diagnosisHelpers(source, violations);
    const seen = new Set<string>();
    for (const m of source.matchAll(/(["'])((?:\\.|(?!\1)[^\n])*)\1/g)) {
      const value = m[2].trim();
      if (!value || seen.has(value) || done.has(value)) continue;
      if (HAN.test(value)) continue;
      if (!looksLikeASentence(value)) continue;
      if (DIRECTIVES.has(value) || LOOKS_LIKE_PATH.test(value) || LOOKS_LIKE_CODE.test(value)) continue;
      if (looksLikeClassNames(value)) continue;
      // 一个 Phrase 的值可以是几段拼起来的（`en: "…" + "…"`）。前面那个 `+`
      // 就是"我是上一段的续"的标记 —— 它自己不是一句独立的漏翻。
      if (/\+\s*$/.test(source.slice(0, m.index).trimEnd() + "")) continue;
      const before = source.slice(Math.max(0, m.index - 300), m.index);
      const after = source.slice(m.index + m[0].length, m.index + m[0].length + 20);
      if (IN_A_TOKEN_ATTRIBUTE.test(before) || IN_A_STYLE.test(before)) continue;
      if (isDiagnosis(before, violations, helpers) || isMatchInput(before, after)) continue;
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
 * 上一条的另外两种写法：模板字符串、JSX 裸文本。
 *
 * 上一条只认带引号的字面量。2026-10-20 扩到 .ts 之后粗扫，还有两种写法整片看不见：
 * `` `${session.driverLabel} is the current driver. …` ``（模板字符串，会话页只读时
 * 输入框上那句）和 `<span>{count} waiting on a decision or resource</span>`
 * （JSX 文本，阻塞面板标题旁那句）—— 中文界面下都原样露出来。
 *
 * 这两种不能再手写词法去找：JSX 文本和代码在源码里长得一样（`a > b`、`Array<T>`、
 * 箭头函数），中文那条之所以能靠「字面量之外还剩汉字」认出 JSX 文本，是因为代码里
 * 没有汉字；英文没有这个便宜。所以交给 TypeScript 自己的解析器：它说是模板的文字
 * 部分、是 JsxText，那就是。
 *
 * 判句子、样例数据、违约诊断、比对输入、EXEMPT —— 都沿用上一条的口径。多出来的
 * 结构性排除只有三种，各自都不是说给人听的：
 *   - `console.*` 的参数：开发者控制台；
 *   - `className` / `rel` 属性：记号列表；`style` 属性：CSS 值；
 *   - `<code>` / `<pre>` / `<kbd>` 里的文本：命令和代码原样显示，翻译它就不能照抄了。
 */
const CODE_ELEMENTS = new Set(["code", "pre", "kbd", "samp"]);

/** 往上走过 `+`、括号、三元，落在 `zh:` / `en:` 上 —— 它是一个 Phrase 的值（或其中一段）。 */
function inAPhrase(node: ts.Node): boolean {
  let at = node;
  while (
    ts.isBinaryExpression(at.parent)
    || ts.isParenthesizedExpression(at.parent)
    || ts.isConditionalExpression(at.parent)
  ) at = at.parent;
  return ts.isPropertyAssignment(at.parent) && /^(zh|en)$/.test(at.parent.name.getText());
}

/**
 * 违约诊断的另一种写法：消息写在子类自己的构造函数里（`super(模板串)`，见
 * live-event-reconciler 的 SequenceConflictError），而不是 `new X(…)` 的调用处。
 * 上一条的 `isDiagnosis` 只看得见后者。
 */
function raisedBySuper(node: ts.Node, violations: ReadonlySet<string>): boolean {
  for (let at = node.parent; at; at = at.parent) {
    if (ts.isCallExpression(at) && at.expression.kind === ts.SyntaxKind.SuperKeyword) {
      for (let owner = at.parent; owner; owner = owner.parent) {
        if (ts.isClassDeclaration(owner)) return Boolean(owner.name && violations.has(owner.name.text));
      }
    }
  }
  return false;
}

function notSaidToAPerson(node: ts.Node): boolean {
  for (let at = node.parent; at; at = at.parent) {
    if (ts.isCallExpression(at) && ts.isPropertyAccessExpression(at.expression)
      && at.expression.expression.getText() === "console") return true;
    if (ts.isJsxAttribute(at) && (TOKEN_ATTRIBUTES.has(at.name.getText()) || at.name.getText() === "style")) return true;
    if (ts.isJsxElement(at) && CODE_ELEMENTS.has(at.openingElement.tagName.getText())) return true;
  }
  return false;
}

/**
 * 模板字符串的文字部分和 JSX 文本。`${…}` 换成 `0`：不是字母，不会凑出单词（换成 `x` 时
 * SVG 路径 `M${a},${b}` 就成了 "Mx"），而 class 名串 `state-${s}` 换完还是 class 名串。
 */
function templateAndJsxTexts(file: string, source: string): { node: ts.Node; text: string; shown: string }[] {
  const tree = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true,
    file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const found: { node: ts.Node; text: string; shown: string }[] = [];
  const visit = (node: ts.Node) => {
    if (ts.isTaggedTemplateExpression(node)) return;
    if (ts.isNoSubstitutionTemplateLiteral(node)) {
      found.push({ node, text: node.text, shown: node.text });
    } else if (ts.isTemplateExpression(node)) {
      found.push({
        node,
        text: node.head.text + node.templateSpans.map((span) => `0${span.literal.text}`).join(""),
        shown: node.head.text + node.templateSpans.map((span) => `\${…}${span.literal.text}`).join(""),
      });
    } else if (ts.isJsxText(node) && !node.containsOnlyTriviaWhiteSpaces) {
      const text = node.text.replace(/\s+/g, " ");
      found.push({ node, text, shown: text });
    }
    ts.forEachChild(node, visit);
  };
  visit(tree);
  return found;
}

test("模板字符串和 JSX 文本里的英文也必须配着中文一起写", () => {
  const offenders: string[] = [];
  const files = walk(SRC);
  const violations = contractViolationClasses(files);
  for (const file of files) {
    const rel = relative(SRC, file);
    if (isExemptFile(rel) || isSampleData(rel)) continue;
    const raw = readFileSync(file, "utf8");
    const helpers = diagnosisHelpers(withoutComments(raw), violations);
    const seen = new Set<string>();
    for (const { node, text, shown } of templateAndJsxTexts(file, raw)) {
      const value = text.trim();
      if (!value || seen.has(value) || HAN.test(value) || !looksLikeASentence(value)) continue;
      if (LOOKS_LIKE_PATH.test(value) || LOOKS_LIKE_CODE.test(value) || looksLikeClassNames(value)) continue;
      if (inAPhrase(node) || notSaidToAPerson(node)) continue;
      const at = node.getStart();
      const before = raw.slice(Math.max(0, at - 300), at);
      const after = raw.slice(node.getEnd(), node.getEnd() + 20);
      if (raisedBySuper(node, violations) || IN_A_STYLE.test(before)) continue;
      if (isDiagnosis(before, violations, helpers) || isMatchInput(before, after)) continue;
      if (isExempt(rel, shown.trim())) continue;
      seen.add(value);
      offenders.push(`${rel}:${ts.getLineAndCharacterOfPosition(node.getSourceFile(), at).line + 1} → ${shown.trim()}`);
    }
  }
  assert.deepEqual(
    offenders,
    [],
    `这些模板字符串 / JSX 文本只有英文，切成中文界面时会原样露出来（${offenders.length} 处）：\n`
      + offenders.join("\n"),
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

/**
 * 第五条：呈现用的纯函数，语言参数不许有默认值。
 *
 * 纯函数要语言就收一个 `lang` 参数（见 useT.ts）。但从前它们大都写成
 * `lang: Language = "zh"` —— 于是**调用方忘了传，界面就安静地退回中文**，
 * 类型检查一声不吭。2026-10-19 英文界面的模型设置页上「已校验」一栏写着
 * 「3 分钟前检测：通过」，就是 `probeSummary(backend)` 少传了一个参数；
 * 去掉默认值后 tsc 当场又报出会话页上三处同样漏传的。
 *
 * 默认值把「忘了」变成「中文」；没有默认值，「忘了」就是编译错误。
 *
 * 同一种病的另一种写法：语言放在选项对象里、可不传，函数体里再补
 * `const lang = options.lang ?? "zh"`（2026-10-20 projectExecution 与
 * projectLiveActivity 两处）。上面那条只认参数位，看不见它。所以「语言」后面跟
 * `?? "zh"` / `?? "en"` 也算 —— 选项里的 lang 改成必填，漏传照样是编译错误。
 */
test("呈现用的纯函数，语言参数不许有默认值", () => {
  const offenders: string[] = [];
  for (const file of walk(SRC)) {
    const rel = relative(SRC, file);
    if (isExemptFile(rel)) continue;
    const source = withoutComments(readFileSync(file, "utf8"));
    // 只看**参数位**（紧跟在 `(` 或 `,` 之后）：`const other: Language = …` 是变量，
    // 不是"调用方可以不传"的那种默认值。
    for (const m of source.matchAll(/(?<=[(,]\s*)(\w+)\??\s*:\s*Language\s*=\s*[^,)\n]+/g)) {
      offenders.push(`${rel} → ${m[0]}`);
    }
    for (const m of source.matchAll(/[\w.]*\b\w*[lL]ang(?:uage)?\s*\?\?\s*["'](?:zh|en)["']/g)) {
      offenders.push(`${rel} → ${m[0]}`);
    }
  }
  assert.deepEqual(
    offenders,
    [],
    "这些函数的语言参数有默认值 —— 调用方漏传时界面会安静地退回那种语言，"
      + `类型检查看不见（${offenders.length} 处）。去掉默认值，让 tsc 把漏传的调用方报出来：\n`
      + offenders.join("\n"),
  );
});
