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

export function artifactIcon(type: string): LucideIcon {
  return ARTIFACT_ICONS[type] ?? FileText;
}
