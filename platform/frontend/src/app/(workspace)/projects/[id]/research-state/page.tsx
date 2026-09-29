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
          <h1 className="page-title">{t({ zh: "研究进展", en: "Research state" })}</h1>
          <p className="page-subtitle">
            {t({
              zh: "分析维护着这本裁定账：哪些假设还开着，哪些被支持或被推翻、凭的什么证据，以及计划说下一步做什么。有版本，跟着 Git 走。",
              en: "Analysis keeps the adjudication ledger: which hypotheses are still open, which were supported or refuted and on what evidence, and what the plan says to do next. Versioned and tracked in Git.",
            })}
          </p>
        </div>
      </header>
      <ResearchStateView projectId={projectId} />
    </div>
  );
}
