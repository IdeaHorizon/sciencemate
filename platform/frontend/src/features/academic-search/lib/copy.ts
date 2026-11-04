/**
 * 学术搜索的双语文案（论文类型的词表在 `@/lib/publication-categories`：
 * 它的取值是 `lib/api.ts` 的响应类型要用的东西，不能只放在 feature 里）。
 *
 * ## 用法
 *
 * 组件里 `const t = useT();` 然后 `t(AC.searchButton)`；
 * 标签页遍历 `CATEGORIES`，通过 `CATEGORY_BY_VALUE` 取当前语言的显示标签。
 */
import type { Phrase } from "@/shared/i18n";

export const AC = {
  // ── 检索入口 ────────────────────────────────────────────────────────────
  searchPlaceholder: {
    zh: "输入研究问题、关键词或论文标题，例如：海洋微塑料的沉降速度研究",
    en: "Enter a research question, keywords or a paper title, e.g. sinking speed of marine microplastics",
  },
  searchInputAria: { zh: "学术搜索内容", en: "Academic search query" },
  searchButton: { zh: "搜索论文", en: "Search papers" },
  searchFailed: { zh: "搜索失败：", en: "Search failed: " },
  retryLater: { zh: "请稍后重试", en: "Please try again later" },

  // ── 全文（本地 PDF、学校馆藏）────────────────────────────────────────────
  assetOpenFailed: {
    zh: "无法打开全文，请稍后重试。",
    en: "Could not open the full text. Please try again later.",
  },
  localAssetsAria: { zh: "全文", en: "Full text" },
  openLocalPdf: { zh: "打开本地 PDF", en: "Open local PDF" },
  openSchoolFulltext: { zh: "查看学校馆藏全文", en: "Open school full text" },

  // ── 论文条目 ────────────────────────────────────────────────────────────
  untitledPaper: { zh: "未命名论文", en: "Untitled paper" },
  aiSummaryLabel: { zh: "AI 总结：", en: "AI summary: " },
  recommendation: { zh: "推荐度 {score}", en: "Recommendation {score}" },
  casQuartile: { zh: "中科院 {quartile} 区", en: "CAS Q{quartile}" },
  casQuartileMissing: { zh: "中科院分区暂无数据", en: "CAS quartile unavailable" },
  impactFactor: { zh: "影响因子 {value}", en: "Impact factor {value}" },
  impactFactorLabel: { zh: "影响因子", en: "Impact factor" },
  impactFactorMissing: { zh: "影响因子暂无数据", en: "Impact factor unavailable" },
  yearSuffix: { zh: "（{year}）", en: " ({year})" },
  /** JCR 分区选项：Q1–Q4 是国际通行写法，两种语言一样，只有"全部分区"要翻。 */
  jcrAllQuartiles: { zh: "全部分区", en: "All quartiles" },

  // ── 评分雷达 ────────────────────────────────────────────────────────────
  radarAria: { zh: "推荐度多维雷达图", en: "Recommendation radar across dimensions" },
  radarCasMissing: { zh: "未收录中科院分区，此项计 0 分", en: "No CAS quartile; this dimension scores 0" },
  dimensionRelevance: { zh: "相关度", en: "Relevance" },
  dimensionCas: { zh: "分区", en: "Quartile" },
  dimensionCitation: { zh: "引用", en: "Citations" },
  dimensionRecency: { zh: "时效", en: "Recency" },

  // ── 论文详情弹窗 ────────────────────────────────────────────────────────
  paperDialogAria: { zh: "论文详情", en: "Paper details" },
  closeDialog: { zh: "关闭论文详情", en: "Close paper details" },

  // ── 筛选栏 ──────────────────────────────────────────────────────────────
  paperTypeAria: { zh: "论文类型", en: "Paper type" },
  publicationYear: { zh: "发表年份", en: "Publication year" },
  publicationYearRange: { zh: "发表年份范围", en: "Publication year range" },
  publicationYearMin: { zh: "发表年份下限", en: "Earliest publication year" },
  publicationYearMax: { zh: "发表年份上限", en: "Latest publication year" },
  jcrQuartile: { zh: "JCR 分区", en: "JCR quartile" },
  jcrFilterAria: { zh: "按 JCR 分区筛选", en: "Filter by JCR quartile" },
  allQuartiles: { zh: "全部分区", en: "All quartiles" },
  all: { zh: "全部", en: "All" },
  notApplicable: { zh: "不适用", en: "N/A" },
  impactFactorRange: { zh: "影响因子范围", en: "Impact factor range" },
  impactFactorMin: { zh: "影响因子下限", en: "Minimum impact factor" },
  impactFactorMax: { zh: "影响因子上限", en: "Maximum impact factor" },
  sortMode: { zh: "排序方式", en: "Sort by" },
  sortModeAria: { zh: "结果排序方式", en: "Result sorting" },
  defaultSort: { zh: "默认排序", en: "Default order" },

  // ── 翻译状态 ────────────────────────────────────────────────────────────
  translating: { zh: "正在翻译本页内容…", en: "Translating this page…" },
  translationFailed: {
    zh: "本页翻译失败，显示原文。",
    en: "Translation failed for this page; showing the original text.",
  },

  // ── 条目操作与分页 ──────────────────────────────────────────────────────
  save: { zh: "收藏", en: "Save" },
  saved: { zh: "已收藏", en: "Saved" },
  share: { zh: "分享", en: "Share" },
  shared: { zh: "已分享", en: "Shared" },
  linkCopied: { zh: "已复制链接", en: "Link copied" },
  noResults: { zh: "没有符合筛选条件的论文。", en: "No papers match the current filters." },
  paginationAria: { zh: "学术搜索结果分页", en: "Academic search result pages" },
  previousPage: { zh: "上一页", en: "Previous" },
  nextPage: { zh: "下一页", en: "Next" },
  pageStatus: { zh: "第 {page} / {total} 页", en: "Page {page} of {total}" },
  doi: { zh: "DOI：{doi}", en: "DOI: {doi}" },
} as const satisfies Record<string, Phrase>;

/** 排序方式：下拉的当前值与选项列表用的是同一批词条，改文案只改一处。 */
export const SORT_PHRASES = {
  recommendation: { zh: "推荐度", en: "Recommendation" },
  relevance: { zh: "相关度", en: "Relevance" },
  published: { zh: "发表时间", en: "Publication date" },
} as const satisfies Record<string, Phrase>;

/** 选项列表顺序 = 界面上的呈现顺序。 */
export const SORT_OPTIONS = [
  ["recommendation", SORT_PHRASES.recommendation],
  ["relevance", SORT_PHRASES.relevance],
  ["published", SORT_PHRASES.published],
] as const;
