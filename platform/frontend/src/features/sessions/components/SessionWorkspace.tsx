"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Archive, Eraser, FileArchive, GitPullRequestArrow, Info, Square, Check, ChevronLeft, History, MoreHorizontal, Pencil, Route, Undo2 } from "lucide-react";
import {
  useAuth,
} from "@/features/auth";
import {
  CanonicalRunActivity,
  ChatPage,
  ChatRunActivity,
  HumanInputPrompt,
  messageRunSegments,
  useChat,
  type ChatMessage,
  type MessageRunSegment,
} from "@/features/chat";
import { useRunDetail, useRunEvents, useSessionRuns } from "@/features/execution";
import { useProject } from "@/features/projects";
import { pushError, pushInfo, pushSuccess } from "@/stores/notification";
import { api } from "@/lib/api";
import { isImeHandledKey } from "@/shared/keyboard";
import { qk } from "@/lib/query/keys";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  modelBackendDisplayName,
  modelBackendModelLabel,
} from "@/features/settings/lib/model-backend-presentation";
import {
  addFileToSession,
  fetchSessionDiagnostics,
  stopSessionTurn,
  resetSessionConversation,
  undoLastSessionWrite,
} from "../api/session-repository";
import { saveBlobAs } from "@/shared/save-file";
import { SessionComposerBar, type OperationMode }
  from "./SessionComposerBar";
import { modelSwitchNotice } from "../lib/model-switch-notice";
import { SessionAboutDrawer } from "@/features/instructions";
import { Skeleton } from "@/shared/ui";
import {
  useSession,
  useSessionChangeSet,
  useSessionConflicts,
  useSessionMessages,
  useSessionMutations,
  useSessionTerminalRefresh,
} from "../hooks/useSessions";
import { rememberProjectSession, type SessionDataMode } from "../api/session-repository";
import { inspectorRunIds } from "../lib/inspector-run-ids";
import { sessionUnpublishedCount } from "../lib/session-change-projection";
import {
  sessionHasActiveDriver,
  sessionHasAdvancedBase,
  sessionIsDrivenBy,
  sessionAboutFacts,
} from "../lib/session-presentation";
import type { ResearchSession } from "../types";
import { sessionInputPlan } from "../lib/answer-affordance";
import { latestContextWindow } from "../lib/context-window";
import { runFreshSessionPublish } from "../lib/publish-error-policy";
import { WorkspacePanelHost } from "@/features/file-preview/components/WorkspacePanelHost";
import { WorkspaceFileOpenerProvider } from "@/features/file-preview/components/WorkspaceFileOpener";
import { useWorkspacePanel } from "@/stores/workspace-panel";
import { SessionChangesPanel } from "./SessionChangesPanel";
import { SessionDeliverables } from "./SessionDeliverables";
import { SessionInspectorPanel } from "./SessionInspectorPanel";
import { composeText, type Answer } from "@/features/chat/lib/answer";
import {
  countLines,
  insertAtSelection,
  pastedFileName,
  pastedFileReference,
  pendingPasteToken,
  replaceToken,
} from "@/features/chat/lib/long-paste";
import type { SendDispatch } from "@/features/chat/hooks/useChat";
import { useT, useLanguage } from "@/shared/i18n";

/**
 * 一次提交在这个页面上的结果。`dispatch` 的每条分支都必须交出其中一种 ——
 * 返回类型非 void，"什么都没做就返回"是编译错误；不是 `sent` 的都当场说出来。
 */
type SubmitOutcome =
  | { kind: "sent"; dispatch: SendDispatch }
  /** 后端说这个人此刻不能对会话动手（只读 / 归档 / 非驾驶者）。 */
  | { kind: "locked" }
  /** 拿驾驶权失败（别人正在开车）。`acquireDriver` 自己已弹出原因。 */
  | { kind: "driver_unavailable" };

/**
 * 一条 run 的活动记录。**它不接答复回调** —— 能点的那张卡只有一处渲染，
 * 由 `sessionInputPlan` 决定（见 lib/answer-affordance.ts）。这里曾经也画
 * 一张可点的，于是需要一个 `hidePausePrompt` 把其中一张静音；两个渲染点、
 * 一个静音开关，就是 2026-09-01 那张卡整个消失的土壤。
 */
function SessionCanonicalRunAccessory({
  runId,
  enabled,
  submitting,
  showStreamingDraft = false,
  sequenceWindow,
  interjectionMessageId,
  turnReplyText,
  onOpenNodeDetail,
}: {
  runId: string;
  enabled: boolean;
  submitting: boolean;
  showStreamingDraft?: boolean;
  sequenceWindow?: { start: number; end: number | null };
  interjectionMessageId?: string;
  turnReplyText?: string;
  onOpenNodeDetail?: (target: { nodeType: string; stepId?: string }) => void;
}) {
  const runQuery = useRunDetail(runId, enabled);
  return (
    <CanonicalRunActivity
      detail={runQuery.data}
      loading={runQuery.isLoading}
      error={runQuery.error}
      submitting={submitting}
      showStreamingDraft={showStreamingDraft}
      sequenceWindow={sequenceWindow}
      interjectionMessageId={interjectionMessageId}
      turnReplyText={turnReplyText}
      onOpenNodeDetail={onOpenNodeDetail}
    />
  );
}

function sessionStateLabel(session: ResearchSession) {
  // 冲突是前端从 changeSet 才知道的事，压过局面标签；其余一律用后端给的
  // `label` —— 它与徽章、停止按钮、输入框出自同一次现算，不会互相打架。
  if (session.conflictCount > 0) return "Conflict";
  return session.execution.label;
}

function SessionTitle({
  session,
  editable,
  onRename,
}: {
  session: ResearchSession;
  editable: boolean;
  onRename: (title: string) => Promise<unknown>;
}) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(session.title);
  useEffect(() => setTitle(session.title), [session.title]);

  const save = async () => {
    const next = title.trim();
    if (!next || next === session.title) {
      setTitle(session.title);
      setEditing(false);
      return;
    }
    try {
      await onRename(next);
      setEditing(false);
    } catch {
      // Mutation feedback is shown by the shared notification layer; keep the draft editable.
    }
  };

  if (editing) {
    return (
      <span className="session-title-edit">
        <input
          value={title}
          autoFocus
          onChange={(event) => setTitle(event.target.value)}
          onKeyDown={(event) => {
            // 中文输入法里的回车是选词、Esc 是撤掉拼音，都还没轮到我们。
            if (isImeHandledKey(event.nativeEvent)) return;
            if (event.key === "Enter") void save();
            if (event.key === "Escape") { setTitle(session.title); setEditing(false); }
          }}
        />
        <button type="button" onClick={() => void save()} aria-label={t({ zh: "保存标题", en: "Save session title" })}><Check size={13} /></button>
      </span>
    );
  }

  return (
    <span className="session-title-display">
      <h1>{session.title}</h1>
      {editable && <button type="button" onClick={() => setEditing(true)} aria-label={t({ zh: "重命名会话", en: "Rename session" })}><Pencil size={11} /></button>}
    </span>
  );
}

/** 从 `?draft=` 预置的初稿最长多少 —— 地址栏塞不下一整篇论文。 */
const MAX_SEEDED_DRAFT = 2000;

