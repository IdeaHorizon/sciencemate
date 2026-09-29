"use client";

import { ArtifactDetail } from "@/features/artifacts";
import { useProjectId, useArtifactId } from "@/shared/routing/route-params";

export default function ArtifactDetailPage() {
  const projectId = useProjectId();
  const artifactId = useArtifactId();
  return <ArtifactDetail projectId={projectId} artifactId={artifactId} />;
}
