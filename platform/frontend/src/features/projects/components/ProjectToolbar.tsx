"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Archive, BookMarked, FlaskConical, FolderTree, Target } from "lucide-react";
import { useProject } from "../hooks/useProjects";
import { projectIdentity } from "../lib/project-identity";
import { useLanguage, useT } from "@/shared/i18n";
import { cn } from "@/shared/ui";

export function ProjectToolbar({ projectId }: { projectId: string }) {
  const t = useT();
  const language = useLanguage();
  const pathname = usePathname() ?? "";
  const project = useProject(projectId);
  const identity = projectIdentity(project.data, { loading: project.isLoading, error: project.isError }, language);
  const root = `/projects/${encodeURIComponent(projectId)}`;
  const tabs = [
    { id: "chat", label: { zh: "对话", en: "Chat" }, icon: FlaskConical, active: /\/(sessions\/[^/]+|chat|research)$/.test(pathname) },
    { id: "files", label: { zh: "文件", en: "Files" }, icon: FolderTree, active: pathname.startsWith(`${root}/files`) },
    { id: "outputs", label: { zh: "成果", en: "Outputs" }, icon: Archive, active: pathname.startsWith(`${root}/outputs`) },
    { id: "research-state", label: { zh: "进展", en: "Progress" }, icon: Target, active: pathname.startsWith(`${root}/research-state`) },
    { id: "memory", label: { zh: "记忆", en: "Memory" }, icon: BookMarked, active: pathname.startsWith(`${root}/memory`) },
  ];
  return (
    <header className="project-workspace-toolbar">
      <div className="project-workspace-identity" title={`${identity.name} · ${identity.meta}`}>
        <strong>{identity.name}</strong>
        <small>{identity.meta}</small>
      </div>
      <nav aria-label={t({ zh: "项目工具", en: "Project tools" })}>
        {tabs.map(({ id, label, icon: Icon, active }) => <Link key={id} href={`${root}/${id}`} data-tour={id}
          className={cn("project-workspace-tool", active && "is-active")} aria-current={active ? "page" : undefined}>
          <Icon size={14} /><span>{t(label)}</span>
        </Link>)}
      </nav>
    </header>
  );
}
