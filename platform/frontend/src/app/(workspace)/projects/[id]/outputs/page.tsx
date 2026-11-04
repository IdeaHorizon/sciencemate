"use client";

import { ProjectOutputsView } from "@/features/catalog/components/ProjectOutputsView";
import { useProjectId } from "@/shared/routing/route-params";
import { useT } from "@/shared/i18n";

export default function ProjectOutputsPage() {
  const t = useT();
  // 从地址栏读，不从服务端 params 读：静态导出下后者永远是占位段 `_`。
  const projectId = useProjectId();
  return (
    // page-fills-height：右栏要能撑满窗口高度，否则一份 11 页的 PDF 只露一条。
    <div className="page page-fills-height">
      <header className="page-header">
        <div>
          <h1 className="page-title">{t({ zh: "研究成果", en: "Research outputs" })}</h1>
          <p className="page-subtitle">
            {t({
              zh: "这个项目的所有成果。交付物排在最前，点击即可在右侧打开；要看完整的文件目录，请前往「项目文件」。",
              en: "Everything this project has produced. Deliverables come first and open in the side panel; for the full file layout, see Project files.",
            })}
          </p>
        </div>
      </header>
      <ProjectOutputsView projectId={projectId} />
    </div>
  );
}
