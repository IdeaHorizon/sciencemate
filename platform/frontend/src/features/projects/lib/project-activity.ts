import type { ResearchSession } from "../../sessions/types";
import type { Phrase } from "../../../shared/i18n/language";

type Session = Pick<ResearchSession, "lifecycleStatus" | "execution">;
export type ProjectActivity = { tone: "running" | "waiting" | "idle" | "unknown" | "archived"; label: Phrase };

/** Project lifecycle is not execution: an active project may have no live runs. */
export function projectActivity(
  status: string,
  sessions: readonly Session[] | undefined,
  error = false,
): ProjectActivity {
  if (status === "archived") return { tone: "archived", label: { zh: "已归档", en: "Archived" } };
  if (error) return { tone: "unknown", label: { zh: "运行状态暂不可用", en: "Activity unavailable" } };
  if (!sessions) return { tone: "unknown", label: { zh: "正在获取运行状态…", en: "Loading activity…" } };
  const alive = sessions.filter((s) => s.lifecycleStatus !== "archived" && s.execution.phase === "alive");
  const needsHuman = (s: Session) => s.execution.waitingOn?.kind === "human" || s.execution.waitingOn?.kind === "permission";
  if (alive.some((s) => !needsHuman(s))) return { tone: "running", label: { zh: "有任务运行中", en: "Tasks running" } };
  if (alive.length) return { tone: "waiting", label: { zh: "等待人工处理", en: "Needs your attention" } };
  return { tone: "idle", label: { zh: "当前无运行任务", en: "No active tasks" } };
}
