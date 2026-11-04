"use client";


import { ProjectFilesView } from "@/features/sessions/components/ProjectFilesView";
import { useProjectId } from "@/shared/routing/route-params";
import { useT } from "@/shared/i18n";

export default function ProjectFilesPage() {
  const t = useT();
  const projectId = useProjectId();
  // 从地址栏读，不从服务端 params 读：静态导出下后者永远是占位段 `_`。
  return (
    // page-fills-height：右栏要能撑满窗口高度。`.page` 默认是块级、高度跟着
    // 内容走，于是右栏塌成内容高 —— 一份 11 页的 PDF 只露出 420px 的一条。
    // 会话页没这个问题（那条路由自己是 grid + height:100%），所以这条只加在
    // 需要它的页面上，不去动所有 `.page`。
    <div className="page page-fills-height">
      <header className="page-header">
        <div>
          <h1 className="page-title">{t({ zh: "项目文件", en: "Project files" })}</h1>
          <p className="page-subtitle">
            {t({
              zh: "这个项目中的所有文件。每次执行改动了哪些文件，可以在对应会话里查看；要添加自己的文件，在会话中点「添加文件」。",
              en: "All the files in this project. To see what a run changed, open its session; to add your own files, use Add a file in a session.",
            })}
          </p>
        </div>
      </header>
      <ProjectFilesView projectId={projectId} />
    </div>
  );
}
