"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Bookmark, LayoutGrid, List, Search, Settings2, Sparkles, Newspaper, UserRound, Plus, X, RefreshCw } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { qk } from "@/lib/query/keys";
import { waitForFeedSettingWrites } from "../lib/feed-setting-writes";
import { Button, Empty, Skeleton } from "@/shared/ui";
import { useFeedItems, useFeedToday, useRefreshFeedToday, useFeedSubscriptions, useFeedSubscriptionSearch, useFeedSubscriptionItems, useSaveFeedSubscriptions } from "../hooks/useFeed";
import { useFeedTranslation } from "../hooks/useFeedTranslation";
import { feedTranslationReady } from "../lib/feed-translation-ready";
import { shouldPreloadFeedPage } from "../lib/feed-pagination";
import { say, useLanguage, type Language } from "@/shared/i18n";
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
import { CurationSwitch } from "./CurationSwitch";
import { ShareLinkForm } from "./ShareLinkForm";
import { AcademicSearch } from "@/features/academic-search";
import type { FeedItem, FeedJournalSubscription, FeedScholarSubscription, FeedProgress } from "@/lib/api";

type Pane = "recommendations" | "saved" | "search";
// 学科与期刊、学者并列，且排在第一个 —— 学科是「关注哪些方向」，期刊/学者是
// 「关注谁」，三者都是订阅。学科的内容仍是原来的方向选择器（InterestPicker），
// 一个字都没改，只是从推荐页的独立入口挪进了订阅。
type SubscriptionKind = "domain" | "journal" | "scholar";
type RecommendationSource = "domain" | "journal" | "scholar" | "project";
// Demo presentation: keep collection/progress active but hide its diagnostics panel.
const SHOW_RECOMMENDATION_PROGRESS = false;

function recommendationSource(reason: string, projectId?: string | null): RecommendationSource {
  if (projectId || /\u8bfe\u9898|\u9879\u76ee/.test(reason)) return "project";
  if (/\u5b66\u8005/.test(reason)) return "scholar";
  if (/\u671f\u520a/.test(reason)) return "journal";
  return "domain";
}

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

