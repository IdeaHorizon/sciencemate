import type { CurrentUser, RunUsage } from "@/lib/api";
import { readContextWindow } from "./context-window.ts";
import type {
  AnswerAffordance,
  ResearchSession,
  ProjectSessionCapability,
  SessionExecutionView,
  SessionGroup,
  SessionLifecycleStatus,
  SessionMessage,
  SessionPendingApproval,
} from "../types";

export interface CanonicalSessionPayload {
  id: string;
  projectId: string;
  title: string;
  summary?: string | null;
  createdByUserId?: string | null;
  primaryDriverUserId?: string | null;
  lifecycleStatus: SessionLifecycleStatus;
  baseCommitSha?: string | null;
  headCommitSha?: string | null;
  aheadBy?: number;
  behindBy?: number;
  projectHeadRevisionId?: string | null;
  projectHeadRevision?: { id: string; revisionNo: number } | null;
  gitBranch?: string | null;
  gitBaseCommitSha?: string | null;
  gitHeadCommitSha?: string | null;
  modelBackendId?: string | null;
  modelBackendName?: string | null;
  primaryDriverName?: string | null;
  createdByName?: string | null;
  capabilities?: ProjectSessionCapability[];
  createdAt: string;
  updatedAt: string;
  archivedAt?: string | null;
  runCount?: number;
  /** 后端现算的局面（含答复入口）—— 形状由 adaptExecutionView 校验。 */
  executionView: unknown;
  usage?: RunUsage;
  /** 后端 `contextWindow`：`context.updated` 的载荷外加 at / runId。 */
  contextWindow?: unknown;
  retryCount?: number;
  unpublishedChangeCount?: number;
  materialMaxBytes?: number;
  conflictCount?: number;
}

export interface CanonicalMessagePayload {
  id: string;
  sessionId: string;
  sequence: number;
  actorUserId?: string | null;
  role: "user" | "assistant" | "system";
  content: string;
  commandId?: string | null;
  runId?: string | null;
  offerId?: string | null;
  createdAt: string;
}

const EMPTY_USAGE: RunUsage = {
  promptTokens: 0,
  completionTokens: 0,
  totalTokens: 0,
  cost: null,
  currency: null,
  coverage: "partial",
};

function adaptPendingApproval(raw: unknown): SessionPendingApproval | null {
  if (!raw || typeof raw !== "object") return null;
  const value = raw as Record<string, unknown>;
  const runId = typeof value.runId === "string" ? value.runId : null;
  if (!runId) return null;
  const details = Array.isArray(value.optionDetails) ? value.optionDetails : [];
  return {
    runId,
    reason: typeof value.reason === "string" ? value.reason : "",
    kind: value.kind === "permission" || value.kind === "decision" ? value.kind : "human_input",
    prompt: typeof value.prompt === "string" ? value.prompt : null,
    context: typeof value.context === "string" ? value.context : null,
    options: Array.isArray(value.options) ? value.options.map(String) : [],
    // 选项**原样**带过，不在这里重建。
    //
    // 上一版是逐字段抄成 {label, description, recommended} 的 —— `id` 就是在
    // 这里被丢掉的一处（另一处在平台 ingest）。选项没有 id，回传时只能拿位次
    // 兜底，服务端撞不上合法动作集，答复被静默丢弃、原地重呈递。
    optionDetails: details.flatMap((entry) => {
      if (!entry || typeof entry !== "object") return [];
      const d = entry as Record<string, unknown>;
      // `...d` 在前：上游给选项加的任何字段都跟着走（`id` 就是此前在这里被丢
      // 掉的那个）。后面几行只保证既有消费方要的三个键**有值**，不是在挑字段。
      return [{
        ...d,
        id: typeof d.id === "string" ? d.id : undefined,
        label: String(d.label ?? ""),
        description: String(d.description ?? ""),
        recommended: Boolean(d.recommended),
      }];
    }),
    // 这一次呈递整份带上（choice id / offer_id / decision_id / facts 都在里面）。
    offer: (value.offer && typeof value.offer === "object")
      ? value.offer as Record<string, unknown>
      : null,
    recommendedOptionIndex: typeof value.recommendedOptionIndex === "number"
      ? value.recommendedOptionIndex
      : null,
    askingNodeType: typeof value.askingNodeType === "string" ? value.askingNodeType : null,
    pauseKind: typeof value.pauseKind === "string" ? value.pauseKind : null,
    askedAt: typeof value.askedAt === "string" ? value.askedAt : null,
  };
}

/**
 * 局面 + 答复入口。**入口缺席时不许静默地把人锁死**。
 *
 * 这里的每一条兜底都朝同一个方向倒：拿不准就把输入框还给人。理由是代价不
 * 对称 —— 多给一个入口最坏是"打了字没人听"（后端会如实拒绝并说明），
 * 少给一个入口是"看得见问题、答不上、也走不掉"（2026-09-01 实测 6 小时）。
 *
 * 未知的 `via` 也走这条：新增取值而客户端还没跟上，是**我们的**疏忽，
 * 不该由用户用一个锁死的会话来承担。
 */
