import { say, type Language } from "../../../shared/i18n/language.ts";

export type ChatFailurePresentation = {
  title: string;
  message: string;
  recovery: string;
  /** 后端 run_failures 的三态：true=重发安全，false=重发无用，缺省=不确定。 */
  retryable?: boolean;
};

/**
 * 后端在 SSE error 帧里送来的**结构化**失败（run_failures 文案表的整份记录：
 * title / body / recovery / retryable / code…）。
 *
 * 这一份和下面按字符串猜的 presentChatFailure 是两代认领：后端握着异常对象，
 * 它写下的 title/body 是事实；字符串猜测只该在没有结构时兜底。2026-08-20
 * 实测：后端明明送了「模型服务不可用 / retryable=true」，api 层把它压成
 * message 字符串，前端猜不出 → 用户看到的是一句笼统的"这一轮没跑完"。
 */
export function presentStructuredChatFailure(
  detail: Record<string, unknown> | undefined,
): ChatFailurePresentation | null {
  if (!detail) return null;
  const text = (key: string): string =>
    typeof detail[key] === "string" ? (detail[key] as string).trim() : "";
  const title = text("title");
  const body = text("body");
  if (!title || !body) return null;
  return {
    title,
    message: body,
    recovery: text("recovery"),
    retryable: detail.retryable === true ? true
      : detail.retryable === false ? false
      : undefined,
  };
}

/**
 * 认不出的失败 → **什么都不显示**（返回 null）。
 *
 * ## 为什么（wangd 2026-08-18，第三次指着同一条）
 *
 *     「这个东西以后严禁以任何形式被我看到。我再也不想看到这个了，
 *       一点点卵用都没有。」
 *
 * 那条兜底文案是「这一轮没能完成 / No usable response was returned /
 * Review the request and send it again」。它三句话没有一句能让人多做对
 * 一件事：会话没死（磁盘上的记录一个字没变）、下一条消息照常从断点接着
 * 跑、"technical details" 里也只有同一句话的英文版。
 *
 * 它存在的唯一理由是**兜底必须返回点什么** —— 判据是"我认不出这个错误"，
 * 而它渲染出来的是"你的研究出问题了"。认不出就是认不出，那是我们的无知，
 * 不是用户要处理的事故。
 *
 * 留下来的都是**用户真能动手**的：登录/权限、限流、服务不可用、网络断。
 * 加新条目的判据只有一条：这条消息能让人多做对一件事吗？不能就别加。
 */
export function presentChatFailure(raw: string, lang: Language): ChatFailurePresentation | null {
  const normalized = raw.trim().toLowerCase();
  const statusMatch = normalized.match(/(?:stream failed|status)\s*:?\s*(\d{3})/);
  const status = statusMatch ? Number(statusMatch[1]) : undefined;

  if (status === 409) {
    return {
      title: say({ zh: "请求还没开始，会话已经变了", en: "This Session changed before the request could start" }, lang),
      message: say({ zh: "没有起新的一轮；你的请求还留在对话里。", en: "No new research run was started; your request remains in the conversation." }, lang),
      recovery: say({ zh: "刷新会话，再发一次。", en: "Refresh the Session, then send the request again." }, lang),
    };
  }
  if (status === 401 || status === 403) {
    return {
      title: say({ zh: "这个请求需要权限", en: "This request needs access" }, lang),
      message: say({ zh: "以你现在的登录状态或项目角色，起不了这一轮。", en: "Your current sign-in or Project role cannot start this research run." }, lang),
      recovery: say({ zh: "重新登录，或请项目负责人看一下你的权限。", en: "Sign in again or ask a Project lead to review your access." }, lang),
    };
  }
  if (status === 429) {
    return {
      title: say({ zh: "模型这会儿忙不过来", en: "The model is temporarily at capacity" }, lang),
      message: say({ zh: "这个请求没有产出结果。", en: "No result was produced for this request." }, lang),
      recovery: say({ zh: "稍等一会儿再发一次。", en: "Wait a moment, then send the request again." }, lang),
    };
  }
  if (status !== undefined && status >= 500) {
    return {
      title: say({ zh: "研究服务不可用", en: "The research service is unavailable" }, lang),
      message: say({ zh: "平台没能开始或跑完这个请求。", en: "The platform could not start or finish this request." }, lang),
      recovery: say({ zh: "请求先留着，查一下服务，再发一次。", en: "Keep your request, check the service, then send it again." }, lang),
    };
  }
  if (/failed to fetch|network|load failed|connection/.test(normalized)) {
    return {
      title: say({ zh: "和研究服务断开了", en: "Connection to the research service was lost" }, lang),
      message: say({ zh: "平台确认不了这个请求有没有跑完。", en: "The platform could not confirm whether this request finished." }, lang),
      recovery: say({ zh: "先刷新会话看看最新状态，再决定要不要重发。", en: "Refresh the Session to check its latest state before sending again." }, lang),
    };
  }
  // 认不出 → 不显示。这一轮没产出回复不是错误，也没有要用户做的事。
  return null;
}
