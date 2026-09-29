"use client";

import { useCallback, useRef, useState } from "react";
import { nanoid } from "nanoid";
import { useQueryClient } from "@tanstack/react-query";
import { api, type ConceptAnnotation } from "@/lib/api";
import { answerBubbleText, buildChatRequest, composeText, type Answer } from "../lib/answer";
import { qk } from "@/lib/query/keys";
import { useNotificationSettings } from "@/features/settings/useNotificationSettings";
import { pushError, pushFailure, pushInfo, pushSuccess } from "@/stores/notification";
import { say, useLanguage, useT, type Phrase } from "@/shared/i18n";
import type { ChatMessage } from "../types";
import type { ChatActivity } from "../types";
import { authoritativeReplyText } from "../lib/authoritative-reply";
import {
  presentChatFailure,
  presentStructuredChatFailure,
  type ChatFailurePresentation,
} from "../lib/chat-error-presentation";
import {
  EMPTY_CHAT_TERMINAL,
  normalizeChatTerminal,
  type ChatTerminalState,
} from "../lib/chat-terminal";

/**
 * `send` 在**派发那一刻**的结果。它不是 void：一个命令处理函数必须交出一个
 * 结果，于是"什么都没做就返回"在类型上写不出来（裸 `return;` 是 TS2322）。
 * 流本身的成败仍走 onDone / onError 回调，那是另一段时间线。
 */
export type SendDispatch =
  /** 开了新一轮，这个标签页在跟它的流。 */
  | { kind: "streaming" }
  /** 已有一条流开着，这句话作为插话发出去了。 */
  | { kind: "interjecting" };

/**
 * 失败 toast 上「重试」发的那句话：平台既有语义 = 下一条消息从断点接着跑。
 *
 * 它按界面语言发 —— 这句话会作为**用户自己的消息**出现在对话里，英文界面
 * 下冒出一句中文，读起来像别人替你说的话。
 */
const CONTINUE_ANSWER: Phrase = { zh: "继续", en: "Continue" };

function activityFromEvent(event: Record<string, unknown>, index: number): ChatActivity {
  const label = [event.label, event.message, event.tool_name, event.action, event.kind, event.type]
    .find((value) => typeof value === "string" && value.trim()) as string | undefined;
  const output = [event.detail, event.output, event.summary]
    .find((value) => typeof value === "string" && value.trim()) as string | undefined;
  return {
    id: String(event.id ?? event.sequence ?? `${Date.now()}-${index}`),
    label: label ?? "Execution update",
    detail: output?.slice(0, 280),
    status: typeof event.status === "string" ? event.status : undefined,
    raw: event,
  };
}

/**
 * Chat send flow with SSE streaming.
 *
 * Holds local message state (user message + assistant streamed response).
 * Persistence is on the server side (conversation_id is passed in/out).
 */
