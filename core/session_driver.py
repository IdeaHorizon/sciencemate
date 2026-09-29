"""会话驱动层 —— CLI 与平台**共用同一份**。

## 为什么有这个模块

内核（agent_loop / executor / pause_driver）两边一直共用，分叉在**会话驱动
层**：`chat.py` 与 `platform_runtime.py` 是平级的两份实现。分叉的代价反复
兑现过：无人值守能力只长在 CLI 那份上，UI 的 autonomous 名不副实；记忆链
的三处 `if 平台: return` 存在三个月没人发现 —— 因为"平台走这条、CLI 走
那条"曾经是常态。

本模块把会话机制收成一份，前端只回答**真正因入口而异**的问题
（见 `SessionFrontend`）。两边真实的差异只有三条：

  1. **谁驱动 pause** —— CLI 有人在进程内 await stdin；平台的人在 HTTP
     那头，pause 必须逃逸出 turn、经 `answer()` 续跑。这是历史上最深的
     一次分叉根因（照抄 CLI 的 undriven_now() 判据会把人正要回答的 pause
     自动答掉 —— 它在平台上恒真）。
  2. **turn 的边界** —— 平台的 RPC 结果带 artifact delta，返回前要等后台
     子节点；CLI 要把提示符还给人，子节点事件异步冒泡。
  3. **事件出口** —— CLI 打屏，平台发 JSONL。

除这三条外**不允许有前端特有的逻辑** —— 新差异先回到这里问"这真的因入口
而异吗"，答不上来就是又在造第二套。

## 分层

  · `run_turn()`     —— 一轮的完整编排（机制）
  · `next_action()`  —— 无人值守续轮策略（读 state → 下一步干什么）
  · `SessionFrontend` —— 前端协议（上面那三个问题）

调用方按返回行事即可，**不许自己再判一次该不该继续** —— 判两次就会分叉，
这个仓库为此付过一个月的代价。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal


class AdditionalPauseRequired(RuntimeError):
    """自动作答链条中途撞上不能自动处理的暂停 —— 整链交回给人。"""


class PausePendingError(RuntimeError):
    """有 pause 挂着、而进程内没人能驱动它 —— 先答完再开新轮。

    只对 `ask_pause is None` 的前端成立：那种前端的 pause 由人从进程外作答
    （平台经 HTTP → answer()），此时开新轮就是两个驱动者抢同一个 pause。
    有进程内驱动者的前端（CLI）不受限 —— 人本来就是唯一驱动者。
    """


class SessionFrontend:
    """一个会话入口需要回答的全部问题。除此之外没有前端特有的逻辑。

    子类只该出现在真正的入口文件里（chat.py 的 REPL、platform_runtime 的
    --serve）。想在这里加第四个属性之前，先回答"这真的因入口而异吗" ——
    答案通常是否：那是该进 run_turn 的机制，写进前端就是下一个
    "机制存在但没接到路径"。
    """

    #: 进程内驱动 pause 的问答函数（拿 PauseEvent 返回答复）。
    #: None = pause 逃逸出 turn，由前端序列化给人、经 resume 路径续跑。
    #: 这正是 pause_driver.drive_pause_chain(ask_fn=…) 已有的注入点 ——
    #: 协议只是把它提到前端声明里，不发明第二条轴。
    ask_pause: Callable[[Any], Awaitable[str]] | None = None

    #: turn 返回前要不要等后台子节点跑完。
    #: 平台 True：RPC 结果带 artifact delta，提前返回=把半成品报给 App Server。
    #: CLI False：提示符要还给人，子节点完成/暂停经 child_event_sink 冒泡。
    waits_for_background: bool = False

    def emit(self, event: str, **fields: Any) -> None:
        """机制事件出口（CLI 打屏 / 平台发 JSONL）。默认丢弃。"""


@dataclass(frozen=True)
class TurnOutcome:
    #: run_loop 的状态词表（completed / paused / cancelled / void）。
    #: paused 只在 pause 逃逸出 turn 时出现（ask_pause=None 的前端）。
    status: str
    #: 用户可见回复；status=paused 时为 ""（文本随 resume 路径产生）。
    reply: str


async def wait_for_background(frontend: SessionFrontend, state) -> None:
    """等所有后台子节点落地（机械等，不轮询超时）。"""
    import asyncio

    from shared.tools import run_node as run_node_module

    while True:
        background = [
            task for task in list(run_node_module._BACKGROUND_TASKS)
            if not task.done()
        ]
        if not background:
            return
        frontend.emit(
            "background_wait",
            run_id=state.run_id,
            task_count=len(background),
        )
        await asyncio.gather(*background, return_exceptions=True)


async def auto_resume_pauses() -> tuple[str, str] | None:
    """autonomous 下就地消化非高风险暂停；返回 (status, final_text) 或 None。

    None = 没接管（未开 auto-approve / 当前暂停是高风险 / 没有待答暂停），
    调用方保持原来的 paused 语义。

    为什么存在：pause 逃逸型前端（平台）此前**没有任何**让 auto-approve 生效
    的路径 —— UI 上的 "Autonomous" 只影响自动 publish 版本，serve 的 turn 遇到
    pause 直接返回 paused，从不经过 pause_driver，AUTO_APPROVE_ENABLED 读都
    没被读过（E2E v17 实测：环境变量明明是 1，决策包照样停人）。CLI 侧同一个
    开关在 loop 内的 ask_fn 里就生效了 —— 差异不在策略，在 pause 的归宿。

    用同一套 drive_pause_chain + auto_approve_answer，不另起一份"挑推荐项"
    的实现；高风险由 NEVER_AUTO_APPROVE_TYPES 兜底，永远抛回给人。
    """
    from core import pause_driver
    from core.executor import finalize_run
    from core.pause import get_deepest_paused

    if not pause_driver.AUTO_APPROVE_ENABLED:
        return None

    pending = get_deepest_paused()
    if pending is None:
        return None
    if pause_driver._is_high_risk(pending.pause_event):
        return None      # 批量写入 / 外部作业提交等：永远抛回给人

    async def _auto(pause_event) -> str:
        if pause_driver._is_high_risk(pause_event):
            raise AdditionalPauseRequired
        return pause_driver.auto_approve_answer(pause_event)

    try:
        final_text = await pause_driver.drive_pause_chain(
            ask_fn=_auto, finalize_fn=finalize_run,
        )
    except AdditionalPauseRequired:
        return None      # 链条里遇到高风险 → 交回给人
    if get_deepest_paused() is not None:
        return None      # 还有没消化掉的（高风险）→ 保持 paused
    return "completed", final_text or ""


AUTONOMY_MODES: tuple[str, ...] = ("assisted", "autonomous", "continuous")


def autonomy_mode(state) -> str:
    """这个会话声明的档位。

    显式声明（`hook_state["autonomy_mode"]`，平台随每条请求下发、CLI 用 `/autonomy` 写）
    是真相源。没有显式声明时按类别列表推：`["*"]` = 连续，非空 = 自主，空 = 协作 ——
    这正是 CLI 表 `chat.AUTONOMY_SCOPES` 的约定。要显式声明的原因只有一个：自主档
    不预授权任何类别时列表是空的，与协作在 worker 眼里没有区别（2026-09-09 node20）。
    """
    hook_state = getattr(state, "hook_state", None) or {}
    mode = str(hook_state.get("autonomy_mode") or "")
    if mode in AUTONOMY_MODES:
        return mode
    declared = list(hook_state.get("authorized_risk_classes") or [])
    if "*" in declared:
        return "continuous"
    return "autonomous" if declared else "assisted"


def apply_autonomy(state) -> bool:
    """把这个会话**声明的自主档**施加到进程的运行时开关上。返回是否连续档。

    ## 一份声明，三个投影

    档位的真相源只有一个：`hook_state["authorized_risk_classes"]` —— 用户预授权
    了哪些高危类别。平台从 `project_configs` 下发它，CLI 用 `/autonomy` 写它。
    进程里那三个开关（`pause_driver.AUTO_APPROVE_ENABLED`、
    `dangerous_commands.PREAUTHORIZED_CATEGORIES` / `BYPASS_ENABLED`）全部由它
    **推导**，没有一个可以被单独设置。

    从前不是这样：档位在 state 里另有一个 `continuous_enabled` 布尔，而
    `run_unattended` 为了让续轮循环跑起来会把它设成 True —— 于是"用户选的档"
    和"这趟要不要自己续轮"共用一个名字，两个写者方向相反，谁最后写谁赢。
    那个名字已经删掉：续轮是 `continuous_loop`，档位是这里这份声明。

    ## 什么时候施加

    两处，都调这一个函数：

      · 每轮开跑前（`run_turn`）—— 状态是真相源，开关是它每轮重放的投影；
      · **声明变化的那一刻**（`platform_runtime.declare_authorization`）——
        一轮无人值守可以跑几十分钟不产生任何轮边界，只在轮首施加等于"这一轮
        剩下的决策点仍按开跑时那档走"。2026-08-23 实测：人在 13:20 切成连续，
        13:39 那个 post-node 决策照样停下问人。

    ## 连续 = 不出现任何需要人操作的行为

    wangd 2026-08-19：「连续模式下，就不应该出现任何需要人操作的行为，必须连续
    进行下去，除非彻底结束完整的研究了，才能停止。」

    这**高于** autonomous：后者的语义是"只在高危点停"，而连续档就是
    `autonomous + 预授权全部高危类别（"*"）` —— 用户已经逐类授权过了，再停下来
    问是自相矛盾。所以连续档下高危确认也自动放行；autonomous 档一个字不动。
    """
    from core import pause_driver
    from shared.lib import dangerous_commands as dc

    hook_state = getattr(state, "hook_state", None) or {}
    declared = list(hook_state.get("authorized_risk_classes") or [])
    mode = autonomy_mode(state)
    continuous = mode == "continuous"
    dc.set_preauthorized_categories(declared)
    # 三档对「决策卡」的态度：协作每张停；自主与连续都自动放行非高危的（wangd
    # 2026-09-09 拍板：自主档也自动放行非高危决策卡）；高危确认只有连续档绕行。
    pause_driver.set_auto_approve(mode != "assisted", 0 if continuous else 5)
    dc.set_bypass_mode(continuous)
    return continuous


async def run_turn(
    state,
    harness,
    messages,
    llm,
    user_text: str,
    *,
    frontend: SessionFrontend,
) -> TurnOutcome:
    """一轮会话的完整编排 —— CLI 与平台都调这一份。

    脊柱：守卫 → run_loop → 回复整形 → （按前端）等后台 → pause 归宿 →
    错误路径（checkpoint + 存盘）。前端只经 `SessionFrontend` 参与，
    不在这里写 `if 平台:` —— 那种分支正是本模块要消灭的东西。

    错误路径对齐平台原实现（异常也要 checkpoint + 存对话再抛）——CLI 此前
    没有这一层，跑挂就只剩终端一行报错，工作区停在半路没有提交点。
    """
    import chat as _chat        # 编排暂借 chat 的既有实现，逐步搬空
    from core.pause import get_deepest_paused

    # 守卫：pause 挂着时，只有进程内有人能驱动才允许开新轮（见异常文档）。
    # 平台在 RPC 入口用同一判据先翻译成协议错误（pause_pending），这里是
    # 面向未来前端的 fail-loud 兜底，不是第二次决策。
    # 连续模式的运行时开关在这里统一施加 —— **每一轮都施加一次**。
    #
    # 这是 2026-08-19 那个 bug 的结构解：开关原本散在各前端自己的启动路径里
    # （`chat._set_continuous_mode` 调 `set_auto_approve` / `set_bypass_mode`），
    # 平台一次都没调，于是 UI 上白纸黑字的「连续」在平台上只兑现了一半 ——
    # 高危类别预授权生效了，而「要不要停下来问人」那个开关根本没人打开。
    #
    # 名单式的修法（"平台侧也补上这两行"）挡不住下一个开关。所以收进脊柱：
    # 状态里怎么写的，每一轮开跑前照着施加，两个前端都不需要记得调。
    apply_autonomy(state)

    if get_deepest_paused() is not None and frontend.ask_pause is None:
        raise PausePendingError(
            "answer the pending pause before starting another turn")

    try:
        loop_result = await _chat._run_one_turn_raw(
            state, harness, messages, llm, user_text)
        status = str(loop_result.status or "completed")
        reply = ""
        if status == "paused" and frontend.ask_pause is None:
            # pause 逃逸出 turn：前端把它序列化给人，resume 路径续跑。它的归宿
            # （给人，还是按档位在进程内消化）只在前端的操作出口判一次
            # （platform_runtime._operation_end），这里不判。
            pass
        else:
            reply = await _chat._post_loop_reply(
                loop_result, harness, state, messages, llm,
                ask_pause=frontend.ask_pause,
            )
            if status == "paused":
                status = "completed"    # 进程内驱动完了，turn 不以 paused 结束

        if frontend.waits_for_background:
            await wait_for_background(frontend, state)
            if get_deepest_paused() is not None:
                # 等待期间子节点停下来问人了：以 paused 交给操作出口，归宿在那里判。
                status, reply = "paused", ""
        return TurnOutcome(status=status, reply=reply)
    except BaseException:
        # 异常不是"这轮没发生"：工作区可能已被改了一半。先要一个 failed
        # checkpoint（有 worktree 才有意义，函数自己判断），再把对话存盘 ——
        # 崩溃后重开会话能接着聊，而不是丢掉整段。
        try:
            from core.project_workspace import request_completion_checkpoint

            request_completion_checkpoint(state, "failed")
        except Exception as checkpoint_exc:
            state.append_transcript(
                "workspace_checkpoint_request_failed",
                error=f"{type(checkpoint_exc).__name__}: {checkpoint_exc}"[:500],
            )
        _chat._save_conversation(state, messages)
        raise


@dataclass(frozen=True)
class SessionAction:
    kind: Literal["wait", "prompt", "stop"]
    #: kind=prompt 时要发给模型的话
    prompt: str = ""
    #: kind=prompt 时发送前的退避秒数（故障期间别高速烧请求）
    delay_s: float = 0.0
    #: kind=wait 时等到的事件描述（供上层记账/展示）
    waited: dict[str, Any] | None = None
    #: 为什么做这个决定 —— 一律带上，否则事后无法复盘
    reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


async def next_action(
    state,
    reply: str,
    *,
    reason: str,
    allow_child_wait: bool = True,
) -> SessionAction:
    """算出这一轮之后该干什么。纯策略，不产生任何 I/O 副作用。

    顺序刻意如此（每一步都是事故换来的）：

    1. **有 pause 挂着就不抢方向盘** —— 除非一个有人管的都没有。抢了会造出
       第二个控制平面（false-PROCEED 事故的第一环）。
    2. **先机械等子节点，再算提示词** —— 醒来时进度指纹已经变了，stall 计数
       和"一字不差重复"检测拿到的是等待**之后**的世界。反过来的话，那两个
       症状层检测会咬到忙等自己的尾巴（E2E-4：writing 其实 5 分钟就跑完了，
       项目却空转 21.9 小时）。
    3. **check-in 是 agent 自己声明的**，本轮声明本轮生效 —— 放到下一轮解析
       会晚一拍（它说"一小时后叫我"，这一轮还是按默认等）。
    """
    import chat as _chat        # 策略实现暂借 chat 的既有实现，逐步搬空

    if not _chat._continuous_running(state):
        return SessionAction(kind="stop", reason="continuous_not_running")

    # 1) pause 归属
    from core.pause import list_paused
    from core.pause_driver import resolve_orphan_pauses, undriven_now

    if list_paused():
        orphans = undriven_now()
        if not orphans:
            return SessionAction(kind="stop", reason="live_pause_pending")
        await resolve_orphan_pauses(state, orphans)

    # 2) check-in（本轮声明本轮生效）
    check_in, note = _chat._continuous_check_in(reply)
    state.hook_state["continuous_check_in_s"] = check_in or 0
    if note:
        state.hook_state["continuous_check_in_note"] = note

    # 3) 先等，后算
    waited = None
    if allow_child_wait:
        waited = await _chat._wait_for_child_progress(state)
        if not _chat._continuous_running(state):
            return SessionAction(kind="stop", reason="stopped_while_waiting")

    prompt, delay = _chat._continuous_followup(state, reply, reason=reason)
    if prompt is None:
        # 原来这里只回一个 `followup_declined` —— 一个**不含任何内容**的标签。
        # 六种停机（complete / no_delta_repeat / stall_livelock / repeated_turn_errors
        # / repeated_producing_failure / system_node_pingpong）在这个出口全被压成
        # 同一个词，而真正的理由就在 hook_state 里躺着没人取。上层（含平台）拿到
        # 它只能说"循环停了"，说不出为什么 —— yuankk 那次从 8:30 起没有新迭代，
        # 界面上什么也没有，根子有一半在这个字上。
        phase = str(state.hook_state.get("continuous_phase") or "")
        detail = str(state.hook_state.get("continuous_abort_reason") or "")
        return SessionAction(
            kind="stop",
            reason=("research_complete" if phase == "complete" else "loop_aborted"),
            waited=waited,
            extra={"phase": phase, "detail": detail},
        )

    # 空轮重放 sentinel 靠**逐字节等值**识别，任何装饰都会破坏驻定性。
    # 下面两处装饰是**前置**的，会把 `_CONTINUOUS_INTERNAL_PREFIX` 挤离句首 ——
    # `_is_continuous_turn` 因此按"哨兵在不在"判、不看位置（issue #744）。
    # 要加新装饰随意，**别让哨兵从文本里消失**。
    skip_delay = False
    if prompt != _chat._VOID_RETRY_PROMPT:
        if waited:
            prompt = f"{_chat._child_wait_note(waited)}\n\n{prompt}"
        pending_note = state.hook_state.pop("continuous_check_in_note", None)
        if pending_note:
            prompt = f"⚠️ {pending_note}\n\n{prompt}"
            skip_delay = True

    # 停靠的理由归停靠，不归上一轮（#1083 第 3 条）。
    #
    # `reason` 这个入参是**上一轮怎么结束的**（"completed"）。它一路带到
    # `worker_parked.why`，于是「因 blocked 停靠、30 分钟后复查」在公开事件上
    # 写着 `why="completed"`。理由字段说反话比没有理由更坏：读的人据此去查一个
    # 不存在的完成。这一轮真的是去停靠的话，理由就该是停靠的理由。
    park = state.hook_state.get("continuous_park")
    if isinstance(park, dict) and park.get("reason"):
        return SessionAction(
            kind="prompt",
            prompt=prompt,
            delay_s=0.0 if skip_delay else float(delay or 0.0),
            waited=waited,
            reason=f"parked_{park['reason']}",
            extra={k: v for k, v in park.items() if k != "stated"},
        )
    return SessionAction(
        kind="prompt",
        prompt=prompt,
        delay_s=0.0 if skip_delay else float(delay or 0.0),
        waited=waited,
        reason=reason,
    )
