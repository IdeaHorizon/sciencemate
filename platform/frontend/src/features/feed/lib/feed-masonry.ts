/** Place each card below the shortest column, using measured border-box heights. */
export function layoutFeedCards(heights: number[], columnCount: number, gap: number) {
  const bottoms = Array<number>(Math.max(1, columnCount)).fill(0);
  const positions = heights.map((height) => {
    let column = 0;
    for (let index = 1; index < bottoms.length; index++) {
      if (bottoms[index] < bottoms[column]) column = index;
    }
    const top = bottoms[column];
    bottoms[column] = top + height + gap;
    return { column, top };
  });
  return { positions, height: Math.max(0, ...bottoms) - (heights.length ? gap : 0) };
}
