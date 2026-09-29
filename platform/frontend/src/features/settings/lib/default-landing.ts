import type { DefaultLanding, ResearchRun } from "@/lib/api";

export async function resolveDefaultLanding(
  preference: DefaultLanding,
  latestRun: () => Promise<ResearchRun | null>,
) {
  // 打开平台先看今天这个领域发生了什么。资讯流不依赖任何 Run 历史，
  // 所以它和 /projects 一样是"不用查就能去"的目的地。
  if (preference === "feed") return "/feed";
  if (preference === "projects") return "/projects";
  // 全局层不再有 Session 入口，/chat 那个 launcher 已经删掉。存量账号里还留着
  // 这个偏好值，落到 Projects 而不是 404。
  if (preference === "new_research") return "/projects";
  try {
    const run = await latestRun();
    if (run?.projectId && run.sessionId) {
      return `/projects/${encodeURIComponent(run.projectId)}/sessions/${encodeURIComponent(run.sessionId)}`;
    }
  } catch {
    // A missing Run index must not block sign-in; Projects is the safe durable fallback.
  }
  return "/projects";
}
