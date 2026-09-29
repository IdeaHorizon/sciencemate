/**
 * API client for the Research Platform backend.
 *
 * Production uses the same-origin gateway. Local development may override
 * this with NEXT_PUBLIC_API_BASE_URL when the backend runs on another port.
 */

import type { ChatRequest } from "./generated/chat-request";
import type { PublicationCategory } from "@/lib/publication-categories";

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api/v1";
const API_BASE = API_BASE_URL;

/** Build a browser URL for a backend-served literature asset. */
export function literatureAssetUrl(path: string): string {
  const origin = API_BASE_URL.replace(/\/api\/v1\/?$/, "");
  return `${origin}${path}`;
}

/** 组织服务器的版本与升级 —— `app/services/server_update.py::ServerUpdate` 的样子。 */
export type ServerUpdate = {
  installed: string | null;
  /** night = 夜里没人用时自己升；off = 等管理员点。 */
  policy: "night" | "off";
  can_update: boolean;
  /** 为什么不能自己升（不是安装脚本装的、没写更新源）。 */
  why_not: string;
  available: string | null;
  available_protocol: number | null;
  /** 新版改了和桌面说话的方式：升完之后，还没更新的桌面会被要求更新。 */
  asks_desktops_to_update: boolean;
  notes: string;
  running: boolean;
  /** 上一趟（或正在跑的这一趟）：安装脚本写的 update-status.json。 */
  last: {
    from?: string | null; to?: string; phase?: string; error?: string;
    started_at?: string; finished_at?: string; by?: string;
  } | null;
  checked_at: string;
  /** 这一次问发布页出的错（网络、签名）。 */
  error: string;
  /** 比这台新、还没传完的 release（tag，新的在前）：`available` 答的是现在升得到的那版。 */
  still_publishing: string[];
};

export class ApiError extends Error {
  readonly status: number;
  /**
   * Every reason a request was rejected, when the endpoint rejects on a list
   * rather than a single cause (currently the password policy). FastAPI's
   * `detail` is a string in that case an object, and `String(detail)` on the
   * object renders "[object Object]" — so keep the parts separate.
   */
  readonly violations: string[];
  /**
   * 桌面和组织服务器版本对不上时，该升级的那一边（"server" / "desktop"）。
   * 由后端机械地定（`app/org_protocol.py`）；界面据此摆按钮，不从那句话里猜。
   */
  readonly whoMustUpdate: "server" | "desktop" | "";

  constructor(
    message: string,
    status: number,
    violations: string[] = [],
    whoMustUpdate: "server" | "desktop" | "" = "",
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.violations = violations;
    this.whoMustUpdate = whoMustUpdate;
  }

  static fromBody(body: unknown, status: number): ApiError {
    const detail = (body as { detail?: unknown } | null)?.detail;
    const fallback = `API error: ${status}`;

    if (detail && typeof detail === "object") {
      const { message, violations, who_must_update: who } =
        detail as { message?: string; violations?: string[]; who_must_update?: string };
      return new ApiError(
        message ?? fallback,
        status,
        Array.isArray(violations) ? violations : [],
        who === "server" || who === "desktop" ? who : "",
      );
    }
    return new ApiError(typeof detail === "string" ? detail : fallback, status);
  }
}

export class ApiClient {
  private token: string | null = null;
  private unauthorizedHandler: (() => void) | null = null;
  private aboutProject: string | null = null;
  private readonly fetchImpl: typeof fetch;

  constructor(fetchImpl: typeof fetch = globalThis.fetch.bind(globalThis)) {
    this.fetchImpl = fetchImpl;
  }

  setToken(token: string | null) {
    this.token = token;
  }

  clearToken() {
    this.token = null;
  }

  hasToken() {
    return this.token !== null;
  }

  onUnauthorized(handler: (() => void) | null) {
    this.unauthorizedHandler = handler;
  }

  /**
   * 这一刻界面在看谁的东西 —— 本机后端按它决定由谁来回答。
   *
   * 项目住在哪，它的记录、会话、产出就在哪。界面不知道也不该知道这件事：它只跟
   * 本机后端说话，由后端替它去问那台服务器（凭据也只在后端那一侧）。后端要做这个
   * 判断，得知道这条请求说的是谁的事。
   *
   * 大多数请求自己说得出来 —— 地址里就带着项目 id（`/projects/{id}/…`），或者
   * 查询串里有 `project_id`。说不出来的那些靠 `X-Project` 兜底：正开着的项目。
   * 新加一个项目级端点时，不必有人记得去后端登记一行 —— 按前缀列名单的话，漏掉的
   * 那个会默默去问错的机器。放在客户端上而不是每个调用点传参：项目页的调用点有
   * 几十个，漏一个的表现是"这一块数据莫名其妙是空的"，而且不报错。
   *
   * **组织不走这里。** 曾经也有一个全局的 `organisation`：组织页在自己的 effect 里
   * 设它 —— 而子组件的查询比父组件的 effect 先发，第一问就落到本机后端，由桌面的
   * 隐式用户回答（2026-09-23 截图里那个 `Dev Researcher` 就是这么来的）；设上之后
   * 侧栏的每一问也被转去了那个组织。组织的事由 `organisation(connectionId)` 每一问
   * 自己点名（`RFC_ORGANISATION_PAGE_20260923` B4）。
   *
   * **这台机器自己的事也不走这里** —— 见 `onThisMachine`。
   */
  askingAbout(about: { project?: string | null }) {
    if ("project" in about) this.aboutProject = about.project ?? null;
  }

  private whoThisIsAbout(): Record<string, string> {
    const said: Record<string, string> = {};
    if (this.aboutProject) said["X-Project"] = this.aboutProject;
    return said;
  }

  /**
   * 问这台机器自己的事 —— 界面这一刻开着哪个项目都与它无关，不带兜底的 `X-Project`。
   *
   * 兜底那个头的意思是"这一问说的是正开着的项目"。有几样东西从来不是：界面偏好
   * （主题、语言、开场教过没有）是这个人在**这台电脑上**的习惯，不是哪个课题的。
   * 带上那个头，组织项目里的这一问就被转去组织服务器，由服务器上那份从没人碰过的
   * "他"来回答（2026-09-24 两次真跑：成员在本机早就关掉了开场，一进组织项目它又弹
   * 出来 —— 服务器上那份说 `onboarding_done: false`；主题、语言也跟着换成服务器那份）。
   *
   * 后端的 `THIS_MACHINES_OWN_AFFAIRS` 不收它：那张名单之外才是组织服务器的线
   * （`app/org_wire.py`），旧桌面还在转交它，服务器得照旧答。说"这一问不关项目"的
   * 是发问的这一侧。
   */
  private onThisMachine<T>(path: string, options: RequestInit = {}): Promise<T> {
    return this.request<T>(path, options, {});
  }

  async fetchWithAuth(input: string, options: RequestInit = {}, about = this.whoThisIsAbout()) {
    const headers = new Headers(options.headers);
    if (this.token) headers.set("Authorization", `Bearer ${this.token}`);
    for (const [key, value] of Object.entries(about)) {
      if (!headers.has(key)) headers.set(key, value);
    }
    const response = await this.fetchImpl(input, { ...options, headers });
    if (response.status === 401) this.unauthorizedHandler?.();
    return response;
  }