function SubscriptionPanel({ now, onboarding = false, onDone, onChange }: { now: Date; onboarding?: boolean; onDone?: () => void; onChange?: () => void }) {
  const lang = useLanguage();
  const subscriptions = useFeedSubscriptions();
  const save = useSaveFeedSubscriptions();
  const today = useFeedToday(false);
  const [kind, setKind] = useState<SubscriptionKind>("domain");
  const items = useFeedSubscriptionItems(kind);
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
    onChange?.();
    if ("impact_factor" in entry) {
      const journals = followedKeys.has(entry.key) ? current.journals.filter((value) => value.key !== entry.key) : [...current.journals, entry as FeedJournalSubscription];
      save.mutate({ ...current, journals });
    } else {
      const scholars = followedKeys.has(entry.key) ? current.scholars.filter((value) => value.key !== entry.key) : [...current.scholars.filter((value) => !(value.identity_status !== "resolved" && value.name.toLowerCase() === entry.name.toLowerCase())), entry as FeedScholarSubscription];
      save.mutate({ ...current, scholars });
    }
  };
  const visibleItems = items.data?.pages.flat() ?? [];
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
      {save.isPending && <p className="feed-section-subtitle" role="status">{say({ zh: "正在保存关注…", en: "Saving subscriptions…" }, lang)}</p>}
      <div className="feed-subscription-kinds" role="tablist">
        <button type="button" className={kind === "domain" ? "feed-tab feed-tab-on" : "feed-tab"} onClick={() => { setKind("domain"); setQuery(""); }}><LayoutGrid size={14} /> {fc("subscriptions.domains", lang)}</button>
        <button type="button" className={kind === "journal" ? "feed-tab feed-tab-on" : "feed-tab"} onClick={() => { setKind("journal"); setQuery(""); }}><Newspaper size={14} /> {fc("subscriptions.journals", lang)}</button>
        <button type="button" className={kind === "scholar" ? "feed-tab feed-tab-on" : "feed-tab"} onClick={() => { setKind("scholar"); setQuery(""); }}><UserRound size={14} /> {fc("subscriptions.scholars", lang)}</button>
      </div>
      {/* 学科订阅：内容与推荐页原来的方向选择器完全一致，只是搬进了这里。 */}
      {kind === "domain" && <InterestPicker showSkip={false} onChange={onChange} onDone={() => { if (!onChange) void today.refetch(); }} />}
      {kind !== "domain" && <label className="feed-subscription-search"><Search size={16} aria-hidden /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={fc(kind === "journal" ? "subscriptions.search_journals" : "subscriptions.search_scholars", lang)} /></label>}
      {kind === "scholar" && <p className="feed-section-subtitle">{say({ zh: "同名学者分别列出，请结合机构、研究方向和 ORCID 选择具体作者。", en: "Namesakes are listed separately. Select by institution, research topics and ORCID." }, lang)}</p>}
      {kind !== "domain" && query.trim() && <div className="feed-subscription-results">
        {search.isLoading && <Skeleton height={72} />}
        {search.isError && <p role="alert">{say({ zh: "学者身份或期刊检索暂不可用，请稍后重试。", en: "Search unavailable. Please try again later." }, lang)}</p>}
        {resultItems.map((entry) => <div className="feed-subscription-result" key={entry.key}>{searchKind === "journal" ? <SubscriptionAvatar kind="journal" url={(entry as FeedJournalSubscription).cover_url} /> : <SubscriptionAvatar kind="scholar" url={(entry as FeedScholarSubscription).avatar_url} />}<div><strong className={searchKind === "scholar" ? "feed-scholar-name" : undefined}>{entry.name}</strong>{"impact_factor" in entry && <small>{[entry.issn, entry.jcr_quartile, entry.impact_factor != null ? "IF " + entry.impact_factor : ""].filter(Boolean).join(" · ")}</small>}{!("impact_factor" in entry) && <><small className="feed-scholar-institution" title={(entry as FeedScholarSubscription).institution}>{(entry as FeedScholarSubscription).institution || (say({ zh: "机构未提供", en: "Institution unavailable" }, lang))}</small><small>{((entry as FeedScholarSubscription).topics ?? []).join(" · ")}</small><a href={`https://openalex.org/${(entry as FeedScholarSubscription).author_id}`} target="_blank" rel="noopener noreferrer">{say({ zh: "查看作者主页和成果", en: "View author and works" }, lang)}</a><small>{[(entry as FeedScholarSubscription).orcid, (entry as FeedScholarSubscription).author_id].filter(Boolean).join(" · ")}</small><small>{say({ zh: "数据库累计成果 {count} 篇", en: "{count} indexed works" }, lang, { count: (entry as FeedScholarSubscription).works_count ?? 0 })}</small></>}</div><Button variant={followedKeys.has(entry.key) ? "secondary" : "primary"} size="sm" onClick={() => toggle(entry)}>{followedKeys.has(entry.key) ? fc("subscriptions.following", lang) : <><Plus size={14} /> {fc("subscriptions.follow", lang)}</>}</Button></div>)}
      </div>}
      {kind !== "domain" && (current.journals.length > 0 || current.scholars.length > 0) && <div className="feed-subscription-chips">
        {kind === "journal" && current.journals.map((entry) => <button type="button" key={entry.key} onClick={() => toggle(entry)}><SubscriptionAvatar kind="journal" url={entry.cover_url} />{entry.name} <X size={12} /></button>)}
        {kind === "scholar" && current.scholars.map((entry) => <div className="feed-scholar-chip" key={entry.key}>
          <button type="button" onClick={() => toggle(entry)}><SubscriptionAvatar kind="scholar" url={entry.avatar_url} /><span className="feed-scholar-name">{entry.name}</span>{entry.institution && <span className="feed-scholar-institution" title={entry.institution}> · {entry.institution}</span>} <X size={12} /></button>
          {entry.identity_status !== "resolved" && <><small>{lang === "zh" ? (entry.identity_message || say({ zh: "正在迁移旧订阅，查询具体作者身份…", en: "Resolving the identity of a legacy subscription…" }, lang)) : say({ zh: "作者身份未确认，已暂停按姓名采集", en: "Identity unresolved; name-only acquisition paused" }, lang)}</small><Button variant="secondary" size="sm" onClick={() => setQuery(entry.name)}>{say({ zh: "选择具体学者", en: "Resolve identity" }, lang)}</Button></>}
          {entry.identity_status !== "resolved" && (entry.identity_candidates ?? []).map((candidate) => <div className="feed-scholar-identity-candidate" key={candidate.key}>
            <div><b className="feed-scholar-name">{candidate.name}</b><small className="feed-scholar-institution" title={candidate.institution}>{candidate.institution || (say({ zh: "机构未提供", en: "Institution unavailable" }, lang))}</small><small>{(candidate.topics ?? []).join(" · ")}</small><a href={`https://openalex.org/${candidate.author_id}`} target="_blank" rel="noopener noreferrer">{say({ zh: "查看作者主页和成果", en: "View author and works" }, lang)}</a><small>{[candidate.orcid, candidate.author_id].filter(Boolean).join(" · ")}</small></div>
            <Button size="sm" variant="secondary" onClick={() => { onChange?.(); const scholars = [...current.scholars.filter((value) => value.key !== entry.key && value.key !== candidate.key), candidate]; save.mutate({ ...current, scholars }); }}>{say({ zh: "绑定这位学者", en: "Select this identity" }, lang)}</Button>
          </div>)}
        </div>)}
      </div>}
      {onboarding && <div className="feed-subscription-onboarding-done"><Button onClick={onDone}>{fc("subscriptions.done", lang)}</Button></div>}
      {!onboarding && <div className="feed-section-head feed-subscription-feed-head">
        <div><h3 className="feed-section-title feed-subscription-feed-title">{fc("subscriptions.feed", lang)}</h3><small>{say({ zh: "已加载 {count} 篇", en: "{count} loaded" }, lang, { count: visibleItems.length })}</small></div>
        {items.hasNextPage && <Button variant="secondary" size="sm" onClick={() => void items.fetchNextPage()} disabled={items.isFetchingNextPage}>{items.isFetchingNextPage ? say({ zh: "加载中…", en: "Loading…" }, lang) : say({ zh: "加载更多", en: "Load more" }, lang)}</Button>}
      </div>}
      {!onboarding && items.isLoading && <Skeleton height={220} />}
      {!onboarding && !items.isLoading && visibleItems.length === 0 && <Empty title={fc("subscriptions.empty", lang)} hint={fc("subscriptions.empty_hint", lang)} />}
      {!onboarding && <div className="feed-list">{visibleItems.map((card) => <FeedItemCard key={card.item.id} item={card.item} reason={card.reason} titleZh={translations[card.item.id]?.titleZh} summaryZh={translations[card.item.id]?.abstractZh} now={now} layout="list" onOpenDetail={() => setDetailId(card.item.id)} />)}</div>}
    </section>
  );
}

