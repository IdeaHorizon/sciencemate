"use client";

import { useEffect, useMemo, useState } from "react";
import { Bookmark, LayoutGrid, List, Search, Settings2, Sparkles, Newspaper, UserRound, Plus, X } from "lucide-react";
import { Button, Empty, Skeleton } from "@/shared/ui";
import { useFeedItems, useFeedToday, useFeedSubscriptions, useFeedSubscriptionSearch, useFeedSubscriptionItems, useSaveFeedSubscriptions } from "../hooks/useFeed";
import { useFeedTranslation } from "../hooks/useFeedTranslation";
import { useLanguage } from "@/shared/i18n";
import { fc } from "../lib/feed-copy";
import { todayHeadline } from "../lib/feed-presentation";
import {
  DEFAULT_VIEW_MODE,
  readStoredViewMode,
  VIEW_MODE_STORAGE_KEY,
  type FeedViewMode,
} from "../lib/view-mode";
import { FeedItemCard } from "./FeedItemCard";
import { FeedMasonry } from "./FeedMasonry";
import { FeedItemDialog } from "./FeedItemDialog";
import { InterestPicker } from "./InterestPicker";
import { ShareLinkForm } from "./ShareLinkForm";
import { AcademicSearch } from "@/features/academic-search";
import type { FeedJournalSubscription, FeedScholarSubscription } from "@/lib/api";

type Pane = "recommendations" | "subscriptions" | "saved" | "search";
// 学科与期刊、学者并列，且排在第一个 —— 学科是「关注哪些方向」，期刊/学者是
// 「关注谁」，三者都是订阅。学科的内容仍是原来的方向选择器（InterestPicker），
// 一个字都没改，只是从推荐页的独立入口挪进了订阅。
type SubscriptionKind = "domain" | "journal" | "scholar";

/**
 * 订阅条目的图像位：学者是圆形头像、期刊是封面。
 *
 * **没有图就留空占位**，不编假图、不留破图：占位块与真图同尺寸同形状，所以
 * 以后接上图像源时行高不会跳，界面也不用改 —— 后端开始往 `avatar_url` /
 * `cover_url` 填值，这里就会自动显示。
 */
function SubscriptionAvatar({
  kind,
  url,
}: {
  kind: "journal" | "scholar";
  url?: string | null;
}) {
  if (url) {
    return (
      <img
        className={kind === "scholar" ? "feed-sub-avatar is-scholar" : "feed-sub-avatar is-journal"}
        src={url}
        alt=""
        loading="lazy"
      />
    );
  }
  return (
    <span
      className={kind === "scholar" ? "feed-sub-avatar is-scholar is-blank" : "feed-sub-avatar is-journal is-blank"}
      aria-hidden
    >
      {kind === "scholar" ? <UserRound size={14} /> : <Newspaper size={14} />}
    </span>
  );
}

