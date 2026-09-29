import type { DefaultLanding } from "@/lib/api";
import type { Phrase } from "@/shared/i18n";

/**
 * 「打开先看什么」的选项 —— **一份**。
 *
 * 设置页和开场那一步都要列它。各写一份的话，两处会各自演化：加了一个落地页
 * 只在一处出现，而两边看起来都对。
 */
export const LANDING_OPTIONS: Array<{ value: DefaultLanding; label: Phrase }> = [
  { value: "feed", label: { zh: "科研资讯 —— 今天你这个领域发生了什么", en: "Research feed — what happened in your field today" } },
  { value: "last_session", label: { zh: "上次的会话", en: "Last active Session" } },
  { value: "projects", label: { zh: "项目列表", en: "Projects" } },
];
