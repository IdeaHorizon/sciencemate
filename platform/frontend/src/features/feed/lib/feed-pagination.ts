/** Keep one page ahead of scrolling; refill short translated pages up to 63 rows. */
export function shouldPreloadFeedPage(loaded: number, visible: number): boolean {
  return loaded > 0 && (loaded < 42 || (loaded < 63 && visible < 21));
}
