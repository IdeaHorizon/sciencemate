"use client";

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { ProjectChatRedirect } from "@/features/sessions/components/ProjectChatRedirect";
import { useProjectId } from "@/shared/routing/route-params";

function ChatCompatibilityRoute() {
  const projectId = useProjectId();
  const query = useSearchParams();
  const fixtureMode =
    query.get("mode") === "demo" || process.env.NEXT_PUBLIC_SESSION_MODE === "demo";
  return <ProjectChatRedirect projectId={projectId} mode={fixtureMode ? "fixture" : "api"} />;
}

export default function ProjectChatCompatibilityPage() {
  return (
    <Suspense fallback={null}>
      <ChatCompatibilityRoute />
    </Suspense>
  );
}
