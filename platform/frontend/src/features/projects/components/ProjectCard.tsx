"use client";

import Link from "next/link";
import { ArrowRight } from "lucide-react";
import { ContextMenu, useContextMenu } from "@/shared/ui/ContextMenu";
import { useDeleteProject } from "../hooks/useProjects";
import { formatRelativeTime } from "@/shared/format/dates";
import { useLanguage, useT } from "@/shared/i18n";
import { projectActivity } from "../lib/project-activity";
import { useProjectSessions } from "@/features/sessions/hooks/useSessions";
import type { Project } from "@/lib/api";

export function ProjectCard({ project }: { project: Project }) {
  // 删除项目不常驻：列表里每行摆一个删除按钮，误点的代价是整个课题。
  // 右键是"我确实在找这个操作"的信号（wangd 2026-08-19）。
  const lang = useLanguage();
  const t = useT();
  const mode = process.env.NEXT_PUBLIC_SESSION_MODE === "demo" ? "fixture" : "api";
  const reachable = !project.home || project.home.kind === "local" || project.home.reachable;
  const enabled = project.status !== "archived" && reachable;
  // Share the sidebar's query and execution projection, including remote routing.
  // Poll only while the project list is mounted; hidden windows use Query defaults.
  const sessions = useProjectSessions(enabled ? project.id : undefined, mode, {
    refetchInterval: enabled && mode === "api" ? 15000 : false,
  });
  const activity = projectActivity(project.status, sessions.data, !reachable || sessions.isError);
  const menu = useContextMenu();
  const remove = useDeleteProject();
  const ownership = project.scope?.name || project.scope_name || project.owner?.display_name || project.owner_name || t({ zh: "个人项目", en: "Personal project" });

  return (
    <>
    <Link
      href={`/projects/${encodeURIComponent(project.id)}/chat`}
      className="project-list-row"
      onContextMenu={menu.onContextMenu}
    >
      <i className={`project-activity-${activity.tone}`} title={t(activity.label)} aria-hidden="true" />
      <span className="project-list-main">
        <strong>{project.name}</strong>
        <small>{project.description || project.research_domain || t({ zh: "还没有研究说明", en: "No research brief yet" })}</small>
      </span>
      <span className="project-list-context">
        <strong>{ownership}</strong>
        <small>{project.member_count === null || project.member_count === undefined ? t({ zh: "成员数未知", en: "Member count unknown" }) : t({ zh: "{count} 个成员", en: "{count} member{s}" }, {
          count: project.member_count,
          s: project.member_count === 1 ? "" : "s",
        })}</small>
      </span>
      {/* Summarize execution without treating an unarchived project as completed. */}
      <span className="project-list-context project-list-activity">
        <strong>{t(activity.label)}</strong>
        <small>{t({ zh: "{time}更新", en: "Updated {time}" }, { time: formatRelativeTime(project.updated_at, lang) })}</small>
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
        confirm: t({ zh: `删除项目「${project.name}」？项目中的会话和记录会一并删除，且无法恢复。`, en: `Delete the project "${project.name}"? Its sessions and records go with it, and this cannot be undone.` }),
        onSelect: () => remove.mutate(project.id),
      }]}
    />
    </>
  );
}
