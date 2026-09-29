"use client";

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { SessionIndex } from "@/features/sessions/components/SessionIndex";
import { useProjectId } from "@/shared/routing/route-params";

function ProjectResearchRoute() {
  const projectId = useProjectId();
  const query = useSearchParams();
  const fixtureMode =
    query.get("mode") === "demo" || process.env.NEXT_PUBLIC_SESSION_MODE === "demo";
  return <SessionIndex projectId={projectId} mode={fixtureMode ? "fixture" : "api"} />;
}

export default function ProjectResearchPage() {
  return (
    <Suspense fallback={null}>
      <ProjectResearchRoute />
    </Suspense>
  );
}
