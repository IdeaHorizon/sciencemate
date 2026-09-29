"use client";

import { useCallback } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/features/auth";
import { qk } from "@/lib/query/keys";
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
  return useQuery({
    queryKey: projectId ? qk.sessions(projectId, mode) : ["sessions", "none"],
    queryFn: () => projectId
      ? listProjectSessions(projectId, user, mode)
      : Promise.reject(new Error("No project id")),
    enabled: !!projectId,
  });
}

export function useSession(projectId: string, sessionId: string, mode: SessionDataMode = "api") {
  const { user } = useAuth();
  return useQuery({
    queryKey: qk.session(projectId, sessionId, mode),
    queryFn: () => getProjectSession(projectId, sessionId, user, mode),
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
  const invalidate = () => queryClient.invalidateQueries({ queryKey: qk.sessionsPrefix(projectId) });
  const invalidateRevisionState = () => Promise.all([
    invalidate(),
    queryClient.invalidateQueries({ queryKey: qk.artifacts(projectId) }),
  ]);
  const create = useMutation({
    mutationFn: (title?: string) => createProjectSession(projectId, title),
    onSuccess: () => { void invalidate(); },
    onError: (error) => pushError(error instanceof Error ? error.message : "Could not create session"),
  });
  const rename = useMutation({
    mutationFn: ({ sessionId, title }: { sessionId: string; title: string }) => renameProjectSession(projectId, sessionId, title),
    onSuccess: () => { void invalidate(); },
    onError: (error) => pushError(error instanceof Error ? error.message : "Could not rename session"),
  });
  const archive = useMutation({
    mutationFn: (sessionId: string) => archiveProjectSession(projectId, sessionId),
    // 空会话是被**删掉**的，不是归档 —— 后端在 outcome 里说了做了哪一件，
    // 这里照着讲。两种结果讲同一句话，人会以为它还躺在归档里。
    onSuccess: (result) => {
      void invalidate();

    },
    onError: (error) => pushError(error instanceof Error ? error.message : "Could not archive session"),
  });
  const cancelActiveRun = useMutation({
    mutationFn: ({ runId, reason }: { runId: string; reason: string }) => cancelRun(runId, reason),
    onSuccess: () => { void invalidate(); },
    onError: (error) => pushError(error instanceof Error ? error.message : "Could not stop this run"),
  });
  const publish = useMutation({
    mutationFn: ({ sessionId, message }: { sessionId: string; message?: string }) =>
      publishSessionChanges(projectId, sessionId, message),
    onSuccess: (published) =>
      pushSuccess(`Changes published as ${published.commitSha.slice(0, 8)}`),
    onError: (error) => pushError(error instanceof Error ? error.message : "Could not publish changes"),
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
    onError: (error) => pushError(error instanceof Error ? error.message : "Could not resolve publication conflict"),
    onSettled: () => invalidateRevisionState(),
  });
  return { create, rename, archive, publish, resolveConflict, cancelActiveRun };
}
