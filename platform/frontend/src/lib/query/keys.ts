/**
 * Centralized query key factory.
 *
 * Always import from here — never inline string arrays. Keeps invalidation
 * consistent and TypeScript-checked.
 */
export const qk = {
  // Projects
  projects: () => ["projects"] as const,
  project: (id: string) => ["project", id] as const,
  projectMembers: (id: string) => ["project", id, "members"] as const,

  // Graph + scheduler (per-project)
  graph: (projectId: string) => ["graph", projectId] as const,
  scheduling: (projectId: string) => ["scheduling", projectId] as const,
  schedulerStatus: (projectId: string) => ["scheduler", projectId, "status"] as const,
  runs: (projectId: string, limit = 50) => ["runs", projectId, limit] as const,
  sessionRuns: (sessionId: string, limit = 10) => ["runs", "session", sessionId, limit] as const,
  sessionRunsPrefix: (sessionId: string) => ["runs", "session", sessionId] as const,
  run: (runId: string) => ["run", runId] as const,
  runEvents: (sessionId: string, runId: string) =>
    ["run", runId, "events", sessionId] as const,
  visibleRuns: (limit = 4) => ["runs", "visible", limit] as const,

  // Canonical project Sessions + Revision staging
  sessionsPrefix: (projectId: string) => ["sessions", projectId] as const,
  sessions: (projectId: string, mode: string = "api") => ["sessions", projectId, mode] as const,
  session: (projectId: string, sessionId: string, mode: string = "api") =>
    ["sessions", projectId, sessionId, mode] as const,
  sessionMessages: (projectId: string, sessionId: string, mode: string = "api") =>
    ["sessions", projectId, sessionId, "messages", mode] as const,
  sessionChangeSet: (projectId: string, sessionId: string) =>
    ["sessions", projectId, sessionId, "change-set"] as const,
  sessionConflicts: (projectId: string, sessionId: string) =>
    ["sessions", projectId, sessionId, "conflicts"] as const,
  /** 冻结交付物。会话只决定"能不能点开"，所以它是键的一部分。 */
  projectMemory: (projectId: string) => ["projects", projectId, "memory-md"] as const,
  projectFileTree: (projectId: string) => ["projects", projectId, "file-tree"] as const,
  projectBlockers: (projectId: string) => ["projects", projectId, "blockers"] as const,
  projectRecords: (projectId: string, sessionId = "") =>
    ["projects", projectId, "records", sessionId] as const,
  /**
   * 这个会话的整棵文件树 —— **前缀**，不带层。
   *
   * 树是一层一个 query（展开一层取一层），所以"刷新这棵树"要作用在所有层上。
   * 前缀只在这里定义一次：失效那边自己拼一个数组的话，加一段就会静默失配 ——
   * 而失配的表现是"跑完一轮树不刷新"，没有任何报错。
   */
  projectCatalog: (projectId: string, sessionId = "") =>
    ["projects", projectId, "catalog", sessionId] as const,
  sessionProjectTreeRoot: (projectId: string, sessionId: string) =>
    ["sessions", projectId, sessionId, "project-tree"] as const,
  sessionProjectTree: (projectId: string, sessionId: string, path = "") =>
    [...qk.sessionProjectTreeRoot(projectId, sessionId), path] as const,
  /**
   * 右栏打开的那个文件。**刻意不复用 projectFileTree 的 key**：树每 5 秒轮询
   * 一次（要显示 live changes），预览跟着轮询就是开着一份 PDF 每 5 秒重下一遍。
   * 两者问的是不同的问题，刷新节奏也就不同。
   */
  projectFilePreview: (projectId: string, sessionId: string, path: string) =>
    ["projects", projectId, "file-preview", sessionId, path] as const,

  // Artifacts
  artifacts: (projectId: string) => ["artifacts", projectId] as const,
  artifact: (projectId: string, id: string) =>
    ["artifact", projectId, id] as const,
  artifactContent: (projectId: string, id: string) =>
    ["artifact-content", projectId, id] as const,
  allArtifacts: () => ["artifacts", "all"] as const,

  // Registered Project resources + observed compute inventory
  projectResources: (projectId: string, includeDisabled = false) =>
    ["project-resources", projectId, includeDisabled] as const,
  computeInventory: () => ["compute", "inventory"] as const,

  // Library (scope=library artifacts)
  library: (query?: string) => ["library", query ?? ""] as const,
  libraryItem: (id: string) => ["library-item", id] as const,

  // Nodes
  nodeTranscript: (projectId: string, nodeId: string, attempt?: number) =>
    ["node-transcript", projectId, nodeId, attempt ?? 0] as const,

  reviewVerdict: (projectId: string, reviewId: string) =>
    ["review-verdict", projectId, reviewId] as const,
  latestReview: (projectId: string) =>
    ["latest-review", projectId] as const,

  // KB
  concepts: (query?: string, type?: string) =>
    ["concepts", query ?? "", type ?? ""] as const,
  concept: (id: string) => ["concept", id] as const,
  conceptClaims: (conceptId: string) => ["concept-claims", conceptId] as const,
  claims: (filters: Record<string, unknown>) =>
    ["claims", filters] as const,
  syntheses: (filters: Record<string, unknown>) =>
    ["syntheses", filters] as const,

  // Memory
  memory: (projectId?: string) => ["memory", projectId ?? "all"] as const,
  proposals: (filters: Record<string, unknown>) =>
    ["proposals", filters] as const,

  // V2: Skills
  skills: (projectId?: string) => ["skills", projectId ?? "all"] as const,
  skill: (id: string) => ["skill", id] as const,

  // V2: Project assets summary

  // V2: Inbox (aggregated pending approvals)
  inbox: (filters?: Record<string, unknown>) =>
    ["inbox", filters ?? {}] as const,

  // Evidence chains
  evidenceChains: (projectId: string) =>
    ["evidence-chains", projectId] as const,

  // Conversations + chat

  // Legacy KB
  kbEntries: (projectId?: string) =>
    ["kb-entries", projectId ?? "all"] as const,
  kbEntry: (id: string) => ["kb-entry", id] as const,
  kbEntryChunks: (id: string) => ["kb-entry-chunks", id] as const,

  // Research feed
  feedToday: () => ["feed", "today"] as const,
  feedItems: (filters?: Record<string, unknown>) => ["feed", "items", filters ?? {}] as const,
  literatureSearch: (query: string) => ["literature", "search", query] as const,
  feedInterests: () => ["feed", "interests"] as const,
  feedSubscriptions: () => ["feed", "subscriptions"] as const,
  feedSubscriptionSearch: (kind: string, query: string) => ["feed", "subscriptions", "search", kind, query] as const,
  feedSubscriptionItems: () => ["feed", "subscriptions", "items"] as const,
  feedDomains: () => ["feed", "domains"] as const,
  feedDigest: (domain: string) => ["feed", "digest", domain] as const,
  feedStatus: () => ["feed", "status"] as const,
  feedCuration: () => ["feed", "curation"] as const,

  // Settings
  settings: () => ["settings"] as const,
  interfaceSettings: () => ["settings", "interface"] as const,
  modelBackends: () => ["settings", "model-backends"] as const,
  modelRoles: () => ["settings", "model-roles"] as const,
  researchSettings: () => ["settings", "research"] as const,
  usageSettings: () => ["settings", "usage"] as const,
  notificationSettings: () => ["settings", "notifications"] as const,
} as const;