export function adaptExecutionView(raw: unknown): SessionExecutionView {
  const value = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const waitingKind = (value.waitingOn as { kind?: unknown } | null)?.kind;
  return {
    phase: value.phase === "ended" || value.phase === "interrupted" ? value.phase : "alive",
    waitingOn:
      waitingKind === "human" || waitingKind === "permission" || waitingKind === "compute"
        ? { kind: waitingKind }
        : null,
    outcome: (value.outcome ?? null) as SessionExecutionView["outcome"],
    error: (value.error && typeof value.error === "object")
      ? value.error as Record<string, unknown>
      : null,
    canStop: value.canStop === true,
    answer: adaptAnswerAffordance(value.answer),
    label: typeof value.label === "string" ? value.label : "",
    runId: typeof value.runId === "string" ? value.runId : null,
    since: typeof value.since === "string" ? value.since : null,
  };
}

function adaptAnswerAffordance(raw: unknown): AnswerAffordance {
  const value = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  if (value.via === "none" && typeof value.reason === "string") {
    return {
      via: "none",
      reason: value.reason,
      until: typeof value.until === "string" ? value.until : null,
    };
  }
  if (value.via === "pause") {
    const pause = adaptPendingApproval(value.pause);
    // 后端保证 via==="pause" ⟹ pause 非空。真到不了这儿的话（协议漂了、
    // 中间层削了字段），**不许**保留 "pause" 这个取值 —— 那正是"说了走卡片
    // 却没有卡片"，也就是那 6 小时。降级成输入框，并说出来。
    if (pause) return { via: "pause", pause };
    return { via: "composer", degraded: "pause_body_unavailable" };
  }
  return {
    via: "composer",
    degraded: typeof value.degraded === "string"
      ? value.degraded
      : value.via === "composer"
        ? undefined
        : "affordance_unrecognized",
  };
}

function attributionLabel(userId: string | null | undefined, currentUser?: CurrentUser | null) {
  if (!userId) return "Unassigned";
  if (currentUser?.id === userId) return currentUser.display_name;
  return "Project member";
}

export function adaptCanonicalSession(
  payload: CanonicalSessionPayload,
  currentUser?: CurrentUser | null,
): ResearchSession {
  return {
    id: payload.id,
    projectId: payload.projectId,
    title: payload.title || "Untitled research",
    summary: payload.summary ?? null,
    createdByUserId: payload.createdByUserId ?? null,
    primaryDriverUserId: payload.primaryDriverUserId ?? null,
    lifecycleStatus: payload.lifecycleStatus,
    baseCommitSha: payload.baseCommitSha ?? null,
    headCommitSha: payload.headCommitSha ?? null,
    aheadBy: payload.aheadBy ?? 0,
    behindBy: payload.behindBy ?? 0,
    gitBranch: payload.gitBranch ?? null,
    gitBaseCommitSha: payload.gitBaseCommitSha ?? null,
    gitHeadCommitSha: payload.gitHeadCommitSha ?? null,
    modelBackendId: payload.modelBackendId ?? null,
    modelBackendName: payload.modelBackendName ?? null,
    createdAt: payload.createdAt,
    updatedAt: payload.updatedAt,
    archivedAt: payload.archivedAt ?? null,
    runCount: payload.runCount ?? 0,
    unpublishedChangeCount: payload.unpublishedChangeCount ?? 0,
    materialMaxBytes: payload.materialMaxBytes,
    conflictCount: payload.conflictCount ?? 0,
    execution: adaptExecutionView(payload.executionView),
    usage: payload.usage ?? { ...EMPTY_USAGE },
    contextWindow: readContextWindow(payload.contextWindow),
    retryCount: payload.retryCount ?? 0,
    driverLabel: payload.primaryDriverName || attributionLabel(payload.primaryDriverUserId, currentUser),
    creatorLabel: payload.createdByName || attributionLabel(payload.createdByUserId, currentUser),
    capabilities: payload.capabilities ?? [],
    source: "canonical",
  };
}

export function adaptCanonicalMessages(messages: CanonicalMessagePayload[]): SessionMessage[] {
  return [...messages]
    .sort((a, b) => a.sequence - b.sequence)
    .map((message) => ({
      id: message.id,
      sessionId: message.sessionId,
      sequence: message.sequence,
      actorUserId: message.actorUserId ?? null,
      role: message.role,
      content: message.content,
      commandId: message.commandId ?? null,
      runId: message.runId ?? null,
      offerId: message.offerId ?? null,
      createdAt: message.createdAt,
    }));
}


export function groupSession(session: ResearchSession): SessionGroup {
  if (session.lifecycleStatus === "archived") return "archived";
  // 「要人管」= 有冲突，或在等人回答，或结局不干净，或途中被打断。
  // 这些以前是一份手写的状态名单（ATTENTION_STATES），名单一多就会各自演化。
  const needsPerson = session.execution.waitingOn?.kind === "human"
    || session.execution.waitingOn?.kind === "permission"
    || session.execution.phase === "interrupted"
    || (session.execution.outcome !== null && session.execution.outcome !== "ok");
  if (session.conflictCount > 0 || needsPerson) {
    return "needs_attention";
  }
  if (session.execution.phase === "alive") return "running";
  if (session.unpublishedChangeCount > 0) return "unpublished";
  return "recent";
}

export function groupSessions(sessions: ResearchSession[]) {
  const groups: Record<SessionGroup, ResearchSession[]> = {
    running: [],
    needs_attention: [],
    unpublished: [],
    recent: [],
    archived: [],
  };
  [...sessions]
    .sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt))
    .forEach((session) => groups[groupSession(session)].push(session));
  return groups;
}
