"use client";

/**
 * 「这里干嘛的」—— 贴在真东西旁边的小气泡。
 *
 * ## 为什么贴在侧栏上，而不是再来几张卡片
 *
 * 卡片能讲，但讲完人还是不知道那句话说的是屏幕上的哪一块。气泡锚在真实的导航项
 * 上，说的和指的是同一个东西 —— 而且用户在读的时候，界面就在旁边亮着。
 *
 * ## 锚点找不到就跳过这一站
 *
 * 不画悬在半空的气泡。侧栏在项目页里会换成另一套，窄屏下某几项也可能不在 ——
 * 那时这一站本来就没有东西可指。
 */

import { useEffect, useState } from "react";
import { useT } from "@/shared/i18n";
import type { TourStop } from "./lib/tour-stops";

export function GuidedTour({ stops, onDone }: { stops: TourStop[]; onDone: () => void }) {
  const t = useT();
  const [index, setIndex] = useState(0);
  const [box, setBox] = useState<{ top: number; left: number } | null>(null);
  const stop = stops[index];

  useEffect(() => {
    if (!stop) { onDone(); return; }
    const anchor = document.querySelector(stop.anchor);
    if (!anchor) {
      setIndex((current) => current + 1);
      return;
    }
    const rect = anchor.getBoundingClientRect();
    // 贴在它右边；太靠下就往上收，别让气泡掉出屏幕。
    const top = Math.min(Math.max(12, rect.top - 6), Math.max(12, window.innerHeight - 190));
    setBox({ top, left: rect.right + 12 });
  }, [stop, onDone]);

  if (!stop || !box) return null;
  const last = index === stops.length - 1;

  return (
    <div className="tour-bubble" style={{ top: box.top, left: box.left }} role="dialog" aria-live="polite">
      <strong>{t(stop.title)}</strong>
      <p>{t(stop.body)}</p>
      <footer>
        <button type="button" className="onboarding-skip" onClick={onDone}>{t({ zh: "不用看了", en: "Skip" })}</button>
        <span className="tour-count">{index + 1} / {stops.length}</span>
        <button type="button" className="tour-next" onClick={() => (last ? onDone() : setIndex(index + 1))}>
          {last ? t({ zh: "知道了", en: "Done" }) : t({ zh: "下一个", en: "Next" })}
        </button>
      </footer>
    </div>
  );
}
