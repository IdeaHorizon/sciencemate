"""Agent Loop Hook 系统。

让 owner 在 agent loop 的 4 个钩子点注入自己的逻辑：
  - on_turn_start    每轮 LLM 调用前（可 inject system message）
  - on_llm_response  LLM 返回后、工具调度前（只观察 —— 记录、统计）
  - on_turn_end      工具调度后、下轮 LLM 前（可 inject system message）
  - on_end           loop 终止后（只观察 —— 结果汇总）

每个 hook 由一个 LoopHook 对象描述，里面 4 个钩子点回调都可选。
回调可以是 sync 或 async 函数，接受 HookContext，返回 None 或 list[LLMMessage]。

`on_turn_end` 发现确定性的 terminal condition 时，可以在
`ctx.state.hook_state['_loop_terminal']` 写入
`{'final_text': '...', 'status': 'completed', 'requested_by': '<hook>'}`。
默认 loop 会在当前 turn 的 tool messages 完整落盘后正常收口；这不同于外部
cancel，不会把能力缺口误记成用户取消。

注册：在自己的 module 顶层调 register_loop_hook(LoopHook(...))。
启用：在 harness yaml 里写
    loop_hooks:
      - your_hook_name

memory_delta hook **永远启用**（在 agent_loop.py 里硬绑），其它都按 yaml。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .harness import NodeHarness
from .llm import LLMMessage, LLMResponse, framework_notice, is_framework_notice
from .state import State

log = logging.getLogger("loop_hooks")


@dataclass
class HookContext:
    """Hook 拿到的上下文 —— 想读 / 写就改这个。"""
    harness: NodeHarness
    state: State
    messages: list[LLMMessage]
    turn: int
    tool_call_records: list[dict] = field(default_factory=list)  # 当前 turn 的工具调用记录


@dataclass
class LoopHook:
    """一个 agent loop hook。

    每个回调可选；不需要的设 None 跳过。

    emits：本 hook 会往 transcript 写哪些事件名（capability 声明）。
    `mechanical.transcript_event` / `evidence_events` 那类机械判据依赖
    某个事件时，run 启动期会校验"至少有一个已启用 hook 声明 emits 它"——
    否则 fail-loud（QCContractError），而不是跑完一整个 run 后因事件缺失
    fail-closed（2026-07 架构审计 item 5：scientific-capital 分支迁移换掉
    hook 层但 QC 契约没跟上，产生运行期才暴露的"不可能契约"）。
    只需声明 QC 契约会引用的事件，普通日志类事件不用列。
    """
    name: str
    description: str = ""
    on_turn_start: Callable[[HookContext], Any] | None = None
    on_llm_response: Callable[[HookContext, LLMResponse], Any] | None = None
    on_turn_end: Callable[[HookContext], Any] | None = None
    on_end: Callable[[HookContext, Any], Any] | None = None
    # v2.1 收尾闸：模型不再调工具、准备结束时跑。返回非空 list[LLMMessage] =
    # **否决本次收尾**，注入这些消息再给一轮。用于"节点声明的强制自检必须真的
    # 跑过"这类不变量 —— on_end 太晚（loop 已结束，agent 无法补救），
    # on_turn_start 又够不到最后那一轮。
    on_before_finish: Callable[[HookContext], Any] | None = None
    emits: tuple[str, ...] = ()


_HOOKS: dict[str, LoopHook] = {}


def register_loop_hook(hook: LoopHook) -> None:
    """注册一个 hook 到全局表。在 hook module 顶层调用。"""
    if hook.name in _HOOKS:
        log.warning("Hook %r 已存在，被覆盖。", hook.name)
    _HOOKS[hook.name] = hook


def get_loop_hook(name: str) -> LoopHook | None:
    return _HOOKS.get(name)


def list_hooks(names: list[str]) -> list[LoopHook]:
    """按 names 顺序返回 hook 对象。找不到的报警 + 跳过。"""
    out: list[LoopHook] = []
    for name in names:
        h = _HOOKS.get(name)
        if h is None:
            log.warning("harness 声明的 hook %r 未注册，跳过。", name)
            continue
        out.append(h)
    return out


def all_hook_names() -> list[str]:
    return sorted(_HOOKS.keys())


async def _maybe_await(result: Any) -> Any:
    """同步或 async 都可以 —— 这里统一 await。"""
    if asyncio.iscoroutine(result):
        return await result
    return result


def _as_framework_notices(msgs: list[LLMMessage]) -> list[LLMMessage]:
    """把 hook 返回的消息统一包成 framework-notice（user 角色 + 归属信封）。

    **在这个咽喉做，不在每个 hook 里做** —— 全仓 20+ 处 `LLMMessage(role="system")`
    逐个改是写名单：漏一个就漏一个，且以后新加的 hook 默认又回到 system 角色、
    没人会发现（护栏要扫盘，不要写名单）。所有 hook 注入都流经这里，包成一条
    规则，未来的 hook 自动被覆盖，hook 作者不需要记住这件事。

    结构性消息（带 tool_calls / tool 结果）不动 —— 那不是"框架说话"。
    """
    out: list[LLMMessage] = []
    for m in msgs:
        if m.tool_calls or m.role == "tool" or not (m.content or "").strip():
            out.append(m)
        elif is_framework_notice(m):
            out.append(m)          # 已经是信封（幂等，别套两层）
        else:
            out.append(framework_notice(m.content or ""))
    return out


async def run_on_turn_start(hooks: list[LoopHook], ctx: HookContext) -> list[LLMMessage]:
    """跑所有 hook 的 on_turn_start，收集要 inject 的消息。"""
    injected: list[LLMMessage] = []
    for h in hooks:
        if h.on_turn_start is None:
            continue
        try:
            result = await _maybe_await(h.on_turn_start(ctx))
        except Exception as e:
            log.warning("Hook %r on_turn_start 失败：%s", h.name, e)
            continue
        if isinstance(result, list):
            injected.extend(result)
        elif isinstance(result, LLMMessage):
            injected.append(result)
    return _as_framework_notices(injected)


async def run_on_llm_response(hooks: list[LoopHook], ctx: HookContext,
                               response: LLMResponse) -> None:
    for h in hooks:
        if h.on_llm_response is None:
            continue
        try:
            await _maybe_await(h.on_llm_response(ctx, response))
        except Exception as e:
            log.warning("Hook %r on_llm_response 失败：%s", h.name, e)


async def run_on_turn_end(hooks: list[LoopHook], ctx: HookContext) -> list[LLMMessage]:
    injected: list[LLMMessage] = []
    for h in hooks:
        if h.on_turn_end is None:
            continue
        try:
            result = await _maybe_await(h.on_turn_end(ctx))
        except Exception as e:
            log.warning("Hook %r on_turn_end 失败：%s", h.name, e)
            continue
        if isinstance(result, list):
            injected.extend(result)
        elif isinstance(result, LLMMessage):
            injected.append(result)
    return _as_framework_notices(injected)


async def run_on_end(hooks: list[LoopHook], ctx: HookContext, loop_result: Any) -> None:
    for h in hooks:
        if h.on_end is None:
            continue
        try:
            await _maybe_await(h.on_end(ctx, loop_result))
        except Exception as e:
            log.warning("Hook %r on_end 失败：%s", h.name, e)


async def run_on_before_finish(hooks: list[LoopHook], ctx: HookContext) -> list[LLMMessage]:
    """收尾闸：任一 hook 返回消息 = 否决收尾，注入后再给一轮。"""
    out: list[LLMMessage] = []
    for h in hooks:
        if h.on_before_finish is None:
            continue
        try:
            result = await _maybe_await(h.on_before_finish(ctx))
        except Exception:
            log.exception("hook %s on_before_finish 失败（不阻断 loop）", h.name)
            continue
        if result:
            out.extend(result)
    return out
