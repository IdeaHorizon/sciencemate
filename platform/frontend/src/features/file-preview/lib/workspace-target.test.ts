import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { looksLikeImagePath, workspaceTargetOf } from "./workspace-target.ts";
import { parseMarkdown } from "../../chat/lib/rich-text-markdown.ts";

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

/**
 * 去注释。这些文件的注释里就写着"为什么不用 `<img>` / 为什么不收 `https:`"——
 * 连注释一起扫的话，**把理由写清楚反而会让护栏变红**，下一个人会去删注释。
 * 断言要落在代码上。
 */
function code(path: string) {
  return source(path).replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
}

test("节点产出的路径照常放行", () => {
  assert.equal(
    workspaceTargetOf("figures/figures/fig_q1_qq.png"),
    "figures/figures/fig_q1_qq.png",
  );
  assert.equal(
    workspaceTargetOf("paper/latex_build/x/main_clean.pdf"),
    "paper/latex_build/x/main_clean.pdf",
  );
  assert.equal(workspaceTargetOf("MEMORY.md"), "MEMORY.md");
  assert.equal(workspaceTargetOf("./data/README.md"), "data/README.md", "./ 前缀该被吃掉");
  assert.equal(workspaceTargetOf("  figures/a.png  "), "figures/a.png", "两头空白不该改变结论");
});

test("外部地址一个都不许当图 —— 渲染即外泄，不用点", () => {
  /**
   * 这是整条特性里唯一"错一次就出事"的地方。`![](https://x/p.png?d=<机密>)`
   * 在渲染那一刻请求就发出去了，用户没有任何介入机会；而这段文本来自 agent，
   * agent 读过的外部论文/网页里可能带着冲它去的指令。
   *
   * 逐条列在这里不是凑数：每一条都是一种"看起来不像外部地址"的写法。
   */
  for (const hostile of [
    "https://attacker.example/p.png",
    "http://attacker.example/p.png?d=secret",
    "HTTPS://ATTACKER.EXAMPLE/p.png",              // 大小写
    "//attacker.example/p.png",                     // 协议相对：不含 http，最像路径
    "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",   // 内联载荷
    "javascript:alert(1)",
    "file:///etc/passwd",
    "mailto:x@y.z",
    "vbscript:msgbox",
    "\\\\attacker.example\\share\\p.png",           // UNC
  ]) {
    assert.equal(workspaceTargetOf(hostile), null, hostile);
  }
});

test("越界路径不许发出去", () => {
  for (const escape of [
    "../secret.txt",
    "../../etc/passwd",
    "paper/../../escape.png",
    "/etc/passwd",
    "/",
    ".git/config",
    "..",
    ".",
    "",
    "   ",
  ]) {
    assert.equal(workspaceTargetOf(escape), null, JSON.stringify(escape));
  }
});

test("查询串 / 锚点 / 控制字符不是文件路径", () => {
  // `a.png?x=1` 放行的话，问号后面那截会被 encodeURIComponent 编进文件名，
  // 变成一个必然 404 的请求 —— 但更要紧的是它是外部 URL 的形状特征。
  assert.equal(workspaceTargetOf("figures/a.png?d=secret"), null);
  assert.equal(workspaceTargetOf("figures/a.png#frag"), null);
  assert.equal(workspaceTargetOf("figures/a\npng"), null);
  assert.equal(workspaceTargetOf("figures/a\u0000.png"), null);
});

test("像不像图只是取之前的筛选，不是判决", () => {
  assert.equal(looksLikeImagePath("a/b/c.png"), true);
  assert.equal(looksLikeImagePath("a/b/c.SVG"), true);
  assert.equal(looksLikeImagePath("a/b/c.pdf"), false);
  assert.equal(looksLikeImagePath("a/b/c"), false);
  // 真正的类型以服务端 Content-Type 为准 —— 这条注释必须留在代码里，
  // 否则下一个人会拿这个函数去做判决。
  assert.match(source("./workspace-target.ts"), /服务端返回的 Content-Type/);
});

