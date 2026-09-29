"""「这个会话/这条 run 此刻是什么局面」—— 一个答案，由后端现算，前端只渲染。

## 为什么要有这个模块（2026-08-27 那次三矛盾并存）

现场：顶部徽章「Queued」+ 转圈「Starting the Project assistant」+ 一个按了回
409「Nothing is running」的停止按钮。三个说法互相矛盾，而后端**早就算对了**：
`sessions.py` 的 `executionState` 只看顶层 run、按 `started_at` 取当前一轮、
再经 `run_liveness.observed_status_map` 活性纠偏 —— 完全正确。

错在前端**把它扔掉又自己算了一遍**：`session-adapter.ts` 从 `/runs` 列表里按
`updatedAt` 倒序取第一条，而子 run 的 `status` 是**故意不被生命周期事件驱动**
的（否则子节点终态会关掉父命令的 attempt），于是那些永远停在 `queued` 的子
run 只要被碰一下 `updated_at` 就赢下排序，成为整个会话的"状态"。

后端为这个病修过两次（先排除子 run、再把 `updated_at` 换成 `started_at`），
两次都没跨过网线，因为客户端在重算。**同一个问题两个答案，分叉时两边都不
报错**（[[feedback_one_truth_source_per_question]]）。

前端一共有 **9 套手写状态集合**（ACTIVE_STATES / ATTENTION_STATES×2 /
LIVE_RUN_STATES / UNRESOLVED_RUN_STATES / TERMINAL_STATES /
INTERRUPTED_PARENT_STATUSES / WAITING_STATUSES / IN_FLIGHT），5 个各自不同输入
的"在跑吗"布尔，3 个独立 spinner。后端在 `models/execution.py` 把自己的五份
名单收成了一份并用测试钉住；客户端从没做过这件事。

## 契约

一个 view，三处下发（SessionOut / RunResponse / SSE end 帧），**互斥且完备**：

    alive        有东西在跑或在等（waitingOn 说在等什么，null = 在动）
    ended        走到了定义好的终点（outcome 说是哪一种）
    interrupted  在动的途中运行时没了，没走到终点

判据全部来自 `models/execution.py` 那份唯一的语义分区，不新写名单。

## canStop 不是"看起来像在跑"

它就是 `/stop` 端点自己的准入条件，**同一个函数**（`has_live_runtime`）。
两边共用一个谓词，"按钮亮着但按下去 409" 在构造上不可能发生 —— 这正是
[[feedback_call_it_dont_reimplement_it]]：让一方调另一方，而不是各写一份。

## `answer`：答复入口只有一个答案（2026-09-01）

`canStop` 享受过的那条纪律，`canSend` 从来没有。它曾经是一个**孤立的布尔**：

    canSend = may_drive and (waiting is None or phase != alive)

"在等人回答时输入框让位给那张卡片" —— 对，但**让位给谁**这件事，它答不出来。
卡片在不在场由完全另一段代码（会话 payload 的 `pendingApproval` + 前端自己那道
`status === "waiting_human"` 名单）决定。三个各自独立的答案，一致时看不出问题，
分叉时就是：

    2026-09-01 现场（node20，会话 de9bd47f，卡了 6 小时）
      后端：canSend=false —— "输入框关掉，答案走卡片"
      后端：pendingApproval 有，5 个选项齐全
      前端：ChatRunActivity 拿到 status="alive"（view 的 phase 被当状态传），
            不在 {waiting_human, waiting_permission, retrying, error, failed}
            名单里 → return null → **一张卡都没画**
      用户：屏幕上写着「Your input is needed · Choose a response below」，
            下面什么都没有，输入框灰着写「先回答上面的问题」。
      三边都没有报错，因为三边各自都是自洽的。

所以这里不再下发"能不能发"，而是下发**答案从哪儿进来**，并且把要渲染的那张卡
**装在同一个字段里**：

    {"via": "composer"}                    输入框可用
    {"via": "pause",  "pause": {...}}      卡片是入口，卡片本体就在这儿
    {"via": "none",   "reason": "..."}     谁都不能动（只读 / 归档 / 非驾驶者）

「输入框关了但卡不在」这个组合**在数据结构里表达不出来** —— `via == "pause"`
必然带着 pause 本体，而没有 pause 本体时唯一的合法取值是 `composer`。不需要
两边都记得对齐，也不需要谁去写一道"检查它们一致"的闸：不一致这件事没有地方
可以发生（[[feedback_mechanism_seams]] 的"落地即死"反面）。

不变量，由 `test_answer_affordance.py` 钉死：

    via == "pause"  ⟺  pause 非空
    may_drive       ⟹  via != "none"      能驱动的人永远有入口

## 两个 view，不是一个

`build_session_view()` 比 `build()` 多一个 `answer` —— 因为"答复入口"是**会话**
的属性，不是某条 run 的。`RunResponse.view` 里**没有** `answer` 这个键，于是
"从一条历史 run 上渲染出一个能点的卡片"在构造上不可能：那份数据里根本没有
可点的东西。历史 run 停在哪个问题上，由它自己的 `summary.pause` 如实记着，
按记录呈现，永远只读。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.models.execution import (
    AWAITING_HUMAN_RUN_STATUSES,
    CLEANLY_FINISHED_RUN_STATUSES,
    Run,
    RunStatus,
    TERMINAL_RUN_STATUSES,
)

PHASE_ALIVE = "alive"
PHASE_ENDED = "ended"
PHASE_INTERRUPTED = "interrupted"

#: 答复入口的三个取值。互斥且完备 —— 见模块头 `answer` 一节。
VIA_COMPOSER = "composer"
VIA_PAUSE = "pause"
VIA_NONE = "none"

#: `via == "composer"` 但**本该**是卡片时带上的诚实说明：我们在等人回答，
#: 却拿不出那个问题本身，所以把输入框还给人，而不是把人锁死。
#:
#: 这不是兜底文案，是**这条路径存在的理由**：唯一比"给错入口"更坏的是
#: "一个入口都不给"。degraded 会被前端如实说出来，不静默。
DEGRADED_NO_PAUSE_BODY = "pause_body_unavailable"

#: 终态 → 用户看到的结局。键覆盖 TERMINAL_RUN_STATUSES 全部取值，由
#: `test_every_terminal_status_has_an_outcome` 钉住：新增一个终态而不给它
#: 结局，测试立刻红（同 models/execution.py 那份分区的做法）。
_OUTCOME: dict[str, str] = {
    RunStatus.COMPLETED.value: "ok",
    RunStatus.COMPLETED_WITH_WARNING.value: "ok_with_warning",
    RunStatus.INCOMPLETE.value: "incomplete",
    RunStatus.FAILED.value: "failed",
    RunStatus.CANCELLED.value: "cancelled",
}

#: 状态词是**界面上的字**，所以它跟着这个人的界面语言走。两种语言并排写着，
#: 漏一种在这里就看得见，而不是等英文界面上冒出一句中文。
_LABEL: dict[str, dict[str, str]] = {
    "ok": {"zh": "已完成", "en": "Completed"},
    "ok_with_warning": {"zh": "完成，有警告", "en": "Completed with warnings"},
    "incomplete": {"zh": "没跑完", "en": "Did not finish"},
    "failed": {"zh": "失败", "en": "Failed"},
    "cancelled": {"zh": "已取消", "en": "Cancelled"},
}

_PHASE_LABEL: dict[str, dict[str, str]] = {
    "interrupted": {"zh": "中断", "en": "Interrupted"},
    "ended_unknown": {"zh": "已结束", "en": "Finished"},
    "running": {"zh": "正在跑", "en": "Running"},
}

#: 「这个会话还没跑过任何一轮」。它曾经是下面那个分支里一句写死的 "Ready" ——
#: 不在任何词表里，于是界面全面中文化之后，**只剩它还是英文**（2026-09-16 走查
#: 时在会话标题旁看见的就是它）。状态词只有一张表，新的也要进来。
_IDLE_LABEL: dict[str, str] = {"zh": "待命", "en": "Ready"}

_WAITING_LABEL: dict[str, dict[str, str]] = {
    "human": {"zh": "等你回答", "en": "Needs your answer"},
    "permission": {"zh": "等你授权", "en": "Needs your approval"},
    "compute": {"zh": "等算力", "en": "Waiting for compute"},
}


def _say(phrase: dict[str, str], lang: str) -> str:
    """缺哪种语言就退回中文 —— 少一句翻译不该让整个 view 拼不出来。"""
    return phrase.get(lang) or phrase["zh"]


def has_live_runtime(project_id: str, session_id: str) -> bool:
    """这个会话此刻有没有一个活的 harness 进程在手上。

    **`/stop` 的准入条件就是这一句**，view 的 `canStop` 也是这一句。共用一个
    函数是有意的：按钮的可见性与端点的准入若各写一份，就会重演"按钮亮着、
    按下去 409"。
    """
    from app.services.harness_sessions import harness_session_manager

    return harness_session_manager.live_binding(project_id, session_id) is not None


def phase_of(observed_status: str) -> str:
    """三态归类。输入是**现算后**的状态（run_liveness.observed_status_map 的结果），
    不是库里那一行的原值 —— 原值会说一条早就没主的 run 还在 running。
    """
    if observed_status in {s.value for s in TERMINAL_RUN_STATUSES}:
        return PHASE_ENDED
    if observed_status == RunStatus.STALE_UNKNOWN.value:
        return PHASE_INTERRUPTED
    return PHASE_ALIVE


def _waiting_on(observed_status: str, *,
                was_waiting_on: str | None = None) -> dict[str, Any] | None:
    """在等什么。

    ⚠️ 「在等什么」与「还活着吗」是**两个问题**。一个还没人回答的问题，不会
    因为问它的进程死了就不存在了：run 被部署重启掐掉后转 `stale_unknown`，
    而 `summary.pause` 连同 question / context / options 一样不少地躺在库里。
    所以 `was_waiting_on`（后端记的 `staleFromStatus`）在被打断时仍然产出
    waitingOn —— 由 `phase` 单独回答"还能不能作答"。

    这里**只答"在等哪一类"**。呈递的身份（offer_id）曾经也挂在这上面，
    那是第二份抄件：它现在只长在 `answer.pause` 里 —— 卡片和卡片的身份是
    同一个东西，没有理由分两处发。
    """
    if observed_status == RunStatus.STALE_UNKNOWN.value and was_waiting_on:
        observed_status = was_waiting_on
    if observed_status in {s.value for s in AWAITING_HUMAN_RUN_STATUSES}:
        kind = (
            "permission"
            if observed_status == RunStatus.WAITING_PERMISSION.value
            else "human"
        )
        return {"kind": kind}
    if observed_status == RunStatus.WAITING_COMPUTE.value:
        return {"kind": "compute"}
    return None


def _label(phase: str, outcome: str | None, waiting: dict[str, Any] | None,
           lang: str = "zh") -> str:
    if phase == PHASE_INTERRUPTED:
        return _say(_PHASE_LABEL["interrupted"], lang)
    if phase == PHASE_ENDED:
        return _say(_LABEL.get(outcome or "", _PHASE_LABEL["ended_unknown"]), lang)
    if waiting is None:
        return _say(_PHASE_LABEL["running"], lang)
    return _say(_WAITING_LABEL[waiting["kind"]], lang)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def answer_affordance(
    *,
    phase: str,
    waiting: dict[str, Any] | None,
    pause: dict[str, Any] | None,
    may_drive: bool,
    readonly_reason: str | None = None,
    readonly_until: str | None = None,
) -> dict[str, Any]:
    """**答案从哪儿进来** —— 整个平台只有这一处回答它。

    纯函数，不碰 db、不碰进程：真实历史回放能直接喂进来，而这正是这次事故
    需要的判据（现场的那份 payload 一放进来就该红）。

    三个取值的顺序是有意的：

    1. 不能驱动 → `none`。只读的人看得见问题，但不该看见一个点了没用的按钮。
    2. 在等人 / 在等授权，且运行时还在 → 卡片是入口。**卡片本体一起给**，
       所以"说了走卡片却没有卡片"不可能被表达出来。
       - 拿不到卡片本体（老会话、事件里只有一句 prompt、上游削过字段）：
         把输入框还给人，并如实标上 `degraded`。**绝不**在这里回一个既关了
         输入框又没有卡片的组合 —— 那是 2026-09-01 那次锁死的全部内容。
    3. 其余 → 输入框。

    "在等算力"（compute）不算在等人：没有人能替算力回答，输入框照常可用
    （人此刻仍然可以插话、改方向、叫停）。
    """
    if not may_drive:
        # `until` = 这个判断自己会过期的时刻（别人的驾驶权租约到点）。给出来，
        # 客户端就能安排"到点再问一次"，而不是自己把租约再算一遍 —— 决定
        # 「什么时候再问」是客户端的事，决定「答案是什么」不是。
        blocked: dict[str, Any] = {
            "via": VIA_NONE,
            "reason": readonly_reason or "You can view this Session only.",
        }
        if readonly_until:
            blocked["until"] = readonly_until
        return blocked
    waits_on_a_human = (
        waiting is not None
        and waiting.get("kind") in {"human", "permission"}
        and phase == PHASE_ALIVE
    )
    if waits_on_a_human:
        if pause:
            return {"via": VIA_PAUSE, "pause": pause}
        return {"via": VIA_COMPOSER, "degraded": DEGRADED_NO_PAUSE_BODY}
    return {"via": VIA_COMPOSER}


def build_session_view(
    run: Run | None,
    *,
    pause: dict[str, Any] | None,
    readonly_reason: str | None = None,
    readonly_until: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """会话的局面 = run 的局面 + **答复入口**。

    入口是会话的属性（"我现在该往哪儿说话"），不是某条 run 的，所以它只长在
    这一个函数的输出里。`build()` 给的那份没有 `answer` 键 —— 拿一条历史 run
    渲染出可点的卡片，因此在构造上不可能。
    """
    view = build(run, **kwargs)
    view["answer"] = answer_affordance(
        phase=view["phase"],
        waiting=view["waitingOn"],
        pause=pause,
        may_drive=bool(kwargs.get("may_drive", True)),
        readonly_reason=readonly_reason,
        readonly_until=readonly_until,
    )
    return view


def build(
    run: Run | None,
    *,
    observed_status: str | None,
    failure: dict[str, Any] | None = None,
    live_runtime: bool = False,
    may_drive: bool = True,
    was_waiting_on: str | None = None,
    lang: str = "zh",
) -> dict[str, Any]:
    """把一条 run 的现算状态拼成 view。纯函数 —— 输入全靠参数，好让真实历史
    回放能直接喂进来（回放没有进程，也没有 db）。

    这里**没有** `answer`：答复入口是会话的属性，见 `build_session_view`。
    这里也**没有** `canSend`：它曾经是一个答不出"让位给谁"的孤立布尔，
    2026-09-01 把用户锁死 6 小时的正是它与另外两处判据的分叉（见模块头）。

    `run is None`：这个会话还没跑过任何一轮。它既不是 alive 也没有结局，
    按 `ended / outcome=None` 呈现（"没有在跑的东西，也没有需要看的结果"）。
    这是唯一一处 phase 与 outcome 都不带信息的组合。
    """
    if run is None:
        return {
            "phase": PHASE_ENDED,
            "waitingOn": None,
            "outcome": None,
            "error": None,
            "canStop": False,
            "label": _say(_IDLE_LABEL, lang),
            "runId": None,
            "since": None,
        }

    # `observed_status` **必须**由调用方给出（run_liveness 现算的那个）。
    # 这里曾经有一句 `or run.status` 的兜底 —— 那是把判据建回投影列上：
    # 一条运行时早就没了的 run，库里那行还写着 running，兜底会让 view 说它
    # 「在跑」。防复发闸（test_schema_classification）就是照着这一句红的。
    if not observed_status:
        raise ValueError(
            "execution_view.build needs the computed status "
            "(run_liveness.observed_status_map), not the stored column"
        )
    status = observed_status
    phase = phase_of(status)
    waiting = _waiting_on(status, was_waiting_on=was_waiting_on)
    outcome = _OUTCOME.get(status) if phase == PHASE_ENDED else None

    # 出错信息只在"结局不干净"或"被打断"时呈现。干净收尾的 run 即使 summary
    # 里留着上一次重试的失败见证，也不该被当成结论摆给用户。
    show_error = phase == PHASE_INTERRUPTED or (
        phase == PHASE_ENDED and status not in {s.value for s in CLEANLY_FINISHED_RUN_STATUSES}
    )

    return {
        "phase": phase,
        "waitingOn": waiting,
        "outcome": outcome,
        "error": failure if (show_error and failure) else None,
        # 能不能停，只问「现在有没有活体运行时」+「这个人有没有资格驱动」。
        # 不看状态长相：一条 status 说 running 而进程早没了的 run，停不了。
        "canStop": bool(live_runtime and may_drive),
        # 「能不能发」不在这里 —— 见模块头：那是 `answer` 回答的问题，而且它
        # 必须和"让位给谁"一起回答，否则就会重演把人锁在中间的那 6 小时。
        "label": _label(phase, outcome, waiting, lang),
        "runId": run.id,
        "since": _iso(run.started_at or run.created_at),
    }
