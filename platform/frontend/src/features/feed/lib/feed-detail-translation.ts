/** Keep an open detail's translation when a feed refresh drops its list entry. */
export interface DetailTranslation {
  id: string;
  title: string;
  summary: string | null | undefined;
  titleZh?: string;
  summaryZh?: string;
}

export function retainDetailTranslation(
  previous: DetailTranslation | null,
  incoming: DetailTranslation,
): DetailTranslation {
  const sameItem = previous?.id === incoming.id;
  const next = {
    ...incoming,
    titleZh: incoming.titleZh?.trim() ||
      (sameItem && previous.title === incoming.title ? previous.titleZh : undefined),
    summaryZh: incoming.summaryZh?.trim() ||
      (sameItem && previous.summary === incoming.summary ? previous.summaryZh : undefined),
  };
  return previous && Object.keys(next).every(
    (key) => next[key as keyof DetailTranslation] === previous[key as keyof DetailTranslation],
  ) ? previous : next;
}
