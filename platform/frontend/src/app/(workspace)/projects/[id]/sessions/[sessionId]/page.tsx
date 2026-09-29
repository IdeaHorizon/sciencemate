"use client";

import { Suspense } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { ChevronLeft } from "lucide-react";
import { SessionExecutionView } from "@/features/execution";
import { SessionWorkspace } from "@/features/sessions";
import { useProjectId, useSessionId } from "@/shared/routing/route-params";
import { useT } from "@/shared/i18n";

function SessionRoute() {
  const t = useT();
  const projectId = useProjectId();
  const sessionId = useSessionId();
  const query = useSearchParams();
  const demoMode =
    query.get("mode") === "demo" || process.env.NEXT_PUBLIC_SESSION_MODE === "demo";
  if (query.get("view") === "execution") {
    return (
      <div className="session-execution-inspector">
        <Link href={`/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}${demoMode ? "?mode=demo" : ""}`}><ChevronLeft size={13} />{t({ zh: "回到会话", en: "Back to session" })}</Link>
        <SessionExecutionView projectId={projectId} sessionId={sessionId} mode={demoMode ? "demo" : "api"} />
      </div>
    );
  }
  return <SessionWorkspace projectId={projectId} sessionId={sessionId} mode={demoMode ? "fixture" : "api"} />;
}

export default function SessionPage() {
  // useSearchParams 在静态导出下必须有 Suspense 边界（构建期没有查询串）。
  return (
    <Suspense fallback={null}>
      <SessionRoute />
    </Suspense>
  );
}
