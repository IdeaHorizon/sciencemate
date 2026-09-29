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
              zh: "Project 就是一个 Git 仓库：每个节点拥有自己的目录，产出直接落成文件。这里看的是「一共有什么」；「这一轮改了什么」在会话里看。你自己的文件从「项目材料」传进来，agent 在新会话里直接可读。",
              en: "A Project is a Git repository: every node owns a directory and writes its outputs straight to files. This page answers \"what is here in total\"; \"what changed this turn\" lives in the session. Your own files come in through Project materials and the agent can read them in a new session.",
            })}
          </p>
        </div>
      </header>
      <ProjectFilesView projectId={projectId} />
    </div>
  );
}