function SubscriptionDialog({ now, onClose, onChange }: { now: Date; onClose: () => void; onChange: () => void }) {
  const lang = useLanguage();
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      const dialogs = document.querySelectorAll('[role="dialog"]');
      if (dialogs[dialogs.length - 1] !== dialogRef.current) return;
      if (event.key === "Escape") { event.preventDefault(); onClose(); }
      if (event.key !== "Tab") return;
      const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]',
      ) ?? []).filter((element) => element.offsetParent !== null);
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, [onClose]);
  return <div className="feed-subscription-backdrop" onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <div ref={dialogRef} className="feed-subscription-dialog" role="dialog" aria-modal="true" aria-labelledby="feed-subscription-dialog-title">
      <header className="feed-subscription-dialog-head">
        <h2 id="feed-subscription-dialog-title">{say({ zh: "订阅管理", en: "Manage subscriptions" }, lang)}</h2>
        <button ref={closeRef} type="button" aria-label={say({ zh: "关闭订阅管理", en: "Close subscriptions" }, lang)} onClick={onClose}><X size={20} aria-hidden /></button>
      </header>
      <div className="feed-subscription-dialog-body"><CurationSwitch onChange={onChange} /><SubscriptionPanel now={now} onChange={onChange} /></div>
    </div>
  </div>;
}


