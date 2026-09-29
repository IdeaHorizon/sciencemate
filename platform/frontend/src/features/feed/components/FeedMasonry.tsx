"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { FeedCard } from "@/lib/api";
import { FeedItemCard } from "./FeedItemCard";
import type { FeedTranslation } from "../hooks/useFeedTranslation";

/** 摘要最多显示行数（接近完整）。 */
const MAX_CLAMP = 6;
/** 摘要最少显示行数（截断的底限，别截到只剩一行让卡片不像样）。 */
const MIN_CLAMP = 3;
/** 三列底部允许的高度差（约两行摘要），超过就继续收短最高的那一列。 */
const BALANCE_THRESHOLD_PX = 48;

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

/**
 * 粗略预估一张卡的高度（相对分），只用于贪心分列时均衡列高；
 * 绝对值无关，只要「长标题/长摘要/带理由」的卡得分更高即可。
 */
function cardWeight(card: FeedCard): number {
  const item = card.item;
  let w = 100; // 配图 + head + foot 等固定部分
  w += Math.ceil((item.title?.length ?? 0) / 45) * 12; // 标题换行
  if (item.summary) {
    w += Math.min(MAX_CLAMP, Math.ceil(item.summary.length / 80)) * 10; // 摘要
  }
  if (card.reason) {
    w += Math.ceil(card.reason.length / 55) * 8; // 「为什么推给你」
  }
  return w;
}

/**
 * 瀑布流：卡片各自不等高，但三列累积总高要大致对齐。
 *
 * 手段是动态裁剪摘要 —— 最矮的列完整显示摘要，最高的列少显示几行，把底部
 * 拉平。纯 CSS（grid 等高 / column 瀑布流）都做不到「既要卡片不等高、又要
 * 列底对齐」，所以这里手动分列（JS）+ 逐列动态 line-clamp：
 *
 *  1. 轮转把卡片均分到 N 列，每列独立 flex column；
 *  2. 渲染后量每列真实高度；
 *  3. 最高的那列摘要 `clamp` 减一行，重新渲染、再量，迭代到列高差够小。
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
  const [clamps, setClamps] = useState<number[]>(() => Array(3).fill(MAX_CLAMP));
  const colRefs = useRef<(HTMLDivElement | null)[]>([]);

  // 截断到列数倍数 + 贪心最短列分列：
  // 轮转分列在卡片数非列数倍数时会让某列多一张卡、列底差出一整张卡的高度
  // （~400-500px），远超摘要裁剪的补偿能力（最多 3 行/卡）。截掉的是排序
  // 最末的 1-2 条，视觉无感；余下用贪心把高矮卡均衡到各列，再交给摘要裁剪微调。
  const columns = useMemo(() => {
    const perCol = Math.floor(cards.length / colCount);
    const kept = perCol >= 1 ? cards.slice(0, perCol * colCount) : cards;
    const heights = new Array(colCount).fill(0);
    const cols: FeedCard[][] = Array.from({ length: colCount }, () => []);
    for (const card of kept) {
      let minIdx = 0;
      for (let i = 1; i < colCount; i++) {
        if (heights[i] < heights[minIdx]) minIdx = i;
      }
      cols[minIdx].push(card);
      heights[minIdx] += cardWeight(card);
    }
    return cols;
  }, [cards, colCount]);

  // 列数变化（拖窗口跨断点）时重置 clamp。
  useEffect(() => {
    setClamps(Array(colCount).fill(MAX_CLAMP));
  }, [colCount]);

  // 渲染后量列高，把最高列的摘要减一行，迭代到列底大致对齐。
  useLayoutEffect(() => {
    if (clamps.length !== colCount) return;
    const heights = colRefs.current.map((el) => el?.offsetHeight ?? 0);
    if (heights.some((h) => !h)) return; // 首帧还没铺开
    const maxH = Math.max(...heights);
    const minH = Math.min(...heights);
    if (maxH - minH <= BALANCE_THRESHOLD_PX) return;
    const maxIdx = heights.indexOf(maxH);
    if (clamps[maxIdx] <= MIN_CLAMP) return;
    setClamps((c) => c.map((v, i) => (i === maxIdx ? v - 1 : v)));
  }, [clamps, columns, colCount]);

  return (
    <div
      className="feed-grid"
      style={{ gridTemplateColumns: `repeat(${colCount}, minmax(0, 1fr))` }}
    >
      {columns.map((colCards, colIdx) => (
        <div
          key={colIdx}
          className="feed-masonry-col"
          ref={(el) => {
            colRefs.current[colIdx] = el;
          }}
        >
          {colCards.map((card) => (
            <FeedItemCard
              key={card.item.id}
              item={card.item}
              reason={card.reason}
              titleZh={translations[card.item.id]?.titleZh}
              summaryZh={translations[card.item.id]?.abstractZh}
              todayPick={card.is_today_pick}
              layout="grid"
              summaryClamp={clamps[colIdx] ?? MAX_CLAMP}
              now={now}
              onOpenDetail={() => onOpenDetail(card.item.id)}
            />
          ))}
        </div>
      ))}
    </div>
  );
}