/**
 * 把 `?draft=` 预置进输入框，**只做一次**。
 *
 * 资讯流的「接入课题」把一条内容交到这里：新建会话 → 带着这条的标题和链接
 * 落在输入框里，用户补一句自己想问什么就能发。没有它，那个按钮只能把人送到
 * 一个空会话，那篇论文什么都没跟过来 —— 一个说到做不到的按钮。
 *
 * ## 为什么用 `useSearchParams` 而不是 `window.location`
 *
 * 第一版是 `useState(() => new URLSearchParams(window.location.search)…)`。
 * 它在**刷新页面时正常**，但从资讯流点过来（`router.push` 的客户端跳转）时
 * 永远拿到空 —— App Router 在跳转过程中会先渲染新页面，那一刻
 * `window.location` 还停在上一个地址（也就是 /feed，它没有 draft 参数）。
 *
 * 两条路径一条对一条错，而错的那条恰恰是这个功能唯一的真实用法。
 * 单测和 typecheck 都看不见它：只有真点一次那个按钮才会发现输入框是空的。
 * `useSearchParams()` 是框架给的当前路由查询，跳转过程中就是对的。
 *
 * ## 为什么可以用 effect（第一版特意避开了它）
 *
 * 担心的是"用户已经开始打字，被这一下顶掉"。所以这里两道闸：`seeded` ref
 * 保证只写一次，以及只在输入框**还是空的**时候写。两者都成立时，不存在可
 * 被覆盖的用户输入。
 */
function useSeededDraft(setDraft: Dispatch<SetStateAction<string>>) {
  const searchParams = useSearchParams();
  const seeded = useRef(false);
  const raw = searchParams.get("draft");
  useEffect(() => {
    if (seeded.current || !raw) return;
    seeded.current = true;
    setDraft((current: string) => (current ? current : raw.slice(0, MAX_SEEDED_DRAFT)));
  }, [raw, setDraft]);
}