function progressText(progress: FeedProgress, lang: Language) {
  const zh = lang === "zh";
  switch (progress.phase) {
    case "searching": return say({ zh: "正在检索新内容，已获取数量还会增加。", en: "Searching for new content. The collected count may increase." }, lang);
    case "preparing": return say({ zh: "正在补齐摘要和图片，完成的内容会逐批进入推荐。", en: "Preparing abstracts and images. Finished items enter recommendations in batches." }, lang);
    case "queued": return say({ zh: "仍有内容等待补齐，当前推荐数量还不是最终结果。", en: "Some items are waiting for preparation. Recommendations are still incomplete." }, lang);
    case "failed": return say({ zh: "课题检索未完成，请检查模型连接；当前推荐结果不完整。", en: "Project discovery failed. Check the model connection; results are incomplete." }, lang);
    default: return progress.collected === 0
      ? (say({ zh: "当前尚未获取到与你的关注和课题匹配的内容。", en: "No matching content has been collected yet." }, lang))
      : (say({ zh: "当前文献补齐已完成；推荐池经过相关性和质量筛选，卡片还需译文完成后才能显示。", en: "Current items are prepared. The recommendation pool reflects relevance and quality filtering." }, lang));
  }
}

function RecommendationProgress({ progress, lang, loaded, matching, visible }: { progress: FeedProgress; lang: Language; loaded: number; matching: number; visible: number }) {
  const zh = lang === "zh";
  const title = { searching: say({ zh: "正在检索", en: "Searching" }, lang), preparing: say({ zh: "正在补齐", en: "Preparing" }, lang), queued: say({ zh: "等待处理", en: "Queued" }, lang), settled: say({ zh: "文献补齐已完成", en: "Metadata preparation complete" }, lang), failed: say({ zh: "部分任务失败", en: "Some tasks failed" }, lang) }[progress.phase];
  return <div className="feed-progress" role="status" aria-live="polite">
    <strong>{say({ zh: "推荐进度", en: "Recommendation progress" }, lang)}</strong>
    <div className="feed-source-progress">
      {(progress.source_progress ?? []).map((row) => {
        const labels: Record<string, string> = { domain: say({ zh: "领域", en: "Domains" }, lang), journal: say({ zh: "期刊", en: "Journals" }, lang), scholar: say({ zh: "学者", en: "Scholars" }, lang), project: say({ zh: "课题", en: "Projects" }, lang) };
        const stages: Record<string, string> = { not_configured: say({ zh: "未设置", en: "Not configured" }, lang), searching: say({ zh: "检索中", en: "Searching" }, lang), preparing: say({ zh: "补齐中", en: "Preparing" }, lang), translating: say({ zh: "翻译中", en: "Translating" }, lang), ready: say({ zh: "已有内容", en: "Content available" }, lang), empty: say({ zh: "完成，暂无匹配", en: "Finished; no matches" }, lang), failed: say({ zh: "部分任务异常", en: "Task errors" }, lang), waiting: say({ zh: "等待采集", en: "Waiting" }, lang), identity_pending: say({ zh: "待确认作者身份", en: "Identity confirmation needed" }, lang) };
        return <div key={row.source} className="feed-source-progress-item">
          <b>{labels[row.source] ?? row.source}</b><span>{stages[row.phase] ?? (say({ zh: "确认进度中", en: "Checking" }, lang))}</span>
          <small>{say({ zh: "已获取 {collected} · 可展示 {ready}", en: "{collected} collected · {ready} ready" }, lang, { collected: row.collected, ready: row.ready })}</small>
          {row.identity_pending > 0 && <small>{say({ zh: "{count} 位学者身份待确认", en: "{count} identities to confirm" }, lang, { count: row.identity_pending })}</small>}
        </div>;
      })}
    </div>
    <details className="feed-progress-details"><summary>{say({ zh: "详细统计", en: "Detailed counts" }, lang)}</summary>
    <strong>{title}</strong>
    <div className="feed-progress-counts">
      <span>{say({ zh: "已获取", en: "Collected" }, lang)} <b>{progress.collected}</b></span>
      <span>{say({ zh: "文献补齐完成（未含翻译）", en: "Metadata prepared (before translation)" }, lang)} <b>{progress.prepared}</b></span>
      <span>{say({ zh: "待文献补齐", en: "Metadata pending" }, lang)} <b>{progress.pending}</b></span>
      <span>{say({ zh: "推荐池", en: "Recommended" }, lang)} <b>{progress.recommendable}</b></span>
      <span>{say({ zh: "译文已缓存（整个推荐池）", en: "Translations cached (whole pool)" }, lang)} <b>{progress.translated}</b></span>
      <span>{say({ zh: "来源筛选命中（整个推荐池）", en: "Matching sources (whole pool)" }, lang)} <b>{matching}</b></span>
      <span>{say({ zh: "本页译文完成并显示", en: "Translated and visible on this page" }, lang)} <b>{visible}</b></span>
      <span>{say({ zh: "后台待翻译", en: "Background translation pending" }, lang)} <b>{progress.translation_pending}</b></span>
    </div>
    <p>{progressText(progress, lang)}</p>
    <small>{say({ zh: "本页已加载 {count} 条（分页显示，每页21条）", en: "{count} browser cards loaded (21 per page)" }, lang, { count: loaded })}</small>
    <small>{say({ zh: "后台自动翻译整个推荐池，已完成译文直接复用共享缓存，不依赖下拉。来源筛选命中统计整个推荐池；本页仅加载已准备好且翻译完成的卡片。", en: "The whole recommendation pool is translated in the background using the shared cache. Source matches cover the whole pool; pagination returns prepared and translated cards only." }, lang)}</small>
    {progress.project_error && progress.phase !== "failed" && <p>{say({ zh: "课题检索存在失败，领域、期刊和学者结果仍可继续展示。", en: "Project discovery has an error. Other recommendations remain available." }, lang)}</p>}
    </details>
  </div>;
}