function SubscriptionPanel({ now, onboarding = false, onDone }: { now: Date; onboarding?: boolean; onDone?: () => void }) {
  const lang = useLanguage();
  const subscriptions = useFeedSubscriptions();
  const items = useFeedSubscriptionItems();
  const save = useSaveFeedSubscriptions();
  const today = useFeedToday();
  const [kind, setKind] = useState<SubscriptionKind>("domain");
  const [query, setQuery] = useState("");
  const [searchTerm, setSearchTerm] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setSearchTerm(query.trim()), 350);
    return () => clearTimeout(timer);
  }, [query]);
  const [detailId, setDetailId] = useState<string | null>(null);
  // 学科 tab 搜的是方向（由 InterestPicker 自己管搜索框），期刊/学者才走订阅搜索。
  const searchKind: "journal" | "scholar" = kind === "scholar" ? "scholar" : "journal";
  const search = useFeedSubscriptionSearch(searchKind, kind === "domain" ? "" : searchTerm);
  const current = subscriptions.data ?? { journals: [], scholars: [] };
  const resultItems = searchKind === "journal" ? (search.data?.journals ?? []) : (search.data?.scholars ?? []);
  const followedKeys = new Set([...current.journals, ...current.scholars].map((value) => value.key));
  const toggle = (entry: FeedJournalSubscription | FeedScholarSubscription) => {
    if ("impact_factor" in entry) {
      const journals = followedKeys.has(entry.key) ? current.journals.filter((value) => value.key !== entry.key) : [...current.journals, entry as FeedJournalSubscription];
      save.mutate({ ...current, journals });
    } else {
      const scholars = followedKeys.has(entry.key) ? current.scholars.filter((value) => value.key !== entry.key) : [...current.scholars, entry as FeedScholarSubscription];
      save.mutate({ ...current, scholars });
    }
  };
  const visibleItems = items.data ?? [];
  // 订阅页与推荐页共用同一套翻译：同一个条目在两个页签里不该一会儿有译文一会儿没有。
  const translations = useFeedTranslation(visibleItems.map((card) => card.item));
  const detail = detailId ? visibleItems.find((card) => card.item.id === detailId) : undefined;
  return (
    <section className="feed-section feed-subscriptions">
      {detail && <FeedItemDialog item={detail.item} reason={detail.reason} now={now}
        titleZh={translations[detail.item.id]?.titleZh}
        summaryZh={translations[detail.item.id]?.abstractZh}
        onClose={() => setDetailId(null)} />}
      <div className="feed-section-head"><div><h2 className="feed-section-title">{fc("subscriptions.title", lang)}</h2><p className="feed-section-subtitle">{fc("subscriptions.subtitle", lang)}</p></div></div>
      <div className="feed-subscription-kinds" role="tablist">
        <button type="button" className={kind === "domain" ? "feed-tab feed-tab-on" : "feed-tab"} onClick={() => { setKind("domain"); setQuery(""); }}><LayoutGrid size={14} /> {fc("subscriptions.domains", lang)}</button>
        <button type="button" className={kind === "journal" ? "feed-tab feed-tab-on" : "feed-tab"} onClick={() => { setKind("journal"); setQuery(""); }}><Newspaper size={14} /> {fc("subscriptions.journals", lang)}</button>
        <button type="button" className={kind === "scholar" ? "feed-tab feed-tab-on" : "feed-tab"} onClick={() => { setKind("scholar"); setQuery(""); }}><UserRound size={14} /> {fc("subscriptions.scholars", lang)}</button>
      </div>
      {/* 学科订阅：内容与推荐页原来的方向选择器完全一致，只是搬进了这里。 */}
      {kind === "domain" && <InterestPicker showSkip={false} onDone={() => void today.refetch()} />}
      {kind !== "domain" && <label className="feed-subscription-search"><Search size={16} aria-hidden /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={fc(kind === "journal" ? "subscriptions.search_journals" : "subscriptions.search_scholars", lang)} /></label>}
      {kind !== "domain" && query.trim() && <div className="feed-subscription-results">
        {search.isLoading && <Skeleton height={72} />}
        {resultItems.map((entry) => <div className="feed-subscription-result" key={entry.key}>{searchKind === "journal" ? <SubscriptionAvatar kind="journal" url={(entry as FeedJournalSubscription).cover_url} /> : <SubscriptionAvatar kind="scholar" url={(entry as FeedScholarSubscription).avatar_url} />}<div><strong>{entry.name}</strong>{"impact_factor" in entry && <small>{[entry.issn, entry.jcr_quartile, entry.impact_factor != null ? "IF " + entry.impact_factor : ""].filter(Boolean).join(" · ")}</small>}</div><Button variant={followedKeys.has(entry.key) ? "secondary" : "primary"} size="sm" onClick={() => toggle(entry)}>{followedKeys.has(entry.key) ? fc("subscriptions.following", lang) : <><Plus size={14} /> {fc("subscriptions.follow", lang)}</>}</Button></div>)}
      </div>}
      {(current.journals.length > 0 || current.scholars.length > 0) && <div className="feed-subscription-chips">
        {current.journals.map((entry) => <button type="button" key={entry.key} onClick={() => toggle(entry)}><SubscriptionAvatar kind="journal" url={entry.cover_url} />{entry.name} <X size={12} /></button>)}
        {current.scholars.map((entry) => <button type="button" key={entry.key} onClick={() => toggle(entry)}><SubscriptionAvatar kind="scholar" url={entry.avatar_url} />{entry.name} <X size={12} /></button>)}
      </div>}
      {onboarding && <div className="feed-subscription-onboarding-done"><Button onClick={onDone}>{fc("subscriptions.done", lang)}</Button></div>}
      {!onboarding && <h3 className="feed-section-title feed-subscription-feed-title">{fc("subscriptions.feed", lang)}</h3>}
      {!onboarding && items.isLoading && <Skeleton height={220} />}
      {!onboarding && !items.isLoading && visibleItems.length === 0 && <Empty title={fc("subscriptions.empty", lang)} hint={fc("subscriptions.empty_hint", lang)} />}
      {!onboarding && <div className="feed-list">{visibleItems.map((card) => <FeedItemCard key={card.item.id} item={card.item} reason={card.reason} now={now} layout="list" onOpenDetail={() => setDetailId(card.item.id)} />)}</div>}
    </section>
  );
}