export function useChat({
  scope,
  conversationId,
  initialMessages = [],
  onProjectCreated,
  onStreamDone,
}: {
  scope: { kind: "global" } | { kind: "project"; projectId: string };
  conversationId: string | null;
  initialMessages?: ChatMessage[];
  /** For global chat: callback when LLM creates a new project as a side-effect. */
  onProjectCreated?: (info: Record<string, unknown>) => void;
  /** Called only after the server emits a successful SSE `done` event. */
  onStreamDone?: (terminal: ChatTerminalState, meta: Record<string, unknown>) => void;
}) {
  const t = useT();
  const lang = useLanguage();
  const queryClient = useQueryClient();
  const notificationSettings = useNotificationSettings();
  const [messages, setMessages] = useState<ChatMessage[]>(initialMessages);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<ChatFailurePresentation | null>(null);
  const [activities, setActivities] = useState<ChatActivity[]>([]);
  const [terminal, setTerminal] = useState<ChatTerminalState>(EMPTY_CHAT_TERMINAL);
  const abortRef = useRef<(() => void) | null>(null);
  // 失败 toast 上的「重试」要在 send 定义之前就被 onError 闭包引用 —— 走 ref，
  // 每次渲染后指向最新的 send。
  const sendRef = useRef<(answer: Answer) => SendDispatch>(() => ({ kind: "streaming" }));

  const reset = useCallback((next: ChatMessage[], clearExecution = true) => {
    abortRef.current?.();
    abortRef.current = null;
    setMessages(next);
    if (clearExecution) {
      setError(null);
      setActivities([]);
      setTerminal(EMPTY_CHAT_TERMINAL);
    }
  }, []);

  const send = useCallback(
    (answer: Answer): SendDispatch => {
      // 这里曾经有一句 `if (!text.trim()) return;` —— 第四道各自为政的"有没有
      // 东西可发"闸。8-31 让"选了选项时附言可空"通过了卡片和 workspace 那两道，
      // 这一道原样留着：只点选项不写附言 → 静默 return → 请求根本没出浏览器
      // （2026-09-03，cuib 会话 de4632cc）。
      //
      // 现在收到的是构造好的 `Answer`：空不空在 composeAnswer 里判过唯一一次，
      // 这里没有东西可判。返回类型非 void，裸 `return;` 是编译错误 ——
      // 沉默从此不是一种写法选项。
      const text = answerBubbleText(answer);
      /**
       * 已经有一条流开着 → 这句话是**插话**，不是新一轮。
       *
       * 原来这里是 `if (sending) return`：一轮能跑几小时，SSE 全程开着，于是
       * 发起那一轮的标签页整场都发不出第二句 —— 而后端一直支持中途插话（同
       * 一个入口机械分流成 routed=interject，见下面 onDone）。那条路只有在
       * **换个标签页/刷新之后**（sending 变回 false）才走得通，等于机制在、
       * 主路径够不着。
       *
       * 插话共用同一段发送逻辑，唯一的差别是**不接管流的状态**：不动
       * sending / terminal / activities / abortRef —— 动了就等于替还在跑的
       * 那一轮宣布结束。
       */
      const secondary = sending;
      const userMsg: ChatMessage = { id: nanoid(), role: "user", text };
      const assistantId = nanoid();
      setMessages((c) => [
        ...c,
        userMsg,
        { id: assistantId, role: "assistant", text: "" },
      ]);
      if (!secondary) {
        setSending(true);
        setError(null);
        setActivities([]);
        setTerminal(EMPTY_CHAT_TERMINAL);
      }

      const history = messages
        .filter((m): m is ChatMessage & { role: "user" | "assistant" } => m.role !== "system")
        .filter((m) => m.id !== "welcome" && !m.id.startsWith("__"))
        .map((m) => ({ role: m.role, content: m.text }));

      // 线格式只有一处组装（answer.ts），目标类型从后端模型生成。
      const req = buildChatRequest(answer, { history, conversationId: conversationId ?? null });

      const appendToken = (token: string) => {
        setMessages((c) =>
          c.map((m) =>
            m.id === assistantId ? { ...m, text: m.text + token } : m,
          ),
        );
      };
      const setConcepts = (annotations?: ConceptAnnotation[]) => {
        if (!annotations?.length) return;
        setMessages((c) =>
          c.map((m) =>
            m.id === assistantId ? { ...m, conceptAnnotations: annotations } : m,
          ),
        );
      };
      const recordProgress = (event: Record<string, unknown>) => {
        if (typeof event.runId === "string" && event.runId) {
          setMessages((current) => current.map((message) =>
            message.id === assistantId ? { ...message, runId: event.runId as string } : message));
        }
        // 插话那条流的事件不进实时活动列表：那一栏说的是"**正在跑的那一轮**
        // 此刻在干什么"，掺进另一条流的事件就成了两件事挤在一行。
        if (secondary) return;
        setActivities((current) => {
          const next = activityFromEvent(event, current.length);
          const existing = current.findIndex((item) => item.id === next.id);
          if (existing < 0) return [...current, next];
          return current.map((item, index) => index === existing ? next : item);
        });
      };
      const onError = (err: string, detail?: Record<string, unknown>) => {
        // 后端送来结构化失败（run_failures 文案表整份记录）就用它 —— 那是
        // 握着异常对象的一方写下的事实（"模型服务不可用"就说模型服务，
        // 别让用户把火撒在平台头上）；没有结构才退回按字符串猜。
        // 认不出的失败 → 什么都不显示，也不改终态（改了别处会跟着画红/弹通知）。
        // 会话没死：磁盘上的记录一个字没变，下一条消息照常从断点接着跑。
        // 见 presentChatFailure 的说明（wangd 2026-08-18 第三次指着同一条）。
        const structured = presentStructuredChatFailure(detail);
        const presented = structured ?? presentChatFailure(err || "Chat failed");
        // 入口 409：这张卡背后已经没有停着的 pause，或呈递换了。屏幕上那张卡
        // 是旧的 —— 让会话那一次现算重新回答"现在停在哪"，别让人对着旧卡再点。
        const code = typeof detail?.code === "string" ? detail.code : "";
        if (
          scope.kind === "project"
          && (code === "no_pause_to_answer" || code === "offer_superseded")
        ) {
          void queryClient.invalidateQueries({ queryKey: qk.sessionsPrefix(scope.projectId) });
        }
        if (secondary) {
          // 插话失败只毁这句话本身，正在跑的那一轮不受影响 —— 所以既不改
          // 终态、也不动 sending，只把占位气泡撤掉并如实说一声。
          setMessages((c) => c.filter((m) => m.id !== assistantId));
          if (presented) pushError(`${presented.message} ${presented.recovery}`, presented.title);
          else pushError(
            t({ zh: "这句话没能送进去，正在跑的那一轮不受影响。", en: "This message could not be delivered; the running turn is unaffected." }),
            t({ zh: "插话失败", en: "Interjection failed" }),
          );
          return;
        }
        setError(presented);
        // 局面由会话那一次现算回答；这里只把上一轮的终帧清掉，不替服务端
        // 编一个 "error" 局面出来（编出来的那个会盖住真实的可续状态）。
        if (presented) setTerminal(EMPTY_CHAT_TERMINAL);
        setMessages((c) => c.filter((m) => m.id !== assistantId));
        setSending(false);
        abortRef.current = null;
        if (structured) {
          // 小弹窗把原因说完整；重发无用（retryable=false）时不给重试按钮，
          // 免得按钮本身撒谎。重试 = 发一条「继续」——平台的既有语义就是
          // "下一条消息从断点接着跑"。
          pushFailure(
            structured.message + (structured.recovery ? ` ${structured.recovery}` : ""),
            structured.title,
            structured.retryable === false
              ? undefined
              : {
                label: t({ zh: "重试", en: "Retry" }),
                onClick: () => { sendRef.current(composeText(say(CONTINUE_ANSWER, lang)) as Answer); },
              },
          );
        }
      };
      const onDone = (meta: Record<string, unknown>) => {
        if (meta.routed === "interject") {
          // 后端判定会话正忙，把这句话分流进了正在跑的调度器的收件箱
          // （单一入口的机械分流）。这一轮**没有**新的 assistant 回复 ——
          // 撤掉占位气泡；user 消息归属到正在跑的 run，让它挂进正确的
          // 时序窗口。回应稍后以时间线事件的形式出现（决策轮的答复）。
          setMessages((current) => current
            .filter((m) => m.id !== assistantId)
            .map((m) => (
              m.id === userMsg.id && typeof meta.runId === "string" && meta.runId
                ? { ...m, runId: meta.runId }
                : m
            )));
          if (!secondary) {
            setTerminal(EMPTY_CHAT_TERMINAL);
            setSending(false);
            abortRef.current = null;
          }
          // 「已送达」不弹：这句话的气泡已经在对话里了，屏幕自己说明了它发出去。
          // 只有"执行进程当前不在线、要等会话续跑才会被处理"是**看不出来**的
          // 事实 —— 那一支留着。
          if (meta.liveness === "unbound") {
            pushInfo(
              t({ zh: "执行进程当前不在线：这句话已排队，会话续跑时才会被处理。", en: "The execution process is offline: this message is queued and will be handled when the Session resumes." }),
              t({ zh: "已排队", en: "Queued" }),
            );
          }
          if (!secondary) onStreamDone?.(EMPTY_CHAT_TERMINAL, meta);
          return;
        }
        if (secondary) {
          // 没被分流成插话 —— 说明这句话发出去的时候会话其实已经不忙了，
          // 后端按新一轮处理并且已经回完。保留回复正文，但**不碰终态和
          // sending**：那两样属于外面那条还开着的流，这里替它宣布结束就会
          // 把一轮还在跑的会话画成"已完成"。
          setMessages((current) => current.map((m) =>
            m.id === assistantId ? { ...m, text: authoritativeReplyText(m.text, meta) } : m));
          return;
        }
        const nextTerminal = normalizeChatTerminal(meta);
        // 收尾时把这条消息收敛成**权威终稿**（2026-08-17）。
        //
        // 流式期间 appendToken 逐个追加，累积出来的是这一轮**所有中间轮次的
        // 散文首尾相接** —— 那是过程，不是回复。以前它就这么留在对话里，于是
        // 同一段话出现两次：这里一坨拼接产物，下面"第 N 轮"叙述事件再画一遍
        // （实测截图）。
        //
        // 流式的价值是跑的时候有反馈，不是"过程即结论"。所以不动流式，只在
        // done 时用后端送来的 `reply`（回复契约的产出，= final_text）替换。
        // 中间散文并没有消失 —— 它以 agent.message 叙述的形式留在时间线上，
        // 和它解释的那次工具调用相邻，这正是它该待的地方。
        setMessages((current) =>
          current.map((m) =>
            m.id === assistantId
              ? { ...m, text: authoritativeReplyText(m.text, meta) }
              : m,
          ),
        );
        setTerminal(nextTerminal);
        setConcepts((meta.concept_annotations as ConceptAnnotation[]) ?? undefined);
        setSending(false);
        abortRef.current = null;
        onStreamDone?.(nextTerminal, meta);
        // 通知的判据也读**局面**，不读状态词。三条各自手写的状态名单
        // （"waiting_human" / ["failed","error","stale_unknown"] / …）是前端
        // 最后几处 run 状态词表，而名单必然漏新写法。
        const doneView = nextTerminal.view;
        if (doneView?.waitingOn?.kind === "human" && notificationSettings.data?.decision_required) {
          pushInfo("This Session is waiting for your response.", "Research decision required");
        } else if (
          (doneView?.phase === "interrupted"
            || doneView?.outcome === "failed"
            || doneView?.outcome === "cancelled")
          && notificationSettings.data?.run_failed
        ) {
          pushError("Open the Session to inspect the recorded failure and recovery options.", "Run needs attention");
        } else if (
          (doneView?.outcome === "ok" || doneView?.outcome === "ok_with_warning")
          && notificationSettings.data?.run_completed
        ) {
          pushSuccess("The latest research Run finished and its result is recorded.", "Run completed");
        }
        if (
          scope.kind === "global" &&
          meta.created_project &&
          onProjectCreated
        ) {
          onProjectCreated(meta.created_project as Record<string, unknown>);
          // Append a confirmation suffix to the assistant message
          const cp = meta.created_project as Record<string, unknown>;
          const suffix = `\n\nCreated project: ${cp.name} (${cp.start_node_type}). ${
            cp.auto_started ? "Scheduler started." : "Ready to start."
          }`;
          appendToken(suffix);
          queryClient.invalidateQueries({ queryKey: qk.projects() });
        }
      };

      let abort: () => void;
      if (scope.kind === "project") {
        abort = api.streamProjectChat(
          scope.projectId,
          req,
          appendToken,
          onDone,
          recordProgress,
          onError,
        );
      } else {
        abort = api.streamGlobalChat(req, appendToken, onDone, onError);
      }
      // 插话不接管中止句柄：Stop 要停的是**正在跑的那一轮**，不是这句话。
      if (!secondary) abortRef.current = abort;
      return { kind: secondary ? "interjecting" : "streaming" };
    },
    [conversationId, messages, scope, sending, queryClient, onProjectCreated, onStreamDone, notificationSettings.data],
  );
  sendRef.current = send;

  return { messages, setMessages, reset, send, sending, error, activities, terminal };
}
