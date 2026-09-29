import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { parseMarkdown, renderFormula } from "./rich-text-markdown.ts";

/** 注释里写的不是代码：扫渲染器源码前先摘掉。 */
function code(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:])\/\/.*$/gm, "$1");
}

test("standard markdown includes GFM tables, nested lists, tasks, rules and formulas", () => {
  const tree = parseMarkdown("## Result\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n---\n\n- [x] first\n  - nested\n\n$$\nx=\\frac{a}{b}\n$$");
  assert.deepEqual(tree.children.map((node) => node.type), ["heading", "table", "thematicBreak", "list", "math"]);
  const list = tree.children[3];
  assert.equal(list.type === "list" && list.children[0].checked, true);
  assert.equal(list.type === "list" && list.children[0].children[1].type, "list");
});

test("inline syntax preserves source offsets and reference definitions", () => {
  const tree = parseMarkdown("Use **evidence**, `code`, and $x^2$. [report][r]\n\n[r]: paper/report.md");
  const p = tree.children[0];
  assert.equal(p.type, "paragraph");
  if (p.type !== "paragraph") return;
  const strong = p.children[1];
  assert.equal(strong.type === "strong" && strong.children[0].position?.start.offset, 6);
  assert.ok(p.children.some((node) => node.type === "inlineMath"));
  assert.equal(tree.children[1].type, "definition");
});

test("formula rendering produces accessible math without trusting executable commands", () => {
  assert.match(renderFormula("\\frac{a}{b}", true), /<math/);
  assert.doesNotMatch(renderFormula("\\href{javascript:alert(1)}{click}", false), /href=/);
  assert.doesNotMatch(renderFormula("\\includegraphics{https://example.test/tracker}", false), /<img/);
  assert.doesNotThrow(() => renderFormula("\\badCommand{", false));
});

test("raw HTML stays an inert html node: the renderer paints it as text and only KaTeX may insert HTML", () => {
  /**
   * 正文里的 HTML（模型从论文/网页里抄来的也算）不能被解释。两层：
   *   · 解析器：`<script>` 块和行内 `<img …>` 都只是 html 节点，不会变成 image 之类的结构；
   *   · 渲染层：html 节点只当文本画，而 dangerouslySetInnerHTML 全文只有 KaTeX 那一处，
   *     吃的是 renderFormula（trust:false）的输出，不是任何来自正文的字符串。
   */
  const tree = parseMarkdown("<script>alert(1)</script>\n\nsee <img src=x onerror=alert(1)> here");
  assert.equal(tree.children[0].type, "html");
  const p = tree.children[1];
  assert.equal(p.type, "paragraph");
  if (p.type !== "paragraph") return;
  const inline = p.children.find((node) => node.type === "html");
  assert.equal(inline?.type === "html" && inline.value, "<img src=x onerror=alert(1)>");
  assert.equal(p.children.some((node) => node.type === "image"), false);

  const rich = code("../components/RichText.tsx");
  assert.match(rich, /case "html": return <span key=\{key\}>\{node\.value\}<\/span>;/);
  assert.equal(rich.split("dangerouslySetInnerHTML").length - 1, 1, "只有 KaTeX 那一处可以插 HTML");
  assert.match(
    rich,
    /const html = useMemo\(\(\) => renderFormula\(text, display\)[\s\S]{0,200}dangerouslySetInnerHTML: \{ __html: html \}/,
  );
});
