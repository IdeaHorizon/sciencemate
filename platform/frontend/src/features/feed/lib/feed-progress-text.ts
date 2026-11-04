import type { FeedProgress } from "../../../lib/api.ts";
import { say, type Language } from "../../../shared/i18n/language.ts";

/** 推荐页空着的时候，下面那一句：此刻推荐在哪一步、还会不会有东西来。纯函数，按行为测。 */
export function feedProgressText(progress: FeedProgress, lang: Language): string {
  // 有可推荐的内容、只是中文译文还没好（中文界面只显示译好的卡片）：如实说，别说成「没有相关内容」。
  // 11-04 实测：306 条可推荐、0 条译好，页面上写的是「暂时没有与你的订阅和项目相关的内容」。
  if (lang === "zh" && progress.recommendable > 0 && progress.translation_pending > 0) {
    return say({ zh: "有 {n} 条推荐内容的中文译文还没准备好，准备好后会显示在这里。",
                 en: "{n} recommended items are waiting for their Chinese translation and will appear here once it is ready." },
               lang, { n: progress.translation_pending });
  }
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