/**
 * 一个组织的事。键里**带着连接 id**：不带的话，30 秒内从 A 组织切到 B 组织，界面摆的
 * 是 A 的名录（缓存还"新鲜"）。`null` = 这个网页就是组织服务器自己开的。
 */
export const organisationKeys = {
  all: (connectionId: string | null) => ["organisation", connectionId ?? "here"] as const,
  members: (connectionId: string | null) => ["organisation", connectionId ?? "here", "members"] as const,
  invitations: (connectionId: string | null) =>
    ["organisation", connectionId ?? "here", "invitations"] as const,
  server: (connectionId: string | null) => ["organisation", connectionId ?? "here", "server"] as const,
  /** 这个组织的模型连接与角色 —— 和本机那两份键分开（`qk.modelBackends()`）。 */
  models: (connectionId: string | null) => ["organisation", connectionId ?? "here", "models"] as const,
  modelRoles: (connectionId: string | null) => ["organisation", connectionId ?? "here", "model-roles"] as const,
  /** 这个组织里我看得见的项目。 */
  projects: (connectionId: string | null) => ["organisation", connectionId ?? "here", "projects"] as const,
  /** 这个组织的算力：机器与授权、作业与用量。 */
  compute: (connectionId: string | null) => ["organisation", connectionId ?? "here", "compute"] as const,
  /** 这个组织的知识：条目（按筛选）、概念、待审。 */
  knowledge: (connectionId: string | null) => ["organisation", connectionId ?? "here", "knowledge"] as const,
  /** 这台组织服务器自己的版本与升级（`/server/update`）。 */
  serverUpdate: (connectionId: string | null) =>
    ["organisation", connectionId ?? "here", "server-update"] as const,
} as const;
