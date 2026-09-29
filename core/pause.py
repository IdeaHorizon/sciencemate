"""Pause / resume mechanism (v0.2).

设计目标：
  - 任何节点（含子节点）调 `request_human_input` 工具，agent_loop 检测到 pause
    返回，让上层（chat.py）处理 I/O，然后用 user 回答 resume。
  - 多级嵌套也支持：子节点 pause → 在 run_node 工具结果里冒泡 → 父节点 loop
    跟着 pause → 一路冒到 chat.py。chat.py 用 user 回答 resume 最深那级。
    深层完成后，自动 cascade resume 父级 run_node 调用。

  解耦：工具产生 pause **事件**；agent_loop 检测并 unwind；chat.py 决定怎么问 user。
  工具本身不再 `input()`。

核心数据：
  - PauseEvent：question / options / context / 哪个 run / 哪个 tool_call 在等
  - 全局 PAUSED_RUNS 注册表：跨函数边界保留 paused 状态
      run_id → {state, messages, harness, pending_tool_call_id, parent_run_id, ...}

API：
  - register_pause(run_id, ctx)
  - get_paused_run(run_id) -> ctx | None
  - get_deepest_paused() -> ctx | None
  - clear_pause(run_id)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .harness import NodeHarness
    from .llm import LLMClient, LLMMessage
    from .state import State


@dataclass
class PauseEvent:
    """request_human_input / present_decision_package 等产生的 pause 事件（结构化）。

    `metadata` 字段用来标记特殊 pause 类型（如 `type='decision_package'`），
    chat.py / web UI 可基于 metadata 做特化 UI（如 countdown 倒计时 + 自动选
    recommended option）。
    """
    question: str
    context: str = ""
    options: list[str] = field(default_factory=list)
    asking_node_type: str = ""
    asking_run_id: str = ""
    pending_tool_call_id: str = ""
    metadata: dict = field(default_factory=dict)   # v0.4: decision package 等特化 pause 用
    #: 工具给出的 pause payload **原样**。命名字段是它的便捷视图，不是它的替代品。
    #:
    #: 为什么要有这一份：这个类此前只有上面那几个命名字段，而构造点
    #: （agent_loop）是**逐个字段手抄**的。手抄的那份必然比源头少几个字段，
    #: 且加新字段时**两边都不报错**——只是流里悄悄少一块：
    #:
    #:   - v0.4.3：漏拷 metadata → _ask_decision_package 永不触发，
    #:     auto-approve 永远选 options[0]=PROCEED，无视 reviewer 推荐。
    #:   - 2026-08-19：decision package 改成带 id 的结构化选项
    #:     （option_details / offer_id / decision_id），在这里被整段丢掉——
    #:     于是"选项有身份"这件事在第一跳就死了。
    #:
    #: 同一条教训在 chat.py 的回复契约里已经写过一次（"整份摊开，不逐个字段
    #: 手抄"）。这里补上：新字段默认活着，想丢它得特意去丢。
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        # payload 打底、命名字段覆盖：命名字段是被规范化过的（asking_* 有回退、
        # options 保证是 list），payload 提供工具给出的其余一切。
        out = dict(self.payload)
        out.update({
            "question": self.question,
            "context": self.context,
            "options": list(self.options),
            "asking_node_type": self.asking_node_type,
            "asking_run_id": self.asking_run_id,
            "pending_tool_call_id": self.pending_tool_call_id,
            "metadata": dict(self.metadata),
        })
        return out

    @classmethod
    def from_payload(cls, data: dict, *, pending_tool_call_id: str = "",
                     default_node_type: str = "", default_run_id: str = "") -> "PauseEvent":
        """从工具的 pause_event dict 构造 —— **整份带走**，命名字段只是规范化视图。

        构造点只此一处：想让某个字段在传输中消失，得先把它从 payload 里删掉，
        而不是"忘了在构造函数里写一行"。
        """
        data = dict(data or {})
        return cls(
            question=str(data.get("question") or ""),
            context=str(data.get("context") or ""),
            options=list(data.get("options") or []),
            asking_node_type=str(data.get("asking_node_type") or default_node_type),
            asking_run_id=str(data.get("asking_run_id") or default_run_id),
            pending_tool_call_id=pending_tool_call_id,
            metadata=dict(data.get("metadata") or {}),
            payload=data,
        )

    # ── 结构化选项：有 id 的那份 ──────────────────────────────────────────

    def option_details(self) -> list[dict]:
        """带 id 的选项。空列表 = 这个 pause 没给结构化选项（自由文本问答）。"""
        details = self.payload.get("option_details")
        return [d for d in details if isinstance(d, dict)] if isinstance(details, list) else []

    def choice_ids(self) -> tuple[str, ...]:
        return tuple(str(d["id"]) for d in self.option_details() if d.get("id"))

    @property
    def offer_id(self) -> str:
        return str(self.payload.get("offer_id") or "")

    def recommended_index(self) -> int:
        """无人值守要替人选的那一项，**已按选项数夹紧**。

        为什么在这里而不是各调用点自己算：pause_driver 里有两处在各自
        `int(meta.get("recommended_option_index", 0))` + clamp —— 同一个判断两份
        实现，改一处不改另一处不报错（无人值守选错项 = 悄悄替人做了别的决定）。

        取值优先读呈递自己的字段（`to_pause_payload()` 摊开在 payload 顶层），
        metadata 只作老 pause 的兜底：recommended 是**呈递的**事实，不是 pause
        的事实，它的家在呈递里。
        """
        raw = self.payload.get("recommended_option_index")
        if raw is None:
            raw = (self.metadata or {}).get("recommended_option_index", 0)
        try:
            index = int(raw)
        except (TypeError, ValueError):
            index = 0
        if not self.options:
            return 0
        return max(0, min(index, len(self.options) - 1))


@dataclass
class PausedRunContext:
    """单个 paused 节点 run 的完整恢复上下文。

    所有字段够让 resume_loop 从原地继续：
      - state / messages / harness / llm
      - pending_tool_call_id：要把哪个 tool_call 的 result 替换成 user 回答
      - parent_run_id：cascade resume 用（深层完成后回 parent 那个 run_node 调用）
      - parent_tool_call_id：parent 那边的 run_node call 的 id
    """
    run_id: str
    state: "State"
    messages: "list[LLMMessage]"
    harness: "NodeHarness"
    llm: "LLMClient"
    pending_tool_call_id: str
    pause_event: PauseEvent
    parent_run_id: str | None = None
    parent_tool_call_id: str | None = None


# ── 全局注册表（进程级单例，chat.py / agent_loop / run_node 共用）─────────

_PAUSED_RUNS: dict[str, PausedRunContext] = {}


# ── "这个 pause 有人管吗" ────────────────────────────────────────────────────
#
# E2E-5a 实测死锁（82 分钟，靠人发现）：`present_decision_package` 注册了 pause，
# 但**创建它的那一轮已经结束了**（transcript 里 loop_pause 与
# continuous_followup_suppressed 同一秒）。于是没有任何人在等这个答复，
# auto-approve 永远不会被调用。
#
# 而 continuous 有一条保护："有 pause 挂着就别抢方向盘"（防两个控制面打架，
# 那次 false-PROCEED 事故的教训）。这条保护的**前提是"pause 有人管"** ——
# 而注册表里根本没有这个信息，`list_paused()` 只答"有没有登记"。
# **前提没被验证** → pause 没人答 → continuous 不敢动 → 永久死锁。
#
# `--continuous` 的定义是无人值守。任何能让它无限期停住的东西都是 bug。
#
# 修法不是加超时兜底，是**把假设变成注册表里的事实**：谁开始等答复，就登记
# 自己是驾驶员。于是"没人管"从"猜"变成"查得到"。
_DRIVEN: set[str] = set()


def claim_driver(run_id: str) -> None:
    """登记：我开始等这个 pause 的答复了。"""
    _DRIVEN.add(run_id)


def release_driver(run_id: str) -> None:
    _DRIVEN.discard(run_id)


def has_driver(run_id: str) -> bool:
    return run_id in _DRIVEN


def undriven_pauses() -> list[PausedRunContext]:
    """登记着、但没有任何人在等答复的 pause —— 它们永远不会被答复。

    两种来源：创建它的那一轮已经结束（E2E-5a 现场）；进程重启后遗留。
    两种都是"无人值守"承诺的破口。
    """
    return [c for c in _PAUSED_RUNS.values() if c.run_id not in _DRIVEN]


def register_pause(ctx: PausedRunContext) -> None:
    _PAUSED_RUNS[ctx.run_id] = ctx


def get_paused_run(run_id: str) -> PausedRunContext | None:
    return _PAUSED_RUNS.get(run_id)


def clear_pause(run_id: str) -> None:
    _PAUSED_RUNS.pop(run_id, None)
    _DRIVEN.discard(run_id)


def list_paused() -> list[PausedRunContext]:
    return list(_PAUSED_RUNS.values())


def get_deepest_paused() -> PausedRunContext | None:
    """找最深一层（没人是它子节点的）的 paused run。"""
    if not _PAUSED_RUNS:
        return None
    # 谁的 run_id 不是别人的 parent_run_id → 它是叶子
    parents = {c.parent_run_id for c in _PAUSED_RUNS.values() if c.parent_run_id}
    leaves = [c for rid, c in _PAUSED_RUNS.items() if rid not in parents]
    if leaves:
        return leaves[0]
    return next(iter(_PAUSED_RUNS.values()))


def clear_all() -> None:
    """主要给测试用。"""
    _PAUSED_RUNS.clear()
    _ACTIVE_RUNS.clear()
    _DRIVEN.clear()


# ── Active (running, not paused) run registry ──────────────────────────────────
#
# Phase 1：让 `inject_into_node` / `cancel_node` 工具能用 child_run_id 找到目标
# child 的 State。run_node 工具在起 child 时 register；child 结束时 unregister。
# paused child 也算"还活着"，跟 PAUSED_RUNS 互补查询（先 active 再 paused）。

@dataclass
class ActiveRunInfo:
    """正在跑的 child run 的轻量索引。

    state ref 共享给 inject/cancel 工具操作 hook_state；
    parent_run_id 用于关联追溯。
    """
    run_id: str
    node_type: str
    state: "State"
    parent_run_id: str | None = None
    sub_run_id: str | None = None
    started_at: str = ""


_ACTIVE_RUNS: dict[str, ActiveRunInfo] = {}


def register_active(info: ActiveRunInfo) -> None:
    _ACTIVE_RUNS[info.run_id] = info


def unregister_active(run_id: str) -> None:
    _ACTIVE_RUNS.pop(run_id, None)


def get_active_run(run_id: str) -> ActiveRunInfo | None:
    return _ACTIVE_RUNS.get(run_id)


def list_active_runs() -> list[ActiveRunInfo]:
    return list(_ACTIVE_RUNS.values())


def find_child_state(run_id: str):
    """统一查询：先 active 再 paused，返 State 或 None。

    inject_into_node / cancel_node 工具用 —— LLM 不关心 child 是 running 还是
    paused，都该能 inject / cancel。
    """
    info = _ACTIVE_RUNS.get(run_id)
    if info is not None:
        return info.state
    paused = _PAUSED_RUNS.get(run_id)
    if paused is not None:
        return paused.state
    return None
