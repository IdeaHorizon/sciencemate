import type { ModelBackend } from "@/lib/api";
import { say, type Language } from "../../../shared/i18n/language.ts";

/**
 * 一次探活的观测有多旧 —— 以及它还算不算数。
 *
 * ## 为什么需要这个东西
 *
 * `backend.status` 是**判决**（ready / credentials_rejected / …），由上一次
 * 探活的结果派生。而探活只在**写操作**时触发：新建、改凭证、设默认。此后那
 * 个 "Ready" 就一直挂在那儿，哪怕 key 早已失效。
 *
 * 2026-08-21 wangd 的原话：「感觉很多都不能用，从而放着有啥用呢」。病根不是
 * 探针不准 —— 拿假 key 建连接，它当场就报 credentials_rejected。病根是**那个
 * 结论没有时间戳**，于是一个几周前的快照长得和当前事实一模一样。
 *
 * 所以这里不改判决，只把"它有多旧"算出来交给界面。判决归后端，年龄归这里。
 */

/** 超过这个岁数，就不该再让人把 "Ready" 当成"现在能用"。 */
export const PROBE_STALE_AFTER_MS = 24 * 60 * 60 * 1000;

export type ProbeFreshness =
  | { kind: "never" }
  | { kind: "fresh"; ageMs: number; label: string }
  | { kind: "stale"; ageMs: number; label: string };

export function probeFreshness(
  backend: Pick<ModelBackend, "last_probe_at">,
  now: number = Date.now(),
  lang: Language = "zh",
): ProbeFreshness {
  const raw = backend.last_probe_at;
  if (!raw) return { kind: "never" };
  const at = new Date(raw).getTime();
  // 解析不出来 = 我不知道它多旧，而"不知道"不能冒充"新鲜"。
  if (Number.isNaN(at)) return { kind: "never" };
  // 时钟偏移会让 at 落在未来。把它当 0 岁，别显示"-3 分钟前"。
  const ageMs = Math.max(0, now - at);
  const label = relativeAge(ageMs, lang);
  return ageMs >= PROBE_STALE_AFTER_MS
    ? { kind: "stale", ageMs, label }
    : { kind: "fresh", ageMs, label };
}

/**
 * 界面上这条连接旁边该写什么。
 *
 * 从没探过、或者观测已经陈旧，都要**明说**，而不是安静地显示 status ——
 * 安静就等于让人继续把快照当事实。
 */
export function probeSummary(
  backend: Pick<ModelBackend, "last_probe_at" | "last_probe_ok" | "last_probe_detail">,
  now: number = Date.now(),
  lang: Language = "zh",
): string {
  const freshness = probeFreshness(backend, now, lang);
  if (freshness.kind === "never") return say({ zh: "从未检测", en: "Never probed" }, lang);
  const verdict = backend.last_probe_ok === true
    ? say({ zh: "通过", en: "passed" }, lang)
    : backend.last_probe_ok === false
      ? say({ zh: "被拒", en: "rejected" }, lang)
      : say({ zh: "未判定", en: "inconclusive" }, lang);
  const suffix = freshness.kind === "stale"
    ? say({ zh: "（已过期，建议重测）", en: " (stale — probe it again)" }, lang)
    : "";
  return say({ zh: "{age}检测：{verdict}{suffix}", en: "Probed {age}: {verdict}{suffix}" }, lang,
    { age: freshness.label, verdict, suffix });
}

function relativeAge(ageMs: number, lang: Language): string {
  const minutes = Math.floor(ageMs / 60000);
  if (minutes < 1) return say({ zh: "刚刚", en: "just now" }, lang);
  if (minutes < 60) return say({ zh: "{minutes} 分钟前", en: "{minutes} min ago" }, lang, { minutes });
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return say({ zh: "{hours} 小时前", en: "{hours}h ago" }, lang, { hours });
  const days = Math.floor(hours / 24);
  return say({ zh: "{days} 天前", en: "{days}d ago" }, lang, { days });
}
