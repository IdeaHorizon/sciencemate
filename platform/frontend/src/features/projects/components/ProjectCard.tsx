"use client";

import Link from "next/link";
import { ArrowRight } from "lucide-react";
import { ContextMenu, useContextMenu } from "@/shared/ui/ContextMenu";
import { useDeleteProject } from "../hooks/useProjects";
import { formatRelativeTime } from "@/shared/format/dates";
import { useLanguage, useT } from "@/shared/i18n";
import type { Project } from "@/lib/api";

export function ProjectCard({ project }: { project: Project }) {
  // 删除项目不常驻：列表里每行摆一个删除按钮，误点的代价是整个课题。
  // 右键是"我确实在找这个操作"的信号（wangd 2026-08-19）。
  const lang = useLanguage();
  const t = useT();
  const menu = useContextMenu();
  const remove = useDeleteProject();
  const ownership = project.scope?.name || project.scope_name || project.owner?.display_name || project.owner_name || t({ zh: "个人项目", en: "Personal project" });

  return (
    <>
    <Link
      href={`/projects/${encodeURIComponent(project.id)}/research`}
      className="project-list-row"
      onContextMenu={menu.onContextMenu}
    >
      <i className={`project-status-${project.status}`} />
      <span className="project-list-main">
        <strong>{project.name}</strong>
        <small>{project.description || project.research_domain || t({ zh: "还没写研究简介", en: "No research brief yet" })}</small>
      </span>
      <span className="project-list-context">
        <strong>{ownership}</strong>
        <small>{project.member_count === null || project.member_count === undefined ? t({ zh: "读不到成员数", en: "Member count unavailable" }) : t({ zh: "{count} 个成员", en: "{count} member{s}" }, {
          count: project.member_count,
          s: project.member_count === 1 ? "" : "s",
        })}</small>
      </span>
      {/* Session 是 Project 内的概念 —— 项目列表这一层只说 Project 自己的状态。
          进了项目才有 Session（wangd 2026-08-21）。 */}
      <span className="project-list-context project-list-activity">
        <strong>{project.status.replaceAll("_", " ")}</strong>
        <small>Updated {formatRelativeTime(project.updated_at, lang)}</small>
      </span>
      <ArrowRight size={13} aria-hidden="true" />
    </Link>
    <ContextMenu
      at={menu.at}
      onClose={menu.close}
      items={[{
        label: t({ zh: "删除项目", en: "Delete project" }),
        danger: true,
        // 确认里带上项目名 —— 列表每行长得差不多，"确定删除吗"回答不了
        // "删的是我选中的那个吗"。
        confirm: t({ zh: `确定删除项目「${project.name}」吗？这会连同它的会话与记录一起删除，且不可撤销。`, en: `Delete the project "${project.name}"? Its sessions and records go with it, and this cannot be undone.` }),
        onSelect: () => remove.mutate(project.id),
      }]}
    />
    </>
  );
}