export function FeedHome() {
  const lang = useLanguage();
  const [pane, setPane] = useState<Pane>("recommendations");
  const [subscriptionsOpen, setSubscriptionsOpen] = useState(false);
  const queryClient = useQueryClient();
  const subscriptionChanged = useRef(false);
  const markSubscriptionChanged = useCallback(() => { subscriptionChanged.current = true; }, []);
  const refreshToday = useRefreshFeedToday();
  const [onboardingActive, setOnboardingActive] = useState<boolean | null>(null);
  const [onboardingStep, setOnboardingStep] = useState<"interests" | "subscriptions">("interests");
  const [recommendationSources, setRecommendationSources] = useState<Record<RecommendationSource, boolean>>({ domain: true, journal: true, scholar: true, project: true });
  const selectedSources = Object.entries(recommendationSources).filter(([, selected]) => selected).map(([source]) => source).join(",");
  const today = useFeedToday(pane === "recommendations" && !subscriptionsOpen, selectedSources, SHOW_RECOMMENDATION_PROGRESS);
  // 截稿倒计时共用同一个时刻，避免同一屏的日界判断不一致。
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

  const data = useMemo(() => {
    const pages = today.data?.pages;
    if (!pages?.length) return undefined;
    const seen = new Set<string>();
    const feed = pages.flatMap((page) => page.feed).filter((card) => {
      if (seen.has(card.item.id)) return false;
      seen.add(card.item.id);
      return true;
    });
    return { ...pages[0], feed };
  }, [today.data]);
  const loadMoreRef = useRef<HTMLDivElement>(null);
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = today;
  const [translationRetry, setTranslationRetry] = useState(0);
  const translations = useFeedTranslation(data?.feed.map((c) => c.item) ?? [], translationRetry, translationRetry > 0);
  const sourceFeed = useMemo(
    () => data?.feed.filter((card) => (card.recommendation_sources?.length ? card.recommendation_sources : [recommendationSource(card.reason, card.project_id)]).some((source) => recommendationSources[source])) ?? [],
    [data?.feed, recommendationSources],
  );
  const visibleFeed = useMemo(
    () => sourceFeed.filter((card) => feedTranslationReady(card.item, translations[card.item.id])),
    [sourceFeed, translations],
  );
  const pendingTranslations = sourceFeed.length - visibleFeed.length;
  useEffect(() => {
    const node = loadMoreRef.current;
    if (subscriptionsOpen || pane !== "recommendations" || today.isPlaceholderData || !hasNextPage || isFetchingNextPage || refreshToday.isPending) return;
    // Hidden untranslated cards must not consume the first visible page.
    if (shouldPreloadFeedPage(data?.feed.length ?? 0, visibleFeed.length)) {
      void fetchNextPage();
      return;
    }
    if (!node || (pendingTranslations > 0 && visibleFeed.length === 0)) return;
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) void fetchNextPage();
    }, { rootMargin: "300px" });
    observer.observe(node);
    return () => observer.disconnect();
  }, [pane, subscriptionsOpen, today.isPlaceholderData, data?.feed.length, hasNextPage, isFetchingNextPage, fetchNextPage, refreshToday.isPending, pendingTranslations, visibleFeed.length]);
  useEffect(() => {
    if (data && onboardingActive === null) setOnboardingActive(!data.onboarded);
  }, [data, onboardingActive]);


  // 正常从最新列表取详情；已读降权使卡片跨过分页边界时，保留正在看的快照，
  // 避免刷新把弹窗突然关掉。
  const [detailId, setDetailId] = useState<string | null>(null);
  const [detailSnapshot, setDetailSnapshot] = useState<{ item: FeedItem; reason?: string } | null>(null);
  const refreshRecommendations = () => {
    setDetailId(null);
    setDetailSnapshot(null);
    setTranslationRetry((current) => current + 1);
    refreshToday.mutate();
  };

  const closeSubscriptions = useCallback(async () => {
    setSubscriptionsOpen(false);
    if (!subscriptionChanged.current) return;
    subscriptionChanged.current = false;
    await waitForFeedSettingWrites(queryClient);
    setDetailId(null);
    setTranslationRetry((current) => current + 1);
    // Setting writes already invalidate the backend snapshot. Keep the current
    // cards visible while React Query fetches the new recommendation result.
    await queryClient.invalidateQueries({ queryKey: qk.feedToday() });
  }, [queryClient]);

  if (pane === "recommendations" && today.isLoading) return <Skeleton height={420} />;

  if (pane === "recommendations" && today.isError) {
    return (
      <Empty
        title={fc("home.error.title", lang)}
        hint={today.error instanceof Error ? today.error.message : fc("home.error.hint", lang)}
        action={<Button loading={refreshToday.isPending} onClick={refreshRecommendations}>{fc("home.refresh", lang)}</Button>}
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
  const detailItem = detailId ? allVisible.find((i) => i.id === detailId)
    ?? (detailSnapshot?.item.id === detailId ? detailSnapshot.item : null) : null;
  const detailReason = detailId
    ? data?.feed.find((c) => c.item.id === detailId)?.reason ?? detailSnapshot?.reason
    : undefined;

  return (
    <div className="feed-home">
      {subscriptionsOpen && <SubscriptionDialog now={now} onClose={closeSubscriptions} onChange={markSubscriptionChanged} />}
      {detailItem && (
        <FeedItemDialog
          item={detailItem}
          reason={detailReason}
          now={now}
          titleZh={translations[detailItem.id]?.titleZh}
          summaryZh={translations[detailItem.id]?.abstractZh}
          onClose={() => { setDetailId(null); setDetailSnapshot(null); }}
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
                <Button
                  variant="secondary" size="sm"
                  title={fc("home.refresh.hint", lang)}
                  loading={refreshToday.isPending}
                  onClick={refreshRecommendations}
                  iconLeft={<RefreshCw size={14} aria-hidden />}
                >
                  {fc("home.refresh", lang)}
                </Button>
                <div className="feed-source-filters" role="group" aria-label={say({ zh: "推荐来源", en: "Recommendation sources" }, lang)}>
                  {([["domain", { zh: "领域", en: "Domains" }], ["journal", { zh: "期刊", en: "Journals" }], ["scholar", { zh: "学者", en: "Scholars" }], ["project", { zh: "课题", en: "Projects" }]] as const).map(([source, label]) => (
                    <label key={source}>
                      <input type="checkbox" checked={recommendationSources[source]} onChange={(event) => setRecommendationSources((current) => ({ ...current, [source]: event.target.checked }))} />
                      {say(label, lang)}
                    </label>
                  ))}
                </div>
                <Button variant="secondary" size="sm" iconLeft={<Settings2 size={14} aria-hidden />} onClick={() => { subscriptionChanged.current = false; setSubscriptionsOpen(true); }}>
                  {fc("pane.subscriptions", lang)}
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
            {SHOW_RECOMMENDATION_PROGRESS && data.progress && <RecommendationProgress progress={data.progress} lang={lang} loaded={data.feed.length} matching={data.progress.source_groups?.reduce((total, group) => total + (group.sources.some((source) => recommendationSources[source as RecommendationSource]) ? group.total : 0), 0) ?? sourceFeed.length} visible={visibleFeed.length} />}
            {SHOW_RECOMMENDATION_PROGRESS && pendingTranslations > 0 && <p className="feed-section-subtitle" role="status">
              {say({ zh: "译文准备：已完成 {visible} / {total} 条，{pending} 条等待标题或摘要翻译。完成后逐批显示；若长时间未完成，可点击“刷新推荐”重试。", en: "Translations ready: {visible} / {total}. {pending} items await translation. Completed cards appear automatically; refresh to retry." }, lang, { visible: visibleFeed.length, total: sourceFeed.length, pending: pendingTranslations })}
            </p>}
            {visibleFeed.length === 0 && (pendingTranslations > 0 || today.isPlaceholderData) ? (
              <Skeleton height={220} />
            ) : visibleFeed.length === 0 ? (
              <Empty
                title={data.refreshing ? fc("home.empty.refreshing_title", lang) : fc("home.empty.title", lang)}
                hint={data.feed.length > 0 ? (say({ zh: "当前勾选项没有匹配内容，可调整上方筛选。", en: "No items match the selected filters." }, lang)) : data.empty_reason || (data.progress ? progressText(data.progress, lang) : fc("home.empty.hint", lang))}
              />
            ) : (
              viewMode === "grid" ? (
                <FeedMasonry
                  cards={visibleFeed}
                  translations={translations}
                  now={now}
                  onOpenDetail={(id) => {
                    const card = data?.feed.find((value) => value.item.id === id);
                    setDetailSnapshot(card ? { item: { ...card.item, viewed: true }, reason: card.reason } : null);
                    setDetailId(id);
                  }}
                />
              ) : (
                <div className="feed-list">
                  {visibleFeed.map((card) => (
                    <FeedItemCard
                      key={card.item.id}
                      item={card.item}
                      reason={card.reason}
                      titleZh={translations[card.item.id]?.titleZh}
                      summaryZh={translations[card.item.id]?.abstractZh}
                      todayPick={card.is_today_pick}
                      layout="list"
                      now={now}
                      onOpenDetail={() => { setDetailSnapshot({ item: { ...card.item, viewed: true }, reason: card.reason }); setDetailId(card.item.id); }}
                    />
                  ))}
                </div>
              )
            )}
            {today.hasNextPage && <div ref={loadMoreRef} style={{ minHeight: 1 }} aria-hidden />}
            <div className="feed-recommendation-controls">
              {today.hasNextPage && <Button variant="secondary" size="sm"
                loading={today.isFetchingNextPage} disabled={refreshToday.isPending}
                onClick={() => void today.fetchNextPage()}>
                {say({ zh: "加载更多", en: "Load more" }, lang)}
              </Button>}
              {pendingTranslations > 0 && <Button variant="secondary" size="sm"
                onClick={() => setTranslationRetry((current) => current + 1)}>
                {say({ zh: "重试未完成翻译", en: "Retry pending translations" }, lang)}
              </Button>}
            </div>
            {today.isFetchingNextPage && <Skeleton height={80} />}
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
              <Button variant="secondary" size="sm" onClick={() => { subscriptionChanged.current = false; setSubscriptionsOpen(true); }}>
                {fc("home.choose_interests", lang)}
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
