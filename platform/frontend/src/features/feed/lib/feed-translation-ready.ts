import { needsTranslation } from "./feed-translation-queue.ts";

/** Missing abstracts have no text to translate; existing abstracts must be ready. */
export function feedTranslationReady(
  item: { title: string; summary?: string | null },
  translation?: { titleZh?: string; abstractZh?: string },
): boolean {
  return (!needsTranslation(item.title) || Boolean(translation?.titleZh?.trim()))
    && (!needsTranslation(item.summary) || Boolean(translation?.abstractZh?.trim()));
}
