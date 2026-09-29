import { say, type Language, type Phrase } from "../../../shared/i18n/language.ts";

/**
 * 产物类型 → 给人看的名字。
 *
 * 一张表，三处用：「研究产出」页的每一行、会话里的交付物 chip、右栏拆开
 * 信封时的标题。2026-09-10 之前是两张（SessionDeliverables 一张 7 条、
 * ProjectOutputsView 一张 14 条），同一种产物在两处叫法可能不同，且分叉不报错。
 *
 * 表里必须覆盖 `shared/lib/artifact_policy.py` 里全部 `retention: permanent`
 * 的类型（kind-label.test.ts 扫那个文件对账）—— 少一个，那一类交付物就会以
 * `analysis_report` 这种代号出现在用户面前。表里没有的照原样显示，**不猜**。
 */
export const ARTIFACT_KIND_LABEL: Record<string, Phrase> = {
  manuscript: { zh: "论文", en: "Manuscript" },
  accepted_paper: { zh: "已接收论文", en: "Accepted paper" },
  paper_pdf: { zh: "论文 PDF", en: "Paper PDF" },
  pre_registration: { zh: "预注册", en: "Pre-registration" },
  search_protocol: { zh: "检索协议", en: "Search protocol" },
  survey_report: { zh: "综述", en: "Survey report" },
  literature_index: { zh: "文献索引", en: "Literature index" },
  literature_evidence_package: { zh: "文献证据包", en: "Literature evidence package" },
  research_plan: { zh: "研究计划", en: "Research plan" },
  research_intent: { zh: "研究意图", en: "Research intent" },
  experiment_log: { zh: "实验记录", en: "Experiment log" },
  raw_results: { zh: "原始结果", en: "Raw results" },
  clean_results: { zh: "结果数据", en: "Clean results" },
  observation_results: { zh: "观测结果", en: "Observation results" },
  dataset_snapshot: { zh: "数据集快照", en: "Dataset snapshot" },
  analysis_report: { zh: "分析报告", en: "Analysis report" },
  review_report: { zh: "审稿报告", en: "Review report" },
  figure: { zh: "图", en: "Figure" },
};

export function artifactKindLabel(kind: string | null | undefined, lang: Language = "zh"): string {
  if (!kind) return say({ zh: "产物", en: "Artifact" }, lang);
  const label = ARTIFACT_KIND_LABEL[kind];
  return label ? say(label, lang) : kind;
}
