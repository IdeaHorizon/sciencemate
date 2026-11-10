"use client";

import { FormEvent, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { ArrowUpDown, Bookmark, CalendarRange, ChartBar, ChevronDown, ExternalLink, Gauge, Search, Share2, X } from "lucide-react";
import { AcademicSearchLanding } from "./AcademicSearchLanding";
import { useAcademicSearch } from "../hooks/useAcademicSearch";
import { usePaperSaves } from "../hooks/usePaperSaves";
import { AC, SORT_OPTIONS, SORT_PHRASES } from "../lib/copy";
import {sourceNotices} from "../lib/source-warning";
import {cnkiProgressLabel} from "../lib/bilingual-search";
import { compareAcademicPapers, type AcademicSortMode as SortMode } from "../lib/academic-search-order";
import {
  CATEGORIES,
  CATEGORY_BY_VALUE,
  CATEGORY_JOURNAL,
  isJournalCategory,
  type PublicationCategory as PaperCategory,
} from "@/lib/publication-categories";
import { Badge, Button, Card, CardBody, CardFooter, CardHeader } from "@/shared/ui";
import { useT } from "@/shared/i18n";
import { pushError } from "@/stores/notification";
import {paginationItems} from "../lib/pagination";
import {
  api,
  literatureAssetUrl,
  type LiteraturePaper,
  type LiteratureTranslation,
} from "@/lib/api";

type ScoreDimension = "relevance" | "cas" | "citation" | "recency";
type JcrFilter = "all" | "Q1" | "Q2" | "Q3" | "Q4";
type FilterControl = "year" | "jcr" | "impact" | "sort" | null;
const TRANSLATION_BATCH_SIZE = 3;
const TRANSLATION_WORKERS = 3;

function paperKey(paper: LiteraturePaper): string {
  return (paper.doi || paper.url || paper.title).toLowerCase();
}

function paperLink(paper: LiteraturePaper): string | null {
  if (paper.doi) return `https://doi.org/${encodeURIComponent(paper.doi)}`;
  return paper.url;
}

async function openLiteratureAsset(path: string, failureMessage: string): Promise<void> {
  // API 鉴权使用内存里的 Bearer token，普通 target=_blank 链接不会携带它。
  // 先同步打开空页以保留用户手势，再用鉴权请求取得 Blob 后替换地址。
  const popup = window.open("about:blank", "_blank");
  if (popup) popup.opener = null;
  try {
    const response = await api.fetchWithAuth(literatureAssetUrl(path));
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const objectUrl = URL.createObjectURL(await response.blob());
    if (popup) {
      popup.location.replace(objectUrl);
    } else {
      const link = document.createElement("a");
      link.href = objectUrl;
      link.target = "_blank";
      link.rel = "noreferrer";
      link.click();
    }
    // 新标签页已经接管 Blob；延迟回收，避免浏览器尚未完成加载就失效。
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
  } catch {
    popup?.close();
    pushError(failureMessage);
  }
}

function LiteratureAssetLink({ path, children }: { path: string; children: ReactNode }) {
  const t = useT();
  return (
    <a
      href={literatureAssetUrl(path)}
      target="_blank"
      rel="noreferrer"
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        void openLiteratureAsset(path, t(AC.assetOpenFailed));
      }}
    >
      {children}
    </a>
  );
}

function showsRecommendation(category: PaperCategory): boolean {
  return category === CATEGORY_JOURNAL.zh;
}

function scoreValue(paper: LiteraturePaper, key: ScoreDimension): number {
  const raw = (paper.score_breakdown?.raw ?? {}) as Record<string, unknown>;
  if (key === "cas" && paper.cas_quartile === null) return 0;
  const value = Number(raw[key]);
  return Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0;
}

