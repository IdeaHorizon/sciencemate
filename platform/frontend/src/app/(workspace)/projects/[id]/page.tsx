"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { useProjectId } from "@/shared/routing/route-params";

/** Open the remembered session without a project or session-list landing page. */
export default function ProjectRootRedirect() {
  const router = useRouter();
  const projectId = useProjectId();
  useEffect(() => {
    router.replace(`/projects/${encodeURIComponent(projectId)}/chat`);
  }, [router, projectId]);
  return null;
}
