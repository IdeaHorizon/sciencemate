import type { RunUsage } from "@/lib/api";
import type { ContextWindowState } from "./lib/context-window";

export type SessionLifecycleStatus = "active" | "completed" | "archived";

/**
 * 一个会话此刻是什么局面 —— **后端现算的成品，客户端零推导**。
 *
 * 2026-08-27 之前这里是一个 13 值枚举，客户端围着它手写了 9 套分类集合、
 * 5 个各自不同输入的"在跑吗"布尔。那些集合互相不一致，而分叉时两边都不报错：
 * 一条永远停在 `queued` 的子 run 只要被碰一下 updated_at，就能让整个会话
 * 显示「Queued + 转圈 + 一个按下去回 409 的停止按钮」。
 *
 * 三态互斥且完备。要判断什么，读这里的字段，**不要再按状态词猜**。
 * 后端契约见 services/execution_view.py。
 */
export type ExecutionPhase = "alive" | "ended" | "interrupted";
export type WaitingKind = "human" | "permission" | "compute";

/**
 * **答案从哪儿进来** —— 后端 `execution_view.answer_affordance` 的投影。
 *
 * 三个取值互斥且完备，而且是**和类型**：`via === "pause"` 必然带着 `pause`
 * 本体。「输入框关了但卡不在」因此在类型上就写不出来 —— 那正是 2026-09-01
 * 把用户锁死 6 小时 25 分的那个组合（后端说走卡片、前端一张卡都没画）。
 *
 * 客户端**不许**再从这些字段推导出第二个答案。要判断"输入框开不开"就读
 * `via`，不要读 phase、不要读 waitingOn、更不要拿状态词去撞名单。
 */
export type AnswerAffordance =
  | {
      via: "composer";
      /**
       * 在等人回答、却拿不到那个问题本身（老会话、上游削过字段）。输入框
       * 还给人，并**如实说出来** —— 唯一比"给错入口"更坏的是"一个入口都不给"。
       */
      degraded?: string;
    }
  | { via: "pause"; pause: SessionPendingApproval }
  | {
      via: "none";
      reason: string;
      /**
       * 这个拒绝会自己过期的时刻（别人的驾驶权租约到点）。客户端据此安排
       * 一次重新查询 —— 决定"何时再问"是客户端的事，决定"答案是什么"不是。
       */
      until?: string | null;
    };

/**
 * 一条 **run** 此刻是什么局面 —— 后端 `execution_view.build()` 的投影。
 *
 * 注意它**没有** `answer`：答复入口是会话的属性，不是某条 run 的。少了这个
 * 字段，"从一条早就结束的 run 上渲染出一个能点的卡片"就没有数据可依 ——
 * 那条 run 当时停在哪个问题上是**记录**（`summary.pause`），记录永远只读。
 */
export interface RunExecutionView {
  /** alive=有东西在跑或在等；ended=走到了终点；interrupted=途中运行时没了。 */
  phase: ExecutionPhase;
  /** phase=alive 且非 null 时：在等**哪一类**东西。null = 在动。
   *  呈递的身份不在这儿 —— 它随卡片一起长在 `answer.pause` 里。 */
  waitingOn: { kind: WaitingKind } | null;
  /** phase=ended 时的结局。 */
  outcome: "ok" | "ok_with_warning" | "incomplete" | "failed" | "cancelled" | null;
  /** 结局不干净或被打断时，给用户看的失败说明。 */
  error: Record<string, unknown> | null;
  /** 与 `/stop` 端点**同一个谓词**：亮着就一定停得掉。 */
  canStop: boolean;
  label: string;
  runId: string | null;
  since: string | null;
}

/**
 * 一个**会话**此刻是什么局面 = run 的局面 + 答复入口。
 *
 * 这里没有 `canSend`：它曾经是一个答不出"让位给谁"的孤立布尔，而"让位给谁"
 * 由另外两段代码各自回答 —— 三个答案，分叉时全都不报错（2026-09-01，6 小时）。
 */
