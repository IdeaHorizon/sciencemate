/**
 * 一个文件该怎么渲染 —— 判据取**服务端实际返回的 media type**，不取扩展名。
 *
 * ## 为什么不在前端再写一张扩展名表
 *
 * 后端已经有一张（`app/services/file_media.py`），它决定 Content-Type。前端
 * 若按扩展名自己再判一次，同一个问题就有了两个答案：加一种格式时只改一边，
 * 两边分叉的那一刻**谁都不会报错** —— 后端认为它是图，前端把它画成文本框。
 *
 * 所以这里只消费服务端的回答。前端要新增一种可渲染格式，先在后端把类型加上，
 * 这里再决定用哪个组件画它。顺序是强制的，因为没有类型就走不到这个函数。
 */

export type PreviewMode =
  /** `<img>`。SVG 也走这里 —— img 里的 SVG 不执行脚本。 */
  | "image"
  /** 浏览器自带的 PDF 阅读器。不引 pdfjs —— 一个依赖换不来什么。 */
  | "pdf"
  /** sandbox 过的 iframe。agent 写的网页当不可信内容处理。 */
  | "html"
  /** 走对话里那套 markdown 渲染，样式与正文一致。 */
  | "markdown"
  /** 等宽纯文本。代码、日志、json、csv 都落这里。 */
  | "text"
  /** 画不了 —— 给出处和大小，让人下载。 */
  | "download";

const TEXTUAL = new Set([
  "application/json",
  "application/x-ndjson",
  "application/xml",
  "application/javascript",
  "application/x-yaml",
]);

export function previewModeFor(mediaType: string): PreviewMode {
  const type = normalizeMediaType(mediaType);
  if (type === "image/svg+xml") return "image";
  if (type.startsWith("image/")) return "image";
  if (type === "application/pdf") return "pdf";
  if (type === "text/html") return "html";
  if (type === "text/markdown") return "markdown";
  if (type.startsWith("text/")) return "text";
  if (TEXTUAL.has(type)) return "text";
  return "download";
}

/** 这个模式需要文本，所以取回来之后要把 blob 解码。 */
export function isTextualMode(mode: PreviewMode): boolean {
  return mode === "markdown" || mode === "text";
}

/**
 * `Content-Type` 常带参数（`text/html; charset=utf-8`），大小写也不保证。
 * 不归一化的话 `text/html; charset=utf-8` 会一路掉到 download。
 */
export function normalizeMediaType(value: string): string {
  return (value.split(";")[0] ?? "").trim().toLowerCase();
}

/**
 * 纯文本在浏览器里是有代价的：一个 8MB 的 jsonl 铺进 DOM 会把标签页顶住，
 * 而它对"看一眼这是什么"毫无帮助。图和 PDF 没有这个问题（渲染在浏览器自己
 * 手里），所以上限只加在文本上。
 */
export const TEXT_PREVIEW_MAX_CHARS = 400_000;
