/**
 * 论文类型的**取值**与它们的双语标签。
 *
 * 分类常量的 `zh` 是后端 `publication_category` 的稳定取值，供分桶和比较。
 * 界面显示从 `CATEGORY_BY_VALUE` 取双语标签，允许改名而不改变接口和历史缓存。
 *
 * 放在 `lib/` 而不是 feature 里：`lib/api.ts` 的响应类型要用这套取值，而 lib
 * 不能反过来依赖 feature（依赖方向只有 feature → lib 一条）。
 */
import type { Phrase } from "@/shared/i18n";

export const CATEGORY_JOURNAL: Phrase = { zh: "期刊文章", en: "Journal article" };
export const CATEGORY_CHINESE_JOURNAL: Phrase = { zh: "中文期刊", en: "Chinese-language journal" };
export const CATEGORY_THESIS: Phrase = { zh: "学位论文", en: "Thesis / dissertation" };
export const CATEGORY_PREPRINT: Phrase = { zh: "预印本", en: "Preprint" };
export const CATEGORY_BOOK: Phrase = { zh: "书籍", en: "Book" };
export const CATEGORY_CONFERENCE: Phrase = { zh: "会议文章", en: "Conference paper" };
export const CATEGORY_OTHER: Phrase = { zh: "其他", en: "Other" };

/** 标签页顺序：英文期刊在前（默认选中），其余按资料类型由正式到非正式排。 */
export const CATEGORIES = [
  CATEGORY_JOURNAL,
  CATEGORY_CHINESE_JOURNAL,
  CATEGORY_THESIS,
  CATEGORY_PREPRINT,
  CATEGORY_BOOK,
  CATEGORY_CONFERENCE,
  CATEGORY_OTHER,
] as const satisfies readonly Phrase[];

/** 后端 `publication_category` 的取值集合 —— 由词条推出来，不另写一份。 */
export type PublicationCategory = (typeof CATEGORIES)[number]["zh"];

/** 取值 → 词条，界面上按取值取当前语言的标签。 */
export const CATEGORY_BY_VALUE: Record<PublicationCategory, Phrase> = {
  [CATEGORY_JOURNAL.zh]: { zh: "英文期刊", en: "English-language journal" },
  [CATEGORY_CHINESE_JOURNAL.zh]: CATEGORY_CHINESE_JOURNAL,
  [CATEGORY_THESIS.zh]: CATEGORY_THESIS,
  [CATEGORY_PREPRINT.zh]: CATEGORY_PREPRINT,
  [CATEGORY_BOOK.zh]: CATEGORY_BOOK,
  [CATEGORY_CONFERENCE.zh]: CATEGORY_CONFERENCE,
  [CATEGORY_OTHER.zh]: CATEGORY_OTHER,
};

/** Both journal tabs can use journal metrics when the source provides them. */
export function isJournalCategory(category: PublicationCategory): boolean {
  return category === CATEGORY_JOURNAL.zh || category === CATEGORY_CHINESE_JOURNAL.zh;
}
