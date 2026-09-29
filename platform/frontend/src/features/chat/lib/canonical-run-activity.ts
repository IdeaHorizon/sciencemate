import type { RunDetailResponse, ResearchRun } from "../../../lib/api.ts";
import type { RunActivityTool } from "../../execution/lib/run-activity-detail.ts";
import { normalizeChatPause, type ChatPause } from "./chat-terminal.ts";
import { say, type Language, type Phrase } from "../../../shared/i18n/language.ts";

// 这两句在三个分支里重复出现 —— 同一句话在两处各写一遍，就会各自演化。
const TURN_DID_NOT_FINISH: Phrase = { zh: "这一轮没跑完", en: "This turn did not finish" };
const NO_USABLE_RESULT: Phrase = {
  zh: "平台没有为这次运行记录到可用的结果。",
  en: "The platform recorded no usable result for this run.",
};

export type CanonicalRunActivitySummary = {
  status: string;
  usage: string;
  retries: string;
};

export type CanonicalRunAttention = {
  title: string;
  message: string;
  recovery?: string;
  /** 原始技术细节。**只出现在折叠区**，永远不进正文。 */
  detail?: string;
  /** run id —— 用户手里有它，日志里才找得到真正的东西。 */
  reference?: string;
  /**
   * ## 为什么没有「失败」这一档（wangd 2026-08-18）
   *
   *     「失败是啥？为啥会有'失败'这么一个状态？什么时候会失败？」
   *
   * 把那张故障表逐条按"用户要不要做不一样的事"过一遍，只剩两类：
   *
   *   · 平台重启 / 进程退出 / 超时 / 内部错误 / 存储冲突 / 协议错 / 会话忙
   *     → **什么都不用做**。这一轮没跑完，下一条消息就从断点接着跑。
   *   · 输入过大 / 凭据 / 额度
   *     → **要动手**，不然重发还是这个结果。
   *
   * 「研究停止了」把第一类说成了后者，于是每次平台自己抖一下，用户都被
   * 吓一跳，还被教着去"刷新页面再决定要不要重发"。
   *
   * 判据不是新造的：后端 `run_failures` 早就有 `retryable` 三态
   * （True=重发安全 / False=重发一定还是这个结果 / None=不确定），
   * 只是呈现层从来没读它。`false` 才是"要你动手"，其余都是"接着跑"。
   */
  tone: "continue" | "action" | "attention";
};

