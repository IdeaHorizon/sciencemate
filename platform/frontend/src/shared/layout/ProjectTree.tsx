"use client";

import { useEffect, useId, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ChevronRight, Loader2, MoreHorizontal, Plus, Search, X } from "lucide-react";
import { type Project } from "@/lib/api";
import { canCreateSessions, useAuth } from "@/features/auth";
import { useProject, useProjects } from "@/features/projects/hooks/useProjects";
import { useProjectSessions, useSessionMutations, type ResearchSession } from "@/features/sessions";
import { useProjectId, useSessionId } from "@/shared/routing/route-params";
import { ContextMenu, useContextMenu } from "@/shared/ui/ContextMenu";
import { cn } from "@/shared/ui";
import { useInterfaceSettings } from "@/features/settings/InterfaceSettingsProvider";
import { useT } from "@/shared/i18n";
import { visibleSessions } from "./project-navigation";

function SessionItem({ session, selected, canArchive, mode }: {
  session: ResearchSession; selected: boolean; canArchive: boolean; mode: "api" | "fixture";
}) {
  const t = useT();
  const menu = useContextMenu();
  const mutations = useSessionMutations(session.projectId);
  const { settings: interfaceSettings } = useInterfaceSettings();
  const usage = interfaceSettings.show_run_usage && session.usage.totalTokens > 0
    ? ` · ${session.usage.totalTokens.toLocaleString()} tokens` : "";
  return (
    <>
      <Link
        href={`/projects/${encodeURIComponent(session.projectId)}/sessions/${encodeURIComponent(session.id)}${mode === "fixture" ? "?mode=demo" : ""}`}
        className={cn("project-tree-session", selected && "is-selected")}
        aria-current={selected ? "page" : undefined}
        title={`${session.title} · ${session.execution.label}${usage}`}
        onContextMenu={canArchive ? menu.onContextMenu : undefined}
      >
        <i className={`session-state-dot state-${session.execution.phase}`} data-waiting={session.execution.waitingOn?.kind} data-outcome={session.execution.outcome ?? undefined} aria-hidden="true" />
        <span>{session.title}</span>
      </Link>
      {canArchive && <ContextMenu at={menu.at} onClose={menu.close} items={[{
        label: t({ zh: "归档 / 删除会话", en: "Archive or delete session" }),
        danger: true,
        confirm: t({ zh: `归档或删除「${session.title}」？有内容的会话会被归档，空会话会被直接删除。`, en: `Archive or delete "${session.title}"? Empty sessions are deleted; others are archived.` }),
        onSelect: () => mutations.archive.mutate(session.id),
      }]} />}
    </>
  );
}

function ProjectBranch({ project, activeProjectId, selectedId, query }: {
  project: Project; activeProjectId: string; selectedId: string; query: string;
}) {
  const t = useT();
  const router = useRouter();
  const { user } = useAuth();
  const childrenId = useId();
  const [expanded, setExpanded] = useState(project.id === activeProjectId);
  const [limit, setLimit] = useState(6);
  const [showArchived, setShowArchived] = useState(false);
  const menu = useContextMenu();
  const active = project.id === activeProjectId;
  const open = expanded || Boolean(query.trim());
  // Collapsed projects do not fetch session lists. Searching loads them on demand.
  const mode = process.env.NEXT_PUBLIC_SESSION_MODE === "demo" ? "fixture" : "api";
  const sessions = useProjectSessions(open ? project.id : undefined, mode);
  const mutations = useSessionMutations(project.id);
  const drivesThisProject = project.capabilities?.includes("drive") ?? false;
  const canCreate = canCreateSessions(user) && mode === "api" && drivesThisProject && project.status !== "archived";
  const canArchive = canCreate;
  const root = `/projects/${encodeURIComponent(project.id)}`;
  const projectMatches = project.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase());
  const needle = projectMatches ? "" : query;
  const live = (sessions.data ?? []).filter((session) => session.lifecycleStatus !== "archived");
  const archived = (sessions.data ?? []).filter((session) => session.lifecycleStatus === "archived");
  const selectedArchived = archived.some((session) => session.id === selectedId);
  const rows = visibleSessions(live, selectedId, limit, needle);
  const archivedRows = visibleSessions(archived, selectedId, archived.length, needle);

  useEffect(() => { if (active) setExpanded(true); }, [active]);

  const createSession = async () => {
    try {
      const created = await mutations.create.mutateAsync(t({ zh: "新的研究", en: "New research" }));
      setExpanded(true);
      router.push(`${root}/sessions/${encodeURIComponent(created.id)}`);
    } catch { /* The mutation presents its error and leaves the current session open. */ }
  };

  if (query.trim() && !projectMatches && sessions.data && rows.length === 0 && archivedRows.length === 0) return null;

  return (
    <section className={cn("project-tree-branch", active && "is-active")}>
      <div className="project-tree-heading" onContextMenu={menu.onContextMenu}>
        <button type="button" className="project-tree-disclosure" aria-expanded={open} aria-controls={childrenId}
          aria-label={t({ zh: "展开或收起 {name}", en: "Expand or collapse {name}" }, { name: project.name })}
          onClick={() => setExpanded(!open)}><ChevronRight size={13} className={open ? "is-open" : ""} /></button>
        <Link href={`${root}/chat`} className="project-tree-name" title={project.name} onClick={() => setExpanded(true)}>{project.name}</Link>
        <div className="project-tree-actions">
          {canCreate && <button type="button" onClick={() => void createSession()} disabled={mutations.create.isPending}
            aria-label={t({ zh: "在 {name} 新建会话", en: "New session in {name}" }, { name: project.name })}>
            {mutations.create.isPending ? <Loader2 size={13} className="spin" /> : <Plus size={13} />}
          </button>}
          <button type="button" aria-label={t({ zh: "{name} 的项目菜单", en: "Project menu for {name}" }, { name: project.name })}
            onClick={(event) => { const rect = event.currentTarget.getBoundingClientRect(); menu.openAt(rect.left, rect.bottom); }}>
            <MoreHorizontal size={14} />
          </button>
        </div>
      </div>
      <ContextMenu at={menu.at} onClose={menu.close} items={[
        { label: t({ zh: "项目设置", en: "Project settings" }), onSelect: () => router.push(`${root}/settings`) },
        { label: t(showArchived ? { zh: "隐藏已归档会话", en: "Hide archived sessions" } : { zh: "显示已归档会话", en: "Show archived sessions" }),
          onSelect: () => { setExpanded(true); setShowArchived(!showArchived); } },
      ]} />
      {open && <div className="project-tree-sessions" id={childrenId}>
        {sessions.isLoading && !sessions.data && <p className="project-tree-message">{t({ zh: "加载会话…", en: "Loading sessions…" })}</p>}
        {sessions.isError && <button type="button" className="project-tree-more" onClick={() => void sessions.refetch()}>{t({ zh: "加载失败，点击重试", en: "Could not load. Retry" })}</button>}
        {rows.map((session) => <SessionItem key={session.id} session={session} selected={active && session.id === selectedId} canArchive={canArchive} mode={mode} />)}
        {!query.trim() && live.length > rows.length && <button type="button" className="project-tree-more" onClick={() => setLimit(limit + 12)}>{t({ zh: "显示更多会话", en: "Show more sessions" })}</button>}
        {(showArchived || selectedArchived || Boolean(query.trim())) && archivedRows.length > 0 && <>
          <span className="project-tree-archived-label">{t({ zh: "已归档", en: "Archived" })}</span>
          {archivedRows.map((session) => <SessionItem key={session.id} session={session} selected={active && session.id === selectedId} canArchive={false} mode={mode} />)}
        </>}
        {sessions.data && live.length === 0 && !(showArchived || selectedArchived || query.trim()) && <Link href={`${root}/chat`} className="project-tree-more">{t({ zh: "开始新的研究", en: "Start new research" })}</Link>}
      </div>}
    </section>
  );
}

