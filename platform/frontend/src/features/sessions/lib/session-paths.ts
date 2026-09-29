export function projectSessionsCollectionPath(
  projectId: string,
  options: { includeArchived?: boolean } = {},
) {
  const path = `/projects/${encodeURIComponent(projectId)}/sessions`;
  return options.includeArchived ? `${path}?includeArchived=true` : path;
}
