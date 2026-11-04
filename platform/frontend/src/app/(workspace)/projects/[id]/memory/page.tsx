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
          <h1 className="page-title">{t({ zh: "项目记忆", en: "Project memory" })}</h1>
          <p className="page-subtitle">
            {t({
              zh: "助手在这个项目中积累的决策和发现，保存在 MEMORY.md 中，每次修改都有记录。",
              en: "Decisions and findings the assistant has gathered in this project, kept in MEMORY.md with every change recorded.",
            })}
          </p>
        </div>
      </header>
      <ProjectMemoryView projectId={projectId} />
    </div>
  );
}
