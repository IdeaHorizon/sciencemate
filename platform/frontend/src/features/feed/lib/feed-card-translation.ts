import type { Language } from "../../../shared/i18n/language.ts";

type Source = {
  id: string;
  title: string;
  summary?: string | null;
  title_zh?: string | null;
  abstract_zh?: string | null;
};

/** A saved row carries its own translations even when it has left today's feed. */
export function feedCardTranslation(
  lang: Language,
  item: Source,
  recommendation?: Source,
  live?: { titleZh?: string; abstractZh?: string },
): { titleZh?: string; summaryZh?: string } {
  if (lang !== "zh") return {};
  const sameItem = recommendation?.id === item.id ? recommendation : undefined;
  return {
    titleZh: item.title_zh?.trim()
      || (sameItem && sameItem.title === item.title ? live?.titleZh?.trim() : undefined),
    summaryZh: item.abstract_zh?.trim()
      || (sameItem && sameItem.summary === item.summary ? live?.abstractZh?.trim() : undefined),
  };
}
