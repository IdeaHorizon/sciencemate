import { say, type Language } from "../../../shared/i18n/language.ts";
import type { ChatActivity } from "../types.ts";
import type { SessionExecutionView } from "@/features/sessions/types";
import { isVisibleResearchTool, researchToolRunningStatus } from "../../execution/lib/tool-presentation.ts";

export type LiveActivityProjection = {
  statusText: string;
  tone: "working" | "attention" | "failed";
};

function string(value: unknown) {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function number(value: unknown) {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : undefined;
}

function eventIdentity(activity: ChatActivity) {
  const outer = string(activity.raw.event) ?? string(activity.raw.type);
  const detail = string(activity.raw.detail);
  const inner = outer?.startsWith("harness.") ? detail : outer;
  return { outer, inner };
}

function projectedStatus(activity: ChatActivity, lang: Language) {
  const { outer, inner } = eventIdentity(activity);
  const toolName = string(activity.raw.tool) ?? string(activity.raw.tool_name);
  const detail = string(activity.raw.detail);
  const rawStatus = string(activity.raw.status);
  const recoveryReason = string(activity.raw.reason);

  if (rawStatus === "failed" || rawStatus === "error") {
    return { statusText: say({ zh: "这次执行需要人看一下", en: "Execution needs attention" }, lang), tone: "failed" as const };
  }
  if (inner === "llm_retry_scheduled" || inner === "llm_truncation_recovery_injected") {
    return { statusText: say({ zh: "正在重试模型请求", en: "Retrying the model request" }, lang), tone: "attention" as const };
  }
  if (
    inner === "void_turn_rolled_back"
    || (inner === "run.recovering" && recoveryReason === "void_turn")
  ) {
    const attempt = number(activity.raw.attempt);
    const maximum = number(activity.raw.max_attempts) ?? number(activity.raw.maxAttempts);
    const counter = attempt && maximum
      ? ` · ${say({ zh: "第 {attempt}/{max} 次重试", en: "retry {attempt}/{max}" }, lang, { attempt, max: maximum })}`
      : ` · ${say({ zh: "重试中", en: "retrying" }, lang)}`;
    return {
      statusText: `${say({ zh: "模型没给出可用的回复", en: "The model returned no usable response" }, lang)}${counter}`,
      tone: "attention" as const,
    };
  }
  if (inner === "run.recovering") {
    return { statusText: say({ zh: "正在恢复这次执行", en: "Recovering the execution" }, lang), tone: "attention" as const };
  }
  if (inner === "run_paused" || inner === "run.paused" || inner === "loop_pause" || outer === "harness.pause_required") {
    return { statusText: say({ zh: "在等你回话", en: "Waiting for your input" }, lang), tone: "attention" as const };
  }
  if (inner === "run.waiting_compute") {
    return { statusText: say({ zh: "在等后台的研究", en: "Waiting for background research" }, lang), tone: "working" as const };
  }
  if (
    (inner === "tool_start" || inner === "tool_call" || inner === "tool.progress")
    && isVisibleResearchTool(toolName)
  ) {
    return { statusText: researchToolRunningStatus(toolName, undefined, detail, lang), tone: "working" as const };
  }
  if (inner === "tool_result" || inner === "tool_long_running") {
    return { statusText: inner === "tool_long_running"
      ? say({ zh: "在等一个研究工具", en: "Waiting for a research tool" }, lang)
      : say({ zh: "正在看工具结果", en: "Analyzing tool results" }, lang), tone: "working" as const };
  }
  if (inner === "llm_request" || inner === "llm_start") {
    return { statusText: say({ zh: "正在想这个研究任务", en: "Thinking through the research task" }, lang), tone: "working" as const };
  }
  if (inner === "llm_response") {
    return { statusText: say({ zh: "正在生成回复", en: "Generating the response" }, lang), tone: "working" as const };
  }
  if (inner === "run.started") {
    // 「Sending the request to DeepSeek · GPUStack (local)」—— 用户看到的是
    // 传输细节，不是研究状态（wangd 2026-08-18：「很奇怪，统一改成 thinking」）。
    // backend 是哪个在 composer 的模型徽标上一直可见，这里不用复读。
    return { statusText: say({ zh: "在想", en: "Thinking" }, lang), tone: "working" as const };
  }
  if (inner === "subagent_call_start" || outer === "harness.child_event") {
    return { statusText: say({ zh: "正在开始一步研究", en: "Starting a research step" }, lang), tone: "working" as const };
  }
  if (outer === "harness.background_wait") {
    return { statusText: say({ zh: "在等后台的研究", en: "Waiting for background research" }, lang), tone: "working" as const };
  }
  if (
    inner === "platform_context_bound"
    || inner === "platform_session_start"
    || inner === "started"
    || inner === "platform_request_start"
    || inner === "loop_start"
    || inner === "hook_injection"
    || inner === "root_step_start"
    || inner === "write_scratchpad"
    || outer === "harness.started"
    || outer === "harness.progress"
    || outer === "harness.transcript"
  ) {
    return { statusText: say({ zh: "正在启动项目助手", en: "Starting the Project assistant" }, lang), tone: "working" as const };
  }
  return undefined;
}

/**
 * ⚠️ 这个函数曾经收一个 `status?: string | null` 并拿它去撞 run 状态名单
 * （"waiting_human" / "retrying" / …）。前端从 2026-08-27 起就不该再有 run
 * 状态词表了 —— 保留它的代价是调用点必须把 view **翻译**成状态串，而两个
 * 调用点翻译得不一样正是那次 6 小时锁死的最后一跳。
 *
 * 现在收 view 本身。判据只读 `phase` / `waitingOn.kind` / `outcome` ——
 * 都是后端那份唯一分区里的取值，加一个新取值前端编译即红。
 */
export function projectLiveActivity(
  activities: readonly ChatActivity[],
  options: { running: boolean; view?: SessionExecutionView | null; lang?: Language },
): LiveActivityProjection {
  const lang = options.lang ?? "zh";
  const view = options.view ?? null;
  if (view?.waitingOn?.kind === "human") {
    return { statusText: say({ zh: "在等你回话", en: "Waiting for your input" }, lang), tone: "attention" };
  }
  if (view?.waitingOn?.kind === "permission") {
    return { statusText: say({ zh: "在等授权", en: "Waiting for permission" }, lang), tone: "attention" };
  }
  if (view?.waitingOn?.kind === "compute") {
    return { statusText: say({ zh: "在等后台的研究", en: "Waiting for background research" }, lang), tone: "working" };
  }
  if (view?.phase === "interrupted") {
    return {
      statusText: say({
        zh: "这一轮没跑完 —— 执行进程在写下终态之前退出了",
        en: "This turn did not finish — the execution process exited before recording a final state",
      }, lang),
      tone: "attention",
    };
  }
  if (view?.outcome === "failed" || view?.outcome === "cancelled") {
    return { statusText: say({ zh: "执行失败了", en: "Execution failed" }, lang), tone: "failed" };
  }

  for (let index = activities.length - 1; index >= 0; index -= 1) {
    const projected = projectedStatus(activities[index], lang);
    if (projected) return projected;
  }

  return {
    statusText: options.running
      ? say({ zh: "正在启动项目助手", en: "Starting the Project assistant" }, lang)
      : say({ zh: "读不到执行进展", en: "Execution update unavailable" }, lang),
    tone: "working",
  };
}
