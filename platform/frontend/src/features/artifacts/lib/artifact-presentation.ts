import type { Artifact } from "@/lib/api";

const HIDDEN_METADATA = new Set(["_content", "frozen_at", "frozen_by"]);

export function publishedRevisionId(artifact: Pick<Artifact, "extra_data">) {
  const value = artifact.extra_data?.published_revision_id;
  return typeof value === "string" && value.trim() ? value : null;
}

export function visibleArtifactMetadata(artifact: Pick<Artifact, "extra_data">) {
  return Object.entries(artifact.extra_data ?? {})
    .filter(([key]) => !HIDDEN_METADATA.has(key));
}
