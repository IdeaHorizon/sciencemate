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
          <h1 className="page-title">{t({ zh: "研究产出", en: "Research outputs" })}</h1>
          <p className="page-subtitle">
            {t({
              zh: "这个项目做出了什么 —— 交付物排在最前，点一下就在右栏打开。分层是排序不是过滤：工作过程默认收起，但照样列得出来。想看盘上的目录结构去「Project files」。",
              en: "What this project produced. Deliverables come first and open in the side panel with one click. The tiers sort, they do not filter: working files start collapsed but are all still listed. For the directory layout on disk, go to Project files.",
            })}
          </p>
        </div>
      </header>
      <ProjectOutputsView projectId={projectId} />
    </div>
  );
}
