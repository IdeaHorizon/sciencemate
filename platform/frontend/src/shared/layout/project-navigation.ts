import type { ResearchSession } from "../../features/sessions/types.ts";

/** Keep the selected session visible even when it is older than the first page. */
export function visibleSessions(sessions: ResearchSession[], selectedId: string, limit: number, query = "") {
  const needle = query.trim().toLocaleLowerCase();
  const matched = sessions.filter((session) => !needle || session.title.toLocaleLowerCase().includes(needle));
  if (needle) return matched;
  const first = matched.slice(0, limit);
  const selected = matched.find((session) => session.id === selectedId);
  return selected && !first.some((session) => session.id === selectedId) ? [...first, selected] : first;
}