function record(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

/**
 * 一次失败的说法**来自后端**，这里只渲染。
 *
 * ## 为什么不在这儿认领
 *
 * 原来这里是一条 if-链：`code === "harness_operation_timeout" ||
 * /Harness session operation timed out/i.test(message)` 认得的给人话，认不得的
 * 把 `message` 原样显示。于是 2026-08-11 会话页上出现了整条 SQLAlchemy
 * `IntegrityError` —— INSERT 语句、全部列名、全部参数值。
 *
 * 两个毛病：
 *
 * 1. **认领的知识有两份**。后端手里有异常对象（类型、code、上下文），却把它压成
 *    字符串；前端再用正则猜回来 —— 而前端那份永远比后端少知道一些。
 * 2. **判据是名单**。名单认不得的默认落到"原样显示"，所以每个新异常的默认行为
 *    就是把它的 `str()` 糊到用户脸上。
 *
 * 现在文案表在 `app/services/run_failures.py`，那是唯一握着异常对象的地方。
 * 这里只做一件事：有结构就渲染结构，没有（老 run 只存了 `message`）就给一句
 * 通用文案，把那串原文塞进 `detail` —— **原文永远不进正文**。
 */
function persistedRunFailure(detail: RunDetailResponse, lang: Language): CanonicalRunAttention | undefined {
  const summary = record(detail.run.summary);
  const failure = record(summary?.failure);
  if (!failure) return undefined;

  const text = (key: string): string =>
    typeof failure[key] === "string" ? (failure[key] as string).trim() : "";

  const title = text("title");
  const body = text("body");
  // 重发一定还是这个结果 = 要你动手；其余一律"接着跑"（含 retryable 缺失的
  // 老记录 —— 不确定时按可续处理：说错的代价是让人多发一条，反过来是把人
  // 吓停在一个其实能继续的地方）。
  const needsUser = failure.retryable === false;
  if (title && body) {
    return {
      title,
      message: body,
      recovery: text("recovery") || undefined,
      detail: text("detail") || undefined,
      reference: text("reference") || undefined,
      tone: needsUser ? "action" : "continue",
    };
  }

  // 老 run：summary 里只有一个字符串 `message`，内容可能是任何东西（包括
  // 一条 SQL 转储）。它是**证据**，不是文案 —— 所以进 detail。
  const legacy = text("message");
  if (!legacy && !text("code")) return undefined;
  return {
    title: say(TURN_DID_NOT_FINISH, lang),
    message: say(NO_USABLE_RESULT, lang),
    recovery: say({
      zh: "下面是已经记录下来的工作；发下一条消息接着跑。",
      en: "What was recorded is below; send another message to continue.",
    }, lang),
    detail: legacy || undefined,
    tone: "continue",
  };
}

/**
 * 一个还没人回答的问题，不会因为问它的进程死了就不存在了。
 *
 * 原判据是 `status in WAITING_STATUSES`，也就是把「有没有待答问题」锚在
 * **当前状态**上。而状态会漂：执行进程被部署重启掐掉之后 run 转
 * `stale_unknown`，`summary.pause` 还完整躺在库里（question / context /
 * options 一样不少），前端却整条丢掉——用户在记录里看见平台问"是否批准
 * 提交这个高危作业"，底下既没有可点的东西，也没有一句话说明它已经问不成了。
 *
 * 后端其实把两件事分开记了：`staleFromStatus` 是"它当时在等什么"，
 * `status` 是"它现在还活着吗"。判据要读前者，能不能操作才读后者——这跟
 * 「不可撤销的事发生后，别把判据建在会被覆盖的那条记录上」是同一条。
 *
 * 于是有三种呈现，都由这个函数一处决定：
 *   - 还在等         → 可作答
 *   - 等的时候被掐了 → 照原样呈现，但只读（HumanInputPrompt 早就写好了这一
 *                      支的文案，此前是死代码）
 *   - 从来没在等     → 不呈现
 */
/**
 * 这个 run 会不会呈现一个待答问题 —— 只看 run 本身，不需要 detail。
 *
 * 抽出来是因为有第二个消费方：对话流里那条与问句一字不差的 assistant 消息
 * 要被折叠掉（面板已经把它显示了一遍）。折叠的判据必须与"面板到底显不显示"
 * 是**同一个函数**，否则会出现「消息藏了、面板没出现」——把信息弄丢。
 */
export function runPauseView(run: ResearchRun): { pause: ChatPause } | null {
  const summary = run.summary;
  // 「在等什么」与「还活着吗」是两个问题，后端的 view 把它们分开答了：
  // `waitingOn` 在 run 被掐掉之后依然给出那个没人回答的问题（它不会因为
  // 问它的进程死了就不存在），`phase` 才回答还能不能作答。
  const waiting = run.view.waitingOn;
  if (!waiting || waiting.kind === "compute") return null;
  // ⚠️ 这里**只产出记录**，永不产出入口（2026-09-01）。
  //
  // 它曾经返回 `{pause, resumable}`，由调用点决定渲染成可点还是只读 ——
  // 于是"这张卡该不该能点"有了第二个判据，而真正的那个（后端算的答复入口）
  // 在别处。两个判据分叉时没有任何一层报错。
  //
  // 还在等、而且运行时还活着的那一个，归会话级的 `answer.via === "pause"`
  // 管；这里让开。判据只读这条 run 自己的 view，不需要谁从外面传一个
  // `hidePausePrompt` 进来告诉它"别画"。
  if (run.view.phase === "alive") return null;
  const pause = normalizeChatPause(summary?.pause);
  if (!pause) return null;
  return { pause };
}

export function canonicalRunPause(detail: RunDetailResponse) {
  return runPauseView(detail.run);
}

export function canonicalRunAttention(
  detail: RunDetailResponse,
  failure?: RunActivityTool["error"],
  lang: Language = "zh",
): CanonicalRunAttention | undefined {
  // 局面读后端那一次现算；下面几支照旧按局面分文案。
  const view = detail.run.view;
  const unresolved = view.phase === "interrupted"
    || view.outcome === "failed" || view.outcome === "cancelled";

  // ── 平台重启打断：能自愈的自然不显示，没自愈的要说清 ──────────────────
  //
  // 这里一度对 `failure.code === "app_server_restarted"` 一律 `return undefined`
  // （什么都不说），前提是"平台自己接上（startup_resume）"。但那个自动续跑
  // 后来被删了（main.py：`op=turn` 要伪造用户消息，wangd 反对）。前提没了，
  // 早退却留着 —— 于是被重启打断的 run 一个字都不显示，用户只看见一条停住的
  // 研究没有任何解释。这正是"最起码得显示正确报错"要修的那半（wangd 2026-08-24）。
  //
  // 现在的分工（A+B）：**continuous 档启动时自愈** → run 转回 `running`，
  // 根本走不到这里（下面的状态判据只认 stale/failed/cancelled），自然什么都
  // 不显示 —— 这才是"平台自己接上就别打扰用户"的正解。**没自愈的（assisted，
  // 或自愈失败）** 停在 `stale_unknown`，就该让 `persistedRunFailure` 把那条
  // 早已写好的 `app_server_restarted` 文案（"被平台重启打断了 / 从断点接着跑"）
  // 显示出来，而不是掉进通用兜底的"这一轮没跑完 / Status unknown"。
  //
  // 后端记下了具体原因就用它 —— 对**每一个**出问题的状态，不只是 failed。
  //
  // 原来 `stale_unknown` 有一段写死的文案，排在读取持久化 failure 之前，于是
  // 后端好不容易分辨出来的"平台重启打断了它，研究没问题"被一句
  // "The latest run status could not be confirmed" 盖掉 —— 前端的猜测盖住了
  // 后端的事实。同一个形状今天出现三次了。
  if (unresolved) {
    const persistedFailure = persistedRunFailure(detail, lang);
    if (persistedFailure) return persistedFailure;
  }

  if (view.phase === "interrupted") {
    return {
      // 中断不是失败（wangd 2026-08-18：「什么叫 stopped 呢？它按理说就不
      // 应该存在这么一个状态」）。而且旧文案教用户"刷新再决定要不要重发"——
      // 那是自动续跑上线前的建议，现在发下一条就接着跑。
      title: say(TURN_DID_NOT_FINISH, lang),
      message: say({
        zh: "执行进程在写下终态之前退出了 —— 已经做完的部分都还在。",
        en: "The execution process exited before recording a final state — everything already done is still here.",
      }, lang),
      recovery: say({
        zh: "发下一条消息就从断点接着跑，不会从头再来。",
        en: "Send another message and it continues from where it stopped, not from the beginning.",
      }, lang),
      tone: "continue",
    };
  }
  if (view.outcome === "failed") {
    return {
      title: failure?.title ?? say(TURN_DID_NOT_FINISH, lang),
      message: failure?.message ?? say(NO_USABLE_RESULT, lang),
      recovery: failure?.recovery ?? say({
        zh: "发下一条消息接着跑；技术细节留在执行历史里。",
        en: "Send another message to continue; the technical detail stays in the execution history.",
      }, lang),
      tone: "continue",
    };
  }
  // ⚠️ 这里曾经有两支「Your input is needed · Choose a response below」/
  // 「Permission is needed」（2026-09-02 部署后当场照出来，删掉）。
  //
  // 它们从**这条 run 的** `waitingOn` 断言"下面有个东西等你点"，而下面有没有
  // 东西是**会话**的答复入口说了算（`execution.answer`）——run 的 view 里
  // 根本没有 `answer` 这个键，所以这两支是在替一个它答不了的问题作答。
  //
  // 后果实测：只读的人（没有 drive 能力）打开这条会话，页面写着
  // 「Choose a response below」，下面什么都没有，输入框写着"你只能看"。
  // 这正是 09-01 那次锁死的形状 —— 第四个各自独立的答案。我上一版只收掉了
  // 三个，扫盘闸也没盖住它（它既不是 run 状态字面量，也不是第二个
  // <HumanInputPrompt>）：护栏也有视野盲区，补的方式是补判据。
  //
  // 「在等人」这件事本来就有归宿，而且只有一处：
  //   能答的人       → `answer.via === "pause"` 渲染那张能点的卡
  //   不能答的人     → `answer.via === "none"` 把原因写在输入框上
  //   问它的进程没了 → `PausedRecord` 按记录呈现
  // 运行记录不再重复宣布一次它管不着的事。
  if (view.outcome === "ok_with_warning") {
    return {
      title: "Research completed with notes",
      message: "A result was produced, but at least one recorded check needs review.",
      recovery: "Review the result and Execution history before publishing.",
      tone: "attention",
    };
  }
  return undefined;
}
