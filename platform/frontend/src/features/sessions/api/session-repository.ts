import { API_BASE_URL, ApiError, api, type CurrentUser } from "@/lib/api";
import {
  adaptCanonicalMessages,
  adaptCanonicalSession,
  type CanonicalMessagePayload,
  type CanonicalSessionPayload,
} from "../lib/session-adapter";
import { sessionFixtures, sessionMessageFixtures } from "../fixtures/session-fixtures";
import { projectSessionsCollectionPath } from "../lib/session-paths";
import {
  adaptSessionCandidate,
  adaptSessionChangeSet,
  adaptSessionConflict,
  adaptSessionConflicts,
  buildSessionCandidateRequest,
} from "../lib/change-set-adapter";
import type {
  ResearchSession,
  ProjectFileTree,
  SessionCandidateInput,
  SessionChangeSet,
  SessionMessage,
} from "../types";
import { say, type Language } from "@/shared/i18n";
import { diagnosticsFailureMessage, diagnosticsFilename } from "../lib/diagnostics";

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body) headers.set("Content-Type", "application/json");
  const response = await api.fetchWithAuth(`${API_BASE_URL}${path}`, { ...init, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(body.detail || `API error: ${response.status}`, response.status);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

function payloadItems<T>(payload: T[] | { items: T[] }) {
  return Array.isArray(payload) ? payload : payload.items;
}

export type SessionDataMode = "api" | "fixture";

export async function listProjectSessions(
  projectId: string,
  currentUser?: CurrentUser | null,
  mode: SessionDataMode = "api",
): Promise<ResearchSession[]> {
  if (mode === "fixture") return sessionFixtures(projectId, currentUser);
  // 不再顺带拉 /runs：会话的局面由后端在 payload 里给成品。原先这里拉项目
  // 最近 100 条 run 供客户端自己推导，于是 run 多的项目里，老会话拿不到自己
  // 的 run，一律显示成 "Ready"。
  const payload = await requestJson<CanonicalSessionPayload[] | { items: CanonicalSessionPayload[] }>(
    projectSessionsCollectionPath(projectId, { includeArchived: true }),
  );
  return payloadItems(payload).map((session) => adaptCanonicalSession(session, currentUser));
}

export async function getProjectSession(
  projectId: string,
  sessionId: string,
  currentUser?: CurrentUser | null,
  mode: SessionDataMode = "api",
): Promise<ResearchSession> {
  if (mode === "fixture") {
    const fixture = sessionFixtures(projectId, currentUser).find((session) => session.id === sessionId);
    if (!fixture) throw new ApiError("Session not found", 404);
    return fixture;
  }
  const payload = await requestJson<CanonicalSessionPayload>(
    `/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}`,
  );
  if (payload.projectId !== projectId) throw new Error("Session project identity mismatch.");
  return adaptCanonicalSession(payload, currentUser);
}

export async function listSessionMessages(
  projectId: string,
  sessionId: string,
  mode: SessionDataMode = "api",
): Promise<SessionMessage[]> {
  if (mode === "fixture") return sessionMessageFixtures(sessionId);
  const payload = await requestJson<CanonicalMessagePayload[] | { items: CanonicalMessagePayload[] }>(
    `/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}/messages?afterSequence=0`,
  );
  return adaptCanonicalMessages(payloadItems(payload));
}

export async function createProjectSession(projectId: string, title = "New research") {
  return requestJson<CanonicalSessionPayload>(
    projectSessionsCollectionPath(projectId),
    { method: "POST", body: JSON.stringify({ title }) },
  );
}

export async function renameProjectSession(projectId: string, sessionId: string, title: string) {
  return requestJson<CanonicalSessionPayload>(
    `/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}`,
    { method: "PATCH", body: JSON.stringify({ title }) },
  );
}

/**
 * 收起一个会话。什么都没发生过的（无消息 / 无 run / 无改动）后端会**直接删掉**，
 * 并在 `outcome` 里说明它做了哪一件——"空"的判据在后端一处，前端不自己数。
 */
export async function archiveProjectSession(projectId: string, sessionId: string) {
  return requestJson<{ outcome: "archived" | "deleted"; session: CanonicalSessionPayload | null }>(
    `/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}/archive`,
    { method: "POST" },
  );
}

/**
 * 停掉一个还在跑的 Run。
 *
 * 后端的 `POST /runs/{id}/cancel` 一直都在，前端**一次都没调过** —— 研究员
 * 看着 agent 往错误方向跑（E2E v18：冻结协议里有 1000x 量纲错误），除了等它
 * 跑完或关浏览器，什么都做不了。v2.1 文档把"运行中干预"列为 HITL 通道之一，
 * 而这条通道在 UI 上不存在。
 */
export async function cancelRun(runId: string, reason: string) {
  return requestJson<{ runId: string; status: string }>(
    `/runs/${encodeURIComponent(runId)}/cancel`,
    { method: "POST", body: JSON.stringify({ reason }) },
  );
}

/** 撤销本会话最近一次写（Git revert，留痕；冻结产物不许回滚）。 */
export async function undoLastSessionWrite(projectId: string, sessionId: string) {
  return requestJson<{
    revertedSubject: string;
    headCommit: string;
    changedPaths: string[];
  }>(`/projects/${projectId}/sessions/${sessionId}/undo`, { method: "POST" });
}

/**
 * 这个会话的诊断包（zip）：全部运行记录 + 同时段的后端日志，密钥已抹。
 * 组织项目上这条请求由本机后端转交给那台服务器（`app/relay.py`），界面不用管。
 */
export async function fetchSessionDiagnostics(
  projectId: string,
  sessionId: string,
  lang: Language = "zh",
): Promise<{ blob: Blob; filename: string }> {
  const response = await api.fetchWithAuth(
    `${API_BASE_URL}/projects/${projectId}/sessions/${sessionId}/diagnostics`,
  );
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(diagnosticsFailureMessage(response.status, body?.detail, lang), response.status);
  }
  return {
    blob: await response.blob(),
    filename: diagnosticsFilename(response.headers.get("Content-Disposition"), sessionId),
  };
}

/** 清对话历史（memory / KB / 产物不动）。只在空闲时可用。 */
export async function resetSessionConversation(projectId: string, sessionId: string) {
  return requestJson<{ status: string; clearedMessages?: number }>(
    `/projects/${projectId}/sessions/${sessionId}/reset`,
    { method: "POST" },
  );
}

/** 跳过本次 dreaming（KB 整理）。项目级。 */
export async function skipProjectDreaming(projectId: string) {
  return requestJson<{ status: string }>(
    `/projects/${projectId}/projects-dreaming/skip`,
    { method: "POST" },
  );
}

/**
 * 会话附件：把文件放进本会话工作区，让 agent 能直接 read_file 读。
 *
 * 与资料库上传不是一回事：资料库是"进项目语料供检索"，这里是"你现在看一下
 * 这个文件"。返回路径与预览，由调用方写进草稿 —— 你看到什么，模型就看到什么，
 * 不引入隐藏注入通道。
 */
export type AddedFile = {
  name: string;
  /** 工作区内的相对路径（`sources/<名字>`）。 */
  path: string;
  /** agent 会用的绝对路径 —— 和 sandbox 里是同一条。 */
  absolutePath: string;
  sha256: string;
  sizeBytes: number;
  commitSha: string | null;
  /** false = 同名同内容重传，没有产生新提交。 */
  committed: boolean;
};

/**
 * 把一份文件交进**这个会话的工作区**。平台上传文件的唯一入口。
 *
 * 落点是 `sources/<名字>`，agent 下一轮就能按绝对路径读到；要跨
 * 会话复用就发布，和其它任何改动同一条路。老实现有两个入口（会话附件、项目
 * 材料）落在两个地方，各覆盖一半场景 —— 用户在会话里传的文件正好落进两者
 * 中间那个洞里，谁都读不到。
 */
export async function addFileToSession(
  projectId: string,
  sessionId: string,
  file: File,
  note = "",
  // 兜底文案跟着界面语言 —— 后端给了 detail.message 就用后端那句。
  lang: Language = "zh",
) {
  const form = new FormData();
  form.append("file", file);
  if (note) form.append("note", note);
  // 走与其它调用同一条鉴权路径（api.fetchWithAuth）；multipart 不能自己设
  // Content-Type，交给浏览器带 boundary。
  const response = await api.fetchWithAuth(
    `${API_BASE_URL}/projects/${projectId}/sessions/${sessionId}/files`,
    { method: "POST", body: form },
  );
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = body?.detail;
    // 后端把「上限多少、超了走哪条路」放在 detail.message 里。把整个对象
    // 丢给 String() 会渲染成 [object Object]，用户拿不到那句唯一有用的话。
    const message =
      typeof detail === "string"
        ? detail
        : typeof detail?.message === "string"
          ? detail.message
          : say({ zh: "文件没能交上去（HTTP {status}）", en: "The file could not be uploaded (HTTP {status})" }, lang, { status: response.status });
    throw new ApiError(message, response.status);
  }
  return response.json() as Promise<AddedFile>;
}