export function ProjectTree() {
  const t = useT();
  const projects = useProjects();
  const activeProjectId = useProjectId();
  const selectedId = useSessionId();
  const missingActive = Boolean(activeProjectId && projects.data && !projects.data.some((project) => project.id === activeProjectId));
  const activeProject = useProject(missingActive ? activeProjectId : undefined);
  const [searching, setSearching] = useState(false);
  const [query, setQuery] = useState("");
  const all = [...(projects.data ?? []), ...(activeProject.data ? [activeProject.data] : [])];
  const visible = all.filter((project) => project.status !== "archived" || project.id === activeProjectId);
  return (
    <nav className="project-tree" aria-label={t({ zh: "项目和会话", en: "Projects and sessions" })} data-tour="research">
      <div className="project-tree-section-heading">
        <Link href="/projects" data-tour="projects">{t({ zh: "项目", en: "Projects" })}</Link>
        <span>
          <button type="button" aria-label={t({ zh: "搜索项目和会话", en: "Search projects and sessions" })} aria-expanded={searching}
            onClick={() => { setSearching(!searching); setQuery(""); }}>{searching ? <X size={13} /> : <Search size={13} />}</button>
          <Link href="/projects?create=1" aria-label={t({ zh: "新建项目", en: "New project" })}><Plus size={14} /></Link>
        </span>
      </div>
      {searching && <input className="project-tree-search" autoFocus type="search" value={query} onChange={(event) => setQuery(event.target.value)}
        placeholder={t({ zh: "搜索项目和会话…", en: "Search projects and sessions…" })} aria-label={t({ zh: "搜索项目和会话", en: "Search projects and sessions" })} />}
      {projects.isLoading && <p className="project-tree-message">{t({ zh: "加载项目…", en: "Loading projects…" })}</p>}
      {projects.isError && <button type="button" className="project-tree-more" onClick={() => void projects.refetch()}>{t({ zh: "项目加载失败，重试", en: "Could not load projects. Retry" })}</button>}
      {projects.data && visible.length === 0 && <Link href="/projects?create=1" className="project-tree-more">{t({ zh: "创建第一个项目", en: "Create your first project" })}</Link>}
      <div className="project-tree-results">
      {visible.map((project) => <ProjectBranch key={project.id} project={project} activeProjectId={activeProjectId} selectedId={selectedId} query={query} />)}
      {query.trim() && <p className="project-tree-no-results">{t({ zh: "没有匹配的项目或会话", en: "No matching projects or sessions" })}</p>}
      </div>
      {all.some((project) => project.status === "archived" && project.id !== activeProjectId) && <Link href="/projects" className="project-tree-more">{t({ zh: "查看已归档项目", en: "View archived projects" })}</Link>}
    </nav>
  );
}
