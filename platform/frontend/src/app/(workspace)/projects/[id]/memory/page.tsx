"use client";


import { ProjectMemoryView } from "@/features/memory/components/ProjectMemoryView";
import { useProjectId } from "@/shared/routing/route-params";
import { useT } from "@/shared/i18n";

export default function ProjectMemoryPage() {
  const t = useT();
  const projectId = useProjectId();
  // 从地址栏读，不从服务端 params 读（静态导出下后者是占位段 `_`）。
  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 className="page-title">{t({ zh: "项目记忆", en: "Memory" })}</h1>
          <p className="page-subtitle">
            {t({
              zh: "项目记忆就是仓库根上的 MEMORY.md —— 一份跟着 Git 走、看得见改动的正文，由 curator 写。",
              en: "Project memory lives in MEMORY.md at the repository root — one tracked, diffable source written by the curator.",
            })}
          </p>
        </div>
      </header>
      <ProjectMemoryView projectId={projectId} />
    </div>
  );
}