/**
 * 跑轮中插话：把一句话投递给**正在干活**的 agent。
 *
 * 与发新消息的区别：新消息要等这一轮结束才轮得到，插话是**当下**送进去的
 * —— agent 会在几秒内取走，然后决定是把话注入给正在跑的子节点、取消它、
 * 还是只回一句进度。回执只保证"已投递"，不保证"已照办"。
 */
export async function interruptSession(
  projectId: string,
  sessionId: string,
  text: string,
) {
  return requestJson<{ status: string; deliveredAt: string }>(
    `/projects/${projectId}/sessions/${sessionId}/interrupt`,
    { method: "POST", body: JSON.stringify({ text }) },
  );
}

/**
 * 停止当前轮。与插话同一条投递通道，但 harness 侧**不经过模型**：直接写
 * kill_signal（与 CLI /stop 同一份逻辑）。正在进行的生成被**立即切断**
 * （llm 读取与中止信号赛跑，不等对端吐字），之后不再发起新的模型/工具
 * 调用，本轮以 cancelled 收尾；下一条消息照常开新轮。
 * 回执只保证"已投递"（收件箱 1s 轮询 + 0.5s 切断节拍，~1–2 秒内停）。
 * 没有在跑的轮时后端返回 409。
 */
export async function stopSessionTurn(projectId: string, sessionId: string) {
  return requestJson<{ status: string; deliveredAt: string }>(
    `/projects/${projectId}/sessions/${sessionId}/stop`,
    { method: "POST" },
  );
}

