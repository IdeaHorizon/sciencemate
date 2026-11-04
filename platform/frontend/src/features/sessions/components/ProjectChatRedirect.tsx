"use client";

import { useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { canCreateSessions, useAuth } from "@/features/auth";
import { Skeleton } from "@/shared/ui";
import { latestSessionId, recalledProjectSession, type SessionDataMode } from "../api/session-repository";
import { useProjectSessions, useSessionMutations } from "../hooks/useSessions";
import { useT } from "@/shared/i18n";

export function ProjectChatRedirect({ projectId, mode = "api" }: { projectId: string; mode?: SessionDataMode }) {
  const t = useT();
  const router = useRouter();
  const { user } = useAuth();
  const sessions = useProjectSessions(projectId, mode);
  const mutations = useSessionMutations(projectId);
  const handled = useRef(false);

  useEffect(() => {
    if (!sessions.data || handled.current) return;
    handled.current = true;
    const remembered = recalledProjectSession(projectId);
    const validRemembered = sessions.data.find((session) => session.id === remembered && session.lifecycleStatus !== "archived");
    const sessionId = validRemembered?.id ?? latestSessionId(sessions.data);
    const suffix = mode === "fixture" ? "?mode=demo" : "";
    if (sessionId) {
      router.replace(`/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}${suffix}`);
      return;
    }
    if (mode === "api" && canCreateSessions(user)) {
      void mutations.create.mutateAsync(t({ zh: "新的研究", en: "New research" })).then((created) => {
        router.replace(`/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(created.id)}`);
      });
      return;
    }
    router.replace(`/projects/${encodeURIComponent(projectId)}/research${suffix}`);
  }, [mode, mutations.create, projectId, router, sessions.data, user]);

  if (sessions.isError) {
    return (
      <div className="session-workspace-error" role="alert">
        <h1>{t({ zh: "无法加载会话列表", en: "Could not load sessions" })}</h1>
        <p>{sessions.error instanceof Error ? sessions.error.message : t({ zh: "请稍后重试。", en: "Please try again later." })}</p>
        <button type="button" onClick={() => void sessions.refetch()}>{t({ zh: "重试", en: "Retry" })}</button>
      </div>
    );
  }

  return <div className="session-redirect-loading"><Skeleton height={54} rounded="sm" /><span>{t({ zh: "正在打开会话…", en: "Opening session…" })}</span></div>;
}
