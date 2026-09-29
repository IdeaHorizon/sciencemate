"""agent loop：LLM ↔ 工具调用循环，直到模型自己决定结束。

每一轮：
  1. 跑 on_turn_start hooks，把它们想 inject 的 system 消息加到 messages
  2. 把当前消息历史（连同工具 schema）发给 LLM
  3. 跑 on_llm_response hooks（只观察）
  4. 如果 LLM 返回了 tool_calls，通过 tool_registry 调度执行，并把结果作为 tool 消息追加
  5. 跑 on_turn_end hooks，把它们想 inject 的 system 消息加到 messages
  6. 重复，直到 LLM 不再调用工具，或达到 max_turns
  7. 跑 on_end hooks

每一步都会写入 transcript（state.append_transcript）。

Hook 系统让 owner 在不动 agent_loop 本身的前提下扩展行为。详见
core/loop_hooks.py。
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time as _time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import loop_hooks, tool_registry
from . import summarizer as _summarizer
from . import context_view as _ctx_view
from . import tool_call_cache as _tool_cache

#: 工作集（工具结果活副本）最多占 context window 的多少。
#:
#: 工具结果的预算不再是固定份额 —— issue #710：0.45×窗（这里）、0.5×窗
#: （清除层）与真实剩余空间三个数各自为政，三层全绿、网关 400。预算的唯一
#: owner 是 `summarizer.derived_tool_budget_bytes`：窗口减去其余一切现算。
#: 0.45 收编为 `summarizer.WORKING_SET_SHARE_CAP`，降级为上限。
from .harness import NodeHarness
from .llm import (
    LLMClient,
    LLMHTTPError,
    LLMMessage,
    context_overflow_numbers,
    framework_notice,
    is_framework_notice,
)
from .pause import (
    PausedRunContext,
    PauseEvent,
    clear_pause,
    register_pause,
)
from .state import State

log = logging.getLogger("agent_loop")


# ── 进度 sink（P1-8：turn 进行中可视化）────────────────────────────────────
# chat.py 把一个回调注册进来；agent_loop 在每次工具调度后调用它打一行 dim 进度。
# 进程级全局（一个 chat session 一个进程）。只用于 UI，绝不影响控制流：sink 抛
# 异常被吞。子节点（run_node 起的 child run）的工具调用也会经过这里 → 长 child
# run 不再是"处理中"黑箱（实测痛点 A7）。默认 None = 无 UI（dogfood / 单测静默）。
_PROGRESS_SINK = None



# ── 循环为什么停：两件事，两个名字（2026-08-11）─────────────────────────────
#
# 本模块的注释一直写着：
#     "completed"：LLM 自己决定停止（不再调工具）**或达到 max_turns**
# 两件完全不同的事记成同一个状态，下游就分不出"做完了"和"被切断了"。
#
# 代价（E2E v23 实测，空转三轮 ≈ 三小时）：experiment 在第 40 轮被截断，
# 模拟全跑完但没做 MSD 分析；框架记 `final_status: completed`，决策层看不出
# 交付物残缺，于是在 retry_reviewer 和 retry_curator 之间来回，一轮一小时。
#
# 这里只**记事实**，不改判决 —— 轮次用尽不是失败（已经跑完的模拟是真产出），
# 它只是**没做完**。判决仍归 reviewer / 决策层，但它们现在看得见。
STOP_FINISHED = "finished"          # 模型自己不再调工具
STOP_MAX_TURNS = "max_turns"        # 撞上轮次上限被切断


def set_progress_sink(fn) -> None:
    """注册进度回调 fn(state, tool_name, args)。传 None 清除。"""
    global _PROGRESS_SINK
    _PROGRESS_SINK = fn


def _emit_progress(state: State, tool_name: str, args: dict) -> None:
    if _PROGRESS_SINK is None:
        return
    try:
        _PROGRESS_SINK(state, tool_name, args)
    except Exception:
        pass


# 永远启用的 hook —— 即使 owner 没写在 harness.loop_hooks 里
# memory_delta 注入新 memory；kb_delta 注入新 KB 条目（concept/claim/synthesis/evidence）
# turn_budget always-on（2026-08-11）：每个节点都有轮次预算，漏开就是
# "被切断还不知道为什么"。走 opt-in 还意味着要逐个改节点 yaml —— 而
# 6 个 producing 节点归 owner 管，框架不该靠他们逐个补才生效。
# `memory_onboarding` 是**始终启用**的，不是 producing 默认 —— 上一代
# `pre_run_briefing` 挂在 producing 默认里，而 `_curator` / `_orchestrator`
# 因为下划线前缀被排除在外，于是调度和策展这两个最需要"本组已知什么"的
# 节点从来收不到任何记忆（2026-08-21 审计实测）。记忆没有节点例外。
#
# `org_orientation` 同理（2026-09-24）：它自己按节点判给谁（立问题 / 定方向的那几个），
# 却要靠各节点的 harness.yaml 挂上 —— 结果只有 `_orchestrator` 挂了，hypothesis 名单上有、
# 一次都没收到过「本组已知」。送达是框架的事，不是节点自己选的。
_ALWAYS_ON_HOOKS = ["memory_delta", "kb_delta", "highrisk_confirm",
                    "turn_budget", "memory_onboarding", "law_review_gate",
                    "org_orientation"]

# v3.1：producing 节点（node_type 不以 "_" 开头）默认启用的 hook。
# owner 可在 harness.yaml 用 loop_hooks_disable: [...] 显式关。
# 目前为空：唯一的成员 `pre_run_briefing` 已被 `memory_onboarding` 取代，
# 而后者对所有节点始终启用（见上）。机制保留 —— 它本身是对的。
# （reflection 仍是 opt-in —— token 代价大，按节点选择性开）
_DEFAULT_PRODUCING_HOOKS: list[str] = []

# issue #223：finish_reason=length 且没发出 tool_call 时，最多再给几次机会
# （每次都注入"只回一个工具调用或一句短话"的强约束）。env 可调。
_TRUNCATION_RETRIES = int(os.environ.get("HARNESS_TRUNCATION_RETRIES", "2") or 0)
# 截断重试时把输出预算翻倍（E2E-5b 实测根因）：原来的恢复只注入一段"这轮请只做
# 一件事"的提示词，**但绑定约束是那个数字，提示词改不动它**。现场三轮
# completion_tokens 精确等于 12000、content=0、tool_calls=0 —— 同样的输入配同样
# 的上限，必然得到同样的结果，注两次提示词也没用。
# 这就是调度层那条"没有增量就别重试"的不变量在 LLM 这一层的缺席：重试必须带上
# **机械层面的真变化**，不能只改措辞。
_TRUNCATION_BUDGET_FACTOR = float(
    os.environ.get("HARNESS_TRUNCATION_BUDGET_FACTOR", "2") or 2)
_TRUNCATION_BUDGET_CEILING = int(
    os.environ.get("HARNESS_TRUNCATION_BUDGET_CEILING", "32768") or 32768)

# ── 空轮事务性（E2E-5a 事故：22 轮空响应、每轮 +2118 token 单调发散）─────────
# 空轮 = 无工具调用 + 无正文的一轮。回滚后重试的退避序列（秒）；容量型空轮
# （压缩会触发）不等待 —— 那是一次更小的、不同的尝试。总计约 10 分钟，用尽后
# 交上层：节点 run 归 infra 账重派，continuous orchestrator 由 chat 层无限驻定重放。
_VOID_RETRY_DELAYS = tuple(
    float(s) for s in (os.environ.get(
        "HARNESS_VOID_RETRY_DELAYS", "20,40,80,160,320") or "20,40,80,160,320"
    ).split(","))
# 容量证据门槛：completion ≤ 此值才算真解码退化（与 llm.py 的
# _DEGENERATE_COMPLETION_MAX 同义，值上放宽一点容忍个别噪声 token）。
_VOID_COMPLETION_MAX = int(os.environ.get("HARNESS_VOID_COMPLETION_MAX", "8") or 8)

# ── 发送前不变量 + context 超限 400 自愈（issue #710）─────────────────────────
#: 输出预算被发送闸收缩时的下限：再小的一轮也得装得下一次像样的工具调用。
_MIN_OUTPUT_TOKENS = int(os.environ.get("HARNESS_MIN_OUTPUT_TOKENS", "4096") or 4096)
#: context 超限 400 的有界重试：每次重试前校准已被服务端权威数字修正过，
#: 连撞两次以上 = 修正不收敛，是结构性问题，抛回上层归因。
_CTX400_MAX_RETRIES = 2



async def _void_backoff_sleep(state, delay_s: float) -> None:
    """空轮退避等待 —— 1s 切片，kill_signal 秒级生效（不吞信号，留给轮初检查）。"""
    end = _time.monotonic() + delay_s
    while _time.monotonic() < end:
        await asyncio.sleep(min(1.0, max(0.0, end - _time.monotonic())))
        if state.hook_state.get("kill_signal"):
            return


# ── 请求可重建性（issue #734，抄 DSH「模型看到的一切都在日志里」）─────────
#
# 不变量（对无压缩、无驱逐的 run 机械可验，测试守护）：**每一轮请求的
# messages 都能仅凭 transcript 逐字节重建**。为此：
#   - loop_seed 记初始 messages 全文（含 system prompt —— probe 事故的教训：
#     "system 在不在场"必须能从日志直接回答）
#   - 每条中途注入记全文（hook_injection.contents / framework_notice_injected /
#     user_message_injected.notice_texts），不再只有 200 字 preview
#   - llm_response 记 tool_calls_full + reasoning_content；tool_result 记
#     appended 的精确 content 与 tool_call_id
#   - llm_request 带 request_digest/request_bytes —— 重建结果与它对账
# 压缩 / 工作集驱逐会替换历史，跨过它们的重建还不成立（#734 余下部分）。

_DIGEST_FIELDS = ("role", "content", "tool_calls", "tool_call_id",
                  "name", "reasoning_content")


def digest_messages(messages: list) -> tuple[int, str]:
    """canonical 序列化 + sha256。任何一层想核对"模型到底收到了什么"，
    用同一个函数算，别各自近似（同一个问题一个真相源）。"""
    import hashlib
    canon = json.dumps(
        [{k: getattr(m, k, None) for k in _DIGEST_FIELDS} for m in messages],
        sort_keys=True, ensure_ascii=False, default=str)
    raw = canon.encode("utf-8")
    return len(raw), hashlib.sha256(raw).hexdigest()


def _inject_notice(state, messages: list, *, turn: int, source: str,
                   text: str) -> None:
    """框架中途插话的唯一入口：追加 + 全文留痕。

    直接 `messages.append(framework_notice(...))` 会让那句话只活在内存里 ——
    transcript 重建不出请求，注入类事故只能靠抓真请求取证。"""
    from core.llm import framework_notice
    messages.append(framework_notice(text))
    state.append_transcript(
        "framework_notice_injected", turn=turn, source=source, text=text)


#: 并行预执行的并发上限（CC 的合批上限是 10；科研工具多为网络/盘 IO，6 够用
#: 且不至于把 provider / 文件系统打成尖峰）。
_PARALLEL_TOOLS_MAX = 6


def _parallel_tools_enabled() -> bool:
    """`HARNESS_PARALLEL_TOOLS=off` 一键退回纯串行。改动波及每个节点每一轮的
    工具派发，必须留一条不改代码就能撤退的路（同 skills v3.9 的 kill switch）。"""
    return (os.environ.get("HARNESS_PARALLEL_TOOLS") or "on").strip().lower() != "off"


async def _pre_execute_safe_prefix(
    state, harness, tool_calls: list[dict], *, turn: int,
) -> dict[str, dict]:
    """连续安全**前缀**合批并发（issue #736，抄 CC 的按输入判 + 合批）。

    返回 {call_id: {"result": ...} | {"exception": BaseException}}；主循环在
    原来的执行位点按原顺序消费 —— transcript / 缓存 / 工作集 / pause 的记账
    路径一个字节不变，只有「真正执行」这一步被提前并发了。

    安全判据（fail-closed，判不出 = 串行）：
      - 工具自声明 `replayable_read`（纯读；与压缩契约同一个声明面 ——
        护栏扫声明不写名单，新工具不声明就默认串行）
      - args JSON 解析成功、工具在本节点白名单内

    只并发**前缀**：第一个不安全/不可判调用之后的一切保持惰性串行。串行
    语义里排在写操作后面的读会看到写的效果 —— 把它们提前执行会打破
    read-after-write 顺序。段长 <2 没有并发收益，不起 gather。

    已知偏差（都只涉及纯读工具，无副作用）：
      - 前缀中某调用随后在主循环命中"结果已在上文"缓存时，这次预执行白跑；
      - 前缀执行后，主循环里更早的调用若触发 pause，其后调用的合成消息说
        "未执行"，而预执行其实读过 —— 结果被丢弃，对世界无影响。
    """
    if not _parallel_tools_enabled() or len(tool_calls) < 2:
        return {}
    from core.tool_call_cache import cache_key as _cache_key
    from core.tool_call_cache import is_cacheable as _replayable

    # 缓存视野：工作集不在场时整段放弃（主循环的缓存判据依赖它，这里看不见
    # 它看得见的东西就别抢跑 —— fail-closed）。
    _view = _ctx_view.get(state)
    if _view is None:
        return {}

    prefix: list[tuple[dict, dict]] = []
    _seen_keys: set[str] = set()
    for call in tool_calls:
        name = call["function"]["name"]
        raw = call["function"].get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (json.JSONDecodeError, TypeError):
            break                       # 崩坏 args：主循环的修复路径处理
        if name not in harness.tools or not _replayable(name):
            break                       # 第一个不安全调用，前缀到此为止
        # 与主循环**同一个**缓存判据（is_live）/ 同一个 key（cache_key），
        # 不另写一份会分叉的近似：
        #   - 答案已活在上文 → 主循环会命中"已在上文"指针，预执行是白跑
        #   - 批内同 key 重复 → 只预执行第一份；后续槽位落回主循环时，第一份
        #     已入账，缓存照常命中 —— 与串行语义逐字节一致（51,937 次
        #     search_kb 的教训：重复读必须走缓存，不许被并行化放大）
        if _view.is_live(name, args):
            break                       # 会命中缓存的调用留给主循环；其后不抢跑
        key = _cache_key(name, args)
        if key in _seen_keys:
            break                       # 批内重复：第一份之后的留给缓存
        _seen_keys.add(key)
        prefix.append((call, args))
    if len(prefix) < 2:
        return {}

    state.append_transcript(
        "parallel_tool_dispatch", turn=turn, count=len(prefix),
        names=[c["function"]["name"] for c, _ in prefix])

    sem = asyncio.Semaphore(_PARALLEL_TOOLS_MAX)

    async def _one(call: dict, args: dict):
        async with sem:
            return await tool_registry.execute(
                call["function"]["name"], state, **args)

    results = await asyncio.gather(
        *[_one(c, a) for c, a in prefix], return_exceptions=True)
    out: dict[str, dict] = {}
    for (call, _), res in zip(prefix, results):
        if isinstance(res, BaseException):
            # 不在这里抛：串行语义下异常发生在**它自己的位点**，前面的调用
            # 已完成记账。留到主循环该调用的槽位再 raise，两种模式下
            # messages / transcript 形状一致。
            out[call["id"]] = {"exception": res}
        else:
            out[call["id"]] = {"result": res}
    return out


def resolve_enabled_hook_names(harness) -> list[str]:
    """本次 run 实际启用哪些 hook：始终启用的 + producing 默认 + harness 声明的
    （去重保序，再扣掉 loop_hooks_disable，但 _ALWAYS_ON_HOOKS 不可关）。

    抽成函数供 executor 复用（issue #166）：QC 契约校验现在在 executor 里
    dispatch 之前做（custom_loop 节点绕过 run_loop，见那里的注释），必须跟这里
    用**同一份**启用集 —— 否则 executor 只看 harness.loop_hooks 会漏掉
    always-on / 默认 hook，把它们 emit 的事件误报成"没有提供方"。
    """
    hook_names: list[str] = list(_ALWAYS_ON_HOOKS)
    # v3.1（审计 高危#14 接线）："经验改变行为"不再依赖每个 owner 记得在 yaml
    # 里写。owner 明确不要某个默认 hook：harness.yaml 写
    # loop_hooks_disable: [<name>]（_ALWAYS_ON_HOOKS 不可关）。
    if not harness.node_type.startswith("_"):
        for name in _DEFAULT_PRODUCING_HOOKS:
            if name not in hook_names:
                hook_names.append(name)
    for name in harness.loop_hooks:
        if name not in hook_names:
            hook_names.append(name)
    disabled = set(getattr(harness, "loop_hooks_disable", []) or [])
    if disabled:
        hook_names = [n for n in hook_names
                      if n not in disabled or n in _ALWAYS_ON_HOOKS]
    return hook_names


# ── 终态三分型（issue #735，抄 grok 的 Stop / StopCancelled / StopFailure）──
#
# 「用户打断 / 轮次上限 / 熔断 / provider 故障 / 模型自己收尾」在旧 status 词表
# 里挤成一团（max_turns 被记成 "completed" —— 截断和做完从此分不开）。三分型：
#
#   stop      —— 模型自己合法收尾（含 hook 判定的确定性终局）
#   cancelled —— 被**外因**切断：外部取消、轮次上限。工作本身没失败。
#   failure   —— 自身/供给侧失败：退化复读、协议熔断、进展熔断、provider 空响应
#   suspended —— pause 等 HITL resume。不是终态，等价于「还没结束」。
#
# `terminal_cause` 是唯一真相源；kind 与旧 status 都由下表派生 —— 同一个问题
# 不留第二个可分叉的答案。新加终止路径必须先来登记 cause，漏登记在构造时
# KeyError（fail loud），不会静默落进某个默认档。
TERMINAL_STOP = "stop"
TERMINAL_CANCELLED = "cancelled"
TERMINAL_FAILURE = "failure"
TERMINAL_SUSPENDED = "suspended"

#: cause → (kind, 旧 status 投影)。旧 status 是老读者的词表（executor/chat 按它
#: 路由），本表**冻结现状**：turn_cap 在旧词表里一直是 "completed"，迁移读者前
#: 不改字节 —— 但 kind 说真话（cancelled）。
_TERMINAL_CAUSES: dict[str, tuple[str, str]] = {
    "model_finished":          (TERMINAL_STOP, "completed"),
    "hook_terminal_completed": (TERMINAL_STOP, "completed"),
    "turn_cap":                (TERMINAL_CANCELLED, "completed"),
    "external_kill":           (TERMINAL_CANCELLED, "cancelled"),
    "degenerate_repetition":   (TERMINAL_FAILURE, "failed"),
    "protocol_circuit_break":  (TERMINAL_FAILURE, "failed"),
    "progress_circuit_break":  (TERMINAL_FAILURE, "failed"),
    "hook_terminal_failed":    (TERMINAL_FAILURE, "failed"),
    "provider_void":           (TERMINAL_FAILURE, "void"),
    "pause_event":             (TERMINAL_SUSPENDED, "paused"),
}


@dataclass
class LoopResult:
    """模型停止后，loop 返还的内容。

    `terminal_cause` / `terminal_kind`（#735）：怎么结束的、算谁的 —— cause 是
    唯一真相源，kind 与 status 由 `_TERMINAL_CAUSES` 派生。构造时显式传的
    status 会与表核对，不一致直接抛错（两个字段不允许各自演化）。

    status 字段（老读者词表，保留兼容；语义如实版）：
      - "completed"：模型自己收尾（cause=model_finished / hook_terminal_completed）
                     **或达到 max_turns（cause=turn_cap，kind=cancelled ——
                     旧词表分不开这两者，新读者请看 terminal_kind）**
      - "paused"：某个工具产生了 pause 事件，等待外部 resume（kind=suspended）
      - "cancelled"：外部 kill_signal（cause=external_kill）
      - "void"：空轮重试用尽（cause=provider_void，kind=failure —— provider 侧账）
      - "failed"：退化复读 / 协议熔断 / 进展熔断 / hook 判失败
    """
    final_text: str
    turns: int
    #: 本次循环的轮次上限。**必须带出来** —— 只给 turns，下游就得拿它跟一个
    #: 它不知道的数字去比，实测没人比（E2E v23 空转三小时）。0 = 无上限/未知。
    max_turns: int = 0
    tool_calls: list[dict] = field(default_factory=list)
    messages: list[LLMMessage] = field(default_factory=list)
    status: str = "completed"
    pause_event: PauseEvent | None = None
    cancel_meta: dict | None = None       # {reason, requested_by, cancelled_at_turn} 当 status='cancelled'
    #: 见类 docstring。空串 = 未盖章（本模块内的构造全部盖章；空串只该出现在
    #: 外部手搓的 LoopResult 上，loop_finished 事件会把它原样亮出来）。
    terminal_cause: str = ""
    terminal_kind: str = ""

    def __post_init__(self) -> None:
        if not self.terminal_cause:
            return
        kind, status = _TERMINAL_CAUSES[self.terminal_cause]   # 未登记 → KeyError
        self.terminal_kind = kind
        if self.status != status:
            raise ValueError(
                f"terminal_cause={self.terminal_cause!r} 按表投影 status 应为 "
                f"{status!r}，但构造传了 {self.status!r} —— cause 是唯一真相源，"
                f"改 status 请改 _TERMINAL_CAUSES，不要在调用点各写各的。")


async def run_loop(
    harness: NodeHarness,
    state: State,
    messages: list[LLMMessage],
    llm: LLMClient,
) -> LoopResult:
    """run_loop 本体 + 取消收口（#284）。

    绑定"当前 run"，让 `LLMClient.chat` / `tool_registry.execute` 这两个咽喉能
    在取消后拒绝新调用；它们抛出的 RunCancelled 在这里收成正常的 cancelled
    LoopResult —— 取消要能体面收场，不能变成 traceback 冒到顶。
    """
    from core import cancellation as _cancel

    with _cancel.bind_run(state):
        try:
            result = await _run_loop_body(harness, state, messages, llm)
        except _cancel.RunCancelled as e:
            state.append_transcript(
                "loop_cancelled_at_chokepoint",
                where=e.where, reason=e.signal.get("reason"),
                requested_by=e.signal.get("requested_by"),
            )
            _cancel.mark_cancelled(state, e.signal)
            _turn = int(state.hook_state.get("_current_turn") or 0)
            result = LoopResult(
                final_text=f"(cancelled: {e.signal.get('reason') or 'run 已取消'})",
                turns=_turn, tool_calls=[], messages=messages, status="cancelled",
                terminal_cause="external_kill",
                cancel_meta={**e.signal, "cancelled_at": e.where,
                             "cancelled_at_turn": _turn},
            )
        # 终局盖章（#735）：每一次 run_loop 返回（含 pause 挂起）在此留一条
        # 统一记录 —— 取证从此问一个事件，不用按九个终止路径各翻各的。
        state.append_transcript(
            "loop_finished", turns=result.turns, status=result.status,
            terminal_kind=result.terminal_kind,
            terminal_cause=result.terminal_cause)
        return result


async def _run_loop_body(
    harness: NodeHarness,
    state: State,
    messages: list[LLMMessage],
    llm: LLMClient,
) -> LoopResult:
    """为单个节点跑一次 LLM 调用工具的循环。

    在以下任一条件下停止：
      - LLM 返回的轮次不再包含 tool_calls（它认为完成了）
      - 达到 max_turns
    """
    tools_for_node = tool_registry.list_tools_for_node(
        harness.node_type, harness.tools, state=state
    )
    tool_schemas = [tool_registry.to_openai_schema(t) for t in tools_for_node]

    # 把 callable_nodes 暴露给 run_node 工具（通过 state.hook_state）
    state.hook_state.setdefault("_callable_nodes", list(harness.callable_nodes))

    # v1.4: owner_config 的 subagent override 也通过 state.hook_state 传给 run_node 工具
    # None = 不 override，run_node 走自己 env / 默认
    if harness.subagent_max_depth is not None:
        state.hook_state["_subagent_max_depth_override"] = int(harness.subagent_max_depth)
    if harness.subagent_max_parallel is not None:
        state.hook_state["_subagent_max_parallel_override"] = int(harness.subagent_max_parallel)
    if harness.subagent_child_timeout_s is not None:
        state.hook_state["_subagent_child_timeout_s_override"] = float(harness.subagent_child_timeout_s)

    # v3.3：shell 探查专用模式（harness.shell_probe_only）传给 run_bash 工具。
    # 显式赋值（不是 setdefault）—— orchestrator 的 hook_state 跨消息持久，
    # yaml 改动后下一轮就要生效。
    state.hook_state["_shell_probe_only"] = bool(harness.shell_probe_only)
    state.hook_state["_deliverable_writes"] = bool(harness.deliverable_writes)

    hook_names = resolve_enabled_hook_names(harness)
    hooks = loop_hooks.list_hooks(hook_names)

    if hooks:
        state.append_transcript(
            "loop_start", hooks=[h.name for h in hooks],
            n_tools=len(tool_schemas),
        )

    # v3.4 context 真校准（其一）：tool schemas 每轮全量随请求发送但不在 messages
    # 里 —— 此前 summarizer 估算完全没数它（orchestrator 几十个工具是 E2E#2 本地
    # 132k vs 服务端 245.8k 差距的一大来源）。算一次落 hook_state 给 should_compress。
    if tool_schemas and "_tool_schema_tokens" not in state.hook_state:
        try:
            state.hook_state["_tool_schema_tokens"] = _summarizer.estimate_text_tokens(
                json.dumps(tool_schemas, ensure_ascii=False))
        except Exception:
            state.hook_state["_tool_schema_tokens"] = 0

    all_tool_calls: list[dict] = []
    # issue #184：provider tool-call 协议故障的两个 per-run 簿记器。
    # repair —— args JSON 崩坏的有界重发（一次成功即清零）。
    # breaker —— 连续协议失败熔断。**不看 max_turns**：实测
    # HARNESS_DEFAULT_MAX_TURNS=0 下空转 ~40 分钟 / ~5300 次失败调用，
    # 把 conversation 灌到 138K token 无法 resume。
    from .tool_call_recovery import (
        MalformedArgsRepair,
        ProtocolFailureBreaker,
        protocol_failure_signature,
    )
    repair = MalformedArgsRepair()
    breaker = ProtocolFailureBreaker()
    # 进展熔断：按"这一轮有没有发生什么"停机，不看工具名单、不看轮数上限。
    from .progress_breaker import ProgressBreaker
    from .progress_breaker import response_signature as progress_response_signature

    progress = ProgressBreaker()
    # issue #223：截断恢复的 per-run 计数。截断 + 无 tool_calls 不是终止信号，
    # 是"这一轮白跑了" —— 有界重试，用尽才走终止路径。
    _truncation_retries = 0
    #: 连续「撞上限 + 零工具调用 + 输出已退化成复读」的轮数。
    #: 与 _truncation_retries 分开数：那个数的是"给了几次机会"，
    #: 这个数的是"确认坏掉了几轮" —— 后者才是停机依据。
    _degenerate_truncations = 0
    # 空轮（回滚重试）的 per-run 计数；任何有产出的一轮清零。
    # 陈旧标记先清：上一次 run_loop 的 _void_turn_final 不许污染本次 status。
    _void_retries = 0
    # context 超限 400 的回滚重试计数（issue #710）—— 与空轮分开数：那边数
    # "provider 什么都没给"，这边数"我们发了装不下的请求"。
    _ctx400_retries = 0
    state.hook_state.pop("_void_turn_final", None)
    # 本轮实际用的输出上限。节点配置的是**默认值**，不是物理上限 —— 撞顶且
    # 一无所出时，重试要抬它（见 _TRUNCATION_BUDGET_FACTOR 的说明）。
    _output_budget = harness.max_output_tokens
    final_result: LoopResult | None = None

    # v3.1（审计 高危#6）：yaml max_turns<=0 不再等于无限 —— 落到框架默认
    # **默认无上限**（2026-08-11，理由见 core/turn_budget.resolve_cap）。
    # 想设：export HARNESS_DEFAULT_MAX_TURNS=<N>。节点 yaml 显式 >0 永远优先。
    # v2.1：请求模式可以收紧轮次预算（定向查证 vs 铺一个领域不该同一个预算）。
    # 模式记在 state 上（run 级状态，跨 pause/resume 存活）。
    from core.turn_budget import loop_bound, resolve_cap

    _mode_turns = harness.max_turns_for(state.hook_state.get("_request_mode"))
    # 上限怎么算只留一处（core/turn_budget.resolve_cap）——判据抄两份就会分叉，
    # 而分叉时两边都不报错。
    _turn_cap = resolve_cap(mode_turns=_mode_turns,
                            env_default=os.environ.get("HARNESS_DEFAULT_MAX_TURNS"))
    _max_turns = loop_bound(_turn_cap)

    # #734：初始 messages 全文入日志（此后每一条变动各自留痕，见 digest_messages）
    _seed_bytes, _seed_digest = digest_messages(messages)
    state.append_transcript(
        "loop_seed", n_messages=len(messages),
        seed_bytes=_seed_bytes, seed_digest=_seed_digest,
        messages=[{k: getattr(m, k, None) for k in _DIGEST_FIELDS}
                  for m in messages])

    for turn in range(1, _max_turns + 1):
        # 咽喉拦截（#284）发生在**轮中**（工具派发/模型请求那一刻），它拿不到
        # 这里的 turn 变量 —— 记一份，好让 cancel_meta 照旧带上 cancelled_at_turn
        # （run_node 的现场打印在读它）。
        state.hook_state["_current_turn"] = turn
        # 上限也要记：`_current_turn` 早就在了，但没有上限，
        # "还剩几轮"这个问题在框架里就无处可答（见 core/turn_budget.py）。
        # 记**真实上限**（0 = 无上限），不是 range 用的哨兵 ——
        # 记哨兵的话，无上限的 run 会被提示层当成"还剩 10 亿轮"。
        state.hook_state["_max_turns"] = _turn_cap
        # 本 session 的用户原话 —— 宪法写入的**防伪依据**。
        # 用户立的铁律权威来自说话那一刻，所以要能立即落盘；但正因为它权威，
        # 模型不能替用户发明。`memory_write` 会核对引文逐字出现在这里面。
        # 采在轮循环咽喉处：工具执行发生在轮内，此时这份已是最新。
        # ⚠️ 必须排掉 framework-notice。框架注入的提示为了不被模型复述，
        # 走的是 **role="user"** 的信封（`_as_framework_notices`，PR#462）。
        # 信封正文里明写「不是用户发言」，但**结构上它就是一条 user 消息** ——
        # 于是任何问"用户说了什么"的消费者都会把它当成用户原话。
        # 在这一处后果最重：这份是宪法的**防伪依据**，`memory_write` 拿它逐字
        # 核对引文。混进框架提示 = 模型可以"引用"一条框架自动提示，把它
        # 洗成用户亲口立下的铁律。判别器 `is_framework_notice` 早就有了，
        # 只是没人在这里接上（机制在场，路径没接）。
        state.hook_state["_user_utterances"] = [
            m.content for m in messages
            if getattr(m, "role", "") == "user"
            and (m.content or "").strip()
            and not is_framework_notice(m)
        ]
        ctx = loop_hooks.HookContext(
            harness=harness, state=state, messages=messages, turn=turn,
        )

        # ── (-1) 外部 kill_signal 检查（最先做，比 summarizer / hook 都早）─────
        # 写法：上层 / 主 harness 用 `cancel_node` 工具往本 run 的
        # state.hook_state['kill_signal'] = {reason, requested_by} 写信号；
        # 下一轮 turn 开始就被检测、立刻退出。不打断当前 tool 调用，保 message 一致。
        kill_sig = state.hook_state.pop("kill_signal", None)
        if kill_sig:
            # #284：pop 掉之后信号就没了 —— 自定义 loop 再进来一次就看不到任何
            # 取消痕迹，于是接着跑（实测 loop_cancelled 后 0.7ms 又起 recovery）。
            # 固化成 run 级终态，咽喉检查照此拒绝后续调用。
            from core import cancellation as _cancel
            _cancel.mark_cancelled(state, kill_sig)
            reason = kill_sig.get("reason") or "(no reason)"
            requested_by = kill_sig.get("requested_by") or "external"
            state.append_transcript(
                "loop_cancelled", turn=turn,
                reason=reason, requested_by=requested_by,
            )
            return LoopResult(
                max_turns=_max_turns,
                final_text=f"(cancelled at turn {turn}: {reason})",
                turns=turn,
                tool_calls=all_tool_calls,
                messages=messages,
                status="cancelled",
                terminal_cause="external_kill",
                cancel_meta={
                    "reason": reason,
                    "requested_by": requested_by,
                    "cancelled_at_turn": turn,
                },
            )

        # ── (-0.5) 外部 injected_messages 注入 ────────────────────────
        # 写法：上层 / 主 harness 用 `inject_into_node` 工具往本 run 的
        # state.hook_state['injected_messages'] 追加 {content, source} 字典；
        # 下一轮 turn 开始就被消费为 system 消息加进 messages，下次 LLM 调用就能看到。
        #
        # 注意：role=system + content 即 caller 传进来的字面内容 —— 框架不再
        # 二次包装。caller 责任：传"权威指令"形态的 content（命令式语气、
        # 明确 action）。chat.py 里 orchestrator 已经做了这步包装；run_node.py
        # 调试模式由 _fake_orchestrator_handle_interrupt 做包装。
        injected = state.hook_state.pop("injected_messages", None) or []
        if injected:
            _notice_texts: list[str] = []
            for item in injected:
                content = item.get("content") or ""
                source = item.get("source") or "external"
                if not content.strip():
                    continue
                _txt = f"📨 调度器中途注入（source={source}, turn {turn}）：\n{content}"
                _notice_texts.append(_txt)
                messages.append(framework_notice(_txt))
            state.append_transcript(
                "user_message_injected", turn=turn,
                count=len(injected),
                previews=[(item.get("content") or "")[:200] for item in injected],
                sources=[item.get("source") or "external" for item in injected],
                # #734：重建请求要的是**实际 append 的文本**，preview 不够
                notice_texts=_notice_texts,
            )

        # ── (-0.3) token 软预算提醒（不硬停）──────────────────────────────
        # 演进史：审计 高危#6 要求"零成本熔断" → v3.1 加了累计 tokens_used ≥
        # 1.5×limit 硬停。v10/v10b dogfood 实测暴露两个误杀：
        #   (1) orchestrator 计数器是全项目 rollup（executor 把子节点 tokens_used
        #       累加回父），套 per-node 预算 → 整个项目 turn 5 被误杀。
        #   (2) 熔断卡的是**累计花费**，会误杀"很多便宜轮次"的合法重活：experiment
        #       跑 4 个 LAMMPS + 解析，21 轮 × 42k = 900k 累计，context 全程健康没
        #       滚雪球，却在"科学算完就差 save_artifact 落盘"那步被 1.5×limit 掐死。
        # 根因：**累计 tokens_used 不是"跑飞"的正确信号**。真正的跑飞边界是
        #   max_turns（轮数上限，拦死循环）+ summarizer（单轮 context 上限，拦
        #   雪球），两者已把总花费 bound 在 ~max_turns × max_context。累计预算只
        #   该**软提醒**"花得多了、若接近完成请收尾"，不该杀合法重活。
        # 所以：token 预算改为 warn-only（一次），移除累计硬停；硬边界交给 max_turns。
        _tl = int(getattr(state, "tokens_limit", 0) or 0)
        if state.node_type == "_orchestrator":
            _tl = 0    # 协调者计数是全项目 rollup，不适用 per-node 预算
        if _tl > 0 and state.tokens_used >= _tl and not state.hook_state.get("_budget_warned"):
            state.hook_state["_budget_warned"] = True
            _inject_notice(
                state, messages, turn=turn, source="budget_soft_warn",
                text=(
                    f"💡 token 花费已达软预算（{state.tokens_used:,}/{_tl:,}）。若接近"
                    f"完成请尽快 save_artifact 落盘收尾；确有必要的重活（如跑仿真、"
                    f"解析大日志）可继续 —— 轮数上限 max_turns 是硬边界，不会硬停在这。"
                ))
            state.append_transcript(
                "budget_soft_warn", turn=turn,
                tokens_used=state.tokens_used, tokens_limit=_tl,
            )

        # ── (0) summarizer：context 超阈值则压缩 ───────────────────────
        should_compress, est_tokens = _summarizer.should_compress(
            harness, messages, turn, state=state,
        )
        _local_prompt_est = est_tokens          # v3.4：本轮请求的本地估算（供观测比）
        if should_compress:
            before_n = len(messages)
            new_messages = await _summarizer.run_summarizer(
                harness, state, messages, llm, turn, est_tokens,
            )
            if new_messages is not messages:
                # 替换内容（保持 list 对象身份，messages 在外面被引用）
                messages[:] = new_messages
            after_est = _summarizer.estimate_tokens(messages)
            _local_prompt_est = after_est
            # v3.1：回报压缩效果 —— 节省 <5% 触发 5-turn 冷却（防 thrash）
            _summarizer.note_compress_result(
                state, turn=turn,
                tokens_before=est_tokens, tokens_after=after_est,
            )
            # 压缩会整段替换 messages（LLM 策略把中间段换成一条摘要）。被换掉的
            # tool 消息必须从工作集账上销号 —— 否则 is_live 会说"答案就在眼前"，
            # 而模型往上翻根本找不到它，只能再问、再拿到同一个指针。
            #
            # 销号**不在这里做**：`ContextView.enforce_budget` 每轮都会先与现场
            # 对账（adopt + sync），而它就在下面几行、在 LLM 调用之前。在这里再
            # 对一次是同一个问题的第二个答案 —— 冗余本身不致命，但它让"对账断了"
            # 这件事无法被任何变异测出来（拿掉任一处，另一处都替它兜住）。
            state.append_transcript(
                "summarize", turn=turn,
                strategy=harness.summarizer.strategy,
                messages_before=before_n, messages_after=len(messages),
                tokens_before=est_tokens, tokens_after=after_est,
            )

        # ── 轮事务边界（E2E-5a 空响应发散循环的根因修复）────────────────────
        # 不变量：**失败的转移不许提交状态**。一轮什么也没产出（无工具调用、
        # 无正文），messages 必须逐字节回到本轮开始前 —— 否则重试不是"相同的
        # 一次尝试"而是"更难的一次"（hook 重灌 + 空 assistant 追加，实测每轮
        # +2118 token 单调发散，症状是上下文太大、重试却把上下文做得更大）。
        # 快照点在 summarizer 之后：压缩是朝解走的状态改进，回滚要保留它。
        _turn_rollback_len = len(messages)

        # ── (0.5) 工作集预算：机械 LRU，不问内容重要不重要 ─────────────
        #
        # 判断"哪份材料还需要"是模型的事 —— 它再调一次就完整回来了，免费。
        # 框架只负责回答"装不装得下"，这是它唯一有资格机械回答的问题。
        if (_wv := _ctx_view.get(state)) is not None:
            _ws_budget = _summarizer.derived_tool_budget_bytes(
                harness, state, messages, output_tokens=_output_budget)
            _dropped = _wv.enforce_budget(messages, max_bytes=_ws_budget,
                                          protect_turn_ge=turn - 1)
            if _dropped:
                messages[:] = _wv.apply(messages)
                state.append_transcript(
                    "working_set_evicted", turn=turn, count=_dropped,
                    budget_bytes=_ws_budget, **_wv.stats(),
                )

        # ── (1) on_turn_start hooks 注入 ─────────────────────────────
        pre_msgs = await loop_hooks.run_on_turn_start(hooks, ctx)
        if pre_msgs:
            for m in pre_msgs:
                messages.append(m)
            state.append_transcript(
                "hook_injection", turn=turn, phase="on_turn_start",
                count=len(pre_msgs),
                previews=[(m.content or "")[:200] for m in pre_msgs],
                roles=[m.role for m in pre_msgs],
                contents=[m.content or "" for m in pre_msgs],   # #734 全文
            )
            _local_prompt_est += _summarizer.estimate_tokens(pre_msgs)

        # ── (1.5) 发送前不变量：这份请求必须装得下 ──────────────────────
        # 渲染出的 context ≤ 窗口是本框架的可证性质，不是撞上 400 再救的火。
        # hook 注入发生在 (0.5) 驱逐之后，所以这里必须再量一次；超了就带着
        # 注入后的现场重新驱逐，还超就收缩本轮输出预算，仍然不够 = 非工具
        # 部分独自超窗（真实数据里极罕见）——那是结构性放不下，明确失败，
        # 绝不发出一个注定 400 的请求（issue #710）。
        _send_output_budget = _output_budget
        _eff, _window, _over, _cap_known = _summarizer.presend_overflow(
            harness, state, messages, output_tokens=_send_output_budget)
        if _over > 0:
            if (_wv := _ctx_view.get(state)) is not None:
                _budget2 = _summarizer.derived_tool_budget_bytes(
                    harness, state, messages, output_tokens=_send_output_budget)
                _dropped2 = _wv.enforce_budget(messages, max_bytes=_budget2,
                                               protect_turn_ge=turn - 1)
                if _dropped2:
                    messages[:] = _wv.apply(messages)
            _eff, _window, _over, _cap_known = _summarizer.presend_overflow(
                harness, state, messages, output_tokens=_send_output_budget)
            if _over > 0 and _send_output_budget - _over >= _MIN_OUTPUT_TOKENS:
                _send_output_budget -= _over
                _over = 0
            state.append_transcript(
                "presend_gate", turn=turn, effective_prompt=_eff,
                window=_window, over=max(0, _over),
                output_budget=_send_output_budget, cap_known=_cap_known,
            )
            # 拒发权只来自权威观测：provider 亲口说过的硬上限。配置窗口是
            # summarizer 触发参考，超了它只做尽力而为的驱逐/收缩后**放行**——
            # 真装不下的话 400 处理器会拿到权威数字，下一轮闸就有拒发权了。
            if _over > 0 and _cap_known:
                # 不发。非工具部分独自超窗 —— 驱逐与收缩输出都救不了。
                # 明确失败、指名框架侧成因，好过发一个注定 400 的请求让
                # 网关替我们把 run 杀了、报错还指着 provider（issue #710）。
                del messages[_turn_rollback_len:]
                _msg = (
                    f"⛔ context 装不下：有效 prompt ≈{_eff} tokens ≥ 窗口 {_window}"
                    f"（输出预算已收缩到下限仍差 {_over} tokens）。"
                    "非工具消息独自超出了上下文窗口 —— 这是框架侧上下文管理缺陷"
                    "或窗口配置远小于实际需要，不是节点或请求的问题。"
                )
                log.error("%s", _msg)
                final_result = LoopResult(
                    max_turns=_max_turns, turns=turn,
                    tool_calls=all_tool_calls, messages=messages,
                    status="failed", final_text=_msg,
                )
                break

        # ── (2) LLM 调用 ────────────────────────────────────────────
        _req_bytes, _req_digest = digest_messages(messages)
        state.append_transcript(
            "llm_request", turn=turn, n_messages=len(messages),
            request_bytes=_req_bytes, request_digest=_req_digest,
            n_tools=len(tool_schemas or []))
        try:
            response = await llm.chat(
                messages,
                tools=tool_schemas if tool_schemas else None,
                max_tokens=_send_output_budget,
                temperature=harness.temperature,
                timeout=harness.llm_timeout_s,        # v1.4: owner_config 可 override（None = 用 LLMClient default）
                max_retries=harness.llm_max_retries,  # v2.x: owner_config 可 override transient 错重试次数
            )
        except LLMHTTPError as _http_exc:
            # context 超限 400 是**框架自己的估算错了**的权威证据，不是节点故障。
            # 服务端在报错正文里给了真实数字（窗口、实收 input tokens）——
            # 喂给校准（观测比只升不降），回滚本轮，下一轮的触发/预算/发送闸
            # 全部按修正后的尺重算。有界重试：估算修正后仍然连撞 = 结构性问题，
            # 把原始异常抛给上层归因（provider failure attribution 原样生效）。
            _ctx_info = context_overflow_numbers(_http_exc)
            if _ctx_info is None or _ctx400_retries >= _CTX400_MAX_RETRIES:
                raise
            _ctx400_retries += 1
            _server_window, _server_input = _ctx_info
            if _server_input:
                _summarizer.note_observed_prompt_tokens(
                    state,
                    local_estimate=_local_prompt_est
                    + int(state.hook_state.get("_tool_schema_tokens") or 0),
                    server_prompt_tokens=_server_input,
                )
            if _server_window:
                # provider 宣称的是**硬上限**（input+output ≤ 此值）——写进
                # 硬上限键，别写解码退化键：两种语义，见 summarizer 注释。
                _old_cap = state.hook_state.get(_summarizer.PROVIDER_HARD_CAP_KEY)
                _new_cap = (min(int(_old_cap), _server_window)
                            if _old_cap else _server_window)
                state.hook_state[_summarizer.PROVIDER_HARD_CAP_KEY] = _new_cap
            del messages[_turn_rollback_len:]          # 失败的转移不许提交状态
            state.append_transcript(
                "context_overflow_rolled_back", turn=turn,
                attempt=_ctx400_retries, max_attempts=_CTX400_MAX_RETRIES,
                server_window=_server_window, server_input_tokens=_server_input,
                local_estimate=_local_prompt_est,
            )
            state.append_transcript(
                "turn_transition", turn=turn, reason="context_overflow_rolled_back")
            continue
        state.append_transcript(
            "llm_response", turn=turn,
            finish_reason=response.finish_reason,
            # 模型这一轮写的话要**完整**记下来。它是用户唯一能看懂"它现在在干嘛、
            # 为什么突然换方向"的东西 —— UI 上那一串工具名回答不了这个问题。
            #
            # 原来只存 `content_preview=[:500]`：硬截断且不留标记，于是"完整的
            # 500 字"和"被砍掉一半"从此分不开，信息在源头就没了，下游怎么修都
            # 找不回来。截断是**展示层**的决定，截容易，还原不可能。
            #
            # `content_preview` 保留原样不动：`nodes/experiment/tools/watch_run.py`
            # 在读它，那是节点 owner 的代码。两个字段在同一条语句里由同一个值
            # 派生，不会各自演化 —— `content` 是权威，preview 只是老读者的入口。
            content=response.content or "",
            content_preview=(response.content or "")[:500],
            tool_calls=[_tc_brief(tc) for tc in response.tool_calls],
            # #734：args_preview 截 200 字重建不出 assistant 消息；全文另给一列，
            # 老读者的 tool_calls 形状不动
            tool_calls_full=response.tool_calls,
            reasoning_content=response.reasoning_content,
            usage=response.usage,
        )
        # provider 异常与框架为它做的恢复，落成结构化事实（issue #501）。
        # 此前这些只进 log：一次 E2E 里 orchestrator 出现 92 次空 SSE，
        # transcript 上一条记录都没有 —— 报告人只能去翻服务器日志数出来，
        # 而"这一轮为什么没有任何产出"恰恰是排查的第一个问题。
        _recovery = getattr(response, "provider_recovery", None)
        if isinstance(_recovery, dict) and _recovery:
            state.append_transcript(
                "llm_provider_recovery", turn=turn, **_recovery)
            if _recovery.get("still_empty") or _recovery.get("reason") == "no_choices":
                # 回退之后仍然什么都没有 = provider 侧故障，不是节点做不出来。
                # 打标给 executor 的失败分类器（与 _zero_output_truncation 同路），
                # 让这次 run 归 infra 账、可被机械重派，而不是记成节点卡死。
                state.hook_state["_provider_void_response"] = turn
        if isinstance(response.usage, dict):
            state.tokens_used += int(response.usage.get("total_tokens") or 0)
            # 每次调用记一行成本 / 缓存账。旁路观测：内部已吞异常，绝不打断主流程。
            # 缓存读占比是"前缀稳不稳定"的唯一判据 —— 没有它，prompt 组装的改动
            # 做完无法证明有效，退化时也无法定位。
            try:
                from core import cost_ledger

                cost_ledger.record(
                    project_root=getattr(state, "project_root", None),
                    run_id=getattr(state, "run_id", None),
                    node_type=harness.node_type,
                    provider=cost_ledger.provider_label(
                        getattr(llm, "base_url", None)
                    ),
                    model=getattr(llm, "model", None),
                    usage=response.usage,
                    turn=turn,
                )
            except Exception:
                pass
            # v3.4 context 真校准（其二）：用服务端权威 prompt_tokens 校准本地估算。
            # 本地估 = should_compress 的 est(压缩后取 after_est) + hook 注入 +
            # tool schema tokens；观测比只升不降，喂给 effective_calibration。
            _summarizer.note_observed_prompt_tokens(
                state,
                local_estimate=_local_prompt_est
                + int(state.hook_state.get("_tool_schema_tokens") or 0),
                server_prompt_tokens=int(response.usage.get("prompt_tokens") or 0),
            )
            # 容量后验的对称清除：在 ≥ 观测上限的 prompt 上拿到**有产出**的响应，
            # 证明当时的空响应是 provider 瞬态而非容量 → 信念恢复为配置值。
            # 瞬态故障不会永久压低窗口。
            if response.tool_calls or (response.content or "").strip():
                _pt = int(response.usage.get("prompt_tokens") or 0)
                _ceiling = state.hook_state.get(_summarizer.OBSERVED_CEILING_KEY)
                if _ceiling and _pt >= int(_ceiling):
                    state.hook_state.pop(_summarizer.OBSERVED_CEILING_KEY, None)
                    state.append_transcript(
                        "context_ceiling_cleared", turn=turn,
                        prompt_tokens=_pt, cleared_ceiling=int(_ceiling),
                    )

        # ── 这一次请求把窗口占到了哪里（给人看的那份）──────────────────────
        # 界面上"当前上下文 xx / 窗口 · 到 70% 自动压缩"读的就是这条。放在
        # 校准更新**之后**：服务端刚报的 prompt_tokens 已经喂进观测比，报告里
        # 的 effective 与下一轮 should_compress 用的是同一个系数。`messages`
        # 此刻仍是发出去的那份请求（assistant 回复在下面才追加）。
        # 旁路观测：报告算不出来只记 warning，不能让一轮因为一条展示事件死掉。
        try:
            state.append_transcript(
                "context_window", turn=turn,
                **_summarizer.context_window_report(
                    harness, state, messages,
                    server_prompt_tokens=(
                        response.usage.get("prompt_tokens")
                        if isinstance(response.usage, dict) else None
                    ),
                ),
            )
        except Exception:
            log.warning("turn %d 的 context_window 报告没算出来", turn, exc_info=True)

        # finish_reason='length' = 撞到 max_output_tokens 被砍。回复 / tool args
        # 半截 → 用户视角看到"回答没说完"。打 warning + 写 transcript event
        # 让 dogfood 能直接 grep 而不是肉眼盯。
        if response.finish_reason == "length":
            log.warning(
                "⚠️ turn %d 撞到 max_output_tokens=%d 被截断（node=%s）。"
                "考虑调高 harness.context_config.max_output_tokens。",
                turn, harness.max_output_tokens, harness.node_type,
            )
            _usage = response.usage or {}
            state.append_transcript(
                "llm_truncated", turn=turn,
                max_output_tokens=_send_output_budget,
                configured_max_output_tokens=harness.max_output_tokens,
                completion_tokens=_usage.get("completion_tokens"),
                reasoning_tokens=_usage.get("reasoning_tokens"),
                # 这两个是判断"这一轮到底有没有产出"的依据 —— 全 0 说明预算
                # 烧光却什么也没吐出来，那才是需要抬预算的场景。
                content_chars=len(response.content or ""),
                n_tool_calls=len(response.tool_calls or []),
            )

            # ── issue #223：截断恢复（原来只记事件就往下走）────────────────
            # jicq 实测：experiment 节点第 9 轮把 hook 注入的内部控制状态
            # （plan_reminder / last_build / engineering_control …）复读到
            # 16384 token 上限被砍，`tool_calls` 因此为空 → 下面的
            # "if not response.tool_calls" 把它当成"模型决定收工"，节点直接
            # incomplete 结束。一整轮上限的 token 花在吐内部状态上，真正的
            # 下一步动作从未发生，而框架只留了一条 warning。
            #
            # 修：截断 + 无 tool_calls = **未完成的一轮**，不是终止信号。
            # 注入一条强约束（只准回一个工具调用或一句极短结论）后重试，
            # 有界（_TRUNCATION_RETRIES）；用尽仍截断才往下走终止路径。
            # ── 退化复读 ≠ 内容写不下（2026-08-17 实测）──────────────────
            # 上面那套恢复是为"模型真的需要更多空间"设计的，所以它**抬预算**。
            # 但同一个 finish=length 还有另一个成因：模型退化成复读，把预算烧
            # 到底也吐不出一个工具调用。对这一类抬预算是火上浇油 ——
            # 实测 literature 节点：turn8 吐 16,384 复读被截断 → 恢复把上限抬到
            # 32,768 → turn9 吐满 32,768 复读 → turn10 再吐 11,105。三轮 6 万
            # 输出 token、零产物，而且**框架自己把伤害翻了一倍**。
            #
            # 两者靠尾部压缩比分得开（判据取自 67 条真实长输出，见
            # progress_breaker 里的实测分布）：复读的尾部压缩比 ≤0.05，正常
            # 收尾的 ≥0.17。
            from core.progress_breaker import (
                is_degenerate_repetition, should_not_raise_budget,
                tail_repetition_ratio,
            )
            _tail_ratio = tail_repetition_ratio(response.content)
            _degenerate = (not response.tool_calls
                           and is_degenerate_repetition(response.content))
            if _degenerate:
                _degenerate_truncations += 1
            else:
                _degenerate_truncations = 0

            # 连续两轮确认退化 = 再给机会也是同一个结果，而每一轮都是满预算。
            # 停机而不是继续退避：这不是瞬态抖动，是这次推理已经进了吸引子。
            if _degenerate_truncations >= 2:
                state.append_transcript(
                    "degenerate_repetition_break", turn=turn,
                    consecutive=_degenerate_truncations,
                    tail_ratio=round(_tail_ratio, 4),
                    completion_tokens=_usage.get("completion_tokens"),
                    output_budget=_send_output_budget,
                )
                log.error(
                    "⛔ turn %d 连续 %d 轮输出退化成复读（尾部压缩比 %.4f）。停机。",
                    turn, _degenerate_truncations, _tail_ratio,
                )
                final_result = LoopResult(
                    max_turns=_max_turns, turns=turn,
                    tool_calls=all_tool_calls, messages=messages, status="failed",
                    terminal_cause="degenerate_repetition",
                    final_text=(
                        f"⛔ 输出退化熔断：连续 {_degenerate_truncations} 轮，模型把整个输出预算"
                        f"（{_send_output_budget} token）烧在复读上，且一个工具调用都没发出"
                        f"（尾部压缩比 {_tail_ratio:.4f}，正常收尾 ≥0.17）。\n"
                        f"再给预算只会得到更长的复读 —— 实测抬到 32,768 就吐满 32,768。停机。\n"
                        f"典型成因：上下文长度把这个推理后端推过了它的稳定区间"
                        f"（实测一次：检索结果一次性把上下文从 2.4 万顶到 6.9 万，下一轮即崩）。"
                        f"换后端、或让上游把大块结果先落盘、只把摘要进上下文。"
                    ),
                )
                break

            if not response.tool_calls and _truncation_retries < _TRUNCATION_RETRIES:
                _truncation_retries += 1
                # **带上机械增量**：只改提示词等于用同样的输入再问一遍。
                _prev_budget = _output_budget
                # 但只在"内容写不下"时才加。已经在复读了还加预算，等于花钱
                # 买更长的复读（实测 16,384 → 32,768 就是这么来的）。
                if should_not_raise_budget(response.content):
                    log.warning(
                        "turn %d 输出尾部压缩比 %.4f（疑似复读）—— 不抬输出预算。",
                        turn, _tail_ratio,
                    )
                else:
                    _output_budget = min(int(_output_budget * _TRUNCATION_BUDGET_FACTOR),
                                         _TRUNCATION_BUDGET_CEILING)
                _inject_notice(state, messages, turn=turn,
                               source="truncation_recovery",
                               text=(
                        f"⚠️ 你上一轮的输出撞到 token 上限（{_prev_budget}）被截断，"
                        f"且没有发出任何工具调用 —— 那一轮等于白跑。\n"
                        # 别在这里说假话：没抬预算时写"已提高到 X"，模型会按
                        # 一个不存在的额度规划输出，下一轮照样撞顶。
                        + (f"**本轮上限已提高到 {_output_budget}**，但更可能的问题是"
                           f"你想一次输出太多东西。\n\n"
                           if _output_budget > _prev_budget else
                           f"**上限没有提高（仍是 {_output_budget}）** —— 你上一轮的输出"
                           f"在不断重复同一句话，多给预算只会得到更长的重复。\n\n") +
                        "**不要复述**上面注入过的计划/状态/提醒文本（plan_reminder、"
                        "build 状态、engineering_control 之类是给你看的控制信息，"
                        "不是要你输出的内容），复述它们只会再次撞上限。\n\n"
                        f"这一轮只做一件事（第 {_truncation_retries}/"
                        f"{_TRUNCATION_RETRIES} 次机会）：\n"
                        "  • 要么**只**发出一个工具调用（参数尽量精简）；\n"
                        "  • 要么用不超过 100 字说明你卡在哪、需要什么才能继续。\n"
                        "禁止长篇复盘。"
                    ))
                state.append_transcript(
                    "llm_truncation_recovery_injected", turn=turn,
                    attempt=_truncation_retries, max_attempts=_TRUNCATION_RETRIES,
                    budget_before=_prev_budget, budget_after=_output_budget,
                )
                state.append_transcript(
                    "turn_transition", turn=turn, reason="truncation_recovery")
                continue      # 不消耗终止路径，重来一轮

            if not response.tool_calls:
                state.append_transcript(
                    "llm_truncation_recovery_exhausted", turn=turn,
                    attempts=_truncation_retries,
                )
                # 零产出撞顶到恢复用尽 = provider 侧故障（E2E-5b 判决性重放：
                # 同样请求事后 246 token 正常完成 —— 瞬态，不是节点做不出来）。
                # 打标给 executor 的失败分类器，让 run 归 infra 账、可被机械重派。
                if not (response.content or "").strip():
                    state.hook_state["_zero_output_truncation"] = turn

        # ── (3) on_llm_response hooks（观察）──────────────────────────
        # v3.1（审计）：observe-only 从 docstring 约定升级为机制 —— hook 默认
        # 收到 deepcopy 只读副本，改了不影响真实流程。迁移期豁免节点
        # （framework_exemptions.yaml: hook_mutable_response）仍收原对象。
        if hooks:
            from shared.lib.exemptions import hook_response_mutable
            if hook_response_mutable(harness.node_type):
                await loop_hooks.run_on_llm_response(hooks, ctx, response)
            else:
                try:
                    _hook_view = copy.deepcopy(response)
                except Exception:
                    _hook_view = response
                await loop_hooks.run_on_llm_response(hooks, ctx, _hook_view)

        # 追加 assistant 轮次
        # reasoning_content：DeepSeek V4 family / o1 等 reasoning model 返回的
        # 思考链，下一轮请求必须原样回传给 API（API 强制）。
        messages.append(LLMMessage(
            role="assistant",
            content=response.content,
            tool_calls=response.tool_calls or None,
            reasoning_content=response.reasoning_content,
        ))

        # LLM 不再调工具 → loop 结束
        if not response.tool_calls:
            final_text = response.content or ""

            # ── 收尾闸（v2.1）─────────────────────────────────────────────
            # 节点声明的强制自检必须**真的跑过**才准收尾。实测（E2E 2026-08-07）：
            # hypothesis 的 prompt 里"结束前必须 validate_hypothesis_outputs()"
            # 写了三遍（工作流第 11 步 / rules / QC 描述），agent 全程 56 次工具
            # 调用一次没调，最后一轮还在建 claim 就收工了；on_end hook 补跑校验
            # 时 loop 已结束，两项失败无法补救 → 整个 run incomplete，下游
            # reviewer 又白跑 40 轮。prompt 里的"必须"不是机制。
            # 闸只放一次（放行后置位），避免与模型拉锯。
            if final_text.strip() and not state.hook_state.get("_finish_gate_used"):
                _gate_msgs = await loop_hooks.run_on_before_finish(hooks, ctx)
                if _gate_msgs:
                    state.hook_state["_finish_gate_used"] = True
                    state.append_transcript(
                        "finish_gate_blocked",
                        turn=turn,
                        n_messages=len(_gate_msgs),
                        roles=[m.role for m in _gate_msgs],
                        contents=[m.content or "" for m in _gate_msgs],  # #734
                    )
                    messages.extend(_gate_msgs)
                    state.append_transcript(
                        "turn_transition", turn=turn, reason="finish_gate_blocked")
                    continue

            # ── 空轮：无工具调用 + 无正文 = 这一轮**没有发生** ────────────────
            # 事务语义：回滚到轮初快照（撤掉 hook 重灌 + 空 assistant 追加），
            # 使重试是真正相同的一次尝试 —— 上下文驻定，不发散，且几乎全命中
            # provider 缓存。两条恢复路径在此机械分叉：
            #   容量问题：空响应观测压低有效窗口 → 下一轮 should_compress 触发
            #             → 请求变小 → 立即重试（不等，因为这是不同的一次尝试）
            #   瞬态故障：上下文本来就不大 → 压缩不会触发 → 退避等待后原样重放
            #             （等待就是对"世界会变"的正确动作）
            # 有界（_VOID_RETRY_DELAYS 用尽）后走原终止路径，节点 run 由 executor
            # 归 infra 账、机械重派；continuous orchestrator 由 chat 层无限驻定重放。
            # finish=length 的零产出豁免：那条腿有自己的机械增量恢复（抬输出预算，
            # issue #223/#256）——同一症状两套重试叠加只会把预算修复推迟 10 分钟。
            if not final_text.strip() and response.finish_reason != "length":
                _usage = response.usage or {}
                _pt = int(_usage.get("prompt_tokens") or 0)
                _ct = _usage.get("completion_tokens")
                # 容量证据只采信真解码退化（completion≈0）。markup 泄漏 /
                # 有产出被清空的场景，模型明明生成了 token，不是容量问题。
                #
                # "这算不算真退化"**已经有唯一答案**：`LLMResponse.leak_kind`
                # （core.llm._classify_leak_kind：completion≤3 判 'empty' =
                # 退化，>3 判 'markup' = 后端把 markup 当文本返回）。这里原来
                # 无视它、另用一个 `_ct <= 8` 的阈值重判一遍 —— 同一个问题两个
                # 真相源，在 3 < ct ≤ 8 这段分叉，而分叉时两边都不报错。
                #
                # 2026-08-17 实测（积算 deepseek-v4-pro，literature 节点）：
                # turn 1 是 markup 泄漏、completion=7，被这个阈值收成"容量证据"
                # → ceiling 钉死在 21335（配置窗口 120000 的 1/6）→ 框架据此
                # 压缩 + 建议轮换 session → 模型读到"你已到上下文极限"，于是
                # 写了张交接便条就不干活了 → 5 次空轮重试（退避累计约 10 分钟）
                # → run incomplete、零交付。一次后端解析抖动，被放大成整轮报废。
                # ⚠️ 一个数据点不是天花板（2026-08-25 实测，session 2220d882）。
                #
                # 调度器交完终稿（completion=818，带 CONTINUOUS_STATUS: complete）
                # 5 秒后返回了一个 **1 token** 的响应。上面这条判据当场把
                # ceiling 钉在 prompt_tokens=132850 —— 而 configured_window 是
                # **256000**，离窗口还差一半。天花板一钉，`should_compress` 立刻
                # 成立（will_compress=true / wait_s=0），prompt 压到 63521，模型
                # 再睁眼时自己刚交付的那份报告已经被摘要掉了，于是**又写了一遍**，
                # 而且同一张图给出了另一个路径。用户看到两大段近乎重复的终稿。
                #
                # 上面那段注释记的是同一个坑的第一种形状（markup 泄漏），当时的
                # 护栏是 `_leak_kind != "markup"` —— 按**长什么样**划边界。这次
                # leak_kind 是 null：一个真的很短的回复。形状不在名单里，护栏就
                # 不在场（[[feedback_guardrails_must_scan_not_list]]）。
                #
                # 判据改成问"这条证据够不够格"：连续第二次空轮才算容量观测。
                # 一次短回复最常见的意思是模型没话说了 —— 尤其紧跟在一份完整
                # 终稿后面。真的撞到容量上限时它不会只出现一次。
                #
                # 覆盖不到的：provider 每次都在同一个 prompt 尺寸上退化且只退化
                # 一次。那种情况第二次空轮照样会把 ceiling 钉上，代价是多一轮
                # 重放 —— 比把窗口砍半便宜得多。
                _leak_kind = getattr(response, "leak_kind", None)
                if (_pt > 0 and isinstance(_ct, int) and _void_retries >= 1
                        and _ct <= _VOID_COMPLETION_MAX and _leak_kind != "markup"):
                    _old = state.hook_state.get(_summarizer.OBSERVED_CEILING_KEY)
                    _new_ceiling = min(int(_old), _pt) if _old else _pt
                    if _new_ceiling != _old:
                        state.hook_state[_summarizer.OBSERVED_CEILING_KEY] = _new_ceiling
                        state.append_transcript(
                            "context_ceiling_observed", turn=turn,
                            prompt_tokens=_pt, ceiling=_new_ceiling,
                            configured_window=harness.max_context_tokens,
                        )
                if _void_retries < len(_VOID_RETRY_DELAYS):
                    _wait_s = _VOID_RETRY_DELAYS[_void_retries]
                    _void_retries += 1
                    _removed = len(messages) - _turn_rollback_len
                    del messages[_turn_rollback_len:]          # ← 回滚：本轮归零
                    _will_compress, _ = _summarizer.should_compress(
                        harness, messages, turn + 1, state=state)
                    state.append_transcript(
                        "void_turn_rolled_back", turn=turn,
                        attempt=_void_retries, max_attempts=len(_VOID_RETRY_DELAYS),
                        removed_messages=_removed,
                        prompt_tokens=_pt, completion_tokens=_ct,
                        leak_kind=getattr(response, "leak_kind", None),
                        will_compress=_will_compress,
                        wait_s=0 if _will_compress else _wait_s,
                    )
                    state.append_transcript(
                        "turn_transition", turn=turn, reason="void_turn_rolled_back")
                    if not _will_compress:
                        await _void_backoff_sleep(state, _wait_s)
                    continue
                # 重试用尽：回滚仍然生效（不把失败提交进历史），亮出结构化标记
                # 供上层消费（executor 失败分类 / chat continuous 驻定重放）。
                del messages[_turn_rollback_len:]
                state.hook_state["_void_turn_final"] = {
                    "turn": turn, "retries": _void_retries,
                    "prompt_tokens": _pt, "completion_tokens": _ct,
                    "leak_kind": getattr(response, "leak_kind", None),
                }

            # provider tool-call 协议泄漏且重请求后仍没恢复出正文/调用
            # （content 只剩 markup 碎片）—— 已被 tool_call_recovery 清成空。
            # 别把空/残渣当最终回答，给一句明确说明（backend 兼容性问题，非模型没话说）。
            if getattr(response, "protocol_leak", False) and not final_text.strip():
                # leak_kind 区分两种根因（jicq 2026-07-24 实测：completion=1 的近乎
                # 空响应被误报成 markup leak，把人往"查 endpoint 解析配置"带偏，实为
                # 大上下文退化）。两种都重试过了仍没恢复，但归因/给的下一步不同。
                _kind = getattr(response, "leak_kind", None)
                _ct = (response.usage or {}).get("completion_tokens")
                _pt = (response.usage or {}).get("prompt_tokens")
                if _kind == "empty":
                    final_text = (
                        "[近乎空响应] 模型本轮几乎没有生成内容"
                        f"（completion_tokens={_ct}，prompt_tokens={_pt}），重试后仍如此。"
                        "常见于大上下文下模型解码退化，不是后端解析配置问题、也不是模型无话可说。"
                        "请重试；若在大 prompt 下持续出现，优先压上下文"
                        "（检查 handoff/summary 是否生效、是否有 hook 在灌历史）。"
                    )
                else:
                    final_text = (
                        "[provider tool-call 协议错误] 模型本轮尝试调用工具，但当前 LLM "
                        "后端把 tool-call markup 当普通文本返回、未结构化，重试后仍未恢复。"
                        "这是后端兼容性问题（见 core/tool_call_recovery.py），不是模型无话可说。"
                        "请重试；若持续出现，检查该 endpoint 的 tool-call 解析配置。"
                    )
                state.append_transcript(
                    "provider_protocol_leak_surfaced", turn=turn,
                    leak_kind=_kind, completion_tokens=_ct, prompt_tokens=_pt)
            final_result = LoopResult(
                max_turns=_max_turns,
                final_text=final_text,
                turns=turn,
                tool_calls=all_tool_calls,
                messages=messages,
                status="void" if state.hook_state.get("_void_turn_final") else "completed",
                terminal_cause=("provider_void"
                                if state.hook_state.get("_void_turn_final")
                                else "model_finished"),
            )
            break

        # ── (4) 调度每个 tool call ────────────────────────────────────
        _void_retries = 0            # 有产出的一轮 → 空轮计数清零
        ctx.tool_call_records = []   # 本 turn 的工具调用记录（给 on_turn_end 用）
        _malformed_this_turn: list[str] = []   # #184：本 turn args 崩坏的工具名
        paused_at_call: dict | None = None  # 设为非 None 时 = 本 turn 触发了 pause
        # ── (4a) 安全前缀并行预执行（#736）——记账仍走下面的串行循环 ──────
        _pre_executed = await _pre_execute_safe_prefix(
            state, harness, response.tool_calls, turn=turn)
        for call in response.tool_calls:
            tool_name = call["function"]["name"]
            raw_args = call["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError as e:
                # issue #184：args JSON 崩坏是一条**独立**的 provider 协议故障路径
                # （tool_call 结构上存在、但参数反序列化失败）。实测：glm 干完 14
                # 轮审稿、编好 7.7KB critique，最后 save_artifact 的 args JSON 非法
                # → review_critique 交不上 → run incomplete；模型收到裸报错
                # "column 7711" 无从定位，下一轮直接空手 stop，不自我纠正。
                # 现在回喂**带断点原文摘录 + 可执行 hint** 的结构化反馈，并有界重发。
                args = {}
                _executed_for_real = False
                result = repair.feedback(
                    tool_name=tool_name, raw_args=raw_args, error=e)
                _malformed_this_turn.append(tool_name)
            else:
                repair.note_success(tool_name)      # 一次成功即清零
                if tool_name not in harness.tools:
                    _executed_for_real = False
                    result = {
                        "status": "error",
                        "error": f"工具 {tool_name!r} 不在本节点的白名单内。",
                    }
                else:
                    state.append_transcript("tool_call", turn=turn, name=tool_name, args=args)
                    _emit_progress(state, tool_name, args)   # P1-8：live 进度
                    # v3.4：同 run 内重复只读调用走记账/复用层（见 core/tool_call_cache）。
                    # 实测事故：一个 run 用 78 个 query 发了 51,937 次 search_kb，
                    # 压缩把结果裁掉后 agent 反复重查。命中**成功**结果时不再真跑
                    # 工具；失败一律真跑（本层无权把失败说成成功）；病态重复返回
                    # error 并计入硬重复，由 progress_breaker 停机。
                    # 判据变了（RFC 工作集）：重复只在**完整答案正摆在 context
                    # 里**时才成立。副本被驱逐之后再调，是框架自己规定的恢复
                    # 路径（墓碑上就那么写着），不是病 —— 旧实现把它算作重复，
                    # 于是 summarizer 的"重调即可取回"和这里的"重调是循环"
                    # 正面打架，模型照框架说的做反被惩罚。
                    _view = _ctx_view.get(state)
                    _looping = _view is None or _view.is_live(tool_name, args)
                    _cached = _tool_cache.lookup(state, tool_name, args,
                                                 is_live=_looping)
                    _executed_for_real = _cached is None
                    if _cached is not None:
                        result = _cached
                        state.append_transcript(
                            "tool_call_cache_hit", turn=turn, name=tool_name,
                            repeat_count=_cached.get("repeat_count"),
                            escalated=(_cached.get("status") == "error"),
                        )
                        state.tool_calls_made += 1
                    else:
                        _pre = _pre_executed.pop(call["id"], None)
                        if _pre is not None and "exception" in _pre:
                            raise _pre["exception"]     # 串行语义：异常在自己的槽位抛
                        if _pre is not None:
                            # 预执行段已真跑过（只读工具）；这里只消费结果。
                            # 不设 _current_tool_call_id_being_executed：那是给
                            # run_node 嵌套级联用的，纯读工具不嵌套。
                            result = _pre["result"]
                        else:
                            # 给嵌套调用（run_node）看见当前父级的 tool_call_id —— cascade resume 用
                            state.hook_state["_current_tool_call_id_being_executed"] = call["id"]
                            try:
                                result = await tool_registry.execute(tool_name, state, **args)
                            finally:
                                state.hook_state.pop("_current_tool_call_id_being_executed", None)
                        state.tool_calls_made += 1
                        # 返回值可能挂了"你已经这样失败过 N 次"的提示 —— 必须替换
                        # 原 result，否则那条事实就没送到模型面前（v3.4）。
                        result = _tool_cache.store(
                            state, tool_name, args, result, turn=turn,
                            was_recovery=not _looping)

            # tool_result 消息正文（即便是 pause，也写一条让 LLM message 序列
            # 合法；resume 时把这条的 content 替换成 user 的真实回答）。
            # 先算：事件和 messages 必须共用同一份序列化（#734 —— 记的就是
            # append 的那份，不是另一次近似）。
            _tool_content = json.dumps(result, ensure_ascii=False, default=str)
            state.append_transcript(
                "tool_result", turn=turn, name=tool_name, result_preview=_brief(result),
                tool_call_id=call["id"], content=_tool_content,
            )
            record = {"name": tool_name, "args": args, "result": result}
            all_tool_calls.append(record)
            ctx.tool_call_records.append(record)
            messages.append(LLMMessage(
                role="tool",
                tool_call_id=call["id"],
                name=tool_name,
                content=_tool_content,
            ))
            # 登记进工作集：原文落 run log（权威、永不编辑），同 key 的旧副本
            # **立刻**变墓碑。所以"读一百次"和"读一次"占一样的 context ——
            # 控体积不再需要任何"惩罚重复"的机制。
            #
            # ⚠️ 只登记**真执行过**的结果。缓存命中时回的是一句"结果已经在上文"
            # 的指针，它不是内容 —— 把它当新副本登记，会立刻把它指向的那份真
            # 副本挤成墓碑，指针于是指向一块墓碑（实测：连读 8 次之后 context
            # 里一份完整结果都不剩）。
            if _executed_for_real and (_view := _ctx_view.get(state)) is not None:
                _view.note_result(
                    tool_name=tool_name, args=args, tool_call_id=call["id"],
                    content=_tool_content, turn=turn,
                    replayable=_tool_cache.is_cacheable(tool_name),
                    ok=not (isinstance(result, dict)
                            and result.get("status") in ("error", "failed")),
                )
                messages[:] = _view.apply(messages)

            # ── pause 检测 ────────────────────────────────────────────
            # 任何工具返回 status=="pause" → 中断本 turn 剩余 tool_calls，unwind
            if isinstance(result, dict) and result.get("status") == "pause":
                paused_at_call = {"call": call, "result": result}
                break

        # 触发了 pause → 不跑 on_turn_end，直接 unwind
        if paused_at_call:
            # v3.1（审计 高危#5）：单 turn 多 tool_calls 中途 pause 时，给剩余
            # **未执行**的 call 各补一条合成 tool 消息 —— 否则 assistant 消息里
            # 的 tool_calls 与 tool 消息不配对，resume 后 API 必撞 400。
            _paused_id = paused_at_call["call"]["id"]
            _seen_paused = False
            for _c in response.tool_calls:
                if _c["id"] == _paused_id:
                    _seen_paused = True
                    continue
                if _seen_paused:
                    messages.append(LLMMessage(
                        role="tool",
                        tool_call_id=_c["id"],
                        name=_c["function"]["name"],
                        content=json.dumps({
                            "status": "deferred",
                            "note": "本 turn 因 pause 中断，该调用未执行；"
                                    "resume 后如仍需要请重新调用。",
                        }, ensure_ascii=False),
                    ))
            ev_data = paused_at_call["result"].get("pause_event") or {}
            # 整份带走，不逐个字段手抄。这里原来是七行命名参数，于是每加一个
            # pause 字段就要记得回来补一行 —— 忘了不报错，只是流里悄悄少一块：
            # v0.4.3 漏拷 metadata（auto-approve 无视 reviewer 推荐）、
            # 2026-08-19 漏拷 option_details/offer_id（选项的身份在第一跳就死）。
            pause_event = PauseEvent.from_payload(
                ev_data,
                pending_tool_call_id=paused_at_call["call"]["id"],
                default_node_type=state.node_type,
                default_run_id=state.run_id,
            )
            # 注册到 pause registry（chat.py 用 run_id 找 ctx 恢复）
            ctx_paused = PausedRunContext(
                run_id=state.run_id,
                state=state,
                messages=messages,
                harness=harness,
                llm=llm,
                pending_tool_call_id=pause_event.pending_tool_call_id,
                pause_event=pause_event,
                parent_run_id=state.parent_run_id,
                # parent_tool_call_id 由 run_node 工具在冒泡时填
                parent_tool_call_id=state.hook_state.get("_parent_tool_call_id"),
            )
            register_pause(ctx_paused)
            state.append_transcript(
                "loop_pause", turn=turn,
                question=pause_event.question[:200],
                pending_tool_call_id=pause_event.pending_tool_call_id,
            )
            # v3.1（审计）：pause registry 是进程内存 —— 进程崩溃后等 HITL 的
            # run 无从发现。落一个 pause_pending.json 标记（resume 时删），
            # `hf` / driver 崩溃重启后能列出悬空 pause + 用 messages checkpoint 恢复。
            try:
                (state.root / "pause_pending.json").write_text(
                    json.dumps({
                        "run_id": state.run_id,
                        "node_type": state.node_type,
                        "question": pause_event.question,
                        "options": pause_event.options,
                        "asking_node_type": pause_event.asking_node_type,
                        "pending_tool_call_id": pause_event.pending_tool_call_id,
                        "paused_at_turn": turn,
                        "paused_at": datetime.utcnow().isoformat() + "Z",
                    }, ensure_ascii=False, indent=1), encoding="utf-8")
            except Exception as e:
                log.debug("pause_pending.json 写入失败：%s", e)
            return LoopResult(
                max_turns=_max_turns,
                final_text="",
                turns=turn,
                tool_calls=all_tool_calls,
                messages=messages,
                status="paused",
                terminal_cause="pause_event",
                pause_event=pause_event,
            )

        # ── (4.5) 协议失败熔断（issue #184）─────────────────────────────
        # 这里就是 ~5300 次空烧的真实路径：args 崩坏 → tool_calls 非空 →
        # 循环照常继续 → 无限重复，且 max_turns=0 时没有任何东西拦得住。
        # 按**连续失败次数**熔断，与轮数上限无关。has_tool_calls 必须显式传
        # True —— 本分支是"确实调了工具"的一轮，漏传会被误判成 blank_stop。
        _decision = breaker.record(protocol_failure_signature(
            has_tool_calls=True, malformed_args_tools=_malformed_this_turn,
        ))
        if _decision.should_abort:
            state.append_transcript(
                "protocol_circuit_break", turn=turn,
                signature=_decision.signature, streak=_decision.streak,
                any_streak=_decision.any_streak,
            )
            final_result = LoopResult(
                max_turns=_max_turns,
                final_text=_decision.diagnosis, turns=turn,
                tool_calls=all_tool_calls, messages=messages, status="failed",
                terminal_cause="protocol_circuit_break",
            )
            break
        if _decision.should_warn:
            _inject_notice(state, messages, turn=turn,
                           source="protocol_breaker_warn",
                           text=_decision.diagnosis)

        # ── (5) on_turn_end hooks 注入 ────────────────────────────────
        post_msgs = await loop_hooks.run_on_turn_end(hooks, ctx)
        if post_msgs:
            for m in post_msgs:
                messages.append(m)
            state.append_transcript(
                "hook_injection", turn=turn, phase="on_turn_end",
                count=len(post_msgs),
                previews=[(m.content or "")[:200] for m in post_msgs],
                roles=[m.role for m in post_msgs],
                contents=[m.content or "" for m in post_msgs],   # #734 全文
            )

        # A deterministic owner hook may discover that continuing cannot change
        # the outcome. Close only after every tool-result message is appended,
        # preserving protocol validity and normal on_end/QC behavior. This is
        # not external cancellation: absent required outputs become incomplete.
        _terminal = state.hook_state.pop("_loop_terminal", None)
        if isinstance(_terminal, dict):
            _terminal_text = str(
                _terminal.get("final_text")
                or "The node reached a deterministic terminal condition."
            )
            _terminal_status = str(_terminal.get("status") or "completed")
            if _terminal_status not in {"completed", "failed"}:
                _terminal_status = "completed"
            state.append_transcript(
                "loop_terminal_requested_by_hook",
                turn=turn,
                requested_by=_terminal.get("requested_by"),
                status=_terminal_status,
                reason=str(_terminal.get("reason") or "")[:500],
            )
            final_result = LoopResult(
                max_turns=_max_turns,
                final_text=_terminal_text,
                turns=turn,
                tool_calls=all_tool_calls,
                messages=messages,
                status=_terminal_status,
                terminal_cause=("hook_terminal_completed"
                                if _terminal_status == "completed"
                                else "hook_terminal_failed"),
            )
            break

        # ── (5a) 进展熔断（2026-08-13）────────────────────────────────────
        # 放在**工具执行完、checkpoint 之前**：此刻这一轮的持久后果已经落地，
        # "有没有发生什么"才问得出真答案。
        #
        # 它补的是另外三道的共同盲区 —— 协议熔断盯 tool-call 崩坏、重复调用
        # 缓存盯"同工具同参数"且靠白名单、空轮盯"无调用无正文"。v26 那条
        # 1482 轮从三道中间穿过去了：协议全程正常、每次 write_scratchpad 参数
        # 都不同、read_file 压根不在那张白名单里。信号改成"有没有变化"之后，
        # 白名单漏项这类缺陷在结构上不存在。
        _progress = progress.record(
            state,
            progress_response_signature(response.content, response.tool_calls),
            turn=turn,
        )
        if _progress.should_abort:
            state.append_transcript(
                "progress_circuit_break", turn=turn,
                repeat_streak=_progress.repeat_streak,
                stall_streak=_progress.stall_streak,
            )
            final_result = LoopResult(
                max_turns=_max_turns,
                final_text=_progress.diagnosis, turns=turn,
                tool_calls=all_tool_calls, messages=messages, status="failed",
                terminal_cause="progress_circuit_break",
            )
            break
        if _progress.should_warn:
            _inject_notice(state, messages, turn=turn,
                           source="progress_breaker_warn",
                           text=_progress.diagnosis)
            state.append_transcript(
                "progress_warning", turn=turn,
                repeat_streak=_progress.repeat_streak,
                stall_streak=_progress.stall_streak,
            )

        # ── (5b) v0.7：messages 持久化（per-turn checkpoint）─────────────
        # kill 后能从这里 resume。每 turn 末写一次（不是每 tool_result —— 那
        # 太频繁；turn 末 = LLM 完成一轮思考的自然边界）。
        try:
            _persist_messages_checkpoint(state, messages, turn=turn)
        except Exception as e:
            log.debug("messages checkpoint failed at turn %d: %s", turn, e)

        # ── 转轮记录（issue #733 第一步）───────────────────────────────────
        # 「这一轮为什么没停」从此有一等字段。三条恢复型 continue 各自带
        # reason（truncation_recovery / finish_gate_blocked / void_turn_rolled_back），
        # 这里是唯一的正常路径。测试从此能断言「恢复路径触发过」，而不是靠
        # 翻消息正文猜 —— [[右判决错路径]] 的机械解。
        state.append_transcript(
            "turn_transition", turn=turn, reason="tool_calls_executed",
            n_tool_calls=len(response.tool_calls or []))

    # 触碰到 max_turns（无限制模式不会触发；除非 LLM / API 真的卡死）
    if final_result is None:
        log.warning("agent loop 在 max_turns=%d 时仍未拿到最终回答。", _max_turns)
        # 收尾宽限调用：轮次用尽 ≠ 没东西可交。已经跑完的模拟、已经写好的产物
        # 都是真产出，但旧行为直接把 final_text 写成一句诊断文案 —— 下游拿到的
        # 是"(达到 max_turns 但仍无最终回答)"，等于这一趟白跑。
        #
        # 再给一次**不带工具**的调用，让它把"做到哪了、产物在哪、还差什么"说清楚。
        # 不带工具是关键：带着工具它会继续干活，那就不是收尾而是偷加一轮。
        grace_text = await _grace_wrapup(
            llm=llm, messages=messages, state=state, turn=_max_turns,
        )
        final_result = LoopResult(
            max_turns=_max_turns,
            final_text=grace_text or "(达到 max_turns 但仍无最终回答)",
            turns=_max_turns,
            tool_calls=all_tool_calls,
            messages=messages,
            terminal_cause="turn_cap",
        )

    # ── (6) on_end hooks ─────────────────────────────────────────
    end_ctx = loop_hooks.HookContext(
        harness=harness, state=state, messages=messages, turn=final_result.turns,
    )
    await loop_hooks.run_on_end(hooks, end_ctx, final_result)

    return final_result


def _tc_brief(tc: dict) -> dict:
    return {
        "name": tc.get("function", {}).get("name"),
        "args_preview": (tc.get("function", {}).get("arguments") or "")[:200],
    }


# ── v0.7: messages checkpoint —— kill 后 resume 用 ────────────────────────

def _checkpoint_path(state):
    """每个 run 的 messages 持久化文件位置。

    2026-08-04：改为委托 conversation_store，**不要**在这里再写一遍路径。
    这份 checkpoint 的读者不止 agent_loop 自己 —— chat.py 的续连也要读它
    （e2e8 事故：第一轮 REPL 没跑完就重启，编排历史全丢，而这份 checkpoint
    就躺在旁边没人读）。路径各写一份 = 迟早对不上。
    """
    from core.conversation_store import checkpoint_path
    return checkpoint_path(state)


def _persist_messages_checkpoint(state, messages: list[LLMMessage],
                                   *, turn: int) -> None:
    """每 turn 末把 messages 序列化写盘。atomic 替换。

    格式：{turn, written_at, messages: [...]}。
    """
    path = _checkpoint_path(state)
    payload = {
        "turn": turn,
        "written_at": datetime.utcnow().isoformat() + "Z",
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "tool_call_id": getattr(m, "tool_call_id", None),
                "name": getattr(m, "name", None),
                "tool_calls": getattr(m, "tool_calls", None),
                "reasoning_content": getattr(m, "reasoning_content", None),
            }
            for m in messages
        ],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False),
                    encoding="utf-8")
    tmp.replace(path)


def load_messages_checkpoint(state) -> tuple[list[LLMMessage], int] | None:
    """driver 在 resume 时调：读上次的 messages + 上次跑到第几 turn。

    返 (messages, last_turn) 或 None（checkpoint 不存在）。
    """
    path = _checkpoint_path(state)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("messages checkpoint 读取失败：%s", e)
        return None
    msgs = [
        LLMMessage(
            role=m["role"],
            content=m.get("content"),
            tool_call_id=m.get("tool_call_id"),
            name=m.get("name"),
            tool_calls=m.get("tool_calls"),
            reasoning_content=m.get("reasoning_content"),
        )
        for m in data.get("messages") or []
    ]
    return msgs, int(data.get("turn") or 0)


_GRACE_PROMPT = (
    "轮次预算已用尽，这是最后一次发言，**不要再调用任何工具**。\n"
    "用几句话交代清楚三件事，供接手的人/节点直接使用：\n"
    "1. 已经做完并且落盘的是什么（给产物路径或标识）；\n"
    "2. 做到哪一步被打断的；\n"
    "3. 接着做要从哪里续上 —— 具体到下一步动作，别写「继续完成剩余工作」这种空话。\n"
    "不要道歉、不要复述任务背景。"
)


async def _grace_wrapup(*, llm, messages: list[LLMMessage], state, turn: int) -> str:
    """轮次用尽后的收尾宽限调用 —— 不带工具，只要一段交接说明。

    为什么值得多花这一次调用：轮次用尽不是失败，已经跑完的模拟和写好的产物都是
    真产出；但如果 final_text 只是一句"(达到 max_turns)"，下游就看不见这些东西，
    等于整趟白跑（E2E v23 实测：experiment 撞 40 轮被切断，14 个模拟全跑完，
    决策层却在 retry_reviewer / retry_curator 之间空转三小时）。

    **不给工具**是这次调用的要点：带着工具它会接着干活，那是偷加一轮，不是收尾。
    任何异常都吞掉回退到旧文案 —— 收尾是锦上添花，不能让它把 run 弄挂。
    """
    try:
        wrap_msgs = list(messages) + [
            LLMMessage(role="user", content=_GRACE_PROMPT)
        ]
        resp = await llm.chat(wrap_msgs, tools=None)
        text = (getattr(resp, "content", "") or "").strip()
        if not text:
            return ""
        try:
            state.append_transcript(
                "grace_wrapup", turn=turn, chars=len(text),
            )
        except Exception:
            pass
        return text
    except Exception as e:                      # noqa: BLE001
        log.warning("收尾宽限调用失败（回退到诊断文案）：%s", e)
        return ""


#: 截断也必须留下的 envelope 字段 —— 下游（平台 ingest / 事后审计）就靠它们
#: 判断这次调用是成是败、是谁的锅、**下一步该谁做**。
#:
#: `recovery` 是 2026-09-09 补的。工具早就可以写这一句、界面早就在读这一句
#: （`payload-view.ts` 的 `error.recovery`），中间没有任何一层把它传下去 ——
#: 读端有、写端零，于是界面永远退回按工具类型猜的兜底文案。编译失败那次的
#: "Review the document source" 就是这么来的：稿子没问题，缺的是 latexmk。
_ENVELOPE_KEYS = ("status", "error_code", "error", "recovery")


def _brief(result: Any) -> Any:
    """把过大的 tool 结果截断，方便看 transcript。

    ⚠️ **截 body，永远不截 envelope。**

    上一版超过 500 字节就把整个 dict `json.dumps` 成一个截断**字符串**，于是
    `{"status": "error", ...}` 变成 `'{"status": "error", "error": "…[truncated]'`。
    平台 ingest 判错的写法是 `isinstance(result, dict) and result["status"]`——
    字符串一律判不出错，这些失败全部落成 `tool.completed`，在界面上显示为
    **成功**。本机库里查到 659 条（占真实失败的 39%）。

    出错的结果尤其不能这样丢：错误正文是模型自纠和人排查的唯一依据。
    """
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= 500:
        return result
    # ⚠️ 只对**失败**改写形状。成功结果照旧压成截断字符串 —— 已经有消费方
    # 依赖这一点：experiment 的 contract_audit 拿"只剩截断字符串"当"不算成功"
    # 的保守判据，hypothesis 的 artifact_recovery 直接在字符串里找 `"passed"`。
    # 把成功结果也换成 envelope dict 会静默放宽一道审计、并弄丢 `passed`。
    if not isinstance(result, dict) or result.get("status") not in ("error", "failed"):
        return text[:500] + "...[truncated]"
    kept = {k: result[k] for k in _ENVELOPE_KEYS if k in result}
    rest = {k: v for k, v in result.items() if k not in kept}
    budget = max(200, 500 - len(json.dumps(kept, ensure_ascii=False, default=str)))
    if isinstance(kept.get("error"), str) and len(kept["error"]) > 1200:
        kept["error"] = kept["error"][:1200] + "…[truncated]"
    kept["_body_truncated"] = json.dumps(
        rest, ensure_ascii=False, default=str)[:budget] + "...[truncated]"
    return kept


# ── Resume API ──────────────────────────────────────────────────────────────

async def resume_loop(ctx: PausedRunContext, response_text: str,
                      *, recorded_decision: str | None = None) -> LoopResult:
    """用 user 回答 resume 一个 paused 节点 run。

    流程：
      1. 在 messages 里找到 pending_tool_call_id 对应那条 tool_result 消息
         把它的 content 从 pause placeholder 替换成 user 回答
      2. 写一条 transcript 记录
      3. 调 run_loop 继续

    recorded_decision（#183）：decision package 的答复已被 `record_decision_answer`
    机械解析成权威动作时，把它的人类可读渲染一并回填。只回原始文本（常是裸
    数字 "1"）会让 LLM 按最常见选项集重新解读 —— 实测 review-failed 专用集里
    [1]=RETRY REVIEWER 被复述成 "PROCEED"，据此标 task complete 并对人谎报
    review 已放行。权威动作进 payload 后，复述不可能再与账本分叉。

    如果 resume 之后又 pause 了，会返回新的 paused LoopResult（chat.py 再 resume）。

    注意：resume 用的 messages 是 paused 时保留的那个列表（mutable）。我们替换
    tool_result 后，run_loop 内不会重新写这条（它检查 messages[-1].role != 'tool'
    才进下一轮 LLM），所以 LLM 下一轮会自然看到完整的工具结果序列。
    """
    # 1. 找到 pending tool_result 并替换 content
    target_id = ctx.pending_tool_call_id
    found = False
    for i in range(len(ctx.messages) - 1, -1, -1):
        m = ctx.messages[i]
        if m.role == "tool" and m.tool_call_id == target_id:
            new_payload = {
                "status": "success",
                "response": response_text,
                "asked_by": ctx.pause_event.asking_node_type,
            }
            if recorded_decision:
                # 权威字段放在 response 之后，且措辞明确压过原始文本的歧义。
                new_payload["recorded_decision"] = recorded_decision
                new_payload["authoritative"] = (
                    f"框架已机械记账人工的选择：{recorded_decision}。"
                    "这是权威结果 —— 不要再自行解读上面的 response 原文，"
                    "更不要把它当成别的选项（不同 package 的选项集不同，"
                    "裸数字在不同集里含义不同）。请严格按此动作执行。"
                )
            ctx.messages[i] = LLMMessage(
                role="tool",
                tool_call_id=target_id,
                name=m.name,
                content=json.dumps(new_payload, ensure_ascii=False),
            )
            found = True
            break

    if not found:
        log.warning("resume_loop: 找不到 tool_call_id=%r 的 pending 消息", target_id)
        return LoopResult(
            max_turns=_max_turns,
            final_text="(resume 失败：找不到 pending tool_call)",
            turns=0,
            messages=ctx.messages,
            status="failed",
        )

    ctx.state.append_transcript(
        "loop_resume", pending_tool_call_id=target_id,
        response_preview=response_text[:200],
        recorded_decision=recorded_decision,      # #183 审计：权威动作是否已回填
    )

    # 2. 清掉 pause registry（同一 run 不会被再次找到）+ 磁盘 pending 标记
    clear_pause(ctx.run_id)
    try:
        (ctx.state.root / "pause_pending.json").unlink(missing_ok=True)
    except Exception:
        pass

    # 3. 继续 run_loop
    return await run_loop(ctx.harness, ctx.state, ctx.messages, ctx.llm)