export function SessionWorkspace({
  projectId,
  sessionId,
  mode = "api",
}: {
  projectId: string;
  sessionId: string;
  mode?: SessionDataMode;
}) {
  const t = useT();
  const lang = useLanguage();
  const { user } = useAuth();
  const sessionQuery = useSession(projectId, sessionId, mode);
  const messageQuery = useSessionMessages(projectId, sessionId, mode);
  // A historical stuck Run can predate several newer terminal Runs. Read the
  // bounded server maximum so re-entry never mistakes "latest completed" for
  // "nothing is still active".
  const sessionRunsQuery = useSessionRuns(sessionId, mode === "api", 200);
  // 输入框上方那个"当前上下文 xx%"chip：会话读模型带着已知最新的一条；在飞
  // 的那一轮里 harness 每次响应都会再报，从这一轮的事件流里补更新的。事件流
  // 与 CanonicalRunActivity 共用同一个 query 缓存（同一个 key），这里不另起
  // 一条连接（follow=false）—— 它跟着那边的流一起更新。
  const liveView = sessionQuery.data?.execution;
  const liveRunId = liveView?.phase === "alive" ? (liveView.runId ?? "") : "";
  const liveEventsQuery = useRunEvents(sessionId, projectId, liveRunId, false, mode === "api" && !!liveRunId);
  const contextWindow = useMemo(
    () => latestContextWindow(sessionQuery.data?.contextWindow ?? null, liveEventsQuery.data),
    [sessionQuery.data?.contextWindow, liveEventsQuery.data],
  );
  const changeSetQuery = useSessionChangeSet(projectId, sessionId, mode === "api");
  const conflictsQuery = useSessionConflicts(projectId, sessionId, mode === "api");
  const projectQuery = useProject(mode === "api" ? projectId : undefined);
  const queryClient = useQueryClient();
  // UI 三档 ⇄ 存储两态。「连续」= 自主 + 预授权全部高危类别（`"*"`）。
  // 这一层翻译只在这里做一次；后端和运行时看到的仍是既有的两个概念。
  const projectConfig = (projectQuery.data?.config as Record<string, unknown> | null) ?? null;
  const storedMode = projectConfig?.operation_mode as
    | "assisted"
    | "autonomous"
    | undefined;
  const authorizedClasses = Array.isArray(projectConfig?.autonomous_authorized_risk_classes)
    ? (projectConfig.autonomous_authorized_risk_classes as string[])
    : [];
  const operationMode: OperationMode | null =
    storedMode === "autonomous"
      ? authorizedClasses.includes("*")
        ? "continuous"
        : "autonomous"
      : (storedMode ?? null);
  const modeMutation = useMutation({
    mutationFn: (next: OperationMode) =>
      api.updateProjectConfig(projectId, {
        operation_mode: next === "assisted" ? "assisted" : "autonomous",
        // 每次都显式写全，包括切回「自主」时把授权清空 —— 只写"想开的那一半"
        // 会让上一次的授权留在库里，于是 UI 显示「自主」而实际仍是全授权。
        autonomous_authorized_risk_classes: next === "continuous" ? ["*"] : [],
      }),
    onError: (error) =>
      pushError(error instanceof Error ? error.message : t({ zh: "档位没能切换", en: "The operation mode could not be switched" })),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: qk.project(projectId) }),
  });
  // 模型可换（PATCH …/sessions/{id}）。锁死会话挡不住配错的后端，只挡住修正：
  // 默认后端配错时，会话里每一轮都 401 且没有出口。归因改由每条 run 记录自己
  // 实际用的 modelBackendId 承担。
  const modelBackendsQuery = useQuery({
    // 用 qk 而不是手写 —— 设置页那边写的是 qk.modelBackends()
    // （`["settings","model-backends"]`）。手写成 `["model-backends"]` 就是
    // 另一个缓存条目：在设置页加完一条连接，这里的下拉永远看不到它。
    queryKey: qk.modelBackends(),
    queryFn: () => api.listModelBackends(),
    enabled: mode === "api",
    staleTime: 60_000,
  });
  /**
   * 「这一轮在不在飞」—— 换模型那句话要按它分档。
   *
   * 用 ref 而不是直接读变量：运行状态的派生（waitingForInput /
   * backgroundRunActive）在下面几百行处，而且在**提前 return 之后**，那里加
   * 不了 hook；这个 mutation 又必须待在提前 return 之前（hooks 顺序）。ref
   * 在每次 render 时被赋成当下的值，点下去那一刻读到的就是当下。
   */
  const turnInFlightRef = useRef(false);
  const modelMutation = useMutation({
    mutationFn: (backendId: string) =>
      api.updateSession(projectId, sessionId, { model_backend_id: backendId }),
    onSuccess: (_result, backendId) => {
      const chosen = (modelBackendsQuery.data ?? []).find((backend) => backend.id === backendId);
      /**
       * 立刻把 chip 改过来。
       *
       * 原来这里只发一个 invalidate，而且 key 写错了：手写的
       * `["session", …]`（单数）跟真正的查询键 `qk.session(…)` =
       * `["sessions", projectId, sessionId, mode]`（复数）对不上 —— 什么都
       * 没失效。于是换完模型 chip 纹丝不动，直到别的东西（发一条消息）顺手
       * 把 sessions 前缀刷了才跟上。wangd：「为啥点了还保持原来的，非得输入
       * 才换啊？」
       *
       * 两步都要：先按服务端刚接受的那个后端就地改（PATCH 已经 200 了，这
       * 不是猜），再 invalidate 让服务端那份权威值盖回来。
       */
      if (chosen) {
        queryClient.setQueryData<ResearchSession>(
          qk.session(projectId, sessionId, mode),
          (current) => current
            ? { ...current, modelBackendId: chosen.id, modelBackendName: modelBackendDisplayName(chosen) }
            : current,
        );
      }
      /**
       * 只有"这一轮仍用旧模型"这件事屏幕上看不出来，才值得弹一句。
       *
       * 空闲时 chip 已经**当场**变成新模型了（上面那次 setQueryData），再弹
       * 一条「已换成 X」就是拿 toast 复述屏幕。wangd 2026-08-20：「这种弹窗
       * 一点点用都没有。」
       */
      if (turnInFlightRef.current) {
        pushInfo(modelSwitchNotice({
          turnInFlight: true,
          modelLabel: chosen ? modelBackendDisplayName(chosen) : null,
        }));
      }
      void queryClient.invalidateQueries({ queryKey: qk.sessionsPrefix(projectId) });
    },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "换模型失败", en: "The model could not be switched" })),
  });
  // 会话附件（不是资料库上传）：文件进本会话工作区，附件行写进草稿 ——
  // 你看到什么，模型就看到什么，不走隐藏注入通道。
  /**
   * 运行中把话投给正在干活的 agent（不新起一轮）。
   *
   * 改造前这里是禁用输入框：agent 一开始跑，人就只剩"干等"或"Stop 掉重来"
   * 两个选择。CLI 一直可以随时插话（orchestrator 决定注入给子节点还是取消
   * 它），平台没有这条路 —— 这是两套会话驱动层里最影响使用方式的一条缺口。
   *
   * ⚠️ 这些 useMutation 必须待在**所有提前 return 之前**。第一版写在下面
   * （紧挨 send 的定义处），组件在 loading/error 分支提前返回时这个 hook 就
   * 不会被调用 —— React 立刻报 "change in the order of Hooks"。tsc 抓不到，
   * 浏览器一跑就现形。
   */
  const stopMutation = useMutation({
    mutationFn: () => stopSessionTurn(projectId, sessionId),
    onSuccess: () => {
      // 不弹提示：停止的结果**就在同一屏上** —— 按钮变回发送、状态徽标
      // 从「运行中」消失。再弹一句"已投递"是拿 toast 复述屏幕上的事。
    },
    onError: (error) => {
      pushError(error instanceof Error ? error.message : t({ zh: "停止请求未能投递", en: "The stop request could not be delivered" }));
    },
  });
  // CLI 里一直有、UI 上一直没有的三件：撤销上一次写 / 清对话 / 跳过 dreaming。
  // 三个都做成"要么真能操作，要么别放这个按钮"——`stoppableRunId` 那个注释里
  // 记着的教训：端点在、前端从没调过，等于没有。
  const undoMutation = useMutation({
    mutationFn: () => undoLastSessionWrite(projectId, sessionId),
    onSuccess: (result) => {
      pushSuccess(t({ zh: `已撤销「${result.revertedSubject}」（${result.changedPaths.length} 个文件）`, en: `Reverted "${result.revertedSubject}" (${result.changedPaths.length} files)` }));
      void queryClient.invalidateQueries({ queryKey: qk.sessionsPrefix(projectId) });
    },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "撤销失败", en: "The undo failed" })),
  });
  // 诊断包：会话出了问题时，说清原因要的记录都在磁盘深处（Windows 上还在隐藏的
  // AppData 里）。点一下就拿到一个 zip，能直接发给帮忙的人。
  const diagnosticsMutation = useMutation({
    mutationFn: () => fetchSessionDiagnostics(projectId, sessionId, lang),
    onSuccess: ({ blob, filename }) => {
      saveBlobAs(blob, filename);
      pushSuccess(t({
        zh: "诊断包已生成。里面有这个会话的完整对话，发给别人之前请确认可以分享。",
        en: "Diagnostics ready. They include this session's full conversation; check before sharing.",
      }));
    },
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "诊断包没能生成", en: "The diagnostics could not be prepared" })),
  });
  const resetMutation = useMutation({
    mutationFn: () => resetSessionConversation(projectId, sessionId),
    onSuccess: (result) => pushSuccess(
      result.status === "no_live_runtime"
        ? t({ zh: "这个 Session 当前没有活着的运行时，下次打开就是干净的对话", en: "This session has no live runtime right now; next time you open it the conversation starts clean" })
        : t({ zh: "对话已重置（memory / KB / 产物都保留）", en: "Conversation reset (memory, KB and outputs are all kept)" }),
    ),
    onError: (error) => pushError(error instanceof Error ? error.message : t({ zh: "重置失败", en: "The reset failed" })),
  });
  // 「跳过本次 KB 整理」不在这里了 —— 它调的是 `skipProjectDreaming(projectId)`，
  // 是**项目级**动作，挂在会话菜单里意味着从任何一个会话点它都会影响别的会话。
  // 现在在项目设置的「知识库整理」里（ProjectDreamingPanel）。
  // 交一份文件进这个会话的工作区。**只有一个入口、一个落点** —— 老实现在这里
  // 有两个（会话附件 + 一键晋升成项目材料），落在两个地方，各覆盖一半场景。
  //
  // 成功后**不动用户的草稿**。老实现往草稿开头塞一行「📎 附件：路径」当作
  // 告知模型的通道：用户一删就没人知道这文件存在过。现在模型每轮由框架把
  // 「用户交来了哪些文件」当机械事实拿到（user_files hook），界面这边只需要
  // 让人知道东西上去了、在哪儿。
  const addFileMutation = useMutation({
    mutationFn: (file: File) => addFileToSession(projectId, sessionId, file),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: qk.projectFileTree(projectId) });
      pushSuccess(
        result.committed
          ? t({ zh: `「${result.name}」已在工作区 ${result.path} —— agent 下一轮就能读它`, en: `"${result.name}" is in the workspace at ${result.path} — the agent can read it next turn` })
          : t({ zh: `「${result.name}」已经在工作区里了（内容相同，没有重复入库）`, en: `"${result.name}" is already in the workspace (same content, not stored twice)` }),
      );
    },
    // 失败必须吵。这里原先只有 onSuccess —— 2026-09-04 同事传一个 800MB 的包，
    // 后端 413 拒了，界面一声不响，于是他以为传上去了，agent 找了半天。
    onError: (error) =>
      pushError(error instanceof Error ? error.message : t({ zh: "文件没能交上去", en: "The file could not be handed over" })),
  });
  const mutations = useSessionMutations(projectId);
  const refreshAfterTerminal = useSessionTerminalRefresh(projectId, sessionId);
  const session = sessionQuery.data;
  const [draft, setDraft] = useState("");
  useSeededDraft(setDraft);
  // 长粘贴 → 工作区文件（见 chat/lib/long-paste）。和「添加文件」走同一条
  // 上传通道；输入框里先占一行「上传中…」（占着时发不出去），回来后换成
  // 文件路径。失败就把原文放回原处 —— 卡一点也比让人贴的东西凭空消失强。
  const pasteSeq = useRef(0);
  const pasteAsFile = (text: string, selection: { start: number; end: number }) => {
    const seq = (pasteSeq.current += 1);
    const lines = countLines(text);
    const file = new File([text], pastedFileName(new Date(), seq), { type: "text/plain" });
    const maxBytes = session?.materialMaxBytes;
    if (maxBytes && file.size > maxBytes) {
      pushError(t({
        zh: `贴进来的文本有 ${Math.round(file.size / 1024 / 1024)} MB，超过单份文件上限，没有贴进去。请把它存成文件后按数据集的方式交给项目。`,
        en: `The pasted text is ${Math.round(file.size / 1024 / 1024)} MB, over the per-file limit, so it was not pasted. Save it as a file and hand it to the project as a dataset instead.`,
      }));
      return;
    }
    const token = pendingPasteToken(seq, lines, lang);
    setDraft((current) => insertAtSelection(current, selection.start, selection.end, token));
    addFileToSession(
      projectId,
      sessionId,
      file,
      t({ zh: "用户在输入框里粘贴的长文本，界面自动存成了文件", en: "Long text the user pasted into the message box, saved as a file automatically" }),
      lang,
    ).then(
      (result) => {
        void queryClient.invalidateQueries({ queryKey: qk.projectFileTree(projectId) });
        setDraft((current) => replaceToken(current, token, pastedFileReference(result.path, lines, lang)));
      },
      (error) => {
        setDraft((current) => replaceToken(current, token, text));
        pushError(t({
          zh: `长文本没能存成文件，已原样放回输入框：${error instanceof Error ? error.message : String(error)}`,
          en: `The long text could not be saved as a file, so it was put back into the message box: ${error instanceof Error ? error.message : String(error)}`,
        }));
      },
    );
  };
  // 右栏的开合与「显示哪个 Tab」归 store —— 打开一个文件这件事发起自另一条
  // 路由（Project files），会话页的 useState 那边够不着。见 stores/workspace-panel。
  // 宽度/拖拽/持久化随之搬进 WorkspacePanelHost。
  const inspectorOpen = useWorkspacePanel((state) => state.open);
  const toggleInspector = useWorkspacePanel((state) => state.toggle);
  const openResearchTab = useWorkspacePanel((state) => state.openResearch);
  const openPanelFile = useWorkspacePanel((state) => state.openFile);
  const [aboutOpen, setAboutOpen] = useState(false);
  const [inspectorFocus, setInspectorFocus] = useState<{ nodeType: string; nonce: number } | null>(null);
  // 打开右栏并定位 —— 每一处渲染节点卡的地方都用这一个，别各写一遍
  // （漏传的那一处就是死卡片）。
  // 定位到**这一次**派发，不是最后一张同类卡（见 SessionInspectorPanel
  // 的 `scrollToRun`）。stepId 缺席时退回按节点类型 —— 地图上点车站就是这种。
  const openNodeDetail = (target: { nodeType: string; stepId?: string }) => {
    openResearchTab();
    setInspectorFocus({ ...target, nonce: Date.now() });
  };
  // 会话里点一个产出文件（"Edited fig1.png"）→ 右栏打开它。会话自己的
  // worktree 就是这些路径的来源，所以 sessionId 直接给当前这个。
  const openWorkspaceFile = (path: string) =>
    openPanelFile({ projectId, sessionId, path });
  // 「N files changed」汇总面板不常驻对话流（2026-08-17 用户：每次改了啥
  // 已经有内联卡原位记录，改完接着输出就行，别搞一块全量统计占地方）。
  // 只在点 composer 的 changes chip 时展开；有待解决冲突时强制显示。
  const [changesOpen, setChangesOpen] = useState(false);
  const loadedRef = useRef(false);
  const hydratedMessagesRef = useRef(messageQuery.data);
  // 这里曾有第二个手工维护的"在飞"标志 `answeringRef`：只在 sendText 返回 false
  // 或 chat.sending 翻面时复位。hook 静默吞掉一次提交时两件事都不发生，闩就
  // 永远关着 —— 之后补写附言再点也被吞，刷新页面才解（2026-09-03，cuib）。
  // 它和 8-18 那个 `busy` 布尔同形（靠"每条路都记得清"活着）。删。`chat.sending`
  // 是唯一的在飞判据，卡片在 sending 时本来就不渲染（sessionInputPlan）。

  useEffect(() => {
    rememberProjectSession(projectId, sessionId);
  }, [projectId, sessionId]);

  const initialMessages = useMemo<ChatMessage[]>(() => {
    if (!messageQuery.data?.length) return [];
    return messageQuery.data.map((message) => ({
      id: message.id,
      role: message.role,
      text: message.content,
      runId: message.runId,
      // 这条消息是不是某一次呈递的持久形态。是的话，能点的那张卡片就画在它
      // 的位置上（见 pauseAnchorMessageId）。
      offerId: message.offerId,
      // 会话级 sequence —— 与执行事件**同一个发生器**（后端
      // `sessions.next_sequence`），所以时间穿插是库里的事实，不靠猜。
      sequence: message.sequence,
    }));
  }, [messageQuery.data]);

  const chat = useChat({
    scope: { kind: "project", projectId },
    conversationId: sessionId,
    initialMessages,
    onStreamDone: mode === "api" ? refreshAfterTerminal : undefined,
  });
  // 「别处发起、这个标签页没在流式跟的那一轮」—— 由后端的 view 指名，不再
  // 从 /runs 快照里自己挑。那份快照是进页面时拍的，新会话第一轮跑着的时候
  // 没人刷新它，只靠它 header 会误报「Ready」（见 inspector-run-ids 的注释）。
  const recoveredLiveRunId = !chat.sending && session?.execution.phase === "alive"
    ? session.execution.runId
    : null;
  // 检查器读 owning run —— 来源以消息流为权威（runs 列表是进页面时的快照，
  // 新会话第一轮跑着的时候没人刷新它，只靠它右栏就是空的）。见 inspectorRunIds。
  const inspectorRootRunIds = useMemo(
    () => inspectorRunIds(chat.messages, chat.terminal.runId, sessionRunsQuery.data?.items),
    [chat.messages, chat.terminal.runId, sessionRunsQuery.data],
  );
  // owning run 的状态表 —— 右栏用它判"被打断"还是"真在跑"（进程被杀的 run
  // 没有任何终态事件，只看事件流会永远转圈）。
  // 「哪些 owning run 没走到终点就断了」—— 一个集合，由后端的 view 回答。
  // 以前这里传的是一整张状态表，消费方还得自己知道哪些状态算"被打断"。
  const interruptedRunIds = useMemo(() => new Set(
    (sessionRunsQuery.data?.items ?? [])
      .filter((run) => run.view.phase === "interrupted"
        || run.view.outcome === "failed" || run.view.outcome === "cancelled")
      .map((run) => run.id),
  ), [sessionRunsQuery.data]);
  // 回复回声去重：每条 assistant 消息的正文，供它窗口下的时间线滤掉同文叙述。
  const replyTextByRun = useMemo(() => {
    const map = new Map<string, string>();
    for (const message of chat.messages) {
      if (message.role === "assistant" && message.runId?.trim() && message.text.trim()) {
        map.set(message.runId, message.text);
      }
    }
    return map;
  }, [chat.messages]);
  const resetChat = chat.reset;
  const runSegmentsByMessage = useMemo(() => {
    // 时间穿插归属：一条消息名下渲染它的 sequence 窗口内发生的活动段，
    // 不再把整个 run 压到最后一条消息下面（见 messageRunSegments 的说明）。
    const segments = messageRunSegments(chat.messages);
    const terminalRunId = chat.terminal.runId;
    if (
      terminalRunId
      && !chat.sending
      && !segments.some((segment) => segment.runId === terminalRunId)
    ) {
      // 这一轮的 run 还没被任何持久消息认领（消息刷新与 runId 回写有竞态）：
      // 兜底挂在最后一条消息的开放尾窗上。
      const lastMessage = chat.messages.at(-1);
      if (lastMessage) {
        const maxSequence = chat.messages.reduce(
          (max, m) => (typeof m.sequence === "number" && m.sequence > max ? m.sequence : max),
          Number.NEGATIVE_INFINITY,
        );
        segments.push({
          messageId: lastMessage.id,
          runId: terminalRunId,
          window: { start: maxSequence, end: null },
          interjection: false,
        });
      }
    }
    const byMessage = new Map<string, MessageRunSegment[]>();
    for (const segment of segments) {
      byMessage.set(segment.messageId, [...(byMessage.get(segment.messageId) ?? []), segment]);
    }
    return byMessage;
  }, [chat.messages, chat.sending, chat.terminal.runId]);
  const recoveredRunNeedsStandalone = Boolean(
    recoveredLiveRunId && !chat.messages.some((message) => message.runId === recoveredLiveRunId),
  );
  // 判据读**局面**，不读状态词：`terminal.status` 那一版是前端最后一处手写的
  // run 状态名单，而名单必然漏新写法（[[护栏要扫盘不要写名单]]）。
  const showTransientRun = chat.sending
    || chat.terminal.view?.waitingOn != null;
  const transientRunHasCanonicalOwner = Boolean(
    chat.messages.at(-1)?.runId,
  );
  // 「在等人回答什么」以**会话**为准，不以这次流式连接为准。
  //
  // 2026-08-10 实测：一轮无人值守 E2E 撞上 submit_job 的高危审批，静默挂了两
  // 小时。问题、选项、连逐字的待执行命令都完整躺在 execution_events 表里，
  // 但审批 UI 只挂在 `showTransientRun && !transientRunHasCanonicalOwner` 上
  // —— 也就是**只要这个 run 在库里登记过就永不显示**，而真实科研 run 全都
  // 登记过。于是"等人"这个状态，人这一侧永远看不见问题。
  //
  // canonical 优先、transient 兜底：后者只覆盖 run 还没登记的那几百毫秒。
  /**
   * 「输入交给谁」—— **一个纯函数算一次**，下面所有渲染点照着它走。
   *
   * 这里曾经是三份各自演化的东西：`canonicalPause`（会话那份）、
   * `chat.terminal.pause`（流式那份）、以及一句 `livePause = A ?? B` 决定谁赢；
   * 再加上组件自己那道 `status === "waiting_human"` 名单。四个判据，
   * 2026-09-01 分叉了 6 小时 25 分。
   */
  const inputPlan = useMemo(
    () => (session
      ? sessionInputPlan({ session, messages: chat.messages, sending: chat.sending })
      : null),
    [session, chat.messages, chat.sending],
  );

  // 卡片画在哪条消息下面，由 `sessionInputPlan` 一并算出（anchorMessageId）——
  // 它和"画不画"是同一个判断的两面，分开算就会出现"藏了消息、没出现卡片"。
  const pauseAnchorMessageId = inputPlan?.kind === "prompt" ? inputPlan.anchorMessageId : null;

  useEffect(() => {
    // A persisted-message refresh can race the active POST observer. Never let
    // hydration call chat.reset(), which owns abortRef, until that observer has
    // emitted its own terminal frame.
    if (!messageQuery.data || chat.sending) return;
    if (loadedRef.current && hydratedMessagesRef.current === messageQuery.data) return;
    resetChat(initialMessages, !loadedRef.current);
    hydratedMessagesRef.current = messageQuery.data;
    loadedRef.current = true;
  }, [chat.sending, initialMessages, messageQuery.data, resetChat]);

  if ((sessionQuery.isLoading && !session) || (messageQuery.isLoading && !messageQuery.data)) {
    return (
      <div className="session-workspace-loading">
        <Skeleton height={70} rounded="sm" />
        <div><Skeleton height={18} rounded="sm" /><Skeleton height={110} rounded="sm" /></div>
      </div>
    );
  }

  if (sessionQuery.isError || messageQuery.isError || !session) {
    const problem = sessionQuery.error ?? messageQuery.error;
    return (
      <div className="session-workspace-error" role="alert">
        <span>{t({ zh: "打不开这条会话", en: "Session unavailable" })}</span>
        <h1>{t({ zh: "这份研究记录读不出来。", en: "This research record could not be opened." })}</h1>
        <p>{problem instanceof Error ? problem.message : t({ zh: "平台没有返回一条能用的会话。", en: "The platform did not return a valid Session." })}</p>
        <Link href={`/projects/${encodeURIComponent(projectId)}/research`}><ChevronLeft size={13} />{t({ zh: "回到研究列表", en: "Back to research" })}</Link>
      </div>
    );
  }

  const canDrive = session.capabilities.includes("drive");
  const activeDriver = sessionHasActiveDriver(session);
  const isDriver = sessionIsDrivenBy(session, user?.id);
  const isArchived = session.lifecycleStatus === "archived";
  /**
   * 「这个人能不能对这个会话动手」—— **只有后端答**（`answer.via !== "none"`）。
   *
   * 此前这里再算一遍驾驶权租约，而后端同时也在算能力与归档。两半的交集才是
   * 真的"能不能"，却没有任何一层持有那个交集 —— `canSend=true` 和一个灰着的
   * 输入框可以同时成立。fixture 模式没有后端，只读。
   */
  const canCompose = mode === "api" && inputPlan?.kind !== "locked";
  // 会话局面由 payload 直接给成品，不再依赖 /runs 列表 —— 于是「这份列表可
  // 信吗」这个问题连同它那块错误横幅一起消失了。
  /**
   * ── 局面只有一个答案 ────────────────────────────────────────────────────
   *
   * 这里曾经是**五个各自不同输入的布尔**：会话状态、恢复出来的 run 的状态、
   * 这次流式连接的终态，三个来源两两组合。它们能互相矛盾，而且确实矛盾过 ——
   * 2026-08-27 现场「Queued 徽章 + 转圈 + 一个按下去回 409 的停止按钮」就是
   * 三个布尔各答各的。
   *
   * 现在只问后端那一次现算（`session.execution`）。本地只补一件后端不知道的
   * 事：这个标签页此刻正在发送（`chat.sending`）。
   */
  const view = session.execution;
  const waitingForInput = view.waitingOn?.kind === "human"
    || view.waitingOn?.kind === "permission";
  const backgroundRunActive = Boolean(
    !chat.sending && !waitingForInput && view.phase === "alive",
  );
  /**
   * 「这一轮在跑」—— 不分是这个标签页发起的（chat.sending）还是别处发起的
   * （backgroundRunActive）。
   *
   * 状态徽标原来只认后者，于是自己刚发出去的那一轮跑着的时候，徽标显示
   * 「就绪」—— 旁边的按钮正在转圈。同一块屏幕上两个互相矛盾的答案。
   */
  const turnRunning = chat.sending || backgroundRunActive;
  // 换模型那句话读它（见 turnInFlightRef）。三个来源合成一个问题的答案：
  // 正在发 / 正在跑 / 停在等人处，都算"这一轮在飞"。
  turnInFlightRef.current = waitingForInput || backgroundRunActive || chat.sending;
  // 只有"确实还能停"的 run 才给按钮。判据不是"看起来像在跑"，而是后端用
  // **`/stop` 自己那个谓词**算出来的 `canStop` —— 亮着就一定停得掉。
  const stoppableRunId = view.canStop ? (view.runId ?? chat.terminal.runId ?? null) : null;
  /**
   * 「这个人能不能对这个会话动手」—— 已经在开车的，和**能接管**的，都算。
   *
   * 此前这里是 `isDriver`，另外单给一个「Take control」按钮去拿驾驶权。可是
   * 发消息这条路早就是"打字即接管"（`sendText` 里那段 acquireDriver）——
   * 于是同一件事有两套规矩：输入框自己会接管，菜单里的操作却要你先去按另一个
   * 按钮，不按就只能看着一排灰的。Take control 按钮删掉之后，这条路必须补上，
   * 不然那些操作就真的没有入口了。
   */
  // 「这个人能不能对这个会话动手」与输入框是**同一个**答案。此前这里另算
  // 一遍（isDriver || canAcquire），于是同一件事有两套规矩：输入框自己会
  // 接管，菜单里的操作却要先按另一个按钮。
  const canOperate = mode === "api" && session.source === "canonical" && canCompose;
  const changeSet = changeSetQuery.data;
  const openConflicts = (conflictsQuery.data ?? []).filter((conflict) => conflict.status === "open");
  const canPublish = mode === "api"
    && session.capabilities.includes("publish")
    && !!changeSet
    && changeSet.filesChanged > 0
    && changeSet.worktreeClean
    && openConflicts.length === 0;
  const canResolve = mode === "api" && session.capabilities.includes("resolve") && openConflicts.length > 0;
  const projectAdvanced = sessionHasAdvancedBase(session);
  const disabledReason = mode === "fixture"
    ? t({ zh: "界面演示只能看。", en: "UI demonstration is read-only." })
    : isArchived
      ? t({ zh: "归档的会话只能看，不能续。", en: "Archived sessions are read-only." })
      : !canDrive
        ? t({ zh: "你能看这个会话，但不能往里发研究指令。", en: "You can view this session, but cannot append research commands." })
        : !isDriver
          ? activeDriver
            ? `${session.driverLabel} is the current driver. This Session is view-only until they release control or the lease expires.`
            : undefined
          : undefined;

  /**
   * 从前这里先去拿驾驶权再执行 —— 驾驶权租约 2026-09-05 删了，「谁在开」现在
   * 是后端现算的（最近一条用户消息的作者），没有可拿的东西。留下这层薄壳只
   * 是为了不动它十几个调用点的形状。
   */
  const withDriver = async <T,>(action: () => Promise<T> | T): Promise<T> => action();

  /**
   * **一次提交只走这一条路** —— 输入框和待答卡片都到这里。
   *
   * 收的是构造好的 `Answer`（answer.ts 是整个前端唯一判断"有没有东西可发"
   * 的地方），所以这里没有空文本判断可写。此前这里是 `sendText` + `send` +
   * `answerHuman` 三层薄包装，内容全是各自的守卫和一个手工闩（answeringRef）；
   * 8-31 改了两层守卫、hook 那层原样 —— 只点选项不写附言时一次点击在四层里
   * 静默消失（2026-09-03，cuib）。
   *
   * 返回类型非 void：每条分支都要交出一个结果，裸 `return;` 是编译错误；
   * 不是 `sent` 的结果都当场说出来，没有"点了没反应"这一种。
   */
  const dispatch = async (answer: Answer): Promise<SubmitOutcome> => {
    if (!canCompose) return { kind: "locked" };
    // 「先拿驾驶权」这一步随租约一起删了（2026-09-05）：没有可拿的东西了。
    // `driver_unavailable` 这个结果因此不再产生 —— 它留在 SubmitOutcome 里
    // 只是为了让别的调用点不必同时改形状。
    // 单一路径（2026-08-17 定案）：忙不忙、这句话该开新轮还是进正在跑的
    // 收件箱，是**后端**按活体进程机械分流的事，客户端不猜。分流结果由
    // done 帧的 routed 字段回告，useChat 负责呈现。
    return { kind: "sent", dispatch: chat.send(answer) };
  };

  const send = async () => {
    // 输入框在等人回答时本来就灰着（sessionInputPlan）；发送键在草稿为空时
    // 也灰着（isComposerSendDisabled）。这两句只是把"够不着"变成"响"。
    // 这里问的是"构造函数造出来了没有"，不是自己再看一眼字符串。
    const composed = composeText(draft);
    if (composed === null) {
      pushError(t({ zh: "没有可发送的内容。", en: "There is nothing to send." }), t({ zh: "未发送", en: "Not sent" }));
      return;
    }
    const outcome = await dispatch(composed);
    if (outcome.kind === "sent") setDraft("");
    else if (outcome.kind === "locked") pushError(t({ zh: "这个会话此刻不接受输入。", en: "This session is not accepting input right now." }), t({ zh: "未发送", en: "Not sent" }));
  };

  const answerHuman = async (answer: Answer) => {
    const outcome = await dispatch(answer);
    if (outcome.kind === "locked") pushError(t({ zh: "这个会话此刻不接受输入。", en: "This session is not accepting input right now." }), t({ zh: "未发送", en: "Not sent" }));
  };

  /**
   * **能点的那张卡，全页面只在这里构造一次。**
   *
   * 它只由 `inputPlan.kind === "prompt"` 产生 —— 也就是后端刚刚回答过
   * 「入口就是这张卡」。没有 `resumable`、没有可选的 `onAnswer`：
   * "看起来能点其实点不了"这个状态构造不出来。
   *
   * 挂在哪儿（锚定在提问那条消息下面，还是列表底部）是**位置**问题，
   * 由 `pauseAnchorMessageId` 决定；画不画是**入口**问题，由 plan 决定。
   * 这两件事此前搅在一起，各写一遍，其中一遍是坏的。
   */
  const answerPrompt = inputPlan?.kind === "prompt" ? (
    <HumanInputPrompt
      key={`pause-${inputPlan.pause.offerId ?? inputPlan.anchorMessageId ?? "tail"}`}
      pause={inputPlan.pause}
      submitting={false}
      onAnswer={answerHuman}
    />
  ) : null;

  return (
    <WorkspaceFileOpenerProvider
      projectId={projectId}
      sessionId={sessionId}
      // 与右栏同一个开关 —— 两处取值必须一致，见 provider 的 enabled 注释。
      enabled={mode === "api"}
      onOpenFile={openWorkspaceFile}
    >
    <main className="session-workspace">
      <header className="session-workspace-header">
        {/* 顶栏只留"这是哪个会话、它在干什么"。版本号 / 基线 / 驾驶者 /
            指令快照三行字都搬进了 ⋯ 里的「会话信息」—— 那些是要查的时候才
            查的东西，不是每一眼都要读的东西（wangd 2026-08-18）。 */}
        <div className="session-header-primary">
          <Link href={`/projects/${encodeURIComponent(projectId)}/research${mode === "fixture" ? "?mode=demo" : ""}`} aria-label={t({ zh: "回到研究列表", en: "Back to research" })}><ChevronLeft size={14} /></Link>
          <SessionTitle
            session={session}
            editable={canOperate}
            onRename={(title) => withDriver(() => mutations.rename.mutateAsync({ sessionId, title }))}
          />
          <span className={`session-state session-meta-state state-${view.phase}`}><i /> {sessionStateLabel(session)}</span>
        </div>
        <div className="session-header-actions">
          {mode === "api" && (
            <button
              type="button"
              className={`session-inspector-toggle${inspectorOpen ? " is-active" : ""}`}
              aria-pressed={inspectorOpen}
              onClick={toggleInspector}
            >
              <Route size={13} />{t({ zh: "研究进程", en: "Research progress" })}</button>
          )}
          <details>
            {/* 项目往前走了要有人知道，但它不值一个常驻按钮：⋯ 上一个点，
                菜单里那一项自己说清楚是什么。 */}
            <summary
              aria-label={t({ zh: "会话操作", en: "Session actions" })}
              className={projectAdvanced ? "has-badge" : undefined}
            ><MoreHorizontal size={15} /></summary>
            <div>
              <Link href={`?${mode === "fixture" ? "mode=demo&" : ""}view=execution`}><History size={13} />{t({ zh: "执行历史", en: "Execution history" })}</Link>
              <button type="button" onClick={() => setAboutOpen(true)}>
                <Info size={13} />{t({ zh: "会话信息", en: "Session details" })}</button>
              {/* 与后端同一个条件（drive_session）：包里是整棵原始运行目录，只读成员
                  看得到会话、拿不走它 —— 按钮也就不给他看，免得点了只换来一句权限错误。 */}
              {mode === "api" && session.source === "canonical" && canDrive && (
                <button
                  type="button"
                  disabled={diagnosticsMutation.isPending}
                  onClick={() => diagnosticsMutation.mutate()}
                  title={t({ zh: "全部运行记录和同时段的后端日志，打成一个 zip；密钥已抹掉", en: "Every run record and the backend log from the same period, in one zip; keys removed" })}
                >
                  <FileArchive size={13} />
                  {diagnosticsMutation.isPending
                    ? t({ zh: "正在打包…", en: "Packing…" })
                    : t({ zh: "下载诊断包", en: "Download diagnostics" })}
                </button>
              )}
              {/* 「项目往前走了」值得说一声，而能对它做点什么的地方就在本页的
                  改动面板里。从前这里链到 /projects/<id>/activity —— 那一页只是
                  把 runs 又列了一遍，看完还得回来。 */}
              {projectAdvanced && (
                <button
                  type="button"
                  className="session-review-updates"
                  onClick={() => setChangesOpen(true)}
                >
                  <GitPullRequestArrow size={13} />{t({ zh: "看改动", en: "See changes" })}<i />
                </button>
              )}
              {/* 后端的 POST /runs/{id}/cancel 一直都在，前端从未调过 —— 研究员
                  看着 agent 往错误方向跑（E2E v18：冻结协议里有 1000x 量纲错误）
                  除了等它跑完或关浏览器什么都做不了。只在真有可停的 run 时出现。*/}
              {canOperate && stoppableRunId && (
                <button
                  type="button"
                  className="session-menu-sectioned"
                  disabled={stopMutation.isPending}
                  onClick={() => void withDriver(() => stopMutation.mutate()).catch(() => {})}
                >
                  <Square size={13} />{t({ zh: "停止这一轮", en: "Stop this run" })}</button>
              )}
              {canOperate && (
                <button
                  type="button"
                  className={stoppableRunId ? undefined : "session-menu-sectioned"}
                  disabled={undoMutation.isPending}
                  onClick={() => void withDriver(() => undoMutation.mutate()).catch(() => {})}
                  title={t({ zh: "用 Git revert 撤销，历史留痕；冻结产物不许回滚", en: "Undone with git revert so history keeps the trace; frozen outputs cannot be rolled back" })}
                ><Undo2 size={13} />{t({ zh: "撤销上一次写", en: "Undo the last write" })}</button>
              )}
              {canOperate && (
                <button
                  type="button"
                  disabled={resetMutation.isPending || backgroundRunActive}
                  onClick={() => void withDriver(() => resetMutation.mutate()).catch(() => {})}
                  title={backgroundRunActive ? t({ zh: "跑轮中不能重置对话", en: "The conversation cannot be reset while a turn is running" }) : t({ zh: "memory / KB / 产物都保留", en: "memory, KB and outputs are all kept" })}
                ><Eraser size={13} />{t({ zh: "清空对话历史", en: "Clear the conversation" })}</button>
              )}
              {/* 收起不需要持有 driver：后端 archive 只要 drive_session 能力。
                  前端原来跟着 canEdit（= 你是当前 driver）走，于是"没有活跃
                  driver"的会话在界面上什么都做不了。 */}
              {mode === "api" && session.source === "canonical" && !isArchived && (
                <button
                  type="button"
                  className="session-menu-sectioned"
                  onClick={() => mutations.archive.mutate(sessionId)}
                >
                  <Archive size={13} />{t({ zh: "归档会话", en: "Archive session" })}</button>
              )}
            </div>
          </details>
        </div>
      </header>

      <SessionAboutDrawer
        projectId={projectId}
        sessionId={sessionId}
        enabled={mode === "api"}
        open={aboutOpen}
        onClose={() => setAboutOpen(false)}
        facts={sessionAboutFacts(session)}
      />

      {mode === "fixture" && <p className="session-fixture-provenance">{t({ zh: "界面演示数据 · 不是真实的项目历史", en: "UI demonstration data · not canonical project history" })}</p>}

      <WorkspacePanelHost
        projectId={projectId}
        enabled={mode === "api"}
        mainClassName="session-document-canvas"
        researchTab={{
          render: () => (
            <SessionInspectorPanel
              sessionId={sessionId}
              rootRunIds={inspectorRootRunIds}
              interruptedRunIds={interruptedRunIds}
              focus={inspectorFocus}
            />
          ),
        }}
      >
        <ChatPage
          messages={chat.messages}
          draft={draft}
          setDraft={setDraft}
          onSend={() => void send()}
          sending={chat.sending || false}
          greeting={t({ zh: "这条会话要达成什么？", en: "What should this research session accomplish?" })}
          emptyDescription={turnRunning
            ? t({ zh: "这个会话正在后台继续跑，记录下来的活动会自动更新到这里。", en: "This Session is continuing in the background. Recorded activity will update here automatically." })
            : undefined}
          placeholder={turnRunning
            ? t({ zh: "它正在跑 —— 在这里插话（调整方向 / 问进度 / 让它停下某个子任务）", en: "It is running — interject here to steer it, ask about progress, or stop one sub-task" })
            : t({ zh: "说下一步做什么，或者问它一个问题", en: "Describe the next research task or ask a question" })}
          error={chat.error}
          centerEmpty={false}
          onStop={turnRunning && canCompose
            ? () => stopMutation.mutate()
            : undefined}
          stopping={stopMutation.isPending}
          // 后端一直支持中途插话（同一入口机械分流成 routed=interject）。
          // 这条路以前只有换个标签页/刷新之后才走得通 —— 发起那一轮的页面
          // 自己被 sending 锁着。接上。
          canInterject={mode === "api" && canCompose}
          onLongPaste={mode === "api" && canCompose ? pasteAsFile : undefined}
          // 输入框开不开、为什么不开：同一个 plan，一个答案。
          composerDisabled={inputPlan?.kind !== "composer"}
          composerDisabledReason={
            inputPlan?.kind === "locked"
              ? inputPlan.reason
              : inputPlan?.kind === "prompt"
                ? t({ zh: "先回答上面那个问题，这一轮才能继续。", en: "Answer the paused question above to continue this Run." })
                : undefined
          }
          bodyOwnedByAccessory={(message) => message.id === pauseAnchorMessageId}
          renderMessageAccessory={(message) => {
            // 卡片只有**一个**渲染表达式（下面 `answerPrompt`），锚定与兜底
            // 位置共用它。此前是两处各写一遍：一处把 waitingOn 翻译对了，
            // 另一处把 `phase` 当状态传了进去 —— 而只有"呈递没有身份"时才会
            // 走到后者，于是缺陷藏了 5 天，直到一次 request_human_input 撞上。
            const pausePrompt = message.id === pauseAnchorMessageId ? answerPrompt : null;
            const segments = runSegmentsByMessage.get(message.id);
            if (!segments?.length) return pausePrompt;
            // 插话消息**是** run 的挂载点（messageRunSegments 把 run 名下每条消息
            // 都当挂载点 —— 之后的活动才渲染在它下面）。这里曾经另有一条"没有
            // segment 的用户消息"槽位分支去画它的回执/答复，可带 runId 的消息
            // 必然有 segment，那条分支永远走不到：让位做了、接住没做，答复两头
            // 落空（#766 实拍）。现在锚到插话的答复由它自己的窗口实例画。
            return [...segments.map((segment) => (
              <SessionCanonicalRunAccessory
                key={`${segment.runId}@${segment.window.start}`}
                runId={segment.runId}
                turnReplyText={replyTextByRun.get(segment.runId)}
                onOpenNodeDetail={openNodeDetail}
                sequenceWindow={segment.window}
                interjectionMessageId={segment.interjection ? message.id : undefined}
                enabled={mode === "api"}
                submitting={chat.sending || false}
                showStreamingDraft={!chat.sending}
              />
            )), pausePrompt];
          }}
          composerAbove={(
            /* 交付物挨着输入框 —— 用户读完汇报、正要说下一句时，"论文在这儿"
               就在手边。放进对话流里的话它会被几百条活动记录冲走；放进 ⋯ 菜单
               里等于还是要翻。 */
            <>
              <SessionDeliverables
                projectId={projectId}
                sessionId={sessionId}
                enabled={mode === "api"}
                onOpenFile={openWorkspaceFile}
              />
            </>
          )}
          composerContext={(
            <SessionComposerBar
              mode={operationMode}
              onModeChange={(next) => modeMutation.mutate(next)}
              modeBusy={modeMutation.isPending || projectQuery.isLoading}
              canChangeMode={mode === "api" && (session.capabilities ?? []).includes("manage_settings")}
              modelLabel={session.modelBackendName ?? null}
              modelId={session.modelBackendId ?? null}
              models={(modelBackendsQuery.data ?? []).map((backend) => ({
                id: backend.id,
                label: modelBackendDisplayName(backend),
                detail: [modelBackendModelLabel(backend), backend.base_url ?? t({ zh: "托管", en: "Managed" })]
                  .filter(Boolean)
                  .join(" · "),
                ready: backend.status === "ready",
              }))}
              onModelChange={(backendId) => modelMutation.mutate(backendId)}
              modelBusy={modelMutation.isPending || modelBackendsQuery.isLoading}
              contextWindow={contextWindow}
              branch={session.gitBranch ?? null}
              // 与面板同一个答案（见 sessionUnpublishedCount）：session 字段只数
              // ChangeItem，而研究产出是直接提交进 session 分支的，那个数常年为
              // 0 —— chip 会说"无未发布改动"，而分支上躺着几十个文件。
              unpublishedCount={sessionUnpublishedCount(changeSet, session.unpublishedChangeCount)}
              onOpenChanges={() => {
                setChangesOpen((open) => {
                  const next = !open;
                  if (next) {
                    // 面板刚挂载，等一帧再滚到它。
                    requestAnimationFrame(() => {
                      document
                        .querySelector(".session-changes-panel")
                        ?.scrollIntoView({ behavior: "smooth", block: "nearest" });
                    });
                  }
                  return next;
                });
              }}
              onAddFile={(file) => addFileMutation.mutate(file)}
              addingFile={addFileMutation.isPending}
              canAddFile={mode === "api" && canCompose}
              maxFileBytes={session.materialMaxBytes}
              state={
                waitingForInput
                  ? { kind: "answering" }
                  : turnRunning
                    ? { kind: "running" }
                    : canCompose
                      ? { kind: "idle" }
                      : { kind: "readonly", reason: disabledReason ?? t({ zh: "你对这个会话只有查看权限", en: "You only have view access to this session" }) }
              }
            />
          )}
        >
          {recoveredLiveRunId && recoveredRunNeedsStandalone && (
            <SessionCanonicalRunAccessory
              runId={recoveredLiveRunId}
              enabled={mode === "api"}
              submitting={false}
              showStreamingDraft
              // 这条 standalone accessory 也要能点开右栏 —— 漏掉它，正在跑
              // 的那张卡（最常被点的一张）就是死的（2026-08-18 实测）。
              onOpenNodeDetail={openNodeDetail}
            />
          )}
          {/*
            待答卡片没有锚点时的位置：列表底部。这**不是第二条渲染路径** ——
            画的是上面那同一个 `answerPrompt` 表达式，只是挂在别处。
            "没有锚点"是一个机械可判的明确情形（消息上没有对得上的 offerId），
            不是一条会各自演化的分支。
          */}
          {!pauseAnchorMessageId && answerPrompt}
          {backgroundRunActive && !recoveredLiveRunId && (
            <ChatRunActivity activities={[]} running view={view} />
          )}
          {showTransientRun && !transientRunHasCanonicalOwner && (
            <ChatRunActivity
              activities={chat.activities}
              running={chat.sending}
              view={view}
            />
          )}
          {/* 「N files changed」汇总面板不常驻（2026-08-17 用户拍板）：每次
              改动已经以内联卡（"Edited foo.py +17 -0"）在发生的位置原地显示，
              改完接着往下输出。这里只在两种情况出现：点了 composer 的
              changes chip（要 publish / 看全量 diff 的时候），或有待解决的
              合并冲突（不弹出来没人知道要处理）。 */}
          {(changesOpen || openConflicts.length > 0) && <SessionChangesPanel
            session={session}
            changeSet={changeSet}
            conflicts={conflictsQuery.data}
            loading={changeSetQuery.isLoading || conflictsQuery.isLoading}
            error={changeSetQuery.error ?? conflictsQuery.error ?? mutations.publish.error ?? mutations.resolveConflict.error}
            canPublish={canPublish}
            canResolve={canResolve}
            publishing={mutations.publish.isPending}
            resolving={mutations.resolveConflict.isPending}
            onPublish={() => {
              if (!changeSet) return;
              runFreshSessionPublish(
                () => mutations.publish.reset(),
                () => mutations.publish.mutate({
                  sessionId,
                  message: `Publish ${changeSet.filesChanged} Session file change${changeSet.filesChanged === 1 ? "" : "s"}`,
                }),
              );
            }}
            onResolve={(conflictId, choice) => mutations.resolveConflict.mutate({ sessionId, conflictId, choice })}
            initiallyExpanded={changesOpen}
            requested={changesOpen}
          />}
        </ChatPage>
      </WorkspacePanelHost>
    </main>
    </WorkspaceFileOpenerProvider>
  );
}