function ScoreRadar({ paper }: { paper: LiteraturePaper }) {
  const t = useT();
  const isJournal = isJournalCategory(paper.publication_category);
  const dimensions: Array<{ key: ScoreDimension; label: string }> = isJournal
    ? [
        { key: "relevance", label: t(AC.dimensionRelevance) },
        { key: "cas", label: t(AC.dimensionCas) },
        { key: "citation", label: t(AC.dimensionCitation) },
        { key: "recency", label: t(AC.dimensionRecency) },
      ]
    : [
        { key: "relevance", label: t(AC.dimensionRelevance) },
        { key: "citation", label: t(AC.dimensionCitation) },
        { key: "recency", label: t(AC.dimensionRecency) },
      ];
  const center = 72;
  const radius = 48;
  const point = (index: number, scale: number) => {
    const angle = -Math.PI / 2 + index * (Math.PI * 2 / dimensions.length);
    return `${center + Math.cos(angle) * radius * scale},${center + Math.sin(angle) * radius * scale}`;
  };
  const polygon = (scale: number) => dimensions.map((_, index) => point(index, scale)).join(" ");
  const values = dimensions.map(({ key }) => scoreValue(paper, key));
  const labels = isJournal
    ? [
        { x: 72, y: 12, anchor: "middle" },
        { x: 139, y: 76, anchor: "start" },
        { x: 72, y: 143, anchor: "middle" },
        { x: 5, y: 76, anchor: "start" },
      ] as const
    : [
        { x: 72, y: 12, anchor: "middle" },
        { x: 133, y: 124, anchor: "end" },
        { x: 12, y: 124, anchor: "start" },
      ] as const;

  return (
    <figure className="academic-score-radar" aria-label={t(AC.radarAria)}>
      <svg viewBox="0 0 145 150" role="img">
        {[0.25, 0.5, 0.75, 1].map((scale) => (
          <polygon key={scale} points={polygon(scale)} className="academic-radar-grid" />
        ))}
        {dimensions.map((_, index) => (
          <line key={index} x1={center} y1={center} x2={point(index, 1).split(",")[0]} y2={point(index, 1).split(",")[1]} className="academic-radar-axis" />
        ))}
        <polygon points={dimensions.map((_, index) => point(index, values[index])).join(" ")} className="academic-radar-value" />
        {dimensions.map((dimension, index) => (
          <text key={dimension.key} x={labels[index].x} y={labels[index].y} textAnchor={labels[index].anchor}>{dimension.label}</text>
        ))}
      </svg>
      {isJournal && paper.cas_quartile === null && <figcaption>{t(AC.radarCasMissing)}</figcaption>}
    </figure>
  );
}


function RelevanceNote({paper}: {paper: LiteraturePaper}) {
  const t = useT();
  const assessment = paper.relevance_assessment;
  if (!assessment || !assessment.tier || assessment.tier === "excluded") return null;
  return <p style={{fontSize:12, margin:"6px 0", color:["background", "unconfirmed"].includes(assessment.tier) ? "#986a24" : "var(--color-fg-muted)"}}>
    {assessment.detail || (assessment.tier === "background" ? t({zh:"背景参考",en:"Background reference"}) : t({zh:"直接相关",en:"Directly relevant"}))}
  </p>;
}

function AcademicPaperDialog({ paper, onClose }: { paper: LiteraturePaper; onClose: () => void }) {
  const t = useT();
  const closeRef = useRef<HTMLButtonElement>(null);
  const link = paperLink(paper);
  const showRecommendation = showsRecommendation(paper.publication_category);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    closeRef.current?.focus();
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
    };
  }, [onClose]);

  return (
    <div className="academic-paper-dialog-backdrop" onClick={onClose}>
      <article
        className="academic-paper-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={paper.title || t(AC.paperDialogAria)}
        onClick={(event) => event.stopPropagation()}
      >
        <button ref={closeRef} type="button" className="academic-paper-dialog-close" aria-label={t(AC.closeDialog)} onClick={onClose}>
          <X size={19} />
        </button>
        <div className="academic-paper-heading academic-paper-dialog-heading">
          <div>
            <h2>
              {link ? (
                <a href={link} target="_blank" rel="noreferrer">
                  {paper.title || t(AC.untitledPaper)}<ExternalLink size={15} />
                </a>
              ) : (paper.title || t(AC.untitledPaper))}
            </h2>
            {paper.title_zh && <p className="academic-paper-title-zh">{paper.title_zh}</p>}
            <RelevanceNote paper={paper} />
          </div>
        </div>
        <div className={`academic-paper-dialog-content${showRecommendation ? "" : " academic-paper-without-score"}`}>
          <div className="academic-paper-copy">
            <div className="academic-paper-metadata">
              {(paper.year || paper.authors.length > 0) && (
                <div className="academic-paper-meta-row academic-paper-meta-primary">
                  {paper.year && <span>{paper.year}</span>}
                  {paper.authors.length > 0 && <span className="academic-paper-author-inline">{paper.authors.join(", ")}</span>}
                </div>
              )}
              <div className="academic-paper-meta-row academic-paper-meta-secondary">
                {paper.venue && <span>{paper.venue}</span>}
                {isJournalCategory(paper.publication_category) && (<>
                  <span>{paper.cas_quartile ? `${t(AC.casQuartile, { quartile: paper.cas_quartile })}${paper.cas_top ? " · Top" : ""}${paper.cas_year ? t(AC.yearSuffix, { year: paper.cas_year }) : ""}` : t(AC.casQuartileMissing)}</span>
                  {paper.jcr_quartile && <span>JCR {paper.jcr_quartile}</span>}
                  <span>{paper.impact_factor === null ? t(AC.impactFactorMissing) : `${t(AC.impactFactor, { value: paper.impact_factor.toFixed(3).replace(/0+$/, "").replace(/\.$/, "") })}${paper.impact_factor_year ? t(AC.yearSuffix, { year: paper.impact_factor_year }) : ""}`}</span>
                </>)}
              </div>
            </div>
            {(paper.abstract_zh || paper.abstract) && (
              <p className="academic-paper-dialog-abstract">{paper.abstract_zh || paper.abstract}</p>
            )}
            {!paper.abstract && paper.ai_summary && (
              <p className="academic-paper-dialog-abstract academic-paper-ai-summary">
                <strong>{t(AC.aiSummaryLabel)}</strong>{paper.ai_summary}
              </p>
            )}
            <div className="academic-paper-identifier"><Badge>{paper.source}</Badge>{paper.school_holdings?.filter((holding) => holding !== paper.source).map((holding) => <Badge key={holding}>{holding}</Badge>)}{paper.doi && <small>{t(AC.doi, { doi: paper.doi })}</small>}</div>
            {(paper.local_pdf_url || paper.school_fulltext_url) && (
              <div className="academic-paper-assets" aria-label={t(AC.localAssetsAria)} onClick={(event) => event.stopPropagation()}>
                {paper.local_pdf_url && <LiteratureAssetLink path={paper.local_pdf_url}>{t(AC.openLocalPdf)}</LiteratureAssetLink>}
                {paper.school_fulltext_url && <LiteratureAssetLink path={paper.school_fulltext_url}>{t(AC.openSchoolFulltext)}</LiteratureAssetLink>}
              </div>
            )}
          </div>
          {showRecommendation && <div className="academic-paper-score-column">
            <ScoreRadar paper={paper} />
            <strong className="academic-recommendation">{t(AC.recommendation, { score: (paper.score * 100).toFixed(1) })}</strong>
          </div>}
        </div>
      </article>
    </div>
  );
}