export interface SessionExecutionView extends RunExecutionView {
  answer: AnswerAffordance;
}

export type ChangeSetStatus =
  | "open"
  | "publishing"
  | "published"
  | "conflicted"
  | "abandoned";

export type SessionSource = "canonical" | "fixture";
/**
 * 与后端 `effective_capabilities(api_names=True)` 的别名表一一对应
 * （app/services/sessions.py）。此前这里只列了前 5 个，而后端一直在发
 * `manage_resources` / `manage_settings` —— 名单抄了一份就会分叉，
 * 且分叉时两边都不报错：后端照发，前端类型上"不存在"，用到时才编译失败。
 */
export type ProjectSessionCapability =
  | "view"
  | "drive"
  | "publish"
  | "resolve"
  | "manage_members"
  | "manage_resources"
  | "manage_settings";
export type RevisionResourceType = "artifact" | "project_doc" | "project_config";
export type RevisionOperation = "create" | "update" | "delete";

export interface SessionPendingApprovalOption {
  /** 选项的**身份**。答复回传的是它，不是文案 —— 文案会变、会有两套。 */
  id?: string;
  label: string;
  description: string;
  recommended: boolean;
}

/**
 * 后端按事件序**现算**出来的待审批（见 sessions._pending_approval）——
 * run 一 resume 就变 null，前端不需要自己记得清掉它。
 */
export interface SessionPendingApproval {
  /** 这次呈递是哪一类 —— 后端说的，前端不猜。 */
  kind: "permission" | "decision" | "human_input";
  runId: string;
  reason: string;
  prompt: string | null;
  /** 逐字的待执行内容（命令、workdir、命中类别）。看不见内容的审批没有意义。 */
  context: string | null;
  options: string[];
  optionDetails: SessionPendingApprovalOption[];
  /** 这一次呈递的原样投影（harness Offer）。不透明：前端只把它交给归一化层。 */
  offer: Record<string, unknown> | null;
  recommendedOptionIndex: number | null;
  askingNodeType: string | null;
  pauseKind: string | null;
  askedAt: string | null;
}

export interface ResearchSession {
  id: string;
  projectId: string;
  title: string;
  summary: string | null;
  createdByUserId: string | null;
  primaryDriverUserId: string | null;
  lifecycleStatus: SessionLifecycleStatus;
  /** 版本 = git 提交（RFC X1）。从前这里是 project_revisions 的行 id 与自增号。 */
  baseCommitSha: string | null;
  headCommitSha: string | null;
  aheadBy: number;
  behindBy: number;
  gitBranch: string | null;
  gitBaseCommitSha: string | null;
  gitHeadCommitSha: string | null;
  modelBackendId: string | null;
  modelBackendName: string | null;
  createdAt: string;
  updatedAt: string;
  archivedAt: string | null;
  runCount: number;
  unpublishedChangeCount: number;
  /** 单份用户文件的上限（字节），后端给的。前端不写常量，见 SessionComposerBar。 */
  materialMaxBytes?: number;
  conflictCount: number;
  /**
   * 局面 + 答复入口。待答问题**在 `execution.answer.pause` 里**，不再是这里
   * 一个平级的 `pendingApproval` —— 那份抄件和 `canSend` 各走各的，
   * 2026-09-01 它们分叉了 6 小时。入口和入口里那张卡是同一个字段。
   */
  execution: SessionExecutionView;
  usage: RunUsage;
  /**
   * 调度器上一次请求把窗口占到了哪里（harness 自报的 `context.updated` 最新
   * 一条）。null = 这个会话还没发过请求。在飞的那一轮由事件流补更新的。
   */
  contextWindow: ContextWindowState | null;
  retryCount: number;
  driverLabel: string;
  creatorLabel: string;
  capabilities: ProjectSessionCapability[];
  source: SessionSource;
}