  async request<T>(
    path: string,
    options: RequestInit = {},
    about?: Record<string, string>,
  ): Promise<T> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(options.headers as Record<string, string>),
    };
    if (this.token) {
      headers["Authorization"] = `Bearer ${this.token}`;
    }

    const res = await this.fetchWithAuth(`${API_BASE}${path}`, {
      ...options,
      headers,
    }, about);

    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw ApiError.fromBody(body, res.status);
    }

    if (res.status === 204) return undefined as T;
    return res.json();
  }

  /** Upload with multipart form data (no JSON content-type). */
  private async upload<T>(path: string, formData: FormData): Promise<T> {
    const headers: Record<string, string> = { ...this.whoThisIsAbout() };
    if (this.token) {
      headers["Authorization"] = `Bearer ${this.token}`;
    }
    const res = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers,
      body: formData,
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `API error: ${res.status}`);
    }
    return res.json();
  }

  async getCurrentUser() {
    return this.request<CurrentUser>("/auth/me");
  }

  // ── 自更新：查 / 装（下载+验证+暂存）/ 重启（后端退出码 3，壳重新拉起） ──
  async getUpdateStatus() {
    return this.request<UpdateStatus>("/update");
  }

  async installUpdate() {
    return this.request<UpdateStatus & { staged_version: string | null }>("/update/install", { method: "POST" });
  }

  async restartForUpdate() {
    return this.request<{ restarting: boolean; staged_version: string; exit_code: number }>(
      "/update/restart", { method: "POST" });
  }

  async updateCurrentUser(data: { display_name: string }) {
    return this.request<CurrentUser>("/auth/me", {
      method: "PATCH",
      body: JSON.stringify(data),
    });
  }

  // ── Projects ─────────────────────────────────────────────────
  async listProjects() {
    return this.request<Project[]>("/projects/");
  }

  async createProject(data: CreateProjectRequest) {
    return this.request<Project>("/projects/", {
      method: "POST",
      body: JSON.stringify(data),
    });
  }

  /** 改会话属性。目前用于换模型：PATCH …/sessions/{id} { model_backend_id }。 */
  async updateSession(
    projectId: string,
    sessionId: string,
    data: { title?: string; summary?: string | null; model_backend_id?: string },
  ) {
    return this.request<unknown>(
      `/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}`,
      { method: "PATCH", body: JSON.stringify(data) },
    );
  }

  async listRuns(projectId?: string, limit = 50, sessionId?: string) {
    const query = new URLSearchParams();
    if (projectId) query.set("projectId", projectId);
    if (sessionId) query.set("sessionId", sessionId);
    query.set("limit", String(limit));
    return this.request<RunListResponse>(`/runs?${query.toString()}`);
  }

  /** 节点报告的阻塞（项目级；见 features/execution/lib/blockers）。 */
  async listProjectBlockers(projectId: string, limit = 50) {
    return this.request<unknown>(
      `/projects/${encodeURIComponent(projectId)}/blockers?limit=${limit}`,
    );
  }

  async getRun(runId: string) {
    return this.request<RunDetailResponse>(`/runs/${encodeURIComponent(runId)}`);
  }

  async getProject(id: string) {
    return this.request<ProjectDetail>(`/projects/${id}`);
  }

  /** 改项目级运行模式。后端：PATCH /projects/{id}/config（需要 manage_settings）。 */
  async updateProjectConfig(
    id: string,
    data: {
      operation_mode?: "assisted" | "autonomous";
      autonomous_authorized_risk_classes?: string[];
    },
  ) {
    return this.request<{ status: string }>(`/projects/${id}/config`, {
      method: "PATCH",
      body: JSON.stringify(data),
    });
  }

  /** 归档 = 冻结（负责人或组织管理员）。有一轮还在跑时服务器会拒（409），说要先停下。 */
  async archiveProject(id: string) {
    return this.request<Project>(`/projects/${encodeURIComponent(id)}/archive`, { method: "POST" });
  }

  async restoreProject(id: string) {
    return this.request<Project>(`/projects/${encodeURIComponent(id)}/restore`, { method: "POST" });
  }

  async updateProject(id: string, data: UpdateProjectRequest) {
    return this.request<Project>(`/projects/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    });
  }

  async deleteProject(id: string) {
    // 走 `fetchWithAuth`：它管着 401 的落地，也带上"这一刻在看谁的东西"那两个头。
    // 裸 fetch 两样都没有 —— 凭据过期时这里会静静地抛一个 "API error: 401"。
    const res = await this.fetchWithAuth(`${API_BASE}/projects/${id}`, { method: "DELETE" });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `API error: ${res.status}`);
    }
  }

  /**
   * Stream transcript events via SSE using fetch (supports auth headers).
   * Calls onEvent for each parsed JSON event. Returns an abort function.
   */

  // ── 研究产出目录 ───────────────────────────────────────────
  /**
   * 「这个项目产出了什么」—— 交付物 / 研究产出 / 工作过程，一次问清。
   *
   * 判决全在 harness 的 `core/catalog.py`：什么算交付物、哪些是框架内务、
   * 一件产物带着哪些点得开的文件。前端一个字都不重算 —— 重算就是第七个
   * 各说各话的入口。
   */
  async getProjectCatalog(projectId: string, sessionId?: string) {
    const query = sessionId ? `?sessionId=${encodeURIComponent(sessionId)}` : "";
    return this.request<ProjectCatalogPayload>(
      `/projects/${encodeURIComponent(projectId)}/catalog${query}`,
    );
  }

  /** 账本上的记录 head（事实：类型 / 版本 / 冻结 / metadata）。正文走 /repository/raw。 */
  async getProjectRecords(projectId: string, sessionId?: string, type?: string) {
    const params = new URLSearchParams();
    if (sessionId) params.set("sessionId", sessionId);
    if (type) params.set("type", type);
    const query = params.toString() ? `?${params.toString()}` : "";
    return this.request<ProjectRecordsPayload>(
      `/projects/${encodeURIComponent(projectId)}/repository/records${query}`,
    );
  }

  // ── Artifacts ────────────────────────────────────────────────
  async listArtifacts(projectId: string) {
    return this.request<Artifact[]>(`/projects/${projectId}/artifacts/`);
  }

  async listAllArtifacts() {
    return this.request<Artifact[]>(`/artifacts/`);
  }

  async getArtifactContent(projectId: string, artifactId: string) {
    return this.request<ArtifactContent>(`/projects/${projectId}/artifacts/${artifactId}/content`);
  }

  async deleteArtifact(projectId: string, artifactId: string) {
    return this.request<void>(`/projects/${projectId}/artifacts/${artifactId}`, {
      method: "DELETE",
    });
  }

  // ── Project resources ────────────────────────────────────────
  async listProjectResources(
    projectId: string,
    options: { resourceType?: ProjectResourceType; includeDisabled?: boolean } = {},
  ) {
    const query = new URLSearchParams();
    if (options.resourceType) query.set("resource_type", options.resourceType);
    if (options.includeDisabled) query.set("include_disabled", "true");
    const suffix = query.size ? `?${query.toString()}` : "";
    return this.request<ProjectResource[]>(`/projects/${encodeURIComponent(projectId)}/resources${suffix}`);
  }

  async createProjectResource(projectId: string, data: ProjectResourceCreate) {
    return this.request<ProjectResource>(`/projects/${encodeURIComponent(projectId)}/resources`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  }

  async updateProjectResource(projectId: string, resourceId: string, data: ProjectResourcePatch) {
    return this.request<ProjectResource>(
      `/projects/${encodeURIComponent(projectId)}/resources/${encodeURIComponent(resourceId)}`,
      { method: "PATCH", body: JSON.stringify(data) },
    );
  }

  async disableProjectResource(projectId: string, resourceId: string) {
    return this.request<void>(
      `/projects/${encodeURIComponent(projectId)}/resources/${encodeURIComponent(resourceId)}`,
      { method: "DELETE" },
    );
  }

  // ── Compute inventory ────────────────────────────────────────
  async getComputeInventory() {
    return this.request<ComputeInventory>("/compute/inventory");
  }

  // ── Library (scope=library artifacts) ────────────────────────

  async deleteLibraryItem(artifactId: string) {
    // 走 `fetchWithAuth`：它管着 401 的落地，也带上"这一刻在看谁的东西"那两个头。
    // 裸 fetch 两样都没有 —— 凭据过期时这里会静静地抛一个 "API error: 401"。
    const res = await this.fetchWithAuth(`${API_BASE}/library/${artifactId}`, { method: "DELETE" });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `API error: ${res.status}`);
    }
  }

  // ── Legacy Knowledge (KBEntry) ───────────────────────────────

  /**
   * 组织层那一份知识 —— 晋升上去的、全组织共读的。
   *
   * 和按项目问的那几个是**两个问题**：项目层 shadow 组织层，按项目问看到的是
   * "这个项目眼下算数的知识"，这里看到的是"这个组织攒下来的知识"。
   */
  async searchKB(query: string, projectId?: string) {
    return this.request<KBSearchResult[]>("/knowledge/search", {
      method: "POST",
      body: JSON.stringify({ query, project_id: projectId }),
    });
  }

  async getKBEntryChunks(entryId: string) {
    return this.request<ChunkResponse[]>(`/knowledge/entries/${entryId}/chunks`);
  }

  // ── KB Concepts ──────────────────────────────────────────────
  async listConcepts(query?: string, conceptType?: string, limit = 50) {
    const params = new URLSearchParams();
    if (query) params.set("query", query);
    if (conceptType) params.set("concept_type", conceptType);
    params.set("limit", String(limit));
    return this.request<KBConcept[]>(`/kb/concepts?${params}`);
  }

  async getConcept(conceptId: string) {
    return this.request<KBConcept>(`/kb/concepts/${conceptId}`);
  }

  async createConcept(data: { canonical_name: string; concept_type: string; short_definition?: string; aliases?: string[] }) {
    return this.request<KBConcept>("/kb/concepts", {
      method: "POST",
      body: JSON.stringify(data),
    });
  }

  async getConceptClaims(conceptId: string, limit = 20) {
    return this.request<KBClaimResponse[]>(`/kb/concepts/${conceptId}/claims?limit=${limit}`);
  }

  // ── KB Claims ────────────────────────────────────────────────
  async listClaims(opts?: { artifact_id?: string; status?: string; verified?: boolean; limit?: number }) {
    const params = new URLSearchParams();
    if (opts?.artifact_id) params.set("artifact_id", opts.artifact_id);
    if (opts?.status) params.set("status", opts.status);
    if (opts?.verified !== undefined) params.set("verified", String(opts.verified));
    params.set("limit", String(opts?.limit ?? 50));
    return this.request<KBClaimResponse[]>(`/kb/claims?${params}`);
  }

  // ── KB Syntheses ─────────────────────────────────────────────
  async listSyntheses(opts?: { status?: string; synthesis_type?: string; limit?: number; project_id?: string }) {
    const params = new URLSearchParams();
    if (opts?.status) params.set("status", opts.status);
    if (opts?.synthesis_type) params.set("synthesis_type", opts.synthesis_type);
    if (opts?.project_id) params.set("project_id", opts.project_id);
    params.set("limit", String(opts?.limit ?? 20));
    return this.request<KBSynthesis[]>(`/kb/syntheses?${params}`);
  }

  // ── Memory Proposals ─────────────────────────────────────────
  async listProposals(opts?: { status?: string; project_id?: string; limit?: number }) {
    const params = new URLSearchParams();
    params.set("status", opts?.status ?? "pending");
    if (opts?.project_id) params.set("project_id", opts.project_id);
    params.set("limit", String(opts?.limit ?? 50));
    return this.request<MemoryProposal[]>(`/kb/proposals?${params}`);
  }

  // 处理建议走 harness 的 resolve_proposal（经桥）——不是翻个状态字段：
  // 接受可能有副作用（skill candidate 会落 SKILL.md），reasoning 是队列可审计的凭据。
  async approveProposal(proposalId: string, projectId: string, reasoning: string) {
    const params = new URLSearchParams({ project_id: projectId, reasoning });
    return this.request<{ proposal_id: string; decision: string }>(
      `/kb/proposals/${proposalId}/approve?${params}`,
      { method: "POST" },
    );
  }

  async rejectProposal(proposalId: string, projectId: string, reasoning: string) {
    const params = new URLSearchParams({ project_id: projectId, reasoning });
    return this.request<{ proposal_id: string; decision: string }>(
      `/kb/proposals/${proposalId}/reject?${params}`,
      { method: "POST" },
    );
  }

  // ── Memory ───────────────────────────────────────────────────
  async listMemoryEntries(projectId?: string) {
    const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
    return this.request<MemoryEntry[]>(`/memory/entries${qs}`);
  }

  // ── Chat ─────────────────────────────────────────────────────
  async globalChat(data: ChatRequest) {
    return this.request<ChatResponse>("/chat/global", {
      method: "POST",
      body: JSON.stringify(data),
    });
  }

  async projectChat(projectId: string, data: ChatRequest) {
    return this.request<ChatResponse>(`/chat/projects/${projectId}`, {
      method: "POST",
      body: JSON.stringify(data),
    });
  }

  /**
   * Stream global chat response via SSE.
   * onToken is called for each text chunk; onDone with final metadata.
   * Returns an abort function.
   */
  streamGlobalChat(
    data: ChatRequest,
    onToken: (text: string) => void,
    onDone: (meta: { created_project?: Record<string, unknown> | null; concept_annotations?: ConceptAnnotation[] }) => void,
    onError?: (err: string, detail?: Record<string, unknown>) => void,
  ): () => void {
    return this._streamChat("/chat/global/stream", data, onToken, onDone, onError);
  }

  /**
   * Stream project chat response via SSE.
   * onToken for text chunks; onProgress for tool/thinking events; onDone with final metadata.
   */
  streamProjectChat(
    projectId: string,
    data: ChatRequest,
    onToken: (text: string) => void,
    onDone: (meta: { executed_actions?: Array<Record<string, unknown>>; concept_annotations?: ConceptAnnotation[] }) => void,
    onProgress?: (evt: Record<string, unknown>) => void,
    onError?: (err: string, detail?: Record<string, unknown>) => void,
  ): () => void {
    return this._streamChat(`/chat/projects/${projectId}/stream`, data, onToken, onDone, onError, onProgress);
  }

  private _streamChat(
    path: string,
    data: ChatRequest,
    onToken: (text: string) => void,
    onDone: (meta: Record<string, unknown>) => void,
    onError?: (err: string, detail?: Record<string, unknown>) => void,
    onProgress?: (evt: Record<string, unknown>) => void,
  ): () => void {
    const controller = new AbortController();
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (this.token) headers["Authorization"] = `Bearer ${this.token}`;

    (async () => {
      try {
        const res = await this.fetchWithAuth(`${API_BASE}${path}`, {
          method: "POST",
          headers,
          body: JSON.stringify(data),
          signal: controller.signal,
        });
        if (!res.ok || !res.body) {
          if (res.status === 401) this.unauthorizedHandler?.();
          // 非 2xx 的响应体是后端**特意**送来的结构化拒绝（入口 409：这张卡已经
          // 没有停着的 pause / 呈递换了）。从前这里把它整个丢掉、只留状态码，
          // 用户读到的是 "Stream failed: 409" —— 三个字符没有一个能让人多做对
          // 一件事。整份 detail 交出去，让 presentStructuredChatFailure 认。
          let detail: Record<string, unknown> | undefined;
          try {
            const body = (await res.json()) as { detail?: unknown };
            if (body && typeof body.detail === "object" && body.detail !== null) {
              detail = body.detail as Record<string, unknown>;
            }
          } catch {
            detail = undefined;
          }
          const message = typeof detail?.message === "string"
            ? (detail.message as string)
            : `Stream failed: ${res.status}`;
          onError?.(message, detail);
          return;
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";
          for (const line of lines) {
            if (line.startsWith("data: ")) {
              const raw = line.slice(6).trim();
              if (!raw) continue;
              try {
                const evt = JSON.parse(raw);
                if (evt.type === "token") onToken(evt.text);
                else if (evt.type === "progress") onProgress?.(evt);
                else if (evt.type === "done") { onDone(evt); return; }
                // 整份 evt 一起交出去 —— 后端特意"整份摊开、不逐个手抄"送来的
                // 结构化失败（title/body/recovery/retryable），在这里压成一个
                // message 字符串就全没了（2026-08-20 实测：模型服务读超时的
                // 真实说法被压扁，用户读到的是笼统的"这一轮没跑完"）。
                else if (evt.type === "error") { onError?.(evt.message || "Unknown error", evt); return; }
              } catch { /* skip malformed */ }
            }
          }
        }
      } catch (err) {
        if (!controller.signal.aborted) onError?.(String(err));
      }
    })();

    return () => controller.abort();
  }

  // ── Conversations ────────────────────────────────────────────

  // ── Scheduler control ────────────────────────────────────────

  async listEvidenceChains(projectId: string) {
    return this.request<EvidenceChain[]>(`/projects/${projectId}/evidence/chains`);
  }

  // ── V2: Skills ───────────────────────────────────────────────
  async listSkills(opts?: {
    project_id?: string;
    is_validated?: boolean;
    is_deprecated?: boolean;
    limit?: number;
  }) {
    const params = new URLSearchParams();
    if (opts?.project_id) params.set("project_id", opts.project_id);
    if (opts?.is_validated !== undefined) params.set("is_validated", String(opts.is_validated));
    if (opts?.is_deprecated !== undefined) params.set("is_deprecated", String(opts.is_deprecated));
    params.set("limit", String(opts?.limit ?? 50));
    return this.request<ResearchSkillResponse[]>(`/skills/?${params}`);
  }

  // ── V2: Convergence ─────────────────────────────────────────

  // ── V2: Reviews ─────────────────────────────────────────────

  // ── V2: Project assets summary ──────────────────────────────

  // ── 学术搜索（literature search）─────────────────────────────
  async searchLiterature(query: string, limit = 200) {
    return this.request<LiteratureSearchResponse>("/literature/search", {
      method: "POST",
      body: JSON.stringify({ query, limit }),
    });
  }

  async translateLiteraturePage(papers: LiteratureTranslatePaperInput[]) {
    return this.request<LiteratureTranslateResponse>("/literature/translate", {
      method: "POST",
      body: JSON.stringify({ papers }),
    });
  }

  streamLiteratureSearch(
    query: string,
    limit: number,
    onProgress: (event: Record<string, unknown>) => void,
    onDone: (result: LiteratureSearchResponse) => void,
    onError: (message: string) => void,
  ): () => void {
    const controller = new AbortController();
    (async () => {
      try {
        const response = await this.fetchWithAuth(`${API_BASE}/literature/search/stream`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query, limit }),
          signal: controller.signal,
        });
        if (!response.ok || !response.body) {
          const body = await response.json().catch(() => ({}));
          onError((body as { detail?: string }).detail ?? `API error: ${response.status}`);
          return;
        }
        // 进度文案由界面按 `stage` 决定：这一层不该知道用户看的是哪种语言
        // （它是普通模块，拿不到 React 语境），也不该把中文写死在网络层。
        onProgress({ type: "progress", stage: "connection", detail: "" });
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const frames = buffer.split(/\r?\n\r?\n/);
          buffer = frames.pop() || "";
          for (const frame of frames) {
            const line = frame.split(/\r?\n/).find((item) => item.startsWith("data: "));
            if (!line) continue;
            try {
              const event = JSON.parse(line.slice(6)) as Record<string, unknown>;
              if (event.type === "progress") onProgress(event);
              else if (event.type === "done") onDone(event as unknown as LiteratureSearchResponse);
              else if (event.type === "error") onError(String(event.message || ""));
            } catch { /* ignore malformed SSE frames */ }
          }
        }
      } catch (error) {
        if (!controller.signal.aborted) onError(error instanceof Error ? error.message : String(error));
      }
    })();
    return () => controller.abort();
  }

  // ── Research feed ────────────────────────────────────────────
  async getFeedToday() {
    return this.request<FeedToday>("/feed/today");
  }

  async listFeedItems(opts?: {
    domain?: string;
    kind?: string;
    saved?: boolean;
    limit?: number;
    offset?: number;
  }) {
    const params = new URLSearchParams();
    if (opts?.domain) params.set("domain", opts.domain);
    if (opts?.kind) params.set("kind", opts.kind);
    if (opts?.saved) params.set("saved", "true");
    params.set("limit", String(opts?.limit ?? 30));
    params.set("offset", String(opts?.offset ?? 0));
    return this.request<FeedItem[]>(`/feed/items?${params}`);
  }

  async recordFeedEngagement(itemId: string, action: FeedEngagementAction) {
    return this.request<void>(`/feed/items/${encodeURIComponent(itemId)}/engagement`, {
      method: "POST",
      body: JSON.stringify({ action }),
    });
  }

  async undoFeedEngagement(itemId: string, action: FeedEngagementAction) {
    return this.request<void>(
      `/feed/items/${encodeURIComponent(itemId)}/engagement/${encodeURIComponent(action)}`,
      { method: "DELETE" },
    );
  }

  async getFeedInterests() {
    return this.request<FeedInterests>("/feed/interests");
  }

  async saveFeedInterests(domains: string[]) {
    return this.request<FeedInterests>("/feed/interests", {
      method: "PUT",
      body: JSON.stringify({ domains }),
    });
  }

  async getFeedSubscriptions() {
    return this.request<FeedSubscriptions>("/feed/subscriptions");
  }

  async saveFeedSubscriptions(data: FeedSubscriptions) {
    return this.request<FeedSubscriptions>("/feed/subscriptions", {
      method: "PUT",
      body: JSON.stringify(data),
    });
  }

  async searchFeedSubscriptions(kind: "journal" | "scholar", query: string) {
    const params = new URLSearchParams({ kind, q: query });
    return this.request<FeedSubscriptionSearch>(`/feed/subscriptions/search?${params}`);
  }

  async getFeedSubscriptionItems() {
    return this.request<FeedCard[]>("/feed/subscriptions/items?limit=50");
  }

  async getFeedDomainCatalog() {
    return this.request<{ groups: FeedDomainGroup[] }>("/feed/domains");
  }

  async getFeedDigest(domain: string) {
    return this.request<FeedItem | null>(`/feed/digests/${encodeURIComponent(domain)}`);
  }

  async shareFeedLink(data: {
    url: string;
    comment?: string;
    domains?: string[];
    visibility?: "platform" | "organization";
  }) {
    return this.request<FeedItem>("/feed/shares", {
      method: "POST",
      body: JSON.stringify({
        url: data.url,
        comment: data.comment ?? "",
        domains: data.domains ?? [],
        visibility: data.visibility ?? "organization",
      }),
    });
  }

  async getFeedCuration() {
    return this.request<FeedCuration>("/feed/curation");
  }

  async setFeedCuration(enabled: boolean) {
    return this.request<FeedCuration>("/feed/curation", {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    });
  }

  /** 删掉一条推断出来的方向，并记住别再推。 */
  async rejectInferredDomain(domain: string) {
    return this.request<FeedInterests>(
      `/feed/interests/inferred/${encodeURIComponent(domain)}`,
      { method: "DELETE" },
    );
  }

  /**
   * 条目配图的地址。**指向我们自己的后端**，不是出版商。
   *
   * 直接渲染外链等于每次打开首页就向出版商广播一次"这个用户在读这条"
   * （IP + Referer）。所以后端代取，前端连外链都拿不到。
   */
  feedImageUrl(itemId: string) {
    return `${API_BASE}/feed/items/${encodeURIComponent(itemId)}/image?v=2`;
  }

  async getFeedStatus() {
    return this.request<FeedStatus>("/feed/status");
  }

  // ── Settings ─────────────────────────────────────────────────
  async getSettings() {
    return this.request<PlatformSettings>("/settings/");
  }

  async getInterfaceSettings() {
    return this.onThisMachine<InterfaceSettings>("/settings/interface");
  }

  async saveInterfaceSettings(data: InterfaceSettings) {
    return this.onThisMachine<InterfaceSettings>("/settings/interface", {
      method: "PUT",
      body: JSON.stringify(data),
    });
  }

  async getUsageSettings() {
    return this.request<UsageSettings>("/settings/usage");
  }

  async getNotificationSettings() {
    return this.request<NotificationSettings>("/settings/notifications");
  }

  async saveNotificationSettings(data: NotificationSettingsPayload) {
    return this.request<NotificationSettings>("/settings/notifications", {
      method: "PUT",
      body: JSON.stringify(data),
    });
  }

  async listModelBackends() {
    try {
      const response = await this.request<
        ModelBackend[] | { backends?: ModelBackend[]; items?: ModelBackend[] }
      >("/settings/model-backends");
      if (Array.isArray(response)) return response;
      return response.backends ?? response.items ?? [];
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 404) throw error;
      const settings = await this.getSettings();
      const builtins: ModelBackend[] = settings.llm.providers.map((provider) => ({
        id: provider.name,
        provider: provider.name,
        display_name: provider.display_name,
        model: provider.models.includes(settings.llm.default_model)
          ? settings.llm.default_model
          : provider.models[0] ?? "",
        models: provider.models,
        base_url: null,
        status: provider.is_configured ? "ready" : "not_configured",
        is_default: provider.name === settings.llm.default_provider,
        scope: "platform",
        has_api_key: provider.is_configured,
        editable: false,
        source: "legacy",
      }));
      const customs: ModelBackend[] = settings.custom_providers.map((provider) => ({
        id: provider.name,
        provider: provider.name,
        display_name: provider.name,
        model: provider.model,
        models: [provider.model],
        base_url: provider.base_url,
        status: "ready",
        is_default: provider.name === settings.llm.default_provider,
        scope: "platform",
        has_api_key: true,
        editable: true,
        source: "legacy",
      }));
      return [...builtins, ...customs];
    }
  }

  async saveModelBackend(data: SaveModelBackendRequest, id?: string) {
    try {
      return await this.request<ModelBackend>(
        id ? `/settings/model-backends/${encodeURIComponent(id)}` : "/settings/model-backends",
        { method: id ? "PUT" : "POST", body: JSON.stringify(data) },
      );
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 404) throw error;
      if (!data.api_key) {
        throw new Error("Enter a new API key to save this backend.");
      }
      await this.addCustomProvider({
        name: data.provider,
        api_key: data.api_key,
        base_url: data.base_url,
        model: data.model,
      });
      const backends = await this.listModelBackends();
      return backends.find((backend) => backend.id === data.provider) ?? backends[0];
    }
  }

  /**
   * 删掉一条自己建的连接。
   *
   * 后端只让删**自己 scope 里**的、且不是该 scope 默认的那条（409）——
   * 判据在后端，这里不重复判，把它的话原样报给人看。
   */
  /**
   * 现在去问一次 provider，别拿上次那张快照糊弄人。
   *
   * 看得见就能探（后端不要求 editable）：研究员改不了机构那条连接，但"它现在
   * 还能不能用"正是他要判断的事。后端有 60 秒冷却，返回里的 `probed_now`
   * 说明这次到底有没有真去打 —— 别把它藏起来，否则界面只能假装每次都是新的。
   */
  /** 指令是文件：读一份、写一份。路径由后端给出（用别的编辑器改它也算数）。 */
  async readInstructionFile(path: string) {
    return this.request<import("@/features/instructions/types").InstructionFile>(path);
  }

  async writeInstructionFile(path: string, content: string) {
    return this.request<import("@/features/instructions/types").InstructionFile>(path, {
      method: "PUT",
      body: JSON.stringify({ content }),
    });
  }

  async readSessionInstructions(projectId: string, sessionId: string) {
    return this.request<import("@/features/instructions/types").SessionInstructions>(
      `/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}/instructions`,
    );
  }

  async probeModelBackend(id: string) {
    return this.request<ModelBackend & { probed_now: boolean; cooldown_seconds: number }>(
      `/settings/model-backends/${encodeURIComponent(id)}/probe`,
      { method: "POST" },
    );
  }

  /** 启用/禁用一条连接。禁用比删除温和：坏了先关掉，不动已有会话的历史。 */
  async setModelBackendEnabled(id: string, isEnabled: boolean) {
    return this.request<ModelBackend>(`/settings/model-backends/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify({ is_enabled: isEnabled }),
    });
  }

  /**
   * 角色目录 + 每个角色当下的绑定。
   *
   * 前端**不枚举角色**：这份返回是唯一来源。provider 词表当年在 Python 和
   * TypeScript 各活一份、靠一条测试钉着相等，那条路不再走第二遍。
   */
  async listModelRoles() {
    const data = await this.request<{ roles: ModelRole[] }>("/settings/model-roles");
    return data.roles ?? [];
  }

  /** 把某条连接指派为**这个角色**的默认。主模型只是 role="reasoning"。 */
  async setRoleDefaultBackend(id: string, role: string) {
    return this.request<{ status: string; backend_id: string; role: string }>(
      `/settings/model-backends/${encodeURIComponent(id)}/roles/${encodeURIComponent(role)}/default`,
      { method: "POST" },
    );
  }

  async deleteModelBackend(id: string) {
    await this.request<void>(`/settings/model-backends/${encodeURIComponent(id)}`, { method: "DELETE" });
  }

  async setDefaultModelBackend(id: string, provider: string, model: string) {
    try {
      return await this.request<{ status: string }>(
        `/settings/model-backends/${encodeURIComponent(id)}/default`,
        { method: "POST" },
      );
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 404) throw error;
      return this.updateLLMSettings({ default_provider: provider, default_model: model });
    }
  }

  async getResearchSettings() {
    return this.request<ResearchSettings>("/settings/research");
  }

  async saveResearchSettings(data: SaveResearchSettingsRequest) {
    return this.request<ResearchSettings>("/settings/research", {
      method: "PUT",
      body: JSON.stringify(data),
    });
  }

  async updateLLMSettings(data: { default_provider?: string; default_model?: string }) {
    return this.request<{ status: string; provider: string; model: string }>("/settings/llm", {
      method: "PUT",
      body: JSON.stringify(data),
    });
  }

  async addCustomProvider(data: { name: string; api_key: string; base_url: string; model: string }) {
    return this.request<{ status: string; name: string }>("/settings/llm/custom", {
      method: "POST",
      body: JSON.stringify(data),
    });
  }

  async removeCustomProvider(name: string) {
    return this.request<{ status: string }>(`/settings/llm/custom/${name}`, {
      method: "DELETE",
    });
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  Types (matching backend schemas)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/**
 * 这台桌面握着的一个组织。
 *
 * 一条连接 = 一个组织，不是一台服务器：一台机器上住着好几个组织，账号也按组织
 * 唯一。凭据**不在这里** —— 它加密躺在本机（钥匙串优先），界面从头到尾看不到它。
 */
export interface OrganisationConnection {
  id: string;
  url: string;
  organisation_id: string;
  organisation_name: string;
  /** 我在那儿是谁。 */
  email: string;
  display_name: string;
  /** 我在那儿是什么身份。列表上「管理」还是「进入」按它画；不知道时是空串。 */
  role: UserRole | "";
  /** `ok` | `needs_sign_in`（那台服务器不认这张凭据了）。 */
  status: "ok" | "needs_sign_in";
  /**
   * 那边要他先换一个密码（管理员给的是一次性口令）。凭据照样能用，但只要这一位还亮着，
   * 管理员就知道他的密码 —— 打开这个组织时先要一个新的。
   */
  must_change_password?: boolean;
}

/**
 * 一个项目住在哪。
 *
 * **项目出生那一刻挑一个家，默认本机**，之后只有「搬家」，没有「同步」。
 * `reachable: false` = 那台机器现在说不上话，摆的是上一次问到的名字。
 */
export type ProjectHome =
  | { kind: "local"; reachable: true }
  | {
      kind: "organisation";
      connection_id: string;
      name: string;
      url: string;
      reachable: boolean;
      needs_sign_in: boolean;
    };

/** 还在做 / 不做了。归档 = 冻结：只读、不再开会话、后台不再替它干活；能恢复。 */
export type ProjectStatus = "active" | "archived";

export interface Project {
  id: string;
  name: string;
  description: string | null;
  status: ProjectStatus;
  research_domain: string | null;
  entry_type: string | null;
  created_at: string;
  updated_at: string;
  owner?: { id: string; display_name: string } | null;
  owner_name?: string | null;
  scope?: { kind: string; id: string; name: string } | null;
  scope_name?: string | null;
  member_count?: number | null;
  active_session_count?: number | null;
  capabilities?: string[];
  /** 它住在哪 —— 侧栏按它分组。 */
  home?: ProjectHome | null;
  /** 组织里谁看得见：组里所有人（只读）/ 只有项目成员。 */
  visibility?: "organisation" | "members";
  /** 在不在「我的项目」里（自己建的 / 是成员的）。组里别人的组内可见项目也在清单里（桌面靠它记
   *  项目住哪），但只有 `mine` 的画进项目页；旧服务器不发它 = 都是我的。 */
  mine?: boolean;
  /** 能不能归档 / 恢复（负责人或同组织管理员）。能力清单答不了：归档之后能力只剩「看」。 */
  can_archive?: boolean;
}

export interface ProjectDetail extends Project {
  config: Record<string, unknown> | null;
}

export interface RunUsage {
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  cost: number | null;
  currency: string | null;
  coverage: "complete" | "partial";
}

import type { RunExecutionView } from "@/features/sessions/types";

export interface ResearchRun {
  id: string;
  tenantId: string;
  workspaceId: string;
  projectId: string;
  sessionId: string;
  parentRunId: string | null;
  nodeType: string | null;
  status: string;
  usage: RunUsage;
  retryCount: number;
  createdAt: string;
  updatedAt: string;
  startedAt: string | null;
  endedAt: string | null;
  summary: Record<string, unknown> | null;
  /**
   * 这条 run 的局面 —— 与会话那一份出自同一个后端 builder。
   * `status` 是 13 值枚举的原始投影；**判断一律读这里**，别再按状态词猜。
   */
  view: RunExecutionView;
}

export interface RunDetailResponse {
  run: ResearchRun;
  attempts: Array<Record<string, unknown>>;
  eventCount: number;
}

export interface RunListResponse {
  items: ResearchRun[];
  nextCursor: string | null;
}

export interface CreateProjectRequest {
  name: string;
  description?: string;
  research_domain?: string;
  entry_type?: string;
  operation_mode?: string;
  reporting_level?: string;
  preferred_model?: string;
  /**
   * 放在哪：不给（或空串）= 本机，否则是一条连接的 id。
   *
   * **默认必须是「什么都不说」就能拿到的**。一个只想在自己电脑上开个课题的人，
   * 不该先回答「你属于哪个组织」—— 那正是 2026-09-22 被否掉的东西。
   */
  home?: string;
}

export type UpdateProjectRequest = Partial<Pick<Project, "name" | "description" | "research_domain" | "visibility">>;

/** 组织页「项目」那一块的一行（`GET /organisation/projects`）。 */
export interface OrganisationProject {
  id: string;
  name: string;
  research_domain: string;
  status: string;
  visibility: "organisation" | "members";
  updated_at: string;
  lead: { id: string; name: string; active: boolean };
  member_count: number;
  mine: boolean;
}

/**
 * 一个人在他的组织里是什么身份 —— 只有两档（后端 `models.user.UserRole`）。
 * 曾经的 `group_admin` 退役了：组没有表、没有入口，没有一个真组织用过它。
 */
export type UserRole = "institution_admin" | "researcher";

export interface GovernanceScope {
  kind: "institution" | "group" | "individual" | "personal";
  id: string;
  name: string;
}

/** GET /update 的样子（后端 services/self_update.UpdateStatus）。 */
export interface UpdateStatus {
  installed_version: string | null;
  installed_from: "payload" | "bundled" | "repo" | "unknown";
  self_updatable: boolean;
  available_version: string | null;
  staged_version: string | null;
  source: string;
  reachable: boolean;
  error: string | null;
  apply_error: string | null;
  checked_at: string;
  notes: string;
  /** 装着的壳自报的家门（壳启动时写 `<数据根>/shell.json`）；null = 没报或读不懂。 */
  shell: {
    platform: "windows" | "macos";
    path: string;
    sha256: string;
    size: number;
    self_replace: boolean;
    declared_at?: string;
  } | null;
  /** 这次更新对壳意味着什么；只在有新版本时给。changed=null ＝ 判不了（老壳不自报家门）。 */
  shell_update: { changed: boolean | null; needs_reinstall: boolean } | null;
  /** needs_reinstall 时人该去哪下安装包；没有就只能说「请重新安装」。 */
  reinstall_url: string | null;
  /** 比装着的新、还没传完的 release（tag，新的在前）：`available_version` 答的是现在装得上的那版。 */
  still_publishing: string[];
}

export interface CurrentUser {
  id: string;
  email: string;
  display_name: string;
  is_active: boolean;
  /** 管理员刚重置过他的密码 —— 他手上是一次性口令，进来第一件事是改掉。 */
  must_change_password?: boolean;
  role?: UserRole;
  /** 我在哪个组织（组织服务器上才有意义；桌面本机是隐式用户）。 */
  institution_id?: string;
  institution_name?: string;
  governance_scope?: GovernanceScope;
  permissions?: string[];
}

export interface ChangePasswordRequest {
  current_password: string;
  new_password: string;
}

export interface PasswordChangeResult {
  access_token: string;
  token_type: string;
  changed_at: string;
}

export interface PasswordPolicy {
  min_length: number;
  max_bytes: number;
  rules: string[];
}

export type InterfaceTheme = "system" | "light" | "dark";
export type InterfaceDensity = "comfortable" | "compact";
export type InterfaceFontScale = 90 | 100 | 110 | 120;
export type DefaultLanding = "feed" | "last_session" | "projects" | "new_research";

export interface InterfaceSettings {
  theme: InterfaceTheme;
  density: InterfaceDensity;
  font_scale: InterfaceFontScale;
  reduce_motion: boolean;
  /** 界面语言。目前真正覆盖资讯流与域词表 —— 见 shared/i18n/language.ts。 */
  language: "zh" | "en";
  default_landing: DefaultLanding;
  show_run_usage: boolean;
  /**
   * 开场那几张卡片教过没有 —— 这份偏好里唯一一个真正的标记。
   *
   * 它只管"要不要再教一遍"，不代表任何东西配好了：缺口（比如没有主模型）由
   * 真实状态各自负责，清掉模型之后提示照样回来。
   */
  onboarding_done: boolean;
  /** 第一次进项目时那一圈气泡教过没有。和上面那个分开：教的是两件事。 */
  project_guide_done: boolean;
  execution_detail: "summary" | "standard" | "trace";
  auto_collapse_completed_tools: boolean;
  auto_collapse_completed_steps: boolean;
  follow_active_run: boolean;
}

export interface UsageDay {
  date: string;
  total_tokens: number;
  run_count: number;
}

export interface UsageSettings {
  session_count: number;
  run_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  retry_count: number;
  known_cost: number;
  cost_currency: string | null;
  cost_known_runs: number;
  cost_unknown_runs: number;
  active_days: number;
  current_streak: number;
  longest_streak: number;
  daily: UsageDay[];
}

export interface NotificationSettingsPayload {
  decision_required: boolean;
  run_failed: boolean;
  run_completed: boolean;
  budget_warning: boolean;
}

export interface NotificationSettings extends NotificationSettingsPayload {
  delivery_capabilities: string[];
}

/** 组织提供的一个模型，成员看得到的样子（`GET /organisation/models`）。 */
export interface ProvidedModel {
  id: string;
  display_name: string;
  provider: string;
  model: string;
  roles: string[];
  default_for_roles: string[];
  status: ModelBackend["status"];
  is_enabled: boolean;
}

/** 组织知识里的一条（晋升上来的知识卡）。 */
export interface OrganisationKnowledgeEntry {
  id: string;
  statement: string;
  domain: string;
  /** verified_finding / recipe / dead_end / … */
  kind: string;
  applicability: Record<string, unknown> | null;
  why: string;
  practice: string;
  confidence_basis: string;
  project: { id: string; name: string } | null;
  /** 管理员的邮箱；机械车道直落的是 `mechanical`。 */
  approved_by: string;
  at: string;
  /** 组织还认不认它（服务器答）。不作数的不删、不再送给新项目。 */
  in_force: boolean;
  /** 组织的裁定 —— 还作数就是 null。 */
  standing: OrganisationKnowledgeStanding | null;
}

/** 组织对一条知识的裁定：推翻（新证据表明它不成立）/ 取代（有更好的一条）。 */
export interface OrganisationKnowledgeStanding {
  verdict: "refuted" | "superseded";
  reason: string;
  by: string;
  at: string;
  /** 取代它的那一条（可以没有）。 */
  superseded_by: string;
}

export type OrganisationKnowledgeVerdict = OrganisationKnowledgeStanding["verdict"];

export interface OrganisationKnowledge {
  entries: OrganisationKnowledgeEntry[];
  domains: string[];
  can_manage: boolean;
}

export interface OrganisationConcept {
  id: string;
  name: string;
  type: string;
  definition: string;
}

/** 待审里的一条：项目交上来的那张卡 + 原文（晋升），或对组织某一条的更正。管理员裁。 */
export interface OrganisationProposal {
  id: string;
  type: "promotion" | "correction";
  /** 更正才有：它说组织的哪一条错了、为什么、谁提的。 */
  correction: OrganisationCorrection | null;
  status: "pending" | "adopted" | "declined";
  kind: string;
  /** 成员直接提的更正不来自任何项目。 */
  project: { id: string; name: string } | null;
  original: string;
  card: {
    domain?: string;
    statement?: string;
    applicability?: Record<string, unknown>;
    why?: string;
    practice?: string;
    confidence_basis?: string;
  };
  evidence_count: number;
  proposed_at: string;
  decided_by: string;
  decided_at: string;
  reason: string;
}

export interface OrganisationCorrection {
  entry: { id: string; statement: string };
  verdict: OrganisationKnowledgeVerdict;
  /** 提出的人写的理由（管理员退回时写的是 `reason`）。 */
  because: string;
  /** member = 组织里的人提的；agent = 项目里的 agent 拿证据提的；contradiction = 机械比对出方向相反。 */
  origin: "member" | "agent" | "contradiction";
  proposed_by: string;
  /** 机械比对时，另一面那条结论。 */
  about: string;
  superseded_by: string;
}

export interface OrganisationProposals {
  proposals: OrganisationProposal[];
  can_manage: boolean;
}

/** 组织里的一个人（`GET /organisation/members`）。 */
export interface OrganisationMember {
  id: string;
  display_name: string;
  email: string;
  role: UserRole;
  /** 停用的人也在管理员的名录里 —— 否则"停用"是一条单行道，没地方恢复。 */
  is_active: boolean;
  /** 自己建的项目数。 */
  projects_led: number;
  /** 在别人项目成员表上的数目。 */
  projects_joined: number;
}

/**
 * 一个组织的名录。**`you` 由服务器说** —— 桌面手里只有一个本机隐式用户，认不出
 * 这里哪一行是它自己。
 */
export interface OrganisationRoster {
  organisation: { id: string; name: string };
  you: { id: string; role: UserRole };
  can_manage: boolean;
  members: OrganisationMember[];
}

export interface IssuedInvitation {
  id: string;
  /** 明文码，只此一次。 */
  code: string;
  role: UserRole;
  email: string | null;
  expires_at: string;
}

/** 还没人用的请柬。码拿不回来了（库里只有哈希）—— 列它是为了能撤回。 */
export interface PendingInvitation {
  id: string;
  role: UserRole;
  email: string | null;
  issued_by: string;
  created_at: string;
  expires_at: string;
}

export interface KBSearchResult {
  chunk: { id: string; text: string; section: string | null };
  source_title: string;
  source_ref: string | null;
  artifact_id: string | null;
  quality_tier: string;
  score: number;
  // Legacy compat
  entry_title?: string;
  entry_source_ref?: string | null;
}

export interface GraphNode {
  id: string;
  project_id: string;
  branch_id: string | null;
  type: string;
  status: string;
  title: string;
  description: string | null;
  iteration: number;
  parent_node_id: string | null;
  harness_config: Record<string, unknown> | null;
  expected_inputs: Record<string, unknown> | null;
  expected_outputs: Record<string, unknown> | null;
  execution_metadata: Record<string, unknown> | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface GraphEdge {
  id: string;
  project_id: string;
  source_node_id: string;
  target_node_id: string;
  type: string;
  extra_data: Record<string, unknown> | null;
  created_at: string;
}

export interface GraphBranch {
  id: string;
  project_id: string;
  name: string;
  status: string;
  hypothesis: string | null;
  parent_branch_id: string | null;
  fork_point_node_id: string | null;
  is_main: boolean;
  created_at: string;
  merged_at: string | null;
}

export interface GraphOverview {
  nodes: GraphNode[];
  edges: GraphEdge[];
  branches: GraphBranch[];
}

export interface SchedulingResponse {
  ready_nodes: string[];
  blocked_nodes: string[];
  active_nodes: string[];
}

export interface CheckpointOption {
  id: string;
  label: string;
  description: string;
  pros: string[];
  cons: string[];
  is_default: boolean;
}

export interface CheckpointReport {
  situation: string;
  attention_reason: string;
  question: string;
  options: CheckpointOption[];
  default_action: string;
  urgency: "blocking" | "important" | "informational";
  trigger_type: string;
  node_type: string;
  node_id: string;
  model_used: string;
  cost_usd: number;
}

export interface PauseEvent {
  id: string;
  reason: string;
  node_id: string | null;
  description: string;
  options: Array<{ id: string; label: string; data?: Record<string, unknown> }>;
  auto_resolve_at: string | null;
  created_at: string;
  checkpoint_report: CheckpointReport | null;
}

export interface SchedulerStatus {
  state: string;
  project_id: string;
  nodes_executed: number;
  nodes_remaining: number;
  total_cost_usd: number;
  current_node_id: string | null;
  current_node_type: string | null;
  pause_event: PauseEvent | null;
  history: Array<Record<string, unknown>>;
  started_at: string | null;
  completed_at: string | null;
  progress_log: Array<{
    ts: string; iteration: number;
    event: string; tool: string | null; detail: string; cost_usd: number;
  }>;
}

export interface NodeExecuteRequest {
  task_description?: string;
  handoff?: Record<string, unknown>;
  is_autonomous?: boolean;
}

export interface NodeExecutionResponse {
  success: boolean;
  node_id: string;
  status: string;
  iterations: number;
  token_usage: Record<string, number> | null;
  cost_usd: number;
  review_triggered: string | null;
  completion_met: string[];
  completion_unmet: string[];
  error: string | null;
  promoted_to_ready: string[];
  growth_proposals: Array<Record<string, unknown>>;
  decision_points: Array<Record<string, unknown>>;
}

export interface NodeTranscriptResponse {
  node_id: string;
  attempt?: number;
  offset?: number;
  count?: number;
  available_attempts?: string[];
  events: Array<Record<string, unknown>>;
}

// `ChatRequest` 不再在这里手写：它是后端 Pydantic 模型的生成投影
// （platform/contracts/generate_wire_types.py → ./generated/chat-request.ts）。
// 两份手写定义正是「什么算一次合法提交」在四层各自演化的土壤（2026-09-03）。
export type { ChatRequest } from "./generated/chat-request";

export interface ConceptAnnotation {
  text: string;
  start: number;
  end: number;
  concept_id: string;
  concept_type: string;
}

export interface ChatResponse {
  reply: string;
  scope: "global" | "project";
  conversation_id: string | null;
  created_project: {
    id: string;
    name: string;
    start_node_type: string;
    start_node_id: string | null;
    auto_started: boolean;
    scheduler_state: string | null;
  } | null;
  executed_actions: Array<{ action: string; success: boolean; detail?: string }>;
  suggested_actions: Array<Record<string, unknown>>;
  concept_annotations: ConceptAnnotation[];
  usage: Record<string, unknown>;
}

/** 一份冻结的交付物。`openablePath` 为 null = 它没有可在右栏打开的文件。 */
/** 三个分层 —— 与 `core/catalog.py` 的 `TIER_*` 同一套词表。 */
export type CatalogTier = "deliverable" | "output" | "working";

/** 一件研究产出。 */
/** 账本上的一个记录 head —— 与后端 `/repository/records` 逐字段对应。 */
export interface RecordHead {
  id: string;
  type: string;
  name: string;
  /** 工作区相对：正文文件（原生格式）。 */
  path: string;
  version: number;
  frozen: boolean;
  frozenVersion: number;
  frozenAt: string;
  createdAt: string;
  producedByNodeType: string;
  producedByRunId: string;
  metadata: Record<string, unknown>;
}

export interface ProjectRecordsPayload {
  schemaVersion: number;
  sessionId: string | null;
  /** 旧布局（信封时代）的工作区：本版本不读它，界面要说清而不是显示"还没有"。 */
  legacyLayout: boolean;
  records: RecordHead[];
}

export interface CatalogEntry {
  artifactId: string;
  /** 产物类型，取自记录自己的 `type` 字段。 */
  kind: string;
  name: string;
  ownerNode: string;
  version: number;
  frozen: boolean;
  permanent: boolean;
  tier: CatalogTier;
  isDeliverable: boolean;
  /** 工作区相对：那份 JSON 记录。 */
  recordPath: string;
  /** 工作区相对：用户点得开的东西（PDF / 图 / bundle）。 */
  files: string[];
  createdAt: string;
  frozenAt: string;
  producedByRunId: string;
}

export interface ProjectCatalogPayload {
  schemaVersion: number;
  sessionId: string | null;
  entries: CatalogEntry[];
  counts: Record<CatalogTier, number>;
}

export interface Artifact {
  id: string;
  project_id: string;
  node_id: string | null;
  name: string;
  type: string;
  scope?: "project" | "library";
  description: string | null;
  mime_type: string | null;
  current_version: number;
  extra_data: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
  project_name?: string | null;
}

export interface ArtifactContent {
  content: string;
  mime_type: string | null;
}

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
export type ResourceHealth = "online" | "offline" | "unknown";
export type ProjectResourceType = "storage" | "dataset" | "database" | "compute";

export interface ProjectResource {
  id: string;
  project_id: string;
  resource_type: ProjectResourceType;
  name: string;
  description: string | null;
  provider: string;
  endpoint: string | null;
  workspace_binding: string | null;
  config: Record<string, JsonValue>;
  is_enabled: boolean;
  health_status: ResourceHealth;
  has_secret_reference: boolean;
  created_by_user_id: string | null;
  updated_by_user_id: string | null;
  created_at: string;
  updated_at: string;
  disabled_at: string | null;
}

export interface ProjectResourceCreate {
  resource_type: ProjectResourceType;
  name: string;
  description?: string | null;
  provider: string;
  endpoint?: string | null;
  workspace_binding?: string | null;
  config?: Record<string, JsonValue>;
  secret_ref?: string | null;
}

export type ProjectResourcePatch = Partial<ProjectResourceCreate & { is_enabled: boolean }>;

export interface ComputeInventory {
  scope: "local_development";
  observed_at: string;
  health: {
    status: ResourceHealth;
    checks: Array<{ name: string; status: ResourceHealth; detail: string }>;
  };
  nodes: Array<{
    id: "local-app-server";
    name: string;
    kind: "local";
    status: ResourceHealth;
    operating_system: string;
    architecture: string;
    cpu: { status: ResourceHealth; logical_cores: number | null };
    memory: { status: ResourceHealth; total_bytes: number | null; available_bytes: number | null };
    storage: { status: ResourceHealth; total_bytes: number | null; free_bytes: number | null };
    gpu: {
      status: ResourceHealth;
      count: number | null;
      devices: Array<{
        id: string;
        name: string;
        memory_total_bytes: number | null;
        memory_available_bytes: number | null;
        utilization_percent: number | null;
      }>;
    };
  }>;
  schedulers: Array<{
    id: "local-process";
    kind: "in_process";
    status: ResourceHealth;
    supports_queue: false;
    queue_depth: null;
    active_sessions: number;
  }>;
  capacity: {
    status: ResourceHealth;
    cpu_logical_cores: number | null;
    memory_total_bytes: number | null;
    memory_available_bytes: number | null;
    storage_total_bytes: number | null;
    storage_free_bytes: number | null;
    gpu_count: number | null;
    gpu_memory_total_bytes: number | null;
    gpu_memory_available_bytes: number | null;
  };
  recent_jobs: {
    supported: false;
    items: Array<{
      id: string;
      name: string;
      status: "queued" | "running" | "completed" | "failed" | "cancelled" | "unknown";
      submitted_at: string | null;
      started_at: string | null;
      ended_at: string | null;
    }>;
  };
  limitations: string[];
}

// Legacy — retained for existing KBEntry-based pages
export interface KBEntry {
  id: string;
  title: string;
  source_type: string;
  scope: string;
  quality_tier: string;
  source_ref: string | null;
  doi: string | null;
  authors: string[] | null;
  abstract: string | null;
  venue: string | null;
  tags: string[] | null;
  added_by: string;
  added_at: string;
  chunk_count: number;
}

export interface ChunkResponse {
  id: string;
  kb_entry_id: string | null;
  artifact_id: string | null;
  text: string;
  chunk_index: number;
  section: string | null;
  page_number: number | null;
  citability: string;
  token_count: number | null;
}

// ── Library (Artifact scope=library) ───────────────────────────

export interface LibraryItem {
  id: string;
  name: string;
  type: string;
  description: string | null;
  mime_type: string | null;
  is_indexed_in_kb: boolean;
  kb_chunk_count: number;
  paper_metadata: {
    title?: string;
    authors?: string[];
    doi?: string;
    venue?: string;
    abstract?: string;
    publication_date?: string;
  } | null;
  content_hash: string | null;
  created_at: string;
  updated_at: string;
}

// ── KB Concepts ────────────────────────────────────────────────

export type ConceptType =
  | "method" | "model" | "equation" | "dataset" | "benchmark"
  | "metric" | "field" | "technique" | "material" | "phenomenon"
  | "software" | "organization" | "other";

export interface KBConcept {
  id: string;
  canonical_name: string;
  concept_type: ConceptType;
  short_definition: string | null;
  living_summary: string | null;
  source_count: number;
  claim_count: number;
  project_usage_count: number;
  aliases: string[];
  last_refined_at: string | null;
}

// ── KB Claims (no stance — stance lives in KBRelation) ─────────

export interface KBClaimResponse {
  id: string;
  claim_text: string;
  confidence: string;
  artifact_id: string;
  is_verified: boolean;
  concept_ids: string[] | null;
  conditions: Record<string, unknown> | null;
  status: string;
  created_at: string;
}

// ── KB Syntheses ───────────────────────────────────────────────

export interface KBSynthesis {
  id: string;
  title: string;
  content: string;
  synthesis_type: string;
  status: string;
  confidence: string;
  source_artifact_ids: string[];
  key_findings: Array<Record<string, unknown>> | null;
  conflicts_detected: Array<Record<string, unknown>> | null;
  created_at: string;
}

// ── Memory ─────────────────────────────────────────────────────

export interface MemoryEntry {
  id: string;
  content: string;
  type: string;
  layer: string;
  confidence: string;
  status: string;
  project_id: string | null;
  user_id: string | null;
  tags: string[] | null;
  topic: string | null;
  source: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  last_accessed_at: string | null;
  last_verified_at: string | null;
  superseded_by_id: string | null;
  conflicts_with_ids: string[] | null;
  token_count: number | null;
}

export interface CreateMemoryRequest {
  content: string;
  type: string;
  layer: string;
  project_id?: string;
  confidence?: string;
  tags?: string[];
  topic?: string;
  source: Record<string, unknown>;
}

export interface MemoryProposal {
  id: string;
  proposed_content: string;
  proposed_layer: string;
  proposed_type: string;
  proposed_confidence: string;
  proposed_topic: string | null;
  reasoning: string | null;
  status: string;
  conflicts_with: string[] | null;
  supersedes: string[] | null;
  created_at: string;
}

// ── Evidence Chains ────────────────────────────────────────────

export interface EvidenceChain {
  id: string;
  project_id: string;
  node_id: string | null;
  artifact_id: string | null;
  claim_text: string;
  provenance_level: string;
  status: string;
  confidence: number | null;
  claim_location: string | null;
  claims: Claim[];
  created_at: string;
}

export interface Claim {
  id: string;
  evidence_chain_id: string;
  evidence_type: string;
  evidence_text: string;
  reasoning: string | null;
  confidence: number | null;
  kb_chunk_id: string | null;
  artifact_id: string | null;
  data_reference: string | null;
  is_verified: boolean;
  created_at: string;
}

// ── Research feed ──────────────────────────────────────────────

export interface LiteraturePaper {
  title: string;
  title_zh: string | null;
  abstract_zh: string | null;
  authors: string[];
  year: number | string | null;
  pub_date: string | null;
  venue: string | null;
  doi: string | null;
  url: string | null;
  abstract: string | null;
  ai_summary: string | null;
  source: string;
  citations: number;
  score: number;
  score_breakdown: Record<string, unknown>;
  intent_coverage: number;
  ranking_score: number;
  cas_quartile: number | null;
  cas_top: boolean;
  impact_factor: number | null;
  impact_factor_year: number | null;
  jcr_quartile: string | null;
  cas_year: number | null;
  journal_metrics_match: string | null;
  index_completeness: Record<string, boolean>;
  metadata_provenance: Record<string, unknown>;
  publication_category: PublicationCategory;
  exact_title_match: boolean;
  local_pdf_url: string | null;
  local_figure_urls: string[];
}

export interface LiteratureTranslatePaperInput {
  key: string;
  title: string;
  abstract: string | null;
  authors: string[];
  year: number | string | null;
  venue: string | null;
}

export interface LiteratureTranslation {
  key: string;
  title_zh: string | null;
  abstract_zh: string | null;
  ai_summary: string | null;
}

export interface LiteratureTranslateResponse {
  translations: LiteratureTranslation[];
  status: string;
  diagnostics: Record<string, unknown>;
}

export interface LiteratureSearchResponse {
  query: string;
  decomposed_queries: string[];
  papers: LiteraturePaper[];
  total: number;
  candidate_count: number;
  excluded_low_relevance: number;
  minimum_relevance: number;
  required_core_terms: string[];
  source_counts: Record<string, number>;
  requested_sources: string[];
  attempted_sources: string[];
  unavailable_sources: string[];
  warnings: Array<Record<string, unknown>>;
  from_cache: boolean;
  timings: Record<string, number>;
}

export type FeedItemKind = "paper" | "release" | "deadline" | "news" | "digest" | "post";
export type FeedEngagementAction = "impression" | "open" | "save" | "dismiss";

export interface FeedItem {
  id: string;
  kind: FeedItemKind;
  title: string;
  url: string | null;
  summary: string | null;
  authors: string[];
  venue: string | null;
  published_at: string | null;
  domains: string[];
  /** 域的人读名，与 domains 同序。词表在 harness，前端不自己查。 */
  domain_labels: string[];
  source_name: string | null;
  author_display_name: string | null;
  /** 这条有没有配图。**没有外链** —— 要图走 feedImageUrl(id)，由后端代取。 */
  saved: boolean;
  extra: Record<string, unknown>;
}

export interface FeedCard {
  item: FeedItem;
  /** 为什么推给你 —— 点名了重叠的术语，用户扫一眼就能验证真假。 */
  reason: string;
  project_id: string | null;
  project_name: string | null;
  /** 今日必读：每天替你挑的少数几条，排在流最前、挂角标。 */
  is_today_pick: boolean;
}

export interface FeedToday {
  feed: FeedCard[];
  onboarded: boolean;
  /** false = 还没有任何个性化依据。界面据此说实话，而不是假装这是为你挑的。 */
  personalized: boolean;
  empty_reason: string;
  refreshing: boolean;
}

export interface FeedDomainOption {
  domain: string;
  label: string;
  kind: "spine" | "leaf" | "archive";
}

export interface FeedDomainGroup {
  archive: string;
  label: string;
  categories: FeedDomainOption[];
}

export interface FeedCuration {
  enabled: boolean;
  /** 平台配了挖掘模型吗。与 enabled 是两件事 —— 合成一个布尔值，用户就
   *  分不清"我没开"和"开了也没用"。 */
  available: boolean;
  model_label: string | null;
  last_run_at: string | null;
  last_error: string | null;
  inferred_domains: string[];
  inferred_domain_labels: string[];
  inferred_queries: string[];
}

export interface FeedJournalSubscription {
  key: string;
  name: string;
  issn: string;
  eissn: string;
  impact_factor: number | null;
  jcr_quartile: string | null;
  /**
   * 期刊封面。**后端目前不填这个字段** —— 界面已经按"有图就显示、没图就留空
   * 占位"渲染好了，等以后接了封面源，这里开始有值就自动显示，不用再改界面。
   */
  cover_url?: string | null;
}

export interface FeedScholarSubscription {
  key: string;
  name: string;
  source: "kb" | "literature";
  /** 学者头像。同 `cover_url`：现在是空的，界面留空白头像占位。 */
  avatar_url?: string | null;
}

export interface FeedSubscriptions {
  journals: FeedJournalSubscription[];
  scholars: FeedScholarSubscription[];
}

export type FeedSubscriptionSearch = FeedSubscriptions;

export interface FeedInterests {
  domains: string[];
  domain_labels: string[];
  /** agent 从你的课题推断的，与手选的分开 —— 界面要标出处、要能单独删。 */
  inferred: FeedDomainOption[];
  /** 从已有 Project 机械猜出来的预填建议 —— 不给空白问卷。 */
  suggested: FeedDomainOption[];
  onboarded: boolean;
}

export interface FeedSourceHealth {
  id: string;
  kind: string;
  name: string;
  is_active: boolean;
  domains: string[];
  poll_interval_seconds: number;
  last_polled_at: string | null;
  last_success_at: string | null;
  last_item_count: number | null;
  consecutive_failures: number;
  last_error: string | null;
}

export interface FeedStatus {
  sources: FeedSourceHealth[];
  total_items: number;
  collector_enabled: boolean;
  external_fetch_enabled: boolean;
  domain_registry_available: boolean;
}

// ── Settings ───────────────────────────────────────────────────

export interface LLMProviderInfo {
  name: string;
  display_name: string;
  is_configured: boolean;
  models: string[];
}

export interface LLMSettings {
  default_provider: string;
  default_model: string;
  providers: LLMProviderInfo[];
}

export interface PlatformSettings {
  llm: LLMSettings;
  custom_providers: Array<{ name: string; base_url: string; model: string }>;
  memory_project_soft_limit: number;
  memory_consolidation_threshold: number;
  default_project_llm_token_budget: number;
}

export interface ModelBackend {
  id: string;
  provider: string;
  display_name: string;
  model: string;
  models?: string[];
  base_url: string | null;
  /**
   * 这个模型能吃多少 token。决定摘要器何时压缩（阈值为窗口的 70%）。
   *
   * 可选：老后端 / legacy provider 列表不返这个字段。缺失与 null 同义 ——
   * 「没配，用平台默认」，不去猜一个数。
   */
  context_window_tokens?: number | null;
  status: "ready" | "not_configured" | "unreachable" | "unknown" | string;
  is_default: boolean;
  scope:
    | { kind: "platform" | "institution" | "group" | "personal" | string; id: string }
    | "platform"
    | "institution"
    | "group"
    | "personal"
    | string;
  has_api_key: boolean;
  editable: boolean;
  /**
   * 这一条是哪个组织提供的（空 = 这台机器自己的）。组织是资源的提供者：加入之后它的模型
   * 直接出现在你自己的列表里，标着来源，改不了（那是组织管理员的事），选了就能用。
   */
  provided_by?: { connection_id: string; organisation_name: string } | null;
  /**
   * 这条连接是不是启用着。库里一直有这个字段、PUT 也一直收，但列表接口从前
   * 不投影它 —— 于是界面连"当前是开是关"都读不到，禁用这条路径整个不存在。
   */
  is_enabled?: boolean;
  /**
   * 上一次探活的**观测**（不是由它派生的 `status`）。
   *
   * `status` 看起来像当前事实，其实是快照：探针只在新建/改凭证/设默认时跑。
   * 没有这几个字段，界面就没法说出"这个 Ready 是三周前的"。
   */
  last_probe_at?: string | null;
  last_probe_ok?: boolean | null;
  last_probe_detail?: string | null;
  /**
   * 这条连接被授权服务哪些**模型角色**（能力槽），以及它是哪些角色的默认。
   * 合法取值来自 GET /settings/model-roles —— 前端不枚举角色。
   */
  roles?: string[];
  default_for_roles?: string[];
  /**
   * 视觉能力的**独立观测**，跟凭据健康分开。合成一个 status 会让"这个模型
   * 不认图"显示成"凭据被拒"，人就去换 key 了 —— 那是两件事。
   */
  serves_vision_role?: boolean;
  last_vision_ok?: boolean | null;
  last_vision_detail?: string | null;
  source?: "legacy";
}

/** 一个模型角色：目录里的定义 + 当下真会用哪条连接。 */
export interface ModelRole {
  id: string;
  title: string;
  description: string;
  modality: "text" | "vision" | string;
  required: boolean;
  /** 缺这个角色的后果与出路 —— 写给**消费方节点**的（"你什么都不用做"）。 */
  absence_note: string;
  /**
   * 同一件事写给**人**："这个槽空着，我会少什么"。设置页读这一句，不读
   * absence_note —— 后者是对被拒绝的节点说的，摆在一个「注册模型」按钮
   * 旁边就是在跟唯一能填这个槽的人说"这不是你能改的事"。
   */
  absence_impact: string;
  available: boolean;
  bound_backend_id: string | null;
  bound_display_name: string | null;
  bound_model: string | null;
}

export interface SaveModelBackendRequest {
  provider: string;
  display_name: string;
  model: string;
  base_url: string;
  api_key?: string;
  /** 留空 = 用平台默认；别替用户编一个数。 */
  context_window_tokens?: number | null;
  /** 留空 = 只服务 reasoning（绝大多数连接就是主模型）。 */
  roles?: string[];
}

export type ResearchInstructionScope = "all" | "literature" | "experiments" | "writing" | "review";

export interface ResearchInstruction {
  id: string;
  title: string;
  scope: ResearchInstructionScope;
  instruction: string;
  enabled: boolean;
}

export interface EffectiveResearchLayer {
  kind: "institution" | "group" | "personal";
  name: string;
  editable: boolean;
  instruction_count: number;
  summary: string;
}

export interface ResearchSettings {
  response_language: "auto" | "zh-CN" | "en";
  citation_style: "author_year" | "numeric" | "apa";
  evidence_standard: "balanced" | "strict" | "exploratory";
  memory_enabled: boolean;
  instructions: ResearchInstruction[];
  effective_layers: EffectiveResearchLayer[];
  updated_at: string | null;
}

export type SaveResearchSettingsRequest = Pick<
  ResearchSettings,
  "response_language" | "citation_style" | "evidence_standard" | "memory_enabled" | "instructions"
>;

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
//  V2 Types
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface ResearchSkillResponse {
  id: string;
  project_id: string | null;
  name: string;
  description: string | null;
  node_types: string[];
  steps: Array<Record<string, unknown>>;
  tools_required: string[] | null;
  expected_outcome: string | null;
  pitfalls: { items?: string[] } | Record<string, unknown> | null;
  is_validated: boolean;
  is_deprecated: boolean;
  scope: string;
  promotion_status: string | null;
  usage_count: number;
  success_rate: number | null;
  extracted_from_node_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ConvergenceResponse {
  project_id: string;
  has_data: boolean;
  verdict:
    | "accept" | "plateau_good" | "plateau_low" | "improving"
    | "diverging" | "insufficient" | "hard_limit" | null;
  confidence: number | null;
  reasons: string[];
  recommendation: string | null;
  score_trend: number[];
  issue_count_trend: number[];
  review_count: number;
  latest_review_node_id: string | null;
  summary_zh: string | null;
}

export interface PanelSubVerdict {
  persona: string;
  score: number | null;
  sub_verdict: string;
  strengths?: string[];
  weaknesses?: string[];
  fatal_flaw?: boolean;
}

export interface ReviewVerdictPayload {
  recommendation?: string;
  overall_score?: number;
  scores?: Record<string, number>;
  pre_review_checks?: Record<string, unknown>;
  hypothesis_coverage?: Array<{
    id: string;
    description: string;
    tested: boolean;
    evidence?: string | null;
    missing_experiment?: string | null;
  }>;
  fatal_flaws?: string[];
  required_revisions?: string[];
  suggested_improvements?: string[];
  actionable_experiments?: Array<Record<string, unknown>>;
  revision_scopes_needed?: string[];
  strengths?: string[];
  panel?: PanelSubVerdict[];
  _enforced_decision?: string;
  _actionable_experiment_specs?: Array<Record<string, unknown>>;
}

export interface LatestReviewResponse {
  project_id: string;
  has_data: boolean;
  review_node_id: string | null;
  review_verdict: ReviewVerdictPayload | null;
  iteration: number;
}

export const api = new ApiClient();


/** 组织登记的一台机器（`core.machines`）。现状是组织服务器探的，带着探于何时。 */
export interface OrganisationMachine {
  id: string;
  name: string;
  kind: "gpu_node" | "cpu_node" | "slurm_cluster" | "pbs_cluster" | "kubernetes";
  host: string;
  port: number;
  username: string;
  status: "online" | "unreachable";
  probed_at: string;
  last_online_at?: string;
  problem?: string;
  /** 一行现状（几张什么卡、分区…），由 harness 说 —— 和 agent 读到的是同一句。 */
  summary: string;
  /** 我能不能用（授给了我，或授给了所有人）。 */
  yours: boolean;
}

/** 一条授权：谁 × 哪台机器 × 哪几张卡 / 哪个分区。`who` 是用户 id，或 `default`（所有人）。 */
export interface OrganisationGrant {
  who: string;
  who_name?: string;
  machine: string;
  machine_name?: string;
  devices: string;
  partition: string;
}

export interface OrganisationComputeOverview {
  machines: OrganisationMachine[];
  grants: OrganisationGrant[];
  /** grants.yaml 里手写的、不引用登记机器的授权条数（照样生效，这里不改）。 */
  other_grants: number;
  /** 管理员编辑授权时挑人用；成员拿到的是空的。 */
  people: { id: string; name: string }[];
  can_manage: boolean;
}

export interface OrganisationJob {
  job_id: string;
  purpose: string;
  resources: string;
  status: string;
  open: boolean;
  scheduler_job_id: string | null;
  project: { id: string; name: string };
  who: { id: string; name: string };
  started_at: number;
  hours: number;
  expected_hours: number | null;
  overrun_ratio: number | null;
  last_progress: string;
}

export interface OrganisationUsageRow {
  name: string;
  jobs: number;
  hours: number;
  gpu_hours: number;
}

export interface OrganisationJobs {
  jobs: OrganisationJob[];
  usage: {
    days: number;
    by_project: OrganisationUsageRow[];
    by_person: OrganisationUsageRow[];
    jobs_without_a_gpu_count: number;
  };
  unreadable: string[];
  can_manage: boolean;
}
