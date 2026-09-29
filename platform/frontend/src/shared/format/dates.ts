import { say, type Language, type Phrase } from "../i18n/language.ts";

/** 「N 秒前」这一档。两种语言并排写着，漏一种在类型上就写不出来。 */
function ago(n: number, unit: Phrase, lang: Language): string {
  return say(unit, lang, { n });
}

/**
 * 相对时间。`lang` **是参数不是全局**：这是个纯函数，它要是自己去够当前语言，
 * 就再也没法用两种语言各测一遍了。
 */
export function formatRelativeTime(iso: string | null | undefined, lang: Language = "zh"): string {
  if (!iso) return "—";
  const date = new Date(iso);
  const now = Date.now();
  // 解析不出的时间戳（NaN）：算出来的每一档都是 NaN 比较 → 落到最后一档，画成「NaNy ago」。
  if (Number.isNaN(date.getTime())) return "—";
  const diffMs = now - date.getTime();
  const sec = Math.floor(diffMs / 1000);
  if (sec < 60) return ago(sec, { zh: "{n} 秒前", en: "{n}s ago" }, lang);
  const min = Math.floor(sec / 60);
  if (min < 60) return ago(min, { zh: "{n} 分钟前", en: "{n}m ago" }, lang);
  const hr = Math.floor(min / 60);
  if (hr < 24) return ago(hr, { zh: "{n} 小时前", en: "{n}h ago" }, lang);
  const day = Math.floor(hr / 24);
  if (day < 30) return ago(day, { zh: "{n} 天前", en: "{n}d ago" }, lang);
  const month = Math.floor(day / 30);
  if (month < 12) return ago(month, { zh: "{n} 个月前", en: "{n}mo ago" }, lang);
  return ago(Math.floor(month / 12), { zh: "{n} 年前", en: "{n}y ago" }, lang);
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return date.toLocaleString();
}

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const min = Math.floor(s / 60);
  const remSec = Math.round(s - min * 60);
  return `${min}m ${remSec}s`;
}
