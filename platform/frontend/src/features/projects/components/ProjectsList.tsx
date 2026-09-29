"use client";

/**
 * 项目清单 —— 按**家**分组。
 *
 * 本机一组，每个组织一组。这是"一个项目只有一个家"在界面上的样子：用户一眼看得出
 * 哪些东西在自己电脑上、哪些在组里。
 *
 * 那台机器关着的时候，它那一组**照样摆出来**（上一次问到的名字），只是灰着并说
 * 一句"连不上"。抹掉的话，用户看到的是自己的项目凭空少了一半。
 */

import { otherHomes } from "../homes";
import { Building2, FolderKanban, Laptop, Plus } from "lucide-react";
import Link from "next/link";
import { Button, Empty, Skeleton } from "@/shared/ui";
import type { Project, ProjectHome } from "@/lib/api";
import { useProjects } from "../hooks/useProjects";
import { ProjectCard } from "./ProjectCard";
import { useT, type Phrase } from "@/shared/i18n";

type Group = { key: string; home: ProjectHome | null; projects: Project[] };

/** 按家分组，本机在前；组织之间按名字排，和「组织」那一页同一个次序。 */
function byHome(projects: Project[]): Group[] {
  const groups = new Map<string, Group>();
  for (const project of projects) {
    const home = project.home ?? null;
    const key = home?.kind === "organisation" ? home.connection_id : "local";
    if (!groups.has(key)) groups.set(key, { key, home, projects: [] });
    groups.get(key)!.projects.push(project);
  }
  return [...groups.values()].sort((a, b) => {
    if (a.key === "local") return -1;
    if (b.key === "local") return 1;
    const left = a.home?.kind === "organisation" ? a.home.name : "";
    const right = b.home?.kind === "organisation" ? b.home.name : "";
    return left.localeCompare(right);
  });
}

export function ProjectsList({ onCreate }: { onCreate?: () => void }) {
  const t = useT();
  const { data: projects, isLoading } = useProjects();

  if (isLoading && !projects) {
    return (
      <div className="project-list project-list-loading">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} height={68} rounded="sm" />
        ))}
      </div>
    );
  }

  if (!projects?.length) {
    return (
      <Empty
        icon={<FolderKanban size={20} />}
        title={t({ zh: "还没有项目", en: "No projects yet" })}
        hint={t({ zh: "建一个项目，研究会话就有了共享的上下文、证据边界和发表历史。", en: "Create a project to give research sessions a shared context, evidence boundary, and publication history." })}
        // 这句话叫人「建一个项目」，按钮就得在这句话旁边。
        //
        // 原来只有右上角一个细边框的「新建项目」—— 2026-09-17 走查时用户盯着
        // 屏幕正中这块空状态问「还是没有看到创建项目的啊？」。他没找错地方：
        // 第一次来的人眼睛就落在这儿，而这儿写着该做什么，却没有做那件事的东西。
        action={onCreate && (
          <Button size="sm" variant="primary" iconLeft={<Plus size={13} />} onClick={onCreate}>
            {t({ zh: "新建项目", en: "New project" })}
          </Button>
        )}
      />
    );
  }

  // 归档的收在最下面、折起来：不做了的项目不该和在做的挤在一起，但它们还在、点开能看、能恢复。
  const live = projects.filter((p) => p.status !== "archived");
  const archived = projects.filter((p) => p.status === "archived");
  const folded = archived.length > 0 && (
    <details className="project-archived-group">
      <summary>{t({ zh: "已归档 {n} 个", en: "{n} archived" }, { n: archived.length })}</summary>
      <div className="project-list">
        {archived.map((p) => <ProjectCard key={p.id} project={p} />)}
      </div>
    </details>
  );

  const groups = byHome(live);
  // 只有本机一组 = 这台机器没连过任何组织。那就不画分组的壳 —— 一个只有一组的
  // 分组界面，是在给一件不存在的选择立个标题。
  if (groups.length === 0 || (groups.length === 1 && groups[0].key === "local")) {
    return (
      <>
        {live.length > 0 ? (
          <div className="project-list">
            {live.map((p) => <ProjectCard key={p.id} project={p} />)}
          </div>
        ) : (
          <p className="project-list-note">{t({ zh: "没有进行中的项目。", en: "Nothing in progress." })}</p>
        )}
        {folded}
      </>
    );
  }

  return (
    <div className="project-groups">
      {groups.map((group) => {
        const org = group.home?.kind === "organisation" ? group.home : null;
        const dark = org !== null && !org.reachable;
        const why: Phrase | null = !org ? null
          : org.needs_sign_in ? { zh: "要重新登录一下", en: "Needs signing in again" }
          : !org.reachable ? { zh: "连不上 —— 这是上次看到的", en: "Out of reach — this is what was last seen" }
          : null;
        return (
          <section key={group.key} className={`project-group${dark ? " is-out-of-reach" : ""}`}>
            <h2 className="project-group-name">
              {org ? <Building2 size={13} /> : <Laptop size={13} />}
              {org ? org.name : t({ zh: "本机", en: "This computer" })}
              {why && (
                <span className="project-group-why">
                  {t(why)}
                  {org?.needs_sign_in && otherHomes.pagePath && (
                    <Link href={otherHomes.pagePath}>{t({ zh: "去登录", en: "Sign in" })}</Link>
                  )}
                </span>
              )}
            </h2>
            <div className="project-list">
              {group.projects.map((p) => <ProjectCard key={p.id} project={p} />)}
            </div>
          </section>
        );
      })}
      {folded}
    </div>
  );
}
