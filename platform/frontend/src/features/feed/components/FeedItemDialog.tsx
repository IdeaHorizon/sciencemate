"use client";

import { useEffect, useRef } from "react";
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
import { useFeedEngagement } from "../hooks/useFeed";
import { useFeedImage } from "../hooks/useFeedImage";
import { HandoffAction } from "./HandoffAction";

/**
 * 点开一张卡之后的详情窗。
 *
 * ## 它解决的是什么
 *
 * 卡片上的摘要是被截断的（列表 180 字、图卡 320 字），因为一屏要放得下几条。
 * 但"这条到底讲什么、值不值得点原文"往往就差那被截掉的半段。此前唯一的出路
 * 是直接跳去出版商页面 —— 那是一次页面跳转、一次对出版商的曝光，只为了读完
 * 一段摘要。
 *
 * 所以：**图更大、摘要完整、动作都在**。真要读原文再点出去。
 *
 * ## 为什么不做成路由
 *
 * 详情是一次**临时查看**，不是一个值得进历史、值得分享的位置 —— 资讯条目的
 * 可分享地址是它的原文出处，不是我们的一个中转页。做成路由等于替它造了第二个
 * 身份，而那个身份指向的内容随时会被采集覆盖。
 */
export function FeedItemDialog({
  item,
  reason,
  now,
  titleZh,
  summaryZh,
  onClose,
}: {
  item: FeedItem;
  reason?: string;
  now: Date;
  titleZh?: string;
  summaryZh?: string;
  onClose: () => void;
}) {
  const { record, undo } = useFeedEngagement();
  const lang = useLanguage();
  const imageSrc = useFeedImage(item.id);
  const closeRef = useRef<HTMLButtonElement>(null);
  const external = hasExternalLink(item);
  const countdown = item.kind === "deadline" ? deadlineCountdown(item, now, lang) : "";

  useEffect(() => {
    // Esc 关掉。一个只能靠鼠标点右上角叉才能关的浮层，键盘用户出不去。
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    // 焦点移进来，否则 Tab 会跑到浮层背后那些看不见的卡片上。
    closeRef.current?.focus();
    // 浮层开着时锁住背景滚动 —— 不锁的话滚轮会滚背后的列表，
    // 而用户以为自己在滚详情。
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
    };
  }, [onClose]);

  return (
    // 点背景关闭。`onClick` 挂在背景上，内容区自己 stopPropagation ——
    // 否则点详情里的任何地方都会把它关掉。
    <div className="feed-dialog-backdrop" onClick={onClose}>
      <div
        className="feed-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={item.title}
        onClick={(event) => event.stopPropagation()}
      >
        <button
          type="button"
          ref={closeRef}
          className="feed-dialog-close"
          aria-label={fc("action.close", lang)}
          onClick={onClose}
        >
          <X size={18} />
        </button>

        {imageSrc && (
          // eslint-disable-next-line @next/next/no-img-element -- 见 FeedItemCard
          <img className="feed-dialog-image" src={imageSrc} alt="" decoding="async" />
        )}

        <div className="feed-dialog-body">
          <div className="feed-card-head">
            <Badge kind={KIND_BADGE[item.kind]}>{kindLabel(item.kind, lang)}</Badge>
            {countdown && <Badge kind="warning">{countdown}</Badge>}
            {visibleDomainTags(item, 4).map((tag) => (
              <Badge key={tag.domain} kind="muted">{tag.label}</Badge>
            ))}
            <span className="feed-card-time">{relativeTime(item.published_at, now, lang)}</span>
          </div>

          {external ? (
            // 标题本身就能点去原文 —— 卡片上能点，打开详情后反而不能点，
            // 是一处不一致。有外链时标题就是链接，没外链（周报等）保持纯文本。
            <a
              className="feed-dialog-title"
              href={item.url ?? "#"}
              target="_blank"
              rel="noopener noreferrer"
              onClick={() => record.mutate({ itemId: item.id, action: "open" })}
            >
              {item.title}
              <ExternalLink size={14} aria-hidden />
            </a>
          ) : (
            <h2 className="feed-dialog-title">{item.title}</h2>
          )}
          {titleZh && <p className="feed-card-title-zh">{titleZh}</p>}

          {reason && <p className="feed-card-reason">{reason}</p>}

          <p className="feed-dialog-attribution">{attribution(item, lang)}</p>

          {/* 摘要**不截断** —— 这正是打开详情要看的东西。 */}
          {(summaryZh || item.summary) && <p className="feed-dialog-summary">{summaryZh || item.summary}</p>}

          <div className="feed-dialog-actions">
            {external && (
              <a
                className="feed-action"
                href={item.url ?? "#"}
                target="_blank"
                rel="noopener noreferrer"
                onClick={() => record.mutate({ itemId: item.id, action: "open" })}
              >
                <ExternalLink size={15} /> <span>{fc("action.open_source", lang)}</span>
              </a>
            )}
            <button
              type="button"
              className="feed-action"
              aria-pressed={item.saved}
              onClick={() =>
                item.saved
                  ? undo.mutate({ itemId: item.id, action: "save" })
                  : record.mutate({ itemId: item.id, action: "save" })
              }
            >
              {item.saved ? <BookmarkCheck size={15} /> : <Bookmark size={15} />}
              <span>{item.saved ? fc("action.saved", lang) : fc("action.save", lang)}</span>
            </button>
            <HandoffAction
              item={item}
              onOpened={() => record.mutate({ itemId: item.id, action: "open" })}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
