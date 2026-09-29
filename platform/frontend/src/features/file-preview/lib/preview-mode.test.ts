import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { isTextualMode, normalizeMediaType, previewModeFor } from "./preview-mode.ts";

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

test("研究产出的那几种格式各自落到能画出来的模式", () => {
  // 这四种是节点真的会产出的：postprocess 出图、writing 出论文、
  // 偶尔出矢量图和网页报告。
  assert.equal(previewModeFor("image/png"), "image");
  assert.equal(previewModeFor("application/pdf"), "pdf");
  assert.equal(previewModeFor("image/svg+xml"), "image");
  assert.equal(previewModeFor("text/html"), "html");
  assert.equal(previewModeFor("text/markdown"), "markdown");
  assert.equal(previewModeFor("text/plain"), "text");
  assert.equal(previewModeFor("application/json"), "text");
});

test("带参数的 Content-Type 不该掉到兜底", () => {
  /**
   * 服务端发的是 `text/html; charset=utf-8` —— 不归一化的话它既不等于
   * "text/html" 也不 startsWith("text/")… 实际上 startsWith 会命中 text，
   * 于是 HTML 被当成纯文本显示源码。症状是"打开网页看到一堆标签"，
   * 而没有任何一层报错。
   */
  assert.equal(previewModeFor("text/html; charset=utf-8"), "html");
  assert.equal(previewModeFor("TEXT/HTML"), "html");
  assert.equal(previewModeFor(" image/png "), "image");
  assert.equal(normalizeMediaType("text/markdown; charset=UTF-8"), "text/markdown");
});

test("认不出来的类型退成下载，不硬画", () => {
  assert.equal(previewModeFor("application/octet-stream"), "download");
  assert.equal(previewModeFor(""), "download");
  assert.equal(previewModeFor("application/x-hdf5"), "download");
});

test("只有文本类模式需要解码 blob", () => {
  // 图和 PDF 走 object URL，多解一次码就是白拷一份几 MB 的字符串。
  assert.equal(isTextualMode("markdown"), true);
  assert.equal(isTextualMode("text"), true);
  assert.equal(isTextualMode("image"), false);
  assert.equal(isTextualMode("pdf"), false);
  assert.equal(isTextualMode("download"), false);
});

test("渲染分流的判据是服务端给的类型，不是前端自己按扩展名再猜一次", () => {
  /**
   * 同一个问题两个答案 = 加格式时只改一边，分叉那一刻谁都不报错。后端那张表
   * （app/services/file_media.py）是唯一真相源，这里只消费它的结论。
   *
   * 判据落在"有没有在这一层重新按扩展名判"上：出现 `.png` / `endsWith` /
   * `split(".")` 这类写法就说明第二张表回来了。
   */
  const lib = source("./preview-mode.ts");
  const api = source("../api/file-preview.ts");
  for (const [name, code] of [["preview-mode.ts", lib], ["file-preview.ts", api]] as const) {
    assert.doesNotMatch(code.replace(/\/\*[\s\S]*?\*\//g, ""), /\.png|\.pdf|\.svg/,
      `${name} 不该按扩展名分流 —— 类型由后端给`);
  }
  assert.match(api, /headers\.get\("content-type"\)/);
});

test("HTML 预览必须 sandbox，SVG 必须走 img", () => {
  /**
   * 这里画的是 **agent 写的文件**。两条都不是风格问题：
   *   - iframe 不带 sandbox → blob URL 继承主站源，页面里的脚本能摸
   *     localStorage 里的凭据。
   *   - SVG 内联进 DOM（dangerouslySetInnerHTML）→ 同样是脚本执行入口；
   *     放进 `<img>` 则不执行脚本。
   */
  // 去注释再断言：这个文件的注释里就解释着"为什么不用 dangerouslySetInnerHTML"，
  // 连注释一起扫的话，写下理由反而会让护栏红。
  const preview = source("../components/FilePreview.tsx");
  const code = preview.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
  const htmlBranch = code.slice(code.indexOf('if (payload.mode === "html")'));
  assert.match(htmlBranch, /sandbox=""/);
  assert.doesNotMatch(code, /dangerouslySetInnerHTML/);
  // 锚在**分支**上，不是第一次出现的地方 —— 组件顶上那个决定要不要建
  // object URL 的条件里也写着 mode === "image"，切到那里等于什么都没断言。
  const imageBranch = code.slice(
    code.indexOf('if (payload.mode === "image")'),
    code.indexOf('if (payload.mode === "pdf")'),
  );
  assert.match(imageBranch, /<img/);
});

test("预览不跟着文件树的 5 秒轮询走", () => {
  /**
   * 树要显示 live changes 所以 `refetchInterval: 5000`。预览复用那个节奏的话，
   * 开着一份 12MB 的 PDF 就是每 5 秒重下一遍。两者问的是不同的问题。
   */
  const preview = source("../components/FilePreview.tsx");
  assert.doesNotMatch(preview, /refetchInterval/);
  assert.match(preview, /staleTime/);
  const keys = source("../../../lib/query/keys.ts");
  assert.match(keys, /projectFilePreview/);
});
