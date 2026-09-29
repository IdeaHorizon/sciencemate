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
export function presentChatFailure(raw: string): ChatFailurePresentation | null {
  const normalized = raw.trim().toLowerCase();
  const statusMatch = normalized.match(/(?:stream failed|status)\s*:?\s*(\d{3})/);
  const status = statusMatch ? Number(statusMatch[1]) : undefined;

  if (status === 409) {
    return {
      title: "This Session changed before the request could start",
      message: "No new research run was started; your request remains in the conversation.",
      recovery: "Refresh the Session, then send the request again.",
    };
  }
  if (status === 401 || status === 403) {
    return {
      title: "This request needs access",
      message: "Your current sign-in or Project role cannot start this research run.",
      recovery: "Sign in again or ask a Project lead to review your access.",
    };
  }
  if (status === 429) {
    return {
      title: "The model is temporarily at capacity",
      message: "No result was produced for this request.",
      recovery: "Wait a moment, then send the request again.",
    };
  }
  if (status !== undefined && status >= 500) {
    return {
      title: "The research service is unavailable",
      message: "The platform could not start or finish this request.",
      recovery: "Keep your request, check the service, then send it again.",
    };
  }
  if (/failed to fetch|network|load failed|connection/.test(normalized)) {
    return {
      title: "Connection to the research service was lost",
      message: "The platform could not confirm whether this request finished.",
      recovery: "Refresh the Session to check its latest state before sending again.",
    };
  }
  // 认不出 → 不显示。这一轮没产出回复不是错误，也没有要用户做的事。
  return null;
}
