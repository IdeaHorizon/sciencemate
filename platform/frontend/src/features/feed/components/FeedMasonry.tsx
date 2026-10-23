"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { FeedCard } from "@/lib/api";
import { FeedItemCard } from "./FeedItemCard";
import type { FeedTranslation } from "../hooks/useFeedTranslation";
import { layoutFeedCards } from "../lib/feed-masonry";

const MAX_CLAMP = 6;

/** 响应式列数，和 CSS 断点保持一致：3 / 2 / 1。 */
function useColumnCount(): number {
  const [cols, setCols] = useState(3);
  useEffect(() => {
    const mq2 = window.matchMedia("(max-width: 1100px)");
    const mq1 = window.matchMedia("(max-width: 700px)");
    const compute = () => setCols(mq1.matches ? 1 : mq2.matches ? 2 : 3);
    compute();
    mq2.addEventListener("change", compute);
    mq1.addEventListener("change", compute);
    return () => {
      mq2.removeEventListener("change", compute);
      mq1.removeEventListener("change", compute);
    };
  }, []);
  return cols;
}

/** Keep cards mounted while placing them by their measured height.
 * ResizeObserver covers image decoding, text changes and container resizing.
 */
export function FeedMasonry({
  cards,
  translations,
  now,
  onOpenDetail,
}: {
  cards: FeedCard[];
  translations: Record<string, FeedTranslation>;
  now: Date;
  onOpenDetail: (id: string) => void;
}) {
  const colCount = useColumnCount();
  const gridRef = useRef<HTMLDivElement>(null);
  const [layout, setLayout] = useState<ReturnType<typeof layoutFeedCards> | null>(null);

  useLayoutEffect(() => {
    const grid = gridRef.current;
    if (!grid) return;
    let frame = 0;
    const measure = () => {
      const elements = Array.from(grid.children) as HTMLElement[];
      const gap = Number.parseFloat(getComputedStyle(grid).columnGap) || 0;
      const next = layoutFeedCards(elements.map((element) => element.getBoundingClientRect().height), colCount, gap);
      setLayout((previous) => previous && previous.height === next.height
        && previous.positions.length === next.positions.length
        && previous.positions.every((position, index) => position.column === next.positions[index].column && position.top === next.positions[index].top)
        ? previous : next);
    };
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(measure);
    });
    observer.observe(grid);
    for (const element of Array.from(grid.children)) observer.observe(element);
    measure();
    return () => { cancelAnimationFrame(frame); observer.disconnect(); };
  }, [cards, colCount]);

  return (
    <div
      ref={gridRef}
      className="feed-grid feed-masonry"
      style={{ gridTemplateColumns: `repeat(${colCount}, minmax(0, 1fr))`, height: layout?.height }}
    >
      {cards.map((card, index) => {
        const position = layout?.positions[index];
        return <div key={card.item.id} className="feed-masonry-item" style={position ? {
          position: "absolute",
          width: `calc((100% - ${colCount - 1} * var(--space-3)) / ${colCount})`,
          left: `calc(${position.column} * ((100% - ${colCount - 1} * var(--space-3)) / ${colCount} + var(--space-3)))`,
          top: position.top,
        } : undefined}>
          <FeedItemCard
            item={card.item}
            reason={card.reason}
            titleZh={translations[card.item.id]?.titleZh}
            summaryZh={translations[card.item.id]?.abstractZh}
            todayPick={card.is_today_pick}
            layout="grid"
            summaryClamp={MAX_CLAMP}
            now={now}
            onOpenDetail={() => onOpenDetail(card.item.id)}
          />
        </div>;
      })}
    </div>
  );
}
