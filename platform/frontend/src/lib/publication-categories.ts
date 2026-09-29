/**
 * 论文类型的**取值**与它们的双语标签。
 *
 * 同一条词条里放着两样东西：`zh` 是后端 `publication_category` 的取值（数据，
 * 前端拿它分桶和比较），`en` 是英文界面上要显示的字。写成一处的理由是它们本来
 * 就是同一件事的两种用法 —— 分两处写就一定会漂：改了展示文案忘了数据取值，
 * 筛选就对不上了。
 *
 * 放在 `lib/` 而不是 feature 里：`lib/api.ts` 的响应类型要用这套取值，而 lib
 * 不能反过来依赖 feature（依赖方向只有 feature → lib 一条）。
 */
import type { Phrase } from "@/shared/i18n";

export const CATEGORY_JOURNAL: Phrase = { zh: "期刊文章", en: "Journal article" };
export const CATEGORY_PREPRINT: Phrase = { zh: "预印本", en: "Preprint" };
export const CATEGORY_BOOK: Phrase = { zh: "书籍", en: "Book" };
export const CATEGORY_CONFERENCE: Phrase = { zh: "会议文章", en: "Conference paper" };
export const CATEGORY_OTHER: Phrase = { zh: "其他", en: "Other" };

/** 标签页顺序：期刊文章在前（默认选中），其余按资料类型由正式到非正式排。 */
export const CATEGORIES = [
  CATEGORY_JOURNAL,
  CATEGORY_PREPRINT,
  CATEGORY_BOOK,
  CATEGORY_CONFERENCE,
  CATEGORY_OTHER,
] as const satisfies readonly Phrase[];

/** 后端 `publication_category` 的取值集合 —— 由词条推出来，不另写一份。 */
export type PublicationCategory = (typeof CATEGORIES)[number]["zh"];

/** 取值 → 词条，界面上按取值取当前语言的标签。 */
export const CATEGORY_BY_VALUE: Record<PublicationCategory, Phrase> = {
  [CATEGORY_JOURNAL.zh]: CATEGORY_JOURNAL,
  [CATEGORY_PREPRINT.zh]: CATEGORY_PREPRINT,
  [CATEGORY_BOOK.zh]: CATEGORY_BOOK,
  [CATEGORY_CONFERENCE.zh]: CATEGORY_CONFERENCE,
  [CATEGORY_OTHER.zh]: CATEGORY_OTHER,
};
