"use client";

import { useEffect, useRef } from "react";
import Link from "next/link";
import { Server } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { qk } from "@/lib/query/keys";
import { availabilityPollInterval, type RemoteAvailability } from "@/lib/remote-runtime";
import { Button } from "@/shared/ui";
import { useLanguage, useT } from "@/shared/i18n";

export function RemoteConnectionStatus({ projectId, serverId, name }: {
  projectId: string; serverId: string; name: string;
}) {
  const t = useT();
  const language = useLanguage();
  const cache = useQueryClient();
  const previous = useRef<RemoteAvailability["state"] | "checking">("checking");
  const lastSuccess = useRef<string | null>(null);
  const connection = useQuery({
    queryKey: qk.remoteAvailability(serverId),
    queryFn: () => api.remoteServerAvailability(serverId),
    retry: false,
    refetchInterval: (query) => query.state.error ? 15_000
      : availabilityPollInterval(query.state.data?.state),
  });
  const state = connection.isError ? "unavailable" : connection.data?.state ?? "checking";

  useEffect(() => {
    if (state === "online") {
      lastSuccess.current = connection.data?.checked_at ?? null;
      if (previous.current !== "online" && previous.current !== "checking") {
        // Resume reads, never replay a submit/stop command after a network failure.
        void cache.invalidateQueries({ queryKey: qk.project(projectId) });
        void cache.invalidateQueries({ queryKey: qk.sessionsPrefix(projectId) });
        void cache.invalidateQueries({ queryKey: qk.projects() });
      }
    }
    previous.current = state;
  }, [state, connection.data, cache, projectId]);

  const unavailable = state !== "online" && state !== "checking";
  const label = state === "online" ? t({ zh: "API 已连接", en: "API connected" })
    : state === "checking" ? t({ zh: "正在检查 API 连接…", en: "Checking API connection…" })
    : state === "disconnected" ? t({ zh: "连接已手动断开，任务状态待确认", en: "Manually disconnected; task status unconfirmed" })
    : state === "authentication_required" ? t({ zh: "连接需要重新认证，任务状态待确认", en: "Sign in again to confirm task status" })
    : state === "verification_required" ? t({ zh: "请核对服务器身份，任务状态待确认", en: "Verify the server identity to confirm task status" })
    : state === "incompatible" ? t({ zh: "请更新远程后端，任务状态待确认", en: "Update the remote runtime to confirm task status" })
    : t({ zh: "API 暂时不可用，任务状态待确认", en: "API temporarily unavailable; task status unconfirmed" });

  return <div className={`project-runtime-status${unavailable ? " is-unavailable" : ""}`}
    data-connection-state={state} role="status" aria-live="polite">
    <Server size={13} />
    <span>{t({ zh: "运行位置：{name}", en: "Running on: {name}" }, { name })}</span>
    <strong>{label}</strong>
    {unavailable && <span className="runtime-stale-note">
      {t({ zh: "保留最后一次已知进度，每 15 秒自动检查。连接中断不会取消远程任务。", en: "Showing the last known progress; checking every 15 seconds. Connection loss does not cancel remote tasks." })}
      {lastSuccess.current && ` ${t({ zh: "最近连接成功：{time}", en: "Last connected: {time}" }, {
        time: new Date(lastSuccess.current).toLocaleTimeString(language === "zh" ? "zh-CN" : "en-US"),
      })}`}
    </span>}
    {state === "unavailable" && <Button size="sm" variant="ghost" disabled={connection.isFetching}
      onClick={() => void connection.refetch()}>
      {connection.isFetching ? t({ zh: "正在检查…", en: "Checking…" }) : t({ zh: "立即重试", en: "Retry now" })}
    </Button>}
    <Link href="/settings/runtimes">{t({ zh: "管理连接", en: "Manage connection" })}</Link>
  </div>;
}