export async function getSessionChangeSet(projectId: string, sessionId: string): Promise<SessionChangeSet | null> {
  try {
    const payload = await requestJson<unknown>(
      `/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}/change-set`,
    );
    return adaptSessionChangeSet(payload, { projectId, sessionId });
  } catch (error) {
    // 会话还没有变更集时后端如实 404 —— 这里的 null 是「没有」，不是「接口不可用」。
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

/**
 * 列举工作区里 `path` 那一层（`""` = 根）—— 树是展开一层取一层的。
 *
 * `sessionId` 可以为空：那时问的是**项目主干**。一个还没开过会话的项目，
 * 仓库里照样有文件（建仓骨架、别人发布过来的产物），说"没有会话"然后一个
 * 都不列是在撒谎（2026-09-10 真机看出来的）。
 */
export async function getSessionProjectTree(
  projectId: string,
  sessionId: string,
  path = "",
): Promise<ProjectFileTree> {
  const params = new URLSearchParams();
  if (sessionId) params.set("sessionId", sessionId);
  if (path) params.set("path", path);
  const query = params.size ? `?${params}` : "";
  return requestJson<ProjectFileTree>(
    `/projects/${encodeURIComponent(projectId)}/repository/tree${query}`,
  );
}

/** 读 Project Git 工作树里的一个文件（MEMORY.md / PROJECT.md / 节点产物…）。 */
export async function getProjectRepositoryFile(
  projectId: string,
  path: string,
  sessionId?: string,
): Promise<{ path: string; content: string; truncated?: boolean }> {
  const session = sessionId ? `&sessionId=${encodeURIComponent(sessionId)}` : "";
  return requestJson(
    `/projects/${encodeURIComponent(projectId)}/repository/file`
      + `?path=${encodeURIComponent(path)}${session}`,
  );
}

export async function stageSessionCandidate(
  projectId: string,
  sessionId: string,
  input: SessionCandidateInput,
) {
  const payload = await requestJson<unknown>(
    `/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}/candidates`,
    { method: "POST", body: JSON.stringify(buildSessionCandidateRequest(input)) },
  );
  return adaptSessionCandidate(payload);
}

export async function listSessionConflicts(projectId: string, sessionId: string) {
  const payload = await requestJson<unknown>(
    `/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}/conflicts`,
  );
  return adaptSessionConflicts(payload);
}

export async function resolveSessionConflict(
  projectId: string,
  sessionId: string,
  conflictId: string,
  choice: "use_project" | "use_proposed",
) {
  const payload = await requestJson<unknown>(
    // 冲突的身份是**文件路径** —— git 里冲突就是两边动了同一个文件。
    // 从前它是 merge_conflicts 表里一行的 id。
    `/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}/conflicts/resolve`,
    { method: "POST", body: JSON.stringify({ path: conflictId, choice }) },
  );
  return adaptSessionConflict(payload);
}

export async function publishSessionChanges(
  projectId: string,
  sessionId: string,
  message?: string,
) {
  return requestJson<{ commitSha: string; projectId: string; sessionId: string }>(
    `/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}/publish`,
    { method: "POST", body: JSON.stringify(message ? { message } : {}) },
  );
}

export function latestSessionId(sessions: ResearchSession[]) {
  return [...sessions]
    .filter((session) => session.lifecycleStatus !== "archived")
    .sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt))[0]?.id ?? null;
}

export function rememberProjectSession(projectId: string, sessionId: string) {
  localStorage.setItem(`atrium.project.${projectId}.recent_session_id`, sessionId);
}

export function recalledProjectSession(projectId: string) {
  return localStorage.getItem(`atrium.project.${projectId}.recent_session_id`);
}