test("渲染层真的走了这道判据，而不是直接把 target 塞给 img", () => {
  /**
   * 判据存在 ≠ 判据在路径上（我在这个仓库里反复栽的形状）。所以查的是渲染
   * 那一处：`<img>` 的 src 只能来自 blob（InlineWorkspaceImage），而
   * RichText 里必须先过 workspaceTargetOf。
   */
  const rich = code("../../chat/components/RichText.tsx");
  assert.match(rich, /workspaceTargetOf\(token\.target\)/);
  assert.doesNotMatch(rich, /<img/, "RichText 不该自己画 img —— 取字节要带鉴权");

  const inline = code("../components/InlineWorkspaceImage.tsx");
  // 唯一的 img 用的是 object URL，不是 token 里的 target。
  assert.match(inline, /<img src=\{url\}/);
  assert.doesNotMatch(inline, /src=\{(path|target)\}/);

  // 分词器只认形状、不判好坏 —— 策略不许悄悄搬回解析层（搬回去就绕过了这道测试）。
  const parser = code("../../chat/lib/rich-text-markdown.ts");
  assert.doesNotMatch(parser, /https?:|workspaceTargetOf/);
});

test("拿不到语境时整条退回成纯文本", () => {
  // fixture / demo 模式没有 provider。那时候画一个点不动的按钮或者裂图，
  // 比什么都不做更糟。
  const rich = code("../../chat/components/RichText.tsx");
  assert.match(rich, /if \(!workspace \|\| !path\)/);
});

test("目录引用的尾部斜杠不算一段路径", () => {
  assert.equal(workspaceTargetOf("paper/figures/"), "paper/figures");
  assert.equal(workspaceTargetOf("./paper/figures/"), "paper/figures");
  // 只认一个尾斜杠：`paper//` 剩下的那个空段照旧被"空路径段"那条拦住。
  assert.equal(workspaceTargetOf("paper//"), null);
});

test("外部图片在 remark 渲染器下落成文字：解析器认它是图，判据拦下，渲染层只画 span", () => {
  /**
   * 换了解析器，通向 `<img>` 的路也换了：现在是 mdast 的 image 节点 → WorkspaceReference。
   * 解析器不做安全判断（它只认形状，外部 URL 照样是 image 节点），所以判据必须
   * 还在 WorkspaceReference 里，而且渲染层只有它一条路通向 InlineWorkspaceImage。
   */
  const tree = parseMarkdown("![tracker](https://attacker.example/p.png?d=secret) and https://attacker.example/x");
  const p = tree.children[0];
  assert.equal(p.type, "paragraph");
  if (p.type !== "paragraph") return;
  const image = p.children[0];
  assert.equal(image.type === "image" && image.url, "https://attacker.example/p.png?d=secret");
  assert.equal(image.type === "image" && workspaceTargetOf(image.url), null, "判据把外部图片源拦下 → 走 !path 分支，画文字");
  // GFM 把裸 URL 也认成 link 节点 —— 同一条判据、同一个 !path 分支，不会变成可点的 <a>。
  const autolink = p.children.find((node) => node.type === "link");
  assert.equal(autolink?.type === "link" && autolink.url, "https://attacker.example/x");
  assert.equal(autolink?.type === "link" && workspaceTargetOf(autolink.url), null);

  const rich = code("../../chat/components/RichText.tsx");
  assert.match(rich, /case "image": return <WorkspaceReference/);
  assert.match(rich, /case "link": return <WorkspaceReference/);
  // 外部链接暂不可点：渲染器里每一个 <a> 都只能是页内锚（脚注）。
  for (const anchor of rich.match(/<a\b[^>]*>/g) ?? []) {
    assert.match(anchor, /href=\{`#/, `不是页内锚的 <a>：${anchor}`);
  }
  assert.doesNotMatch(rich, /target="_blank"|href=\{token\.target\}/);
});