export function FeedHome() {
  const lang = useLanguage();
  const today = useFeedToday();
  const [pane, setPane] = useState<Pane>("recommendations");
  const [onboardingActive, setOnboardingActive] = useState<boolean | null>(null);
  const [onboardingStep, setOnboardingStep] = useState<"interests" | "subscriptions">("interests");
  // 渲染期间只读一次时钟，整页所有相对时间用同一个 now —— 每张卡各读一次
  // 会让同一屏上出现「2h ago」和「3h ago」指向同一时刻。
  const now = useMemo(() => new Date(), []);

  // 排布偏好。初值取**默认值**而不是读 localStorage —— 服务端渲染时没有
  // window，读了两边首帧就不一致（React 会整棵重挂）。存下来的值在下面的
  // effect 里补上，与 SessionWorkspace 的右栏宽度同一个处理。
  const [viewMode, setViewMode] = useState<FeedViewMode>(DEFAULT_VIEW_MODE);
  useEffect(() => {
    setViewMode(readStoredViewMode(typeof window === "undefined" ? null : window.localStorage));
  }, []);
  const chooseViewMode = (mode: FeedViewMode) => {
    setViewMode(mode);
    try {
      window.localStorage.setItem(VIEW_MODE_STORAGE_KEY, mode);
    } catch {
      // 隐私模式下写不进去。记不住排布不该让切换本身失效。
    }
  };

  const saved = useFeedItems({ saved: true, limit: 50 });

  const data = today.data;
  useEffect(() => {
    if (data && onboardingActive === null) setOnboardingActive(!data.onboarded);
  }, [data, onboardingActive]);

  // 后台按需翻译英文标题（复用学术搜索的 /literature/translate）。
  const translations = useFeedTranslation(today.data?.feed.map((c) => c.item) ?? []);

  // 打开的是哪一条详情。存 id 而不是整条 item：列表刷新之后（收藏了、
  // 忽略了）要显示的是**新的**那一份，存副本就会停在打开那一刻的样子。
  const [detailId, setDetailId] = useState<string | null>(null);

  if (today.isLoading) return <Skeleton height={420} />;

  if (today.isError) {
    return (
      <Empty
        title={fc("home.error.title", lang)}
        hint={today.error instanceof Error ? today.error.message : fc("home.error.hint", lang)}
      />
    );
  }

  const needsOnboarding = onboardingActive ?? (data ? !data.onboarded : false);
  const headline = todayHeadline({
    personalized: data?.personalized ?? false,
    onboarded: data?.onboarded ?? false,
    pickCount: data?.feed?.filter((c) => c.is_today_pick).length ?? 0,
      lang,
  });

  // 新用户第一次进来直接看选择界面 —— 不是弹窗盖在一个他还看不懂的列表上。
  if (needsOnboarding) {
    return (
      <div className="feed-onboarding">
        <header>
          <Sparkles size={18} aria-hidden />
          <div>
            <h2>{fc("home.onboarding.title", lang)}</h2>
            <p>
              {fc("home.onboarding.subtitle", lang)}
            </p>
          </div>
        </header>
        {onboardingStep === "interests" ? (
          <InterestPicker onDone={() => setOnboardingStep("subscriptions")} />
        ) : (
          <SubscriptionPanel onboarding now={now} onDone={() => { setOnboardingActive(false); void today.refetch(); }} />
        )}
      </div>
    );
  }

  // 从**当前**数据里找，不存副本 —— 收藏之后详情里的按钮要跟着变。
  const allVisible = [
    ...(data?.feed.map((c) => c.item) ?? []),
    ...(saved.data ?? []),
  ];
  const detailItem = detailId ? allVisible.find((i) => i.id === detailId) ?? null : null;
  const detailReason = detailId
    ? data?.feed.find((c) => c.item.id === detailId)?.reason
    : undefined;

  return (
    <div className="feed-home">
      {detailItem && (
        <FeedItemDialog
          item={detailItem}
          reason={detailReason}
          now={now}
          onClose={() => setDetailId(null)}
        />
      )}
      <nav className="feed-tabs" aria-label={fc("home.tabs.label", lang)}>
        <button
          type="button"
          className={pane === "recommendations" ? "feed-tab feed-tab-on" : "feed-tab"}
          onClick={() => setPane("recommendations")}
        >
          {fc("pane.today", lang)}
        </button>
        <button
          type="button"
          className={pane === "subscriptions" ? "feed-tab feed-tab-on" : "feed-tab"}
          onClick={() => setPane("subscriptions")}
        >
          <Newspaper size={14} aria-hidden /> {fc("pane.subscriptions", lang)}
        </button>
        <button
          type="button"
          className={pane === "saved" ? "feed-tab feed-tab-on" : "feed-tab"}
          onClick={() => setPane("saved")}
        >
          <Bookmark size={14} aria-hidden /> {fc("pane.saved", lang)}
        </button>
        <button
          type="button"
          className={pane === "search" ? "feed-tab feed-tab-on" : "feed-tab"}
          onClick={() => setPane("search")}
        >
          <Search size={14} aria-hidden /> {fc("pane.search", lang)}
        </button>
      </nav>

      {pane === "subscriptions" && (
        <SubscriptionPanel now={now} />
      )}

      {pane === "search" && (
        <section className="feed-section">
          <AcademicSearch />
        </section>
      )}

      {pane === "saved" && (
        <section className="feed-section">
          <h2 className="feed-section-title">{fc("home.saved.title", lang)}</h2>
          {saved.isLoading && <Skeleton height={200} />}
          {saved.data && saved.data.length === 0 && (
            <Empty title={fc("home.saved.empty", lang)} hint={fc("home.saved.empty_hint", lang)} />
          )}
          <div className="feed-list">
            {(saved.data ?? []).map((item) => (
              <FeedItemCard
                key={item.id}
                item={item}
                now={now}
                onOpenDetail={() => setDetailId(item.id)}
              />
            ))}
          </div>
        </section>
      )}

      {pane === "recommendations" && data && (
        <>
          <section className="feed-section feed-section-picks">
            <div className="feed-section-head">
              <div>
                <h2 className="feed-section-title">{headline.title}</h2>
                <p className="feed-section-subtitle">{headline.subtitle}</p>
              </div>
              <div className="feed-recommendation-controls">
                <Button variant="secondary" size="sm" onClick={() => setPane("subscriptions")}>
                  <Settings2 size={14} aria-hidden /> {fc("pane.subscriptions", lang)}
                </Button>
              <div className="feed-view-toggle" role="group" aria-label={fc("home.layout.label", lang)}>
                <button
                  type="button"
                  className={viewMode === "list" ? "feed-view-on" : ""}
                  aria-pressed={viewMode === "list"}
                  title={fc("view.list.hint", lang)}
                  onClick={() => chooseViewMode("list")}
                >
                  <List size={14} aria-hidden /> {fc("view.list", lang)}
                </button>
                <button
                  type="button"
                  className={viewMode === "grid" ? "feed-view-on" : ""}
                  aria-pressed={viewMode === "grid"}
                  title={fc("view.grid.hint", lang)}
                  onClick={() => chooseViewMode("grid")}
                >
                  <LayoutGrid size={14} aria-hidden /> {fc("view.grid", lang)}
                </button>
              </div>
              </div>
            </div>
            {data.feed.length === 0 ? (
              <Empty
                title={fc("home.empty.title", lang)}
                hint={data.empty_reason || fc("home.empty.hint", lang)}
              />
            ) : (
              viewMode === "grid" ? (
                <FeedMasonry
                  cards={data.feed}
                  translations={translations}
                  now={now}
                  onOpenDetail={setDetailId}
                />
              ) : (
                <div className="feed-list">
                  {data.feed.map((card) => (
                    <FeedItemCard
                      key={card.item.id}
                      item={card.item}
                      reason={card.reason}
                      titleZh={translations[card.item.id]?.titleZh}
                      summaryZh={translations[card.item.id]?.abstractZh}
                      todayPick={card.is_today_pick}
                      layout="list"
                      now={now}
                      onOpenDetail={() => setDetailId(card.item.id)}
                    />
                  ))}
                </div>
              )
            )}
          </section>

          <section className="feed-section">
            <h2 className="feed-section-title">{fc("home.share.title", lang)}</h2>
            <p className="feed-section-subtitle">
              {fc("home.share.subtitle", lang)}
            </p>
            <ShareLinkForm onShared={() => void today.refetch()} />
          </section>

          {!data.personalized && (
            <div className="feed-nudge">
              <p>
                {fc("home.not_personalized", lang)}
              </p>
              <Button variant="secondary" size="sm" onClick={() => setPane("subscriptions")}>
                {fc("home.choose_interests", lang)}
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
