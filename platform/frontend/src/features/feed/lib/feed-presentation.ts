import type { FeedItem, FeedItemKind } from "@/lib/api";
import type { Language } from "../../../shared/i18n/language.ts";
import { fc } from "./feed-copy.ts";

/**
 * 资讯卡的纯呈现逻辑。
 *
 * 抽成纯函数是为了能被测到：这个仓库没有渲染测试（`npm test` 跑的是 node:test
 * + strip-types，只收集 `.test.ts`），所以任何值得验的判断都必须先离开 .tsx。
 */

/**
 * 卡片上的类型徽章。
 *
 * `lang` 一路当参数传，不在函数里读设置 —— 同 `relativeTime` 收 `now`
 * 的理由（见下）。文案本体在 `feed-copy.ts` 那张表里。
 */
export function kindLabel(kind: FeedItemKind, lang: Language): string {
  return fc(`kind.${kind}` as const, lang);
}

export const KIND_BADGE: Record<FeedItemKind, "accent" | "info" | "warning" | "muted" | "success"> = {
  paper: "info",
  release: "success",
  deadline: "warning",
  news: "muted",
  digest: "accent",
  post: "success",
};

/**
 * 卡片上那行出处。
 *
 * 顺序是刻意的：**期刊/会议名在前**，采集源在后。读者关心的是"这东西发在
 * 哪儿"，不是"我们从哪个管子里捞到的"。用户分享的条目署分享人。
 */
export function attribution(item: FeedItem, lang: Language): string {
  const parts: string[] = [];
  if (item.kind === "post" && item.author_display_name) {
    parts.push(fc("attribution.shared_by", lang, { name: item.author_display_name }));
  }
  if (item.venue) parts.push(item.venue);
  else if (item.source_name) parts.push(item.source_name);
  if (item.authors.length > 0) parts.push(authorLine(item.authors));
  return parts.join(" · ");
}

/** 作者行：超过三个就折成 et al.，别让一行作者把卡片撑成三行。 */
export function authorLine(authors: string[]): string {
  if (authors.length === 0) return "";
  if (authors.length <= 3) return authors.join(", ");
  return `${authors.slice(0, 3).join(", ")} et al.`;
}

/**
 * 相对时间。
 *
 * `now` 必须是**传进来的参数**，不是函数里读的时钟 —— 否则这个判断自己依赖
 * 真实时间，测试就只能在某些时刻通过。
 */
export function relativeTime(iso: string | null, now: Date, lang: Language): string {
  if (!iso) return "";
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "";
  const minutes = Math.round((now.getTime() - then.getTime()) / 60000);
  if (minutes < 1) return fc("time.just_now", lang);
  if (minutes < 60) return fc("time.minutes", lang, { n: minutes });
  const hours = Math.round(minutes / 60);
  if (hours < 24) return fc("time.hours", lang, { n: hours });
  const days = Math.round(hours / 24);
  if (days < 30) return fc("time.days", lang, { n: days });
  return then.toISOString().slice(0, 10);
}

/**
 * 截稿倒计时。deadline 卡上"还剩几天"比"什么时候截止"更能驱动动作。
 *
 * ## 为什么按日历天算，而且按 UTC
 *
 * 第一版拿两个时刻相减再 `Math.ceil`，于是"今晚 20:00 截止"（距now 8 小时）
 * 被算成 1 天 → 显示"明天截止"。用户据此以为还有一整天。差之毫厘的地方在于：
 * 倒计时问的是**跨过几个日界**，不是**过了几个 24 小时**。
 *
 * 用 UTC 日历而不是本地日历：本地日历会让同一个截稿日对不同时区的两个人
 * 显示不同的剩余天数，而截稿日本来就是发布方按某个固定时区（多为 AoE）定的
 * 一个日子。顺带，这也让这个函数的判据不依赖运行环境 —— 否则它在 CI 上绿、
 * 在某些时区红。
 */
export function deadlineCountdown(item: FeedItem, now: Date, lang: Language): string {
  const raw = item.extra?.["deadline_at"];
  if (typeof raw !== "string") return "";
  const when = new Date(raw);
  if (Number.isNaN(when.getTime())) return "";
  const utcDay = (d: Date) => Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
  const days = Math.round((utcDay(when) - utcDay(now)) / 86400000);
  if (days < 0) return fc("deadline.passed", lang);
  if (days === 0) return fc("deadline.today", lang);
  if (days === 1) return fc("deadline.tomorrow", lang);
  return fc("deadline.days_left", lang, { n: days });
}

/**
 * 卡片摘要的显示长度。列表里长摘要会把一屏挤到只剩两张卡。
 */
export function truncate(text: string | null, limit: number): string {
  const value = (text ?? "").trim();
  if (value.length <= limit) return value;
  return `${value.slice(0, limit).trimEnd()}…`;
}

/**
 * 卡片上要显示的域标签。
 *
 * ## 为什么不能直接 map(domain_labels)
 *
 * 两个不同的分类可以有**同一个人读名**：`cs.LG` 和 `stat.ML` 都叫
 * "Machine Learning"。直接按标签渲染有两个后果：卡片上出现两个一模一样的
 * 标签（读者看不出区别，也没有任何信息），以及 React 的重复 key ——
 * 而 React 明确说重复 key 下"子节点可能被复制或被丢弃"。
 *
 * 实测（2026-08-22，部署后打开浏览器才看见）：一篇同属 cs.LG 与 stat.ML 的
 * 论文触发 `Encountered two children with the same key`。355 条前端测试
 * 和 typecheck 全绿 —— 它只在真实数据里才现形。
 *
 * 所以按 slug 配对、按标签去重：key 取 slug（一定唯一），显示取标签。
 */
export function visibleDomainTags(
  item: FeedItem,
  limit: number,
): Array<{ domain: string; label: string }> {
  const seen = new Set<string>();
  const tags: Array<{ domain: string; label: string }> = [];
  item.domains.forEach((domain, index) => {
    const label = item.domain_labels[index] ?? domain;
    if (seen.has(label)) return;
    seen.add(label);
    tags.push({ domain, label });
  });
  return tags.slice(0, limit);
}

/**
 * 这条内容能不能点开外链。
 *
 * 域周报是平台自己生成的，没有外部出处（`url` 为空）—— 给它渲染一个死链接
 * 比不渲染更糟。
 */
export function hasExternalLink(item: FeedItem): boolean {
  return typeof item.url === "string" && item.url.length > 0;
}

/**
 * 首页顶部那句话。**说实话**：没有个性化依据时不假装这是为你挑的。
 */
export function todayHeadline(params: {
  personalized: boolean;
  onboarded: boolean;
  pickCount: number;
  lang: Language;
}): { title: string; subtitle: string } {
  const { lang } = params;
  if (params.pickCount === 0) {
    return {
      title: fc("today.title", lang),
      subtitle: fc("today.subtitle.filling", lang),
    };
  }
  if (!params.personalized) {
    return {
      title: fc("today.title", lang),
      subtitle: params.onboarded
        ? fc("today.subtitle.no_signal", lang)
        : fc("today.subtitle.pick_fields", lang),
    };
  }
  return {
    title: fc("today.title.personalized", lang),
    subtitle: fc("today.subtitle.personalized", lang),
  };
}
