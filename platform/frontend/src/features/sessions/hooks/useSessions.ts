"use client";

import { useCallback } from "react";
import { skipToken, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/features/auth";
import { qk } from "@/lib/query/keys";
import { useLanguage, useT } from "@/shared/i18n";
import { pushError, pushSuccess } from "@/stores/notification";
import {
  archiveProjectSession,
  cancelRun,
  createProjectSession,
  getProjectSession,
  getSessionChangeSet,
  getSessionProjectTree,
  listSessionConflicts,
  listProjectSessions,
  listSessionMessages,
  publishSessionChanges,
  renameProjectSession,
  resolveSessionConflict,
  type SessionDataMode,
} from "../api/session-repository";
import { invalidateSessionAfterTerminal } from "../lib/session-terminal-invalidation";
import { clearStaleSessionPublishError } from "../lib/publish-error-policy";
import type { ChatTerminalState } from "@/features/chat";

export function useProjectSessions(projectId: string | undefined, mode: SessionDataMode = "api") {
  const { user } = useAuth();
  // 适配时补的几个字（未命名、项目成员）按这一屏的语言写；换语言时
  // InterfaceSettingsProvider 会把所有查询作废重取，不用把语言放进 queryKey。
  const lang = useLanguage();
  return useQuery({
    queryKey: projectId ? qk.sessions(projectId, mode) : ["sessions", "none"],
    queryFn: projectId ? () => listProjectSessions(projectId, lang, user, mode) : skipToken,
  });
}

export function useSession(projectId: string, sessionId: string, mode: SessionDataMode = "api") {
  const { user } = useAuth();
  const lang = useLanguage();
  return useQuery({
    queryKey: qk.session(projectId, sessionId, mode),
    queryFn: () => getProjectSession(projectId, sessionId, lang, user, mode),
  });
}

export function useSessionMessages(projectId: string, sessionId: string, mode: SessionDataMode = "api") {
  return useQuery({
    queryKey: qk.sessionMessages(projectId, sessionId, mode),
    queryFn: () => listSessionMessages(projectId, sessionId, mode),
  });
}

export function useSessionChangeSet(projectId: string, sessionId: string, enabled = true) {
  return useQuery({
    queryKey: qk.sessionChangeSet(projectId, sessionId),
    queryFn: () => getSessionChangeSet(projectId, sessionId),
    enabled,
  });
}

/** 工作区里**一层**的列举（`path` 为 `""` 是根）—— 树展开一层取一层。 */
export function useSessionProjectTree(
  projectId: string,
  sessionId: string,
  path = "",
  enabled = true,
) {
  return useQuery({
    queryKey: qk.sessionProjectTree(projectId, sessionId, path),
    queryFn: () => getSessionProjectTree(projectId, sessionId, path),
    enabled,
    refetchInterval: enabled ? 5000 : false,
  });
}

export function useSessionConflicts(projectId: string, sessionId: string, enabled = true) {
  return useQuery({
    queryKey: qk.sessionConflicts(projectId, sessionId),
    queryFn: () => listSessionConflicts(projectId, sessionId),
    enabled,
  });
}

export function useSessionTerminalRefresh(projectId: string, sessionId: string) {
  const queryClient = useQueryClient();
  return useCallback(
    (terminal: ChatTerminalState) => invalidateSessionAfterTerminal(
      queryClient,
      projectId,
      sessionId,
      terminal.runId,
    ),
    [projectId, queryClient, sessionId],
  );
}

export function useSessionMutations(projectId: string) {
  const queryClient = useQueryClient();
  const t = useT();
  const invalidate = () => queryClient.invalidateQueries({ queryKey: qk.sessionsPrefix(projectId) });
  const invalidateRevisionState = () => Promise.all([
    invalidate(),
    queryClient.invalidateQueries({ queryKey: qk.artifacts(projectId) }),
  ]);
  const create = useMutation({
    mutationFn: (title: string) => createProjectSession(projectId, title),
    onSuccess: () => { void invalidate(); },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "会话没能建成", en: "Could not create session" })),
  });
  const rename = useMutation({
    mutationFn: ({ sessionId, title }: { sessionId: string; title: string }) => renameProjectSession(projectId, sessionId, title),
    onSuccess: () => { void invalidate(); },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "会话没能改名", en: "Could not rename session" })),
  });
  const archive = useMutation({
    mutationFn: (sessionId: string) => archiveProjectSession(projectId, sessionId),
    // 空会话是被**删掉**的，不是归档 —— 后端在 outcome 里说了做了哪一件，
    // 这里照着讲。两种结果讲同一句话，人会以为它还躺在归档里。
    onSuccess: (result) => {
      void invalidate();

    },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "会话没能归档", en: "Could not archive session" })),
  });
  const cancelActiveRun = useMutation({
    mutationFn: ({ runId, reason }: { runId: string; reason: string }) => cancelRun(runId, reason),
    onSuccess: () => { void invalidate(); },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "没能停下这一轮", en: "Could not stop this run" })),
  });
  const publish = useMutation({
    mutationFn: ({ sessionId, message }: { sessionId: string; message?: string }) =>
      publishSessionChanges(projectId, sessionId, message),
    onSuccess: (published) =>
      pushSuccess(t({ zh: "改动已发布为 {sha}", en: "Changes published as {sha}" }, { sha: published.commitSha.slice(0, 8) })),
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "改动没能发布", en: "Could not publish changes" })),
    onSettled: () => invalidateRevisionState(),
  });
  const resolveConflict = useMutation({
    mutationFn: ({
      sessionId,
      conflictId,
      choice,
    }: {
      sessionId: string;
      conflictId: string;
      choice: "use_project" | "use_proposed";
    }) => resolveSessionConflict(projectId, sessionId, conflictId, choice),
    onSuccess: () => {
      clearStaleSessionPublishError(() => publish.reset());

    },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "发布冲突没能解决", en: "Could not resolve publication conflict" })),
    onSettled: () => invalidateRevisionState(),
  });
  return { create, rename, archive, publish, resolveConflict, cancelActiveRun };
}
