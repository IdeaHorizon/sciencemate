"use client";

import Link from "next/link";

import { Activity, ArrowRight, FlaskConical } from "lucide-react";
import { useProjectRuns } from "@/features/execution";
import { useProject } from "@/features/projects/hooks/useProjects";
import { useProjectId } from "@/shared/routing/route-params";
import { useT } from "@/shared/i18n";

export default function ProjectIndex() {
  const t = useT();
  const projectId = useProjectId();
  const project = useProject(projectId);
  const runs = useProjectRuns(projectId);

  return (
    <div className="platform-index-page">
      <header>
        <span>{t({ zh: "项目总览", en: "Project overview" })}</span>
        <h1>{project.data?.name ?? (project.isError ? t({ zh: "读不到这个项目", en: "Project unavailable" }) : t({ zh: "载入中…", en: "Loading…" }))}</h1>
        <p>{project.data?.description || project.data?.research_domain || t({ zh: "打开研究对话，说清楚下一步要做什么。", en: "Open the research conversation to define the next objective." })}</p>
      </header>
      <section>
        <div className="platform-section-heading"><h2>{t({ zh: "工作区", en: "Workspace" })}</h2><span>{project.data?.status?.replaceAll("_", " ") ?? ""}</span></div>
        <Link href={`/projects/${projectId}/chat`} className="platform-index-row">
          <FlaskConical size={17} />
          <span><strong>{t({ zh: "研究对话", en: "Research conversation" })}</strong><small>{t({ zh: "在这个项目的上下文里提问、规划、接着做", en: "Ask, plan and continue work in this project context" })}</small></span>
          <span>{t({ zh: "打开", en: "Open" })}</span>
          <ArrowRight size={15} />
        </Link>
        {/* 这里曾经是「Run activity」那一行，链到 /projects/<id>/activity。那一页
            是 runs 列表，而「这个项目在跑什么」Research 那一页和侧栏的 Recent
            sessions 已经各答一遍 —— 三处同答一个问题。留下计数（它是这一页的
            事实），但不再送人去第三个答案。 */}
        <div className="platform-index-row">
          <Activity size={17} />
          <span><strong>{t({ zh: "执行记录", en: "Run activity" })}</strong><small>{t({ zh: "在 Research 里按会话看", en: "See it per Session under Research" })}</small></span>
          <span>{runs.data ? t({ zh: `${runs.data.items.length} 次执行`, en: `${runs.data.items.length} runs` }) : t({ zh: "载入中", en: "Loading project" })}</span>
        </div>
      </section>
    </div>
  );
}
