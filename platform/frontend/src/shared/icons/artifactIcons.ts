import {
  BarChart3, Brain, ClipboardList, Code2, Database, FileText,
  Image as ImageIcon, Shield, Table2, TerminalSquare, type LucideIcon,
} from "lucide-react";

export const ARTIFACT_ICONS: Record<string, LucideIcon> = {
  survey_report: Database,
  research_plan: ClipboardList,
  experiment_log: Code2,
  analysis_report: Brain,
  paper_outline: FileText,
  paper_draft: FileText,
  paper_tex: FileText,
  paper_pdf: FileText,
  review_report: Shield,
  dataset: TerminalSquare,
  code: Code2,
  data_profile: BarChart3,
  data_pipeline: TerminalSquare,
  figure: ImageIcon,
  table: Table2,
  report: FileText,
};

export const ARTIFACT_CATEGORIES: Array<{ label: string; icon: LucideIcon; types: string[] }> = [
  { label: "Research Pipeline", icon: Database, types: ["survey_report", "research_plan", "experiment_log", "analysis_report"] },
  { label: "Paper", icon: FileText, types: ["paper_outline", "paper_draft", "paper_tex", "paper_pdf", "review_report"] },
  { label: "Data & Code", icon: Code2, types: ["dataset", "code", "data_profile", "data_pipeline"] },
  { label: "Tables", icon: Table2, types: ["table"] },
  { label: "Figures", icon: ImageIcon, types: ["figure"] },
];

export function artifactIcon(type: string): LucideIcon {
  return ARTIFACT_ICONS[type] ?? FileText;
}