export function AcademicSearch() {
  const t = useT();
  const queryInputRef = useRef<HTMLTextAreaElement>(null);
  const pageSize = 20;
  const search = useAcademicSearch();
  const {query, setQuery} = search;
  const [category, setCategory] = useState<PaperCategory>(CATEGORY_JOURNAL.zh);
  const [sortMode, setSortMode] = useState<SortMode>("recommendation");
  const sortPhrase = (mode: SortMode) => mode === "recommendation" && !showsRecommendation(category)
    ? AC.defaultSort
    : SORT_PHRASES[mode];
  const [showBackground, setShowBackground] = useState(true);
  const [jcrFilter, setJcrFilter] = useState<JcrFilter>("all");
  const [impactMin, setImpactMin] = useState(0);
  const [impactMax, setImpactMax] = useState<number | null>(null);
  const [yearMin, setYearMin] = useState<number | null>(null);
  const [yearMax, setYearMax] = useState<number | null>(null);
  const [activeControl, setActiveControl] = useState<FilterControl>(null);
  const [page, setPage] = useState(1);
  // 「收藏」接的是科研资讯的收藏（usePaperSaves）；「点赞」没有对应的后端，删了。
  const saves = usePaperSaves();
  const [shared, setShared] = useState<Set<string>>(new Set());
  // 浏览器不支持系统分享时只是把链接复制进剪贴板：按钮要说「已复制链接」，不说「已分享」。
  const [copied, setCopied] = useState<Set<string>>(new Set());
  const [selectedPaper, setSelectedPaper] = useState<LiteraturePaper | null>(null);
  const [translations, setTranslations] = useState<Record<string, LiteratureTranslation>>({});
  const [translationStatus, setTranslationStatus] = useState<"idle" | "pending" | "error">("idle");
  const translationRequestId = useRef(0);
  const requestedTranslations = useRef<Set<string>>(new Set());
  const filterBarRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const closeOnOutsideClick = (event: PointerEvent) => {
      if (!filterBarRef.current?.contains(event.target as Node)) setActiveControl(null);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setActiveControl(null);
    };
    document.addEventListener("pointerdown", closeOnOutsideClick);
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsideClick);
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, []);

  const sharePaper = async (paper: LiteraturePaper) => {
    const url = paperLink(paper);
    if (!url) return;
    try {
      if (navigator.share) {
        await navigator.share({ title: paper.title, url });
        setShared((current) => new Set(current).add(paperKey(paper)));
      } else {
        await navigator.clipboard.writeText(url);
        setCopied((current) => new Set(current).add(paperKey(paper)));
      }
    } catch {
      // 用户取消分享或浏览器拒绝剪贴板时保持原状态。
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const value = query.trim();
    if (value) {
      setPage(1);
      setSelectedPaper(null);
      setTranslationStatus("idle");
      requestedTranslations.current.clear();
      setJcrFilter("all");
      setImpactMin(0);
      setImpactMax(null);
      setYearMin(null);
      setYearMax(null);
      setActiveControl(null);
      search.start({ query: value, limit: null, source: "all" });
    }
  };
  const result = search.data;
  // Detailed source counts, query groups and admission diagnostics remain in
  // the search response/backend records. Only actionable notices reach the UI.
  const blockedStates = ["waiting_for_verification", "error", "paused", "browser_closed", "unavailable"];
  const channelAlerts = Object.entries(search.channels)
    .filter(([, channel]) => blockedStates.includes(channel.state))
    .filter(() => !search.isError)
    .map(([key, channel]) => ({
      key,
      detail: (key==="cnki" && (channel.count || 0)>0
        ? t({zh:"已获取 {count} 篇；{categories}暂未完成。",en:"Collected {count} papers; {categories} remain incomplete."},
            {count:channel.count || 0,categories:channel.groups?.filter(group=>blockedStates.includes(group.state)).map(group=>group.label).join(t({zh:"、",en:", "})) || t({zh:"部分分类",en:"some categories"})})
        : "") + (channel.groups?.find(group => blockedStates.includes(group.state))?.detail || channel.detail),
    }));
  const coveredSources=channelAlerts.map(alert=>alert.key);
  const unavailableSources=(result?.unavailable_sources || []).filter(source=>
    !coveredSources.includes(source) && !(result?.source_counts?.[source]));
  const visibleWarnings=sourceNotices(result?.warnings || [],{
    coveredSources,hasRemoteResults:Boolean(result?.papers.some(p=>!["cnki","school","local"].includes(p.source.split("/")[0]))),
  },t);
  const progressLabels: Record<string, string> = {
    strategy: t({zh:"正在准备检索…",en:"Preparing search\u2026"}), dedup: t({zh:"正在整理检索结果…",en:"Organizing results\u2026"}),
    metadata_enrichment: t({zh:"正在补充文献信息…",en:"Enriching paper metadata\u2026"}), final_abstract_enrichment: t({zh:"正在补充文献信息…",en:"Enriching paper metadata\u2026"}),
    scoring: t({zh:"正在整理检索结果…",en:"Organizing results\u2026"}), translation: t({zh:"正在准备文献信息…",en:"Preparing paper details\u2026"}),
    finalize: t({zh:"正在整理检索结果…",en:"Organizing results\u2026"}), cnki: cnkiProgressLabel(search.channels.cnki,t),
  };
  const categoryCounts = useMemo(() => {
    const counts = Object.fromEntries(CATEGORIES.map((item) => [item.zh, 0])) as Record<PaperCategory, number>;
    for (const paper of result?.papers ?? []) counts[paper.publication_category] += 1;
    return counts;
  }, [result]);
  const yearBounds = useMemo(() => {
    const years = (result?.papers ?? [])
      .map((paper) => Number(paper.year))
      .filter((year) => Number.isInteger(year) && year > 0);
    const currentYear = new Date().getFullYear();
    return {
      min: years.length > 0 ? Math.min(...years) : currentYear,
      max: years.length > 0 ? Math.max(...years) : currentYear,
    };
  }, [result]);
  const selectedYearMin = yearMin ?? yearBounds.min;
  const selectedYearMax = yearMax ?? yearBounds.max;
  const yearFilterActive = selectedYearMin > yearBounds.min || selectedYearMax < yearBounds.max;

  const impactCeiling = useMemo(() => {
    const maximum = Math.max(
      0,
      ...(result?.papers
        .filter((paper) => isJournalCategory(paper.publication_category))
        .map((paper) => Number(paper.impact_factor || 0)) ?? []),
    );
    return Math.max(10, Math.ceil(maximum));
  }, [result]);
  const selectedImpactMax = impactMax ?? impactCeiling;
  const impactFilterActive = impactMin > 0 || selectedImpactMax < impactCeiling;
  const visiblePapers = useMemo(() => {
    const matching = result?.papers.filter((paper) => {
      if (!showBackground && paper.relevance_assessment?.tier === "background") return false;
      if (paper.publication_category !== category) return false;
      if (yearFilterActive) {
        const paperYear = Number(paper.year);
        if (!Number.isInteger(paperYear) || paperYear < selectedYearMin || paperYear > selectedYearMax) return false;
      }
      if (!isJournalCategory(category)) return true;
      const normalizedJcr = String(paper.jcr_quartile || "").trim().toUpperCase();
      if (jcrFilter !== "all" && normalizedJcr !== jcrFilter) return false;
      if (!impactFilterActive) return true;
      if (paper.impact_factor === null) return false;
      return paper.impact_factor >= impactMin && paper.impact_factor <= selectedImpactMax;
    }) ?? [];
    return [...matching].sort((left, right) => compareAcademicPapers(left, right, sortMode));
  }, [category, impactFilterActive, impactMin, jcrFilter, result, selectedImpactMax, selectedYearMax, selectedYearMin, showBackground, sortMode, yearFilterActive]);
  const pageCount = Math.max(1, Math.ceil(visiblePapers.length / pageSize));
  const currentPage = Math.min(page, pageCount);
  const pagedPapers = visiblePapers.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  const translationPageKey = pagedPapers.map(paperKey).join("|");

  useEffect(() => {
    if (!result || !translationPageKey) return;
    const targets = pagedPapers.filter((paper) => {
      const key = paperKey(paper);
      if (requestedTranslations.current.has(key)) return false;
      const translated = translations[key];
      const needsAbstract = Boolean(
        paper.abstract
        && /[A-Za-z]/.test(paper.abstract)
        && !/[\u4e00-\u9fff]/.test(paper.abstract)
        && !(translated?.abstract_zh || paper.abstract_zh)
      );
      const needsTitle = /[A-Za-z]/.test(paper.title) && !/[\u4e00-\u9fff]/.test(paper.title)
        && !(translated?.title_zh || paper.title_zh);
      const needsSummary = paper.source !== "cnki" && !paper.abstract && !(translated?.ai_summary || paper.ai_summary);
      return needsTitle || needsAbstract || needsSummary;
    });
    if (targets.length === 0) {
      setTranslationStatus("idle");
      return;
    }
    for (const paper of targets) requestedTranslations.current.add(paperKey(paper));
    const requestId = ++translationRequestId.current;
    setTranslationStatus("pending");
    // Separate requests use separate bridge processes. Keep each request small
    // while allowing a few independent website translation sessions to run.
    const batches = Array.from({ length: Math.ceil(targets.length / TRANSLATION_BATCH_SIZE) }, (_, index) =>
      targets.slice(index * TRANSLATION_BATCH_SIZE, (index + 1) * TRANSLATION_BATCH_SIZE));
    let nextBatch = 0;
    let failed = false;
    const worker = async () => {
      while (nextBatch < batches.length) {
        const batch = batches[nextBatch++];
        try {
          const response = await api.translateLiteraturePage(batch.map((paper) => ({
            // Large author lists can breach the translation input contract.
            key: paperKey(paper).slice(0, 500),
            title: paper.title.slice(0, 1000),
            doi: paper.doi,
            url: paper.url,
            abstract: paper.abstract,
            authors: paper.authors.slice(0, 10),
            year: paper.year,
            venue: paper.venue?.slice(0, 1000) ?? null,
          })));
          setTranslations((current) => {
            const next = { ...current };
            for (const item of response.translations) {
              const previous = next[item.key];
              next[item.key] = {
                key: item.key,
                title_zh: item.title_zh || previous?.title_zh || null,
                abstract_zh: item.abstract_zh || previous?.abstract_zh || null,
                ai_summary: item.ai_summary || previous?.ai_summary || null,
              };
            }
            return next;
          });
          if (response.status === "failed" || response.status === "partial") {
            for (const paper of batch) requestedTranslations.current.delete(paperKey(paper));
            failed = true;
          }
        } catch {
          for (const paper of batch) requestedTranslations.current.delete(paperKey(paper));
          failed = true;
        }
      }
    };
    void Promise.all(Array.from({ length: Math.min(TRANSLATION_WORKERS, batches.length) }, () => worker()))
      .then(() => {
        if (translationRequestId.current === requestId) setTranslationStatus(failed ? "error" : "idle");
      });
  }, [category, currentPage, result, translationPageKey]);

  return (
    <div className="academic-search academic-search-discovery">
      <AcademicSearchLanding initial={!result && !search.isPending && !search.cnkiPending && !search.isError}
        onExample={(value) => { setQuery(value); queryInputRef.current?.focus(); }}>
      <form className="academic-search-form" onSubmit={submit}>
        <textarea
          ref={queryInputRef}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t(AC.searchPlaceholder)}
          rows={3}
          aria-label={t(AC.searchInputAria)}
        />
        <div className="academic-discovery-form-actions">
          <span>{t({ zh: "自然语言提问 · 中英文文献检索 · 每类最多 200 篇", en: "Natural-language questions · Chinese and English literature · Up to 200 per category" })}</span>
        <Button type="submit" variant="primary" loading={search.isPending} iconLeft={<Search size={16} />}>
          {t(AC.searchButton)}
        </Button>
        </div>
      </form>
      </AcademicSearchLanding>
      {search.isError && <Card accent="danger"><CardBody>{t(AC.searchFailed)}{search.error instanceof Error && search.error.message ? search.error.message : t(AC.retryLater)}</CardBody></Card>}
      {(search.cnkiPending || channelAlerts.length > 0 || unavailableSources.length > 0 || visibleWarnings.length > 0) && <Card><CardBody>
        {channelAlerts.map(alert => <p key={alert.key} role="status">
          {alert.key === "cnki" ? t({zh:"知网：",en:"CNKI: "}) : t({zh:"部分文献检索暂未完成：",en:"Some sources are still pending: "})}{alert.detail}
        </p>)}
        {(search.cnkiPending || channelAlerts.some(alert => alert.key === "cnki")) && <div style={{display:"flex",gap:12,alignItems:"center"}}>
          {!channelAlerts.some(alert => alert.key === "cnki") && <span>{cnkiProgressLabel(search.channels.cnki,t)}</span>}
          {["waiting_for_verification","error","paused","browser_closed"].includes(search.channels.cnki?.state) && <Button onClick={search.retryChinese}>{t({zh:"重试知网",en:"Retry CNKI"})}</Button>}
          <Button onClick={search.stopWaiting}>{t({zh:"停止检索（保留已获取结果）",en:"Stop search (keep collected results)"})}</Button>
        </div>}
        {unavailableSources.length > 0 && <p>{t({zh:"本次暂不可用的来源",en:"Sources unavailable in this search"})}：{unavailableSources.join("、")}</p>}
        {visibleWarnings.map(detail=><p key={detail}>{detail}</p>)}
      </CardBody></Card>}
      {search.hasMore && !search.isPending && <div style={{display:"flex",gap:12,alignItems:"center"}}>
        <span>{t({zh:"国际来源本轮检索已完成，可按需查看更多结果。",en:"This international search round is complete. More results are available on request."})}</span>
        <Button onClick={search.loadMore}>{t({zh:"继续检索更多",en:"Find more results"})}</Button>
      </div>}
      {search.isPending && search.progress && (
        <Card accent="info">
          <CardBody>
            <div
              className="academic-search-progress"
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={search.progress.percent}
            >
              <div className="academic-search-progress-track">
                <span style={{ width: `${search.progress.percent}%` }} />
              </div>
              <p>{progressLabels[search.progress.stage] || t({zh:"正在检索文献…",en:"Searching literature\u2026"})}</p>
              <Button onClick={search.stopWaiting}>{t({zh:"停止检索（保留已获取结果）",en:"Stop search (keep collected results)"})}</Button>
            </div>
          </CardBody>
        </Card>
      )}
      {result && (
        <>
          <div className="academic-search-category-tabs" role="tablist" aria-label={t(AC.paperTypeAria)}>
            {CATEGORIES.map((item) => (
              <button
                key={item.zh}
                type="button"
                role="tab"
                aria-selected={category === item.zh}
                className={category === item.zh ? "is-active" : ""}
                onClick={() => { setCategory(item.zh); setPage(1); }}
              >
                {t(CATEGORY_BY_VALUE[item.zh])} <span>{categoryCounts[item.zh]}</span>
              </button>
            ))}
          </div>
          {Boolean(result.papers.some(p=>p.relevance_assessment?.tier === "background")) && <label style={{display:"flex",alignItems:"center",gap:8,margin:"10px 0"}}>
            <input type="checkbox" checked={showBackground} onChange={event=>{setShowBackground(event.target.checked);setPage(1);}} />
            {t({zh:"显示背景参考",en:"Show background references"})}
          </label>}
          <div className="academic-filter-bar" ref={filterBarRef}>
            <div className="academic-filter-menu">
              <button
                type="button"
                className={activeControl === "year" ? "academic-filter-trigger is-open" : "academic-filter-trigger"}
                aria-expanded={activeControl === "year"}
                onClick={() => setActiveControl((current) => current === "year" ? null : "year")}
              >
                <CalendarRange size={19} />
                <span><small>{t(AC.publicationYear)}</small><strong>{selectedYearMin}–{selectedYearMax}</strong></span>
                <ChevronDown size={15} />
              </button>
              {activeControl === "year" && (
                <div className="academic-filter-popover">
                  <fieldset className="academic-range-filter">
                    <legend>{t(AC.publicationYearRange)}</legend>
                    <strong>{selectedYearMin}–{selectedYearMax}</strong>
                    <div className="academic-range-axis">
                      <input type="range" min={yearBounds.min} max={yearBounds.max} step={1} value={selectedYearMin} aria-label={t(AC.publicationYearMin)} onChange={(event) => { setYearMin(Math.min(Number(event.target.value), selectedYearMax)); setPage(1); }} />
                      <input type="range" min={yearBounds.min} max={yearBounds.max} step={1} value={selectedYearMax} aria-label={t(AC.publicationYearMax)} onChange={(event) => { const value = Math.max(Number(event.target.value), selectedYearMin); setYearMax(value >= yearBounds.max ? null : value); setPage(1); }} />
                    </div>
                    <div className="academic-range-scale"><span>{yearBounds.min}</span><span>{yearBounds.max}</span></div>
                  </fieldset>
                </div>
              )}
            </div>

            <div className="academic-filter-menu">
              <button
                type="button"
                className={activeControl === "jcr" ? "academic-filter-trigger is-open" : "academic-filter-trigger"}
                aria-expanded={activeControl === "jcr"}
                disabled={!isJournalCategory(category)}
                onClick={() => setActiveControl((current) => current === "jcr" ? null : "jcr")}
              >
                <ChartBar size={19} />
                <span><small>{t(AC.jcrQuartile)}</small><strong>{isJournalCategory(category) ? (jcrFilter === "all" ? t(AC.all) : jcrFilter) : t(AC.notApplicable)}</strong></span>
                <ChevronDown size={15} />
              </button>
              {activeControl === "jcr" && isJournalCategory(category) && (
                <div className="academic-filter-popover">
                  <div className="academic-filter-options" role="group" aria-label={t(AC.jcrFilterAria)}>
                    {(["all", "Q1", "Q2", "Q3", "Q4"] as const).map((value) => (
                      <button key={value} type="button" className={jcrFilter === value ? "is-active" : ""} onClick={() => { setJcrFilter(value); setPage(1); setActiveControl(null); }}>{value === "all" ? t(AC.jcrAllQuartiles) : value}</button>
                    ))}
                  </div>
                </div>
              )}
            </div>

            <div className="academic-filter-menu">
              <button
                type="button"
                className={activeControl === "impact" ? "academic-filter-trigger is-open" : "academic-filter-trigger"}
                aria-expanded={activeControl === "impact"}
                disabled={!isJournalCategory(category)}
                onClick={() => setActiveControl((current) => current === "impact" ? null : "impact")}
              >
                <Gauge size={19} />
                <span><small>{t(AC.impactFactorLabel)}</small><strong>{isJournalCategory(category) ? `${impactMin.toFixed(1)}–${selectedImpactMax.toFixed(1)}` : t(AC.notApplicable)}</strong></span>
                <ChevronDown size={15} />
              </button>
              {activeControl === "impact" && isJournalCategory(category) && (
                <div className="academic-filter-popover">
                  <fieldset className="academic-range-filter">
                    <legend>{t(AC.impactFactorRange)}</legend>
                    <strong>{impactMin.toFixed(1)}–{selectedImpactMax.toFixed(1)}</strong>
                    <div className="academic-range-axis">
                      <input type="range" min={0} max={impactCeiling} step={0.5} value={impactMin} aria-label={t(AC.impactFactorMin)} onChange={(event) => { setImpactMin(Math.min(Number(event.target.value), selectedImpactMax)); setPage(1); }} />
                      <input type="range" min={0} max={impactCeiling} step={0.5} value={selectedImpactMax} aria-label={t(AC.impactFactorMax)} onChange={(event) => { const value = Math.max(Number(event.target.value), impactMin); setImpactMax(value >= impactCeiling ? null : value); setPage(1); }} />
                    </div>
                    <div className="academic-range-scale"><span>0</span><span>{impactCeiling}</span></div>
                  </fieldset>
                </div>
              )}
            </div>

            <div className="academic-filter-menu">
              <button
                type="button"
                className={activeControl === "sort" ? "academic-filter-trigger is-open" : "academic-filter-trigger"}
                aria-expanded={activeControl === "sort"}
                onClick={() => setActiveControl((current) => current === "sort" ? null : "sort")}
              >
                <ArrowUpDown size={19} />
                <span><small>{t(AC.sortMode)}</small><strong>{t(sortPhrase(sortMode))}</strong></span>
                <ChevronDown size={15} />
              </button>
              {activeControl === "sort" && (
                <div className="academic-filter-popover is-right">
                  <div className="academic-filter-options" role="group" aria-label={t(AC.sortModeAria)}>
                    {SORT_OPTIONS.map(([mode]) => (
                      <button key={mode} type="button" className={sortMode === mode ? "is-active" : ""} onClick={() => { setSortMode(mode); setPage(1); setActiveControl(null); }}>{t(sortPhrase(mode))}</button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
          {translationStatus === "pending" && (
            <p className="academic-page-translation-status">{t(AC.translating)}</p>
          )}
          {translationStatus === "error" && (
            <p className="academic-page-translation-status is-error">{t(AC.translationFailed)}</p>
          )}
          <div className="academic-search-results">
            {pagedPapers.map((originalPaper, index) => {
              const key = paperKey(originalPaper);
              const translated = translations[key];
              const paper = {
                ...originalPaper,
                ...translated,
                // 分页后台只负责摘要；空的覆盖字段不能抹掉主体检索已经
                // 交付的中文标题。
                title_zh: translated?.title_zh || originalPaper.title_zh,
              };
              const link = paperLink(paper);
              const showRecommendation = showsRecommendation(paper.publication_category);
              const isSaved = Boolean(saves.savedItemId(originalPaper));
              return (
                <Card key={`${key}-${index}`} className="academic-paper-result-card" interactive onClick={() => setSelectedPaper(paper)}>
                  <CardHeader className="academic-paper-card-header">
                    <div className="academic-paper-heading academic-paper-list-heading">
                      <div>
                        <h2>
                          {link ? (
                            <a href={link} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>
                              <span className="academic-paper-title-text">{paper.title || t(AC.untitledPaper)}</span><ExternalLink size={14} />
                            </a>
                          ) : <span className="academic-paper-title-text">{paper.title || t(AC.untitledPaper)}</span>}
                        </h2>
                        {paper.title_zh && <p className="academic-paper-title-zh">{paper.title_zh}</p>}
                        <RelevanceNote paper={paper} />
                      </div>
                    </div>
                  </CardHeader>
                  <CardBody>
                    <div className={`academic-paper-content${showRecommendation ? "" : " academic-paper-without-score"}`}>
                      <div className="academic-paper-copy">
                        <div className="academic-paper-metadata">
                          {(paper.year || paper.authors.length > 0) && (
                            <div className="academic-paper-meta-row academic-paper-meta-primary">
                              {paper.year && <span>{paper.year}</span>}
                              {paper.authors.length > 0 && <span className="academic-paper-author-inline">{paper.authors.join(", ")}</span>}
                            </div>
                          )}
                          <div className="academic-paper-meta-row academic-paper-meta-secondary">
                            {paper.venue && <span>{paper.venue}</span>}
                            {isJournalCategory(paper.publication_category) && (<>
                              <span>{paper.cas_quartile ? `${t(AC.casQuartile, { quartile: paper.cas_quartile })}${paper.cas_top ? " · Top" : ""}${paper.cas_year ? t(AC.yearSuffix, { year: paper.cas_year }) : ""}` : t(AC.casQuartileMissing)}</span>
                              {paper.jcr_quartile && <span>JCR {paper.jcr_quartile}</span>}
                              <span>{paper.impact_factor === null ? t(AC.impactFactorMissing) : `${t(AC.impactFactor, { value: paper.impact_factor.toFixed(3).replace(/0+$/, "").replace(/\.$/, "") })}${paper.impact_factor_year ? t(AC.yearSuffix, { year: paper.impact_factor_year }) : ""}`}</span>
                            </>)}
                          </div>
                        </div>
                        {(paper.abstract_zh || paper.abstract) && (
                          <p className="academic-paper-abstract">{paper.abstract_zh || paper.abstract}</p>
                        )}
                        {!paper.abstract && paper.ai_summary && (
                          <p className="academic-paper-abstract academic-paper-ai-summary">
                            <strong>{t(AC.aiSummaryLabel)}</strong>{paper.ai_summary}
                          </p>
                        )}
                        <div className="academic-paper-identifier"><Badge>{paper.source}</Badge>{paper.school_holdings?.filter((holding) => holding !== paper.source).map((holding) => <Badge key={holding}>{holding}</Badge>)}{paper.doi && <small>{t(AC.doi, { doi: paper.doi })}</small>}</div>
                        {(paper.local_pdf_url || paper.school_fulltext_url) && (
                          <div className="academic-paper-assets" aria-label={t(AC.localAssetsAria)} onClick={(event) => event.stopPropagation()}>
                            {paper.local_pdf_url && <LiteratureAssetLink path={paper.local_pdf_url}>{t(AC.openLocalPdf)}</LiteratureAssetLink>}
                            {paper.school_fulltext_url && <LiteratureAssetLink path={paper.school_fulltext_url}>{t(AC.openSchoolFulltext)}</LiteratureAssetLink>}
                          </div>
                        )}
                      </div>
                      {showRecommendation && <div className="academic-paper-score-column">
                        <ScoreRadar paper={paper} />
                        <strong className="academic-recommendation">{t(AC.recommendation, { score: (paper.score * 100).toFixed(1) })}</strong>
                      </div>}
                    </div>
                  </CardBody>
                  <CardFooter className="academic-paper-actions">
                    <button type="button" aria-pressed={isSaved} className={isSaved ? "is-active" : ""} disabled={saves.busy} onClick={(event) => { event.stopPropagation(); saves.toggle(originalPaper); }}><Bookmark size={16} />{isSaved ? t(AC.saved) : t(AC.save)}</button>
                    <button type="button" className={shared.has(key) || copied.has(key) ? "is-active" : ""} disabled={!link} onClick={(event) => { event.stopPropagation(); void sharePaper(paper); }}><Share2 size={16} />{shared.has(key) ? t(AC.shared) : copied.has(key) ? t(AC.linkCopied) : t(AC.share)}</button>
                  </CardFooter>
                </Card>
              );
            })}
            {visiblePapers.length === 0 && <p className="academic-paper-muted">{t(AC.noResults)}</p>}
          </div>
          {visiblePapers.length > pageSize && (
            <nav className="academic-search-pagination" aria-label={t(AC.paginationAria)}>
              <button type="button" disabled={currentPage === 1} onClick={() => setPage(currentPage - 1)}>{t(AC.previousPage)}</button>
              {paginationItems(currentPage,pageCount).map((item,index) => (
                item==="gap" ? <span key={`gap-${index}`} aria-hidden="true">…</span> :
                <button key={item} type="button" aria-current={currentPage === item ? "page" : undefined} className={currentPage === item ? "is-active" : ""} onClick={() => setPage(item)}>{item}</button>
              ))}
              <button type="button" disabled={currentPage === pageCount} onClick={() => setPage(currentPage + 1)}>{t(AC.nextPage)}</button>
              <span>{t(AC.pageStatus, { page: currentPage, total: pageCount })}</span>
            </nav>
          )}
        </>
      )}
      {selectedPaper && (
        <AcademicPaperDialog
          paper={{ ...selectedPaper, ...(translations[paperKey(selectedPaper)] ?? {}) }}
          onClose={() => setSelectedPaper(null)}
        />
      )}
    </div>
  );
}
