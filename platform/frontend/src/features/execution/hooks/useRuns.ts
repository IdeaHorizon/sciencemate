"use client";

import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { API_BASE_URL, api, type RunDetailResponse } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import {
  invalidateSessionAfterTerminal,
  invalidateSessionLifecycle,
} from "@/features/sessions/lib/session-terminal-invalidation";
import { assertCanonicalRunDetail } from "../lib/run-detail-contract";
import { createExecutionReadClient } from "../lib/execution-read-client";
import type { ExecutionEvent } from "../lib/execution-event";
import {
  mergeCanonicalRunEvents,
  readCanonicalRunEvents,
  readCanonicalRunEventsAfter,
} from "../lib/run-event-reader";
import { readCanonicalRunEventStream } from "../lib/run-event-stream";
import { canonicalRunEventRegistry } from "../lib/run-event-registry";

const DETAIL_REFRESH_EVENT_KINDS = new Set([
  "decision.required",
  "decision.resolved",
  "permission.required",
  "permission.resolved",
  "run.paused",
  "run.resumed",
  "run.retrying",
  "run.recovering",
]);

export function useProjectRuns(projectId: string | undefined, limit = 50) {
  return useQuery({
    queryKey: projectId ? qk.runs(projectId, limit) : ["runs", "none"],
    queryFn: () => projectId
      ? api.listRuns(projectId, limit)
      : Promise.reject(new Error("No project id")),
    enabled: !!projectId,
    refetchInterval: 5000,
  });
}

export function useVisibleRuns(limit = 4) {
  return useQuery({
    queryKey: qk.visibleRuns(limit),
    queryFn: () => api.listRuns(undefined, limit),
    refetchInterval: 5000,
  });
}

export function useSessionRuns(sessionId: string, enabled = true, limit = 10) {
  return useQuery({
    queryKey: qk.sessionRuns(sessionId, limit),
    queryFn: () => api.listRuns(undefined, limit, sessionId),
    enabled: enabled && !!sessionId,
  });
}

export function useRunDetail(runId: string | null, enabled = true) {
  return useQuery({
    queryKey: runId ? qk.run(runId) : ["run", "none"],
    queryFn: async () => {
      if (!runId) throw new Error("No run id");
      return assertCanonicalRunDetail(await api.getRun(runId), runId);
    },
    enabled: enabled && !!runId,
  });
}

