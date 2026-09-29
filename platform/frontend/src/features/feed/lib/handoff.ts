import type { FeedItem } from "@/lib/api";
import type { Language } from "../../../shared/i18n/language.ts";
import { fc } from "./feed-copy.ts";

/**
 * 把一条资讯交给一个课题时，输入框里预置什么。
 *
 * ## 为什么这条路径存在
 *
 * 任何 RSS 阅读器都能把论文列给你看，到链接为止。这里能接到执行：读到一条
 * 有用的东西，当场带着它开一个会话问「它对我的假设有什么影响」。**从一条
 * 资讯到一个可审计的研究，这条路只有这个平台走得通。**
 *
 * ## 为什么必须带链接
 *
 * 只带标题的话，agent 只能凭标题猜内容 —— 那正是这个平台最不该做的事。
 * 带上出处，它可以自己去取全文；带上摘要，它在取全文之前就有据可依。
 *
 * 末尾**不替用户把问题写好**：他要问什么只有他知道，替他写一句会让他删掉
 * 重写，或者更糟 —— 直接发出去，然后得到一个他没想问的答案。
 */
export function handoffDraft(item: FeedItem, lang: Language): string {
  const lines: string[] = [fc("handoff.draft.intro", lang), ""];
  lines.push(fc("handoff.draft.title", lang, { value: item.title }));
  if (item.url) lines.push(fc("handoff.draft.url", lang, { value: item.url }));
  if (item.venue) lines.push(fc("handoff.draft.venue", lang, { value: item.venue }));
  if (item.authors.length > 0) {
    const names = `${item.authors.slice(0, 5).join(", ")}${item.authors.length > 5 ? " et al." : ""}`;
    lines.push(fc("handoff.draft.authors", lang, { value: names }));
  }
  if (item.summary) {
    lines.push("", fc("handoff.draft.summary", lang, { value: item.summary.slice(0, 800) }));
  }
  lines.push("", "");
  return lines.join("\n");
}

/**
 * 交接目的地。`draft` 走 query，由 SessionWorkspace 取一次当输入框初值。
 */
export function handoffHref(projectId: string, sessionId: string, draft: string): string {
  const query = new URLSearchParams({ draft });
  return `/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}?${query}`;
}

/**
 * 会话标题：交接过去之后，侧栏上要能一眼看出这个会话在谈哪一条。
 */
export function handoffSessionTitle(item: FeedItem): string {
  const title = item.title.trim();
  return title.length > 60 ? `${title.slice(0, 60)}…` : title;
}
