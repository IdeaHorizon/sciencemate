import {
  Brain, ClipboardList, Code2, Database, Eye, FileSearch, FileText,
  Search, Zap, type LucideIcon,
} from "lucide-react";

export const NODE_TYPE_ICONS: Record<string, LucideIcon> = {
  exploration: Zap,
  survey: Database,
  planning: ClipboardList,
  experiment: Code2,
  data_process: FileSearch,
  analysis: Brain,
  writing: FileText,
  review: Eye,
  search: Search,
};

export const NODE_TYPE_LABELS: Record<string, string> = {
  exploration: "Exploration",
  survey: "Survey",
  planning: "Planning",
  experiment: "Experiment",
  data_process: "Data Process",
  analysis: "Analysis",
  writing: "Writing",
  review: "Review",
};

export function nodeIcon(type: string): LucideIcon {
  return NODE_TYPE_ICONS[type] ?? FileText;
}

export function nodeLabel(type: string): string {
  return NODE_TYPE_LABELS[type] ?? type;
}