export function useRunEvents(
  sessionId: string,
  projectId: string,
  runId: string,
  follow: boolean,
  enabled = true,
) {
  const queryClient = useQueryClient();
  const [streamError, setStreamError] = useState<unknown>();
  const [assistantDraft, setAssistantDraft] = useState("");
  const key = useMemo(() => qk.runEvents(sessionId, runId), [runId, sessionId]);
  const client = useMemo(() => createExecutionReadClient({
    baseUrl: API_BASE_URL,
    fetchImpl: (input, init) => api.fetchWithAuth(input, init),
  }), []);
  const query = useQuery({
    queryKey: key,
    queryFn: async () => mergeCanonicalRunEvents(
      queryClient.getQueryData<ExecutionEvent[]>(key) ?? [],
      // 「这一轮干了什么」包含它派出去的子节点做的事 —— 不带的话，用户只
      // 看得到编排器自己那一坨工具名（wangd 2026-08-11：「literature 都结束
      // 了…还是在下面显示一大坨」）。
      await readCanonicalRunEvents(client, sessionId, runId),
      sessionId,
      runId,
    ),
    enabled: enabled && !!sessionId && !!runId,
    staleTime: Number.POSITIVE_INFINITY,
  });

  useEffect(() => {
    if (!enabled || !follow || !sessionId || !projectId || !runId) return;

    const storeEvents = (incoming: readonly ExecutionEvent[]) => {
      let merged: ExecutionEvent[] = [];
      let novel: ExecutionEvent[] = [];
      queryClient.setQueryData<ExecutionEvent[]>(key, (current = []) => {
        const knownIds = new Set(current.map((event) => event.id));
        novel = incoming.filter((event) => !knownIds.has(event.id));
        merged = mergeCanonicalRunEvents(current, incoming, sessionId, runId);
        return merged;
      });
      queryClient.setQueryData<RunDetailResponse>(qk.run(runId), (current) => current
        ? { ...current, eventCount: merged.length }
        : current);
      // run 的状态**不在这里合成**。这里曾有一份 RUN_STATUS_BY_EVENT 映射，
      // 把事件 kind 直接写成 run.status 塞进缓存 —— 那是第二个真相源，而且
      // 是唯一一个**没有活性矫正**的：一条运行时早已丢失的 run，只要事件流
      // 里最后一条是 run.started，客户端就会一直显示「运行中」。
      // 状态由后端现算（run_liveness + execution_view），这里只负责让它去问。
      const sawRunLifecycle = novel.some((event) => event.kind.startsWith("run."));
      if (sawRunLifecycle || novel.some((event) => DETAIL_REFRESH_EVENT_KINDS.has(event.kind))) {
        void queryClient.invalidateQueries({ queryKey: qk.run(runId), exact: true });
      }
      // run 生命周期事件 → 会话 runs 列表也要追上来。这份列表是很多判断的
      // 依据（standaloneLiveRun 的"在跑吗"、右栏检查器的 owning run 清单），
      // 而它原本只在 turn 终态时刷新 —— 新会话第一轮跑着的时候永远是空快照，
      // header 显示 Ready、右栏显示"还没有节点跑过"（2026-08-17 实测）。
      if (sawRunLifecycle) {
        void queryClient.invalidateQueries({ queryKey: qk.sessionRunsPrefix(sessionId) });
        // session 对象也得跟上：`executionState` 是 run 状态的投影，`title` 由
        // agent 在跑的过程中生成 —— 两者都只在这里之外的「终态」路径被刷新过，
        // 于是一轮跑起来之后顶栏会一直挂着**上一轮**的终态（实测：上一次 503
        // 的 Failed + 标题生成之前那段原始 prompt）。
        void invalidateSessionLifecycle(queryClient, projectId, sessionId);
      }
      return merged.at(-1)?.sequence ?? 0;
    };

    setStreamError(undefined);
    return canonicalRunEventRegistry.acquire({
      key: `${sessionId}:${runId}`,
      sessionId,
      runId,
      initialEvents: queryClient.getQueryData<ExecutionEvent[]>(key) ?? [],
      readStream: (afterSequence, signal, onEvent, onToken, onTokenGap) => readCanonicalRunEventStream({
        baseUrl: API_BASE_URL,
        fetchImpl: (input, init) => api.fetchWithAuth(input, init),
        sessionId,
        runId,
        afterSequence,
        signal,
        onEvent,
        onToken,
        onTokenGap,
      }),
      readFallback: (afterSequence) => readCanonicalRunEventsAfter(
        client,
        sessionId,
        runId,
        afterSequence,
      ),
      onEvents: (events) => { storeEvents(events); },
      async onTerminal(end) {
          queryClient.setQueryData<RunDetailResponse>(qk.run(runId), (current) => current
            ? {
                ...current,
                eventCount: queryClient.getQueryData<ExecutionEvent[]>(key)?.length ?? current.eventCount,
                run: { ...current.run, status: end.status },
              }
            : current);
          await invalidateSessionAfterTerminal(queryClient, projectId, sessionId, runId);
          await queryClient.invalidateQueries({ queryKey: qk.sessionRunsPrefix(sessionId) });
      },
    }, (snapshot) => {
      setAssistantDraft(snapshot.assistantDraft);
      setStreamError(snapshot.phase === "error" ? snapshot.error : undefined);
    });
  }, [client, enabled, follow, key, projectId, queryClient, runId, sessionId]);

  return {
    ...query,
    error: query.error ?? streamError ?? null,
    isError: query.isError || streamError !== undefined,
    assistantDraft,
  };
}
