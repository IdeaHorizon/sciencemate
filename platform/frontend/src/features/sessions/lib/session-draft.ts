// Hand off the first draft through client navigation without putting research text in a URL.
const drafts = new Map<string, string>();
const key = (projectId: string, sessionId: string) => JSON.stringify([projectId, sessionId]);
export function stageSessionDraft(projectId: string, sessionId: string, draft: string) {
  drafts.set(key(projectId, sessionId), draft);
}
export function readSessionDraft(projectId: string, sessionId: string) {
  return drafts.get(key(projectId, sessionId));
}
export function clearSessionDraft(projectId: string, sessionId: string) {
  drafts.delete(key(projectId, sessionId));
}
