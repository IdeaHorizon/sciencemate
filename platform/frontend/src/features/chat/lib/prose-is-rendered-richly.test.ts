import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

/**
 * 模型写的散文，**每一处**都要走 RichText。
 *
 * 现场（wangd 2026-08-25）：调度器交付时写了
 * `- 📄 [英国饮食文化论文 PDF](writing/latex_build/…/main_clean.pdf)`，
 * 界面上原样打印出这行 markdown。「那个链接也没法点」。
 *
 * 能力是有的 —— `RichText`/`WorkspaceReference`（PR#642）把工作区相对路径变成
 * 可点文件、把图内联，安全策略也在那里。但它只接到了 **ChatMessages** 一处，
 * 而模型的散文有四个到达口：
 *
 *   · assistant 消息正文        → ChatMessages          ✅ 一直是富文本
 *   · 调度器 said（左栏对话）    → CanonicalRunActivity   ❌ 曾是 `{said.text}`
 *   · 调度器独白（左栏主线）      → CanonicalRunActivity   ❌ 曾是 `{item.narration.text}`
 *   · 子节点独白（右栏过程）      → Narration             ❌ 曾是 `{item.text}`
 *
 * 而这个会话里 assistant 消息因为另一个缺陷根本没落库（见 PR#680），于是那条
 * **唯一**接对了的路径一次都没被走到，用户看到的每一个字都来自纯文本的三处。
 *
 * ## 判据为什么写成"扫盘"
 *
 * 断言"这三处调了 RichText"只保得住这三处。新加第四个散文出口时它不会红 ——
 * 而漏接的表现恰恰是"看起来正常，只是链接点不动"，没有任何一层会报错。
 * 所以反过来写：**把合法那条路命名出来（`<RichText text={…}>`），文件里任何
 * 其它把 `.text` 直接插进 JSX 的写法一律违规。**
 */
/** 注释里写的不是代码。扫盘前先摘掉，否则一句解释缺陷的话就能把守卫自己判红。
 *  （第一版就栽在这上面：注释里写着 `` `{said.text}` 裸文本 `` 用来解释旧写法，
 *   扫盘把它当成了违规现场。） */
function code(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
}

test("模型散文没有一处是裸插值", () => {
  const source = code(readFileSync(
    new URL("../components/CanonicalRunActivity.tsx", import.meta.url),
    "utf8",
  ));

  // 合法：`<RichText text={x.text} />`。先摘掉，剩下的裸插值就是违规。
  const withoutRich = source.replace(/<RichText\s+text=\{[^}]+\}\s*\/>/g, "");
  const bare = [...withoutRich.matchAll(/\{\s*([A-Za-z_$][\w.$]*\.text)\s*\}/g)]
    .map((match) => match[1]);

  assert.deepEqual(bare, [], (
    `这些散文字段被直接插进 JSX，markdown 会原样打印、链接点不动：${bare.join(", ")}。`
    + "散文一律走 <RichText text={…} />。"
  ));

  // 三个到达口都在，且都走 RichText —— 扫盘只保证"没有裸的"，这里保证"在场"。
  for (const field of ["said.text", "item.narration.text", "item.text"]) {
    assert.ok(
      source.includes(`<RichText text={${field}} />`),
      `${field} 没有走 RichText —— 那条路径上的链接是死的`,
    );
  }
});

test("调度器说的话不再叠 pre-wrap", () => {
  const css = code(readFileSync(
    new URL("../../../shared/styles/chat.css", import.meta.url),
    "utf8",
  ));
  const rule = /\.chat-orchestrator-said \{[^}]*\}/.exec(css)?.[0] ?? "";
  assert.ok(rule, "找不到 .chat-orchestrator-said 规则");
  assert.equal(
    /white-space:\s*pre-wrap/.test(rule),
    false,
    "RichText 已经按块切段，再叠 pre-wrap 会把块之间的空行画两遍",
  );
});
