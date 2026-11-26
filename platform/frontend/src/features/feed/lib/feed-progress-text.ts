import type { FeedProgress, FeedToday } from "../../../lib/api.ts";
import { say, type Language } from "../../../shared/i18n/language.ts";
import { fc } from "./feed-copy.ts";

/** 推荐页空着的时候，下面那一句：此刻推荐在哪一步、还会不会有东西来。纯函数，按行为测。 */
export function feedEmptyState(page: FeedToday, selection: Record<string, boolean>, lang: Language) {
  const title = fc("home.empty.title", lang);
  const choices = Object.keys(selection);
  const selected = choices.filter((source) => selection[source]);
  if (!selected.length) {
    return { title, hint: say({ zh: "请勾选要查看的推荐来源。", en: "Select a recommendation source to view." }, lang) };
  }
  if (page.empty_reason) return { title, hint: page.empty_reason };

  const progress = page.progress;
  const filtered = selected.length < choices.length;
  const groups = (progress?.source_groups ?? []).filter((group) =>
    group.sources.some((source) => selected.includes(source)));
  // Every card belongs to one source combination, even when it matches several selected sources.
  const total = filtered ? groups.reduce((n, group) => n + group.total, 0) : progress?.recommendable ?? 0;
  const ready = filtered ? groups.reduce((n, group) => n + group.translated, 0) : progress?.translated ?? 0;
  const pending = filtered ? total - ready : progress?.translation_pending ?? 0;
  if (lang === "zh" && total > 0 && pending === total) {
    return { title: fc("home.empty.refreshing_title", lang), hint: say({
      zh: "有 {n} 条推荐内容的中文译文还没准备好，准备好后会显示在这里。",
      en: "{n} recommended items are waiting for their Chinese translation and will appear here once it is ready.",
    }, lang, { n: total }) };
  }
  if (total > 0) {
    return { title, hint: say({ zh: "请刷新推荐，重新加载当前列表。", en: "Refresh recommendations to reload this list." }, lang) };
  }
  if (filtered) {
    return { title, hint: say({ zh: "当前勾选的来源暂无推荐内容。", en: "There are no recommendations for the selected sources yet." }, lang) };
  }
  return {
    title: page.refreshing ? fc("home.empty.refreshing_title", lang) : title,
    hint: progress ? collectionProgressText(progress, lang) : fc("home.empty.hint", lang),
  };
}

function collectionProgressText(progress: FeedProgress, lang: Language): string {
  switch (progress.phase) {
    case "searching": return say({ zh: "正在搜索新内容…", en: "Searching for new content…" }, lang);
    case "preparing": return say({ zh: "正在整理摘要和配图，准备好的内容会陆续显示。", en: "Preparing abstracts and images. Items appear as they are ready." }, lang);
    case "queued": return say({ zh: "还有内容在处理中，推荐列表会继续更新。", en: "Some items are still being processed. The list will keep updating." }, lang);
    case "failed": return say({ zh: "无法根据项目查找内容，请检查模型连接。当前推荐可能不完整。", en: "Could not search based on your projects. Check your model connection; recommendations may be incomplete." }, lang);
    default: return progress.collected === 0
      ? (say({ zh: "暂时没有与你的订阅和项目相关的内容。", en: "No content matches your subscriptions or projects yet." }, lang))
      : (say({ zh: "已收集的内容暂时都没有通过相关性筛选。", en: "Nothing collected so far has passed the relevance filter." }, lang));
  }
}