export interface SessionMessage {
  id: string;
  sessionId: string;
  sequence: number;
  actorUserId: string | null;
  role: "user" | "assistant" | "system";
  content: string;
  commandId: string | null;
  runId: string | null;
  /** 这条消息**就是**哪一次呈递。有值时，待答卡片渲染在它的位置上。 */
  offerId: string | null;
  createdAt: string;
}

export interface ProjectFileEntry {
  path: string;
  /** 这一层显示的名字（`path` 的最后一段）。 */
  name: string;
  kind: "file" | "directory";
  sizeBytes: number;
  owner: string;
  /** 平台记账（`.research/ledger/` `.history/` …），不是研究产出。后端判，界面不猜。 */
  bookkeeping: boolean;
  tracked: boolean;
  status: "committed" | "modified" | "untracked";
  /** 目录行：里面递归有多少文件 / 其中多少条还没提交。 */
  fileCount?: number;
  changedCount?: number;
  /** 用户交来的材料才有的几项。 */
  note?: string;
  sha256?: string;
  missing?: boolean;
}

/**
 * 工作区里**一层**的列举。
 *
 * 不是整棵树：整棵树一次给完的那版按字母序切前 5000 条，于是排在后面的整个
 * 目录（`paper/` 就是其中之一）会从界面上凭空消失，而响应里没有任何字段
 * 说少给了 —— 界面理直气壮地把"我只拿到这么多"显示成"一共就这么多"。
 */
export interface ProjectFileTree {
  schemaVersion: number;
  sessionId: string | null;
  /** 这次列举的是哪一层（`""` = 根）。 */
  path: string;
  entries: ProjectFileEntry[];
  /** 这一层真实有多少行 —— 到顶时它大于 `entries.length`。 */
  totalEntries: number;
  truncated: boolean;
  /** 整个工作区一共有多少文件。 */
  totalFiles: number;
}

export interface SessionChangeSet {
  projectId: string;
  sessionId: string;
  /** `git diff --name-only`：这一轮动过的仓库相对路径。 */
  changedPaths: string[];
  changeCount: number;
  conflictCount: number;
  gitBranch: string | null;
  gitBaseCommitSha: string | null;
  gitHeadCommitSha: string | null;
  aheadBy: number;
  behindBy: number;
  patch: string;
  additions: number;
  deletions: number;
  filesChanged: number;
  worktreeClean: boolean;
  patchTruncated: boolean;
}

export interface SessionChangeItem {
  id: string;
  resourceType: RevisionResourceType;
  resourceKey: string;
  operation: RevisionOperation;
  baseVersionId: string | null;
  proposedVersionId: string | null;
  proposedPreview: string | null;
  proposedPreviewTruncated: boolean;
  repositoryPath: string | null;
}

export interface SessionMergeConflict {
  id: string;
  resourceType: RevisionResourceType;
  resourceKey: string;
  baseVersionId: string | null;
  projectVersionId: string | null;
  proposedVersionId: string | null;
  status: "open" | "resolved";
  resolution: { choice?: "use_project" | "use_proposed" } | null;
  basePreview: string | null;
  theirsPreview: string | null;
  oursPreview: string | null;
  previewTruncated: {
    base: boolean;
    theirs: boolean;
    ours: boolean;
  };
}

export interface SessionCandidateInput {
  resourceType: RevisionResourceType;
  resourceKey: string;
  name: string;
  content: string;
  artifactType?: string;
  artifactId?: string | null;
  mimeType?: string;
  description?: string | null;
}

export interface SessionCandidate {
  artifactId: string;
  versionId: string;
  version: number;
  resourceKey: string;
  lifecycleStatus: string;
  checksum: string;
  sizeBytes: number;
  gitCommitSha: string | null;
  repositoryPath: string | null;
}

export type SessionGroup =
  | "running"
  | "needs_attention"
  | "unpublished"
  | "recent"
  | "archived";
