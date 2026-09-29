import { say, type Language, type Phrase } from "../../../shared/i18n/language.ts";

/**
 * 长粘贴不进输入框，转成工作区里的一份文件。
 *
 * 为什么：输入框是受控的 `<textarea>`，草稿挂在会话页的 state 上 —— 贴进
 * 几千行之后，**每敲一个字**整个会话页都带着这几十万字符重渲一遍，浏览器
 * 很快就卡到像死掉（wangd 2026-09-23）。几千行的东西本来也不是「一句话」，
 * 是一份材料；材料在平台上只有一个落点：会话工作区 `sources/`（同「添加
 * 文件」那条上传通道，agent 下一轮就能按路径读，框架每轮把「用户交来了哪些
 * 文件」当事实递给它）。
 *
 * 输入框里只留一行引用，写明文件路径和行数 —— 用户看得见自己贴了什么去了
 * 哪儿，模型读这句话时也知道「上面那段」指的是哪份文件。
 */

/** 超过任一条就转文件。一段几十行的代码/报错照常贴进来，几百行起就是材料了。 */
export const LONG_PASTE_MIN_LINES = 200;
export const LONG_PASTE_MIN_CHARS = 20_000;

export function countLines(text: string): number {
  if (!text) return 0;
  let lines = 1;
  for (let i = 0; i < text.length; i += 1) if (text.charCodeAt(i) === 10) lines += 1;
  return text.endsWith("\n") ? lines - 1 : lines;
}

export function isLongPaste(text: string): boolean {
  return text.length >= LONG_PASTE_MIN_CHARS || countLines(text) >= LONG_PASTE_MIN_LINES;
}

/**
 * 文件名：带到秒的时间戳 + 本页序号。后端对同名不同内容回 409，所以名字
 * 必须一次一个；序号保证同一秒贴两次也不撞。
 */
export function pastedFileName(now: Date, seq: number): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  return `pasted-${stamp}-${seq}.txt`;
}

const PENDING: Phrase = {
  zh: "[粘贴的文本 #{seq}（{lines} 行）上传中…]",
  en: "[Pasted text #{seq} ({lines} lines) uploading…]",
};
const SAVED: Phrase = {
  zh: "[粘贴的文本（{lines} 行）已存为 {path}]",
  en: "[Pasted text ({lines} lines) saved as {path}]",
};

/** 上传还没回来时占着位置的那一行。带序号，替换时按它精确找到自己。 */
export function pendingPasteToken(seq: number, lines: number, lang: Language): string {
  return say(PENDING, lang, { seq, lines });
}

export function pastedFileReference(path: string, lines: number, lang: Language): string {
  return say(SAVED, lang, { path, lines });
}

/** 在选区处插入 —— 与原生粘贴同一个位置语义（替换选中的那段）。 */
export function insertAtSelection(draft: string, start: number, end: number, insert: string): string {
  const a = Math.max(0, Math.min(start, draft.length));
  const b = Math.max(a, Math.min(end, draft.length));
  return draft.slice(0, a) + insert + draft.slice(b);
}

/**
 * 把占位换成最终文字。用户在上传期间删掉了占位 —— 那就是不要了，不再塞回去。
 */
export function replaceToken(draft: string, token: string, replacement: string): string {
  const at = draft.indexOf(token);
  if (at < 0) return draft;
  return draft.slice(0, at) + replacement + draft.slice(at + token.length);
}

/** 从占位模板本身推出识别式 —— 手写一份正则，改文案时两边就分叉了。 */
const PENDING_PATTERN = new RegExp(
  Object.values(PENDING)
    .map((template) => template
      .replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
      .replace(/\\\{(seq|lines)\\\}/g, "\\d+"))
    .join("|"),
);

/** 还有没回来的上传就不许发：发出去的会是一句「上传中…」，模型拿不到那份文件。 */
export function draftHasPendingPaste(draft: string): boolean {
  return PENDING_PATTERN.test(draft);
}
