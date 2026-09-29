"use client";

import { useEffect, useRef, useState } from "react";
import { Bookmark, BookmarkCheck, ExternalLink, X } from "lucide-react";
import { Badge } from "@/shared/ui";
import type { FeedItem } from "@/lib/api";
import { useLanguage } from "@/shared/i18n";
import { fc } from "../lib/feed-copy";
import {
  KIND_BADGE,
  attribution,
  kindLabel,
  deadlineCountdown,
  hasExternalLink,
  relativeTime,
  visibleDomainTags,
} from "../lib/feed-presentation";
import { useFeedEngagement, useFeedImpression } from "../hooks/useFeed";
import { useFeedImage } from "../hooks/useFeedImage";
import { HandoffAction } from "./HandoffAction";

/**
 * 一张资讯卡。
 *
 * 卡上四个动作里有三个是**通往研究**的，这是这个功能和任何 RSS 阅读器的
 * 分界：读到一条有用的东西之后，下一步不是"记下来以后再说"，而是当场把它
 * 接进手上的课题。
 */
export function FeedItemCard({
  item,
  reason,
  titleZh,
  summaryZh,
  todayPick = false,
  compact = false,
  layout = "list",
  summaryClamp,
  now,
  onOpenDetail,
}: {
  item: FeedItem;
  reason?: string;
  /** 标题译文，跟原题下面一行（后台按需翻译，未返回就是 undefined）。 */
  titleZh?: string;
  summaryZh?: string;
  /** 今日必读：挂在流最前的少数几条，角标强调。 */
  todayPick?: boolean;
  compact?: boolean;
  /** `grid` 才显示配图 —— 一行一条的高度里塞图，图小到没有信息量。 */
  layout?: "list" | "grid";
  /** 摘要最多显示几行（瀑布流按列动态裁剪；不传用 CSS 默认）。 */
  summaryClamp?: number;
  now: Date;
  /** 点整张卡打开详情。不传就是不可点（收藏页那种简单列表）。 */
  onOpenDetail?: () => void;
}) {
  const { record, undo } = useFeedEngagement();
  const { mutate: recordImpression } = useFeedImpression();
  const cardRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const element = cardRef.current;
    if (!element || typeof IntersectionObserver === "undefined") return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting && entry.intersectionRatio >= 0.6) {
        timer = setTimeout(() => recordImpression(item.id), 1000);
      } else if (timer) {
        clearTimeout(timer);
        timer = undefined;
      }
    }, { threshold: [0.6] });
    observer.observe(element);
    return () => { if (timer) clearTimeout(timer); observer.disconnect(); };
  }, [item.id, recordImpression]);
  // 语言在组件里读一次，往下当参数传 —— 纯函数不该自己去够上下文
  // （同 `now`：一屏所有相对时间用同一个时刻，见 feed-presentation）。
  const lang = useLanguage();
  // 图取不回来（网络断了、后端在重启）就当没有图。**不显示碎图标**：
  // 那会让用户以为是平台坏了。正常情况下这不会发生 —— 每条都给得出图，
  // 真图拿不到时后端会画一张生成封面（见 feed/thumbnail.py）。
  const [imageBroken, setImageBroken] = useState(false);
  const wantsImage = layout === "grid" && !imageBroken;
  // 代理端点要鉴权，而 `<img>` 带不了 Authorization 头 —— 必须自己取。
  // 见 useFeedImage 的 docstring（第一版直接 src=代理地址，一张都不显示）。
  const imageSrc = useFeedImage(item.id, wantsImage);
  const showImage = wantsImage && imageSrc !== null;
  const external = hasExternalLink(item);
  const countdown = item.kind === "deadline" ? deadlineCountdown(item, now, lang) : "";

  const openExternally = () => {
    if (!external) return;
    record.mutate({ itemId: item.id, action: "open" });
  };

  const className = [
    "feed-card",
    compact ? "feed-card-compact" : "",
    layout === "grid" ? "feed-card-grid" : "",
  ].filter(Boolean).join(" ");

  /**
   * 整张卡是打开详情的热区，但**卡上的按钮和链接不算** —— 点"收藏"不该
   * 顺手把详情也弹出来。用 `closest` 而不是 `stopPropagation` 逐个挡：
   * 后者要求每加一个控件都记得加一次，漏一个就是一次意外弹窗。
   */
  const onCardClick = (event: React.MouseEvent<HTMLElement>) => {
    if (!onOpenDetail) return;
    if ((event.target as HTMLElement).closest("button, a, input, select, textarea")) return;
    onOpenDetail();
  };

  return (
    <article
      ref={cardRef}
      className={className}
      onClick={onCardClick}
      // 键盘也要能打开 —— 只能鼠标点的话，这条路径对键盘用户不存在。
      role={onOpenDetail ? "button" : undefined}
      tabIndex={onOpenDetail ? 0 : undefined}
      onKeyDown={
        onOpenDetail
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                if ((event.target as HTMLElement).closest("button, a")) return;
                event.preventDefault();
                onOpenDetail();
              }
            }
          : undefined
      }
    >
      {showImage && (
        /* eslint-disable-next-line @next/next/no-img-element --
           不用 next/image：这张图**已经**是我们自己后端代理并限过尺寸的
           （image_proxy 限 3MB、只放行位图类型），再套一层 Next 优化器等于
           对自家端点再抓一遍、再缓存一份。next/image 的价值在于优化**外部
           未受控**的图，而这条路上的图早就受控了。 */
        <img
          className="feed-card-image"
          /* blob 地址，内容由后端代取 —— 不是出版商的外链。直连外链等于
             每次打开首页就向对方广播一次"这个用户在读这条"。 */
          src={imageSrc ?? ""}
          alt=""
          /* ⚠️ **不要加 `loading="lazy"`**。
             它在这条路上不但没用，还会让图根本不显示：图是 useFeedImage 用
             fetch 取回来的，`<img>` 拿到 blob 时网络请求**已经发生过**了，
             惰性加载只推迟解码、省不下任何东西。而实测（2026-08-23，真部署）
             带上它之后元素永远停在 `complete: false`、`naturalWidth: 0`，
             同一个 blob 换成新建的 `Image()` 却能正常加载出 90×90。
             这是从"直接 src=外链"那一版照抄过来的残留 —— 换了取图方式，
             这个属性的前提就没了。 */
          decoding="async"
          onError={() => setImageBroken(true)}
        />
      )}
      <div className="feed-card-head">
        {todayPick && <Badge kind="accent">{fc("badge.today_pick", lang)}</Badge>}
        <Badge kind={KIND_BADGE[item.kind]}>{kindLabel(item.kind, lang)}</Badge>
        {countdown && <Badge kind="warning">{countdown}</Badge>}
        {visibleDomainTags(item, 2).map((tag) => (
          // key 取 slug 不取标签：`cs.LG` 与 `stat.ML` 的人读名都是
          // "Machine Learning"，按标签当 key 会重复。
          <Badge key={tag.domain} kind="muted">
            {tag.label}
          </Badge>
        ))}
        <span className="feed-card-time">{relativeTime(item.published_at, now, lang)}</span>
      </div>

      {external ? (
        <a
          className="feed-card-title"
          href={item.url ?? "#"}
          target="_blank"
          rel="noopener noreferrer"
          onClick={openExternally}
        >
          {item.title}
          <ExternalLink size={13} aria-hidden />
        </a>
      ) : (
        // 域周报没有外部出处 —— 渲染一个死链接比不渲染更糟。
        <h3 className="feed-card-title feed-card-title-plain">{item.title}</h3>
      )}
      {titleZh && <p className="feed-card-title-zh">{titleZh}</p>}

      {reason && <p className="feed-card-reason">{reason}</p>}

      {(summaryZh || item.summary) && (
        <p
          className="feed-card-summary"
          style={summaryClamp ? { WebkitLineClamp: summaryClamp } : undefined}
        >
          {summaryZh || item.summary}
        </p>
      )}

      <div className="feed-card-foot">
        <span className="feed-card-attribution">{attribution(item, lang)}</span>
        <div className="feed-card-actions">
          <button
            type="button"
            className="feed-action"
            aria-pressed={item.saved}
            title={item.saved ? fc("action.unsave", lang) : fc("action.save", lang)}
            onClick={() =>
              item.saved
                ? undo.mutate({ itemId: item.id, action: "save" })
                : record.mutate({ itemId: item.id, action: "save" })
            }
          >
            {item.saved ? <BookmarkCheck size={15} /> : <Bookmark size={15} />}
            <span>{item.saved ? fc("action.saved", lang) : fc("action.save", lang)}</span>
          </button>
          {/* 从一条资讯直通一个研究 —— 别家的资讯产品到链接为止，这里能接
              到执行：带着这条内容开一个会话问"它对我的假设有什么影响"。 */}
          <HandoffAction item={item} onOpened={() => record.mutate({ itemId: item.id, action: "open" })} />
          <button
            type="button"
            className="feed-action feed-action-quiet"
            title={fc("action.dismiss.hint", lang)}
            onClick={() => record.mutate({ itemId: item.id, action: "dismiss" })}
          >
            <X size={15} />
            <span>{fc("action.dismiss", lang)}</span>
          </button>
        </div>
      </div>
    </article>
  );
}
