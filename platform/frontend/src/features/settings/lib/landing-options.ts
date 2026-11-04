import type { DefaultLanding } from "@/lib/api";
import type { Phrase } from "@/shared/i18n";

/**
 * 「启动时显示」的选项 —— **一份**。
 *
 * 设置页和新手引导那一步都要列它。各写一份的话，两处会各自演化：加了一个落地页
 * 只在一处出现，而两边看起来都对。
 *
 * `hint` 只在新手引导里显示（第一次见到这个选项的人需要知道它是什么）；设置页的
 * 下拉只列名字。
 */
export const LANDING_OPTIONS: Array<{ value: DefaultLanding; label: Phrase; hint?: Phrase }> = [
  { value: "feed", label: { zh: "科研资讯", en: "Research feed" }, hint: { zh: "你所在领域的最新动态", en: "What's new in your field" } },
  { value: "last_session", label: { zh: "上次的会话", en: "Last session" } },
  { value: "projects", label: { zh: "项目列表", en: "Projects" } },
];
