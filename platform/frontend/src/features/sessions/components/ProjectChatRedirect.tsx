"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, FlaskConical, Loader2 } from "lucide-react";
import { canCreateSessions, useAuth } from "@/features/auth";
import { useProject } from "@/features/projects/hooks/useProjects";
import { Skeleton } from "@/shared/ui";
import { latestSessionId, recalledProjectSession, type SessionDataMode } from "../api/session-repository";
import { useProjectSessions, useSessionMutations } from "../hooks/useSessions";
import { useT } from "@/shared/i18n";
import { stageSessionDraft } from "../lib/session-draft";

export function ProjectChatRedirect({ projectId, mode = "api" }: { projectId: string; mode?: SessionDataMode }) {
  const t = useT();
  const router = useRouter();
  const { user } = useAuth();
  const project = useProject(projectId);
  const sessions = useProjectSessions(projectId, mode);
  const mutations = useSessionMutations(projectId);
  const [draft, setDraft] = useState("");
  const [opening, setOpening] = useState(false);
  const drivesThisProject = project.data?.capabilities?.includes("drive") ?? false;
  const canCreate = canCreateSessions(user) && mode === "api" && drivesThisProject && project.data?.status !== "archived";

  useEffect(() => {
    if (!sessions.data || opening) return;
    const remembered = recalledProjectSession(projectId);
    const validRemembered = sessions.data.find((session) => session.id === remembered && session.lifecycleStatus !== "archived");
    const sessionId = validRemembered?.id ?? latestSessionId(sessions.data);
    const suffix = mode === "fixture" ? "?mode=demo" : "";
    if (sessionId) {
      router.replace(`/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}${suffix}`);
      return;
    }
  }, [mode, opening, projectId, router, sessions.data]);

  const start = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canCreate || opening) return;
    setOpening(true);
    try {
      const created = await mutations.create.mutateAsync(t({ zh: "新的研究", en: "New research" }));
      stageSessionDraft(projectId, created.id, draft);
      router.push(`/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(created.id)}`);
    } catch { setOpening(false); }
  };

  if (sessions.isError) {
    return (
      <div className="session-workspace-error" role="alert">
        <h1>{t({ zh: "无法加载会话列表", en: "Could not load sessions" })}</h1>
        <p>{sessions.error instanceof Error ? sessions.error.message : t({ zh: "请稍后重试。", en: "Please try again later." })}</p>
        <button type="button" onClick={() => void sessions.refetch()}>{t({ zh: "重试", en: "Retry" })}</button>
      </div>
    );
  }

  const hasSession = sessions.data?.some((session) => session.lifecycleStatus !== "archived");
  if (!sessions.data || hasSession) return <div className="session-redirect-loading"><Skeleton height={54} rounded="sm" /><span>{t({ zh: "正在打开会话…", en: "Opening session…" })}</span></div>;

  return (
    <div className="project-start">
      <div className="project-start-content">
        <FlaskConical size={27} strokeWidth={1.4} />
        <h1>{t({ zh: "从一个研究问题开始", en: "Start with a research question" })}</h1>
        <p>{t({ zh: "描述你想解决的问题，会话会出现在左侧这个项目下。", en: "Describe what you want to work on. Your session will appear under this project in the sidebar." })}</p>
        {canCreate ? <form onSubmit={(event) => void start(event)}>
          <textarea value={draft} onChange={(event) => setDraft(event.target.value)} rows={4}
            aria-label={t({ zh: "研究问题", en: "Research question" })}
            placeholder={t({ zh: "想研究什么？已有的材料、限制和预期成果也可以写在这里。", en: "What would you like to research? Include any materials, constraints or expected outcomes." })} />
          <footer><span>{t({ zh: "下一步可补充材料并发送", en: "Add materials and send in the next step" })}</span>
            <button type="submit" disabled={opening}>{opening ? <Loader2 size={14} className="spin" /> : <ArrowRight size={14} />}{t({ zh: "打开新会话", en: "Open new session" })}</button>
          </footer>
        </form> : <p className="project-start-readonly">{t({ zh: "当前项目没有可继续的会话。你可以从顶部查看项目文件、成果和进展。", en: "There are no active sessions. You can view project files, outputs and progress above." })}</p>}
      </div>
    </div>
  );
}
