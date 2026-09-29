import type { ChatPause, ChatPauseOption } from "@/features/chat";
import { say, type Language } from "../../../shared/i18n/language.ts";
import type { ResearchSession, SessionPendingApproval } from "../types";

/**
 * 顶栏那一行版本。
 *
 * 从前是 `main · r3` —— 一个只在 `project_revisions` 表里有意义的自增号。
 * 那张表随第二份版本账一起删了（RFC X1），取代它的是 main 的短 sha：一个
 * **能拿去 `git show` 的**标识。
 */
export function sessionRevisionLabel(session: ResearchSession, lang: Language = "zh") {
  return session.headCommitSha === null
    ? say({ zh: "读不到版本信息", en: "Revision data unavailable" }, lang)
    : `main · ${session.headCommitSha.slice(0, 8)}`;
}

/** 项目在这个会话开跑之后又往前走了 —— git 的说法就是"落后几个提交"。 */
export function sessionHasAdvancedBase(session: ResearchSession) {
  return session.behindBy > 0;
}

/**
 * 「谁在开这个会话」——后端现算：最近一条用户消息的作者。
 *
 * 2026-09-05 删掉驾驶权租约之前，这里还要读一个 `driverLeaseUntil` 判它过期
 * 没有，于是同一个问题在前端有一份判据、后端有另一份，两份各自演化。现在
 * 后端给的就是答案，前端只做展示。
 *
 * 「我能不能开」是另一个问题，只有后端答（`execution.answer.via`）。
 */
export function sessionHasActiveDriver(session: ResearchSession) {
  return !!session.primaryDriverUserId;
}

export function sessionIsDrivenBy(
  session: ResearchSession,
  userId: string | null | undefined,
) {
  return !!userId && session.primaryDriverUserId === userId;
}

/**
 * 「这个人能不能对这个会话动手」曾经在这里算 —— 而后端**同时**在算它的另一半
 * （能力 + 归档）。两半的交集才是真的"能不能"，却没有任何一层持有那个交集：
 * `canSend=true` 与前端灰着的输入框可以同时成立，反过来也可以。
 *
 * 现在整件事只有一个答案，由后端 `drive_access_for` 给出，随局面下发成
 * `execution.answer`（`via === "none"` 就是不能动，并附原因）。客户端保留的
 * 只剩下面两个**关于驾驶者身份**的谓词 —— 它们回答的是"谁在开车"，
 * 不是"我能不能开"，那是另一个问题。
 */

export function sessionDriverLabel(session: ResearchSession, lang: Language = "zh") {
  if (session.lifecycleStatus === "archived") return null;
  if (!sessionHasActiveDriver(session)) return say({ zh: "没人在开车", en: "No active driver" }, lang);
  return `${session.driverLabel} · ${say({ zh: "驾驶者", en: "driver" }, lang)}`;
}

/**
 * 顶栏原来常驻的那几行事实 —— 现在归「会话信息」抽屉。
 *
 * 它们全都是**要查的时候才查**的：审计这个会话基于哪个版本、项目有没有往前
 * 走、谁在开车。每次打开会话都先读三行才看得到标题，代价和收益是反的。
 *
 * 事实本身一个都没丢，只是换了位置 —— 顶栏挤不下不等于可以不给。
 */
export function sessionAboutFacts(
  session: ResearchSession,
  lang: Language = "zh",
): { term: string; value: string }[] {
  const facts = [{
    term: say({ zh: "版本", en: "Revision" }, lang),
    value: sessionRevisionLabel(session, lang),
  }];
  if (sessionHasAdvancedBase(session)) {
    facts.push({
      term: say({ zh: "基线", en: "Baseline" }, lang),
      value: say({ zh: "项目已往前走了 {count} 个提交", en: "The Project has moved ahead by {count} commits" }, lang, { count: session.behindBy }),
    });
  }
  const driver = sessionDriverLabel(session, lang);
  if (driver) facts.push({ term: say({ zh: "驾驶者", en: "Driver" }, lang), value: driver });
  return facts;
}

export function sessionFrozenModelLabel(modelBackendName: string | null | undefined, lang: Language = "zh") {
  const name = modelBackendName?.replace(/\s*\(environment\)\s*$/i, "").trim()
    || say({ zh: "项目模型", en: "Project model" }, lang);
  return say({ zh: "{name} · 这个会话固定用它", en: "{name} · fixed for this Session" }, lang, { name });
}

/**
 * 会话级待审批 → 聊天区能画的 pause。
 *
 * 只是形状转换，没有判断逻辑：**问不问、问什么，后端已经算完了**
 * （sessions._pending_approval 按事件序推导）。这里再补一层"要不要显示"
 * 的判据，就又多了一份会各自演化的真相源。
 */
export function toChatPause(
  pending: SessionPendingApproval | null,
): ChatPause | null {
  if (!pending) return null;
  const labels = pending.optionDetails.length
    ? pending.optionDetails
    : pending.options.map((label, index) => ({
        // 纯文案兜底路径：没有身份可言。
        id: undefined as string | undefined,
        label,
        description: "",
        recommended: index === pending.recommendedOptionIndex,
      }));
  // 身份只来自呈递方给的 id。这里原来给**每个**选项编 `${runId}:${index}` ——
  // 写它的时候答复走文案匹配（value=label），编造 id 只当 React key 用，无害；
  // 后来 HumanInputPrompt 升级成回传 `selected.id`，这个编造 id 就成了答复本体：
  // `{"choice_id": "run_3517…:1"}` 去撞合法集 `[retry_reviewer, …]`，
  // `choice_not_offered` 三连拒，人点三次零反馈（2026-08-19 transcript 实录）。
  // 两层各自演化，谁都没报错 —— 所以现在把「渲染 key」和「可回传的身份」拆成
  // 两个字段，编造的只能进前者。
  const details: ChatPauseOption[] = labels.map((option, index) => ({
    id: option.id ?? `${pending.runId}:${index}`,
    choiceId: option.id,
    label: option.label,
    value: option.label,
    description: option.description || undefined,
    recommended: option.recommended,
  }));
  const offer = pending.offer;
  return {
    question: pending.prompt ?? "This Run is waiting for your answer.",
    // 逐字的待执行内容。批准一个看不见内容的高危操作等于没有审批 ——
    // 这一段丢了，审批 UI 就退化成一个"确定/取消"弹窗。
    context: pending.context ?? undefined,
    askingNodeType: pending.askingNodeType ?? undefined,
    askingRunId: pending.runId,
    // 是哪一类由**呈递自己**说（后端 `_pending_approval` 一处判）。这里曾经
    // 拿 run 状态词去猜，而那是前端本不该持有的词表。
    kind: pending.kind,
    // 呈递的身份与事实：answer_preview 里 offer_id 为空串就是这里此前没带。
    offerId: typeof offer?.offer_id === "string" ? offer.offer_id : undefined,
    facts: (offer && typeof offer.facts === "object" && offer.facts !== null)
      ? offer.facts as Record<string, unknown>
      : undefined,
    options: details,
  };
}
