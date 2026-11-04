"use client";


import { ResearchStateView } from "@/features/research-state/components/ResearchStateView";
import { useProjectId } from "@/shared/routing/route-params";
import { useT } from "@/shared/i18n";

export default function ResearchStatePage() {
  const t = useT();
  const projectId = useProjectId();
  // 从地址栏读，不从服务端 params 读（静态导出下后者是占位段 `_`）。
  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 className="page-title">{t({ zh: "研究进展", en: "Research progress" })}</h1>
          <p className="page-subtitle">
            {t({
              zh: "各个假说目前的状态：哪些还待验证，哪些已被支持或推翻、依据是什么，以及下一步计划。",
              en: "Where each hypothesis stands: which are still open, which were supported or refuted and on what evidence, and what comes next.",
            })}
          </p>
        </div>
      </header>
      <ResearchStateView projectId={projectId} />
    </div>
  );
}
