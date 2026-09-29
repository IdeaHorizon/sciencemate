"""取消要在**必经之路**上生效，不靠每个 loop 自己记得查（issue #284）。

现场（qinp 产品 E2E）：用户 `/stop`，transcript 里 `loop_cancelled` 已记录，
可 0.7 毫秒之后 `data_auto_planning_recovery` 又起来了，接着又跑一轮 —— 取消
之后仍在调模型、烧 token、改 run 状态，最后只能 Ctrl-C 杀进程。

信号传播本来是全的：`chat._do_panic_stop` 会把 `kill_signal` 写进顶层 state
**和每一个 active run 的 state**。缺的是消费方 —— 全仓只有 `run_loop` 的轮初
检查这一处。而模型调用有 9 个直调点（`nodes/data/tools/preprocessing_planner.py`、
`nodes/literature/tools/classify_papers.py`、
`shared/tools/library/cross_model.py` …），自定义 agent loop 更是整个绕过
run_loop。每个调用点各查一遍 = 迟早漏，而且漏的那个不会有人发现。

所以把检查放进两个所有人都得过的咽喉：

  - `LLMClient.chat`      —— 进程里**唯一**发模型请求的地方
  - `tool_registry.execute` —— 唯一派发工具的地方

`RunCancelled` 继承 **BaseException**，理由与 `asyncio.CancelledError` 相同：
取消不是普通错误，不该被 `except Exception` 的重试兜底吞掉再重来一次（那正是
#284 的现场形态）。框架自己在 run_loop / execute_node 收口，转成 cancelled
的 LoopResult，照常走统一 finalize。
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any

# 当前 run 的 State。由 executor.execute_node / agent_loop.run_loop 绑定，
# 覆盖它们之下的一切：自定义 loop、工具、工具里直调的模型请求。
_CURRENT_RUN: ContextVar[Any] = ContextVar("harness_current_run", default=None)


class RunCancelled(BaseException):
    """本 run 已被取消，不许再发起新的模型/工具调用。

    继承 BaseException 而非 Exception —— 见模块 docstring。
    """

    def __init__(self, where: str, signal: dict | None = None) -> None:
        self.where = where
        self.signal = dict(signal or {})
        reason = self.signal.get("reason") or "run 已取消"
        super().__init__(f"{reason}（拦截点：{where}）")


@contextlib.contextmanager
def bind_run(state: Any) -> Iterator[None]:
    """把 state 绑成"当前 run"。可重入（嵌套绑同一个/不同 state 都安全）。"""
    token = _CURRENT_RUN.set(state)
    try:
        yield
    finally:
        _CURRENT_RUN.reset(token)


# 粘性取消标记。**存在的理由**：run_loop 的轮初检查是 `pop("kill_signal")` ——
# 信号被消费掉之后，自定义 loop 再调一次 run_default 就看不到任何取消痕迹，
# 于是继续跑。#284 的现场（loop_cancelled 之后 0.7ms 又起 recovery）就是这么
# 来的。取消是 **run 级终态**，不是一次性事件：一旦取消，这个 run 剩下的时间
# 里谁都别想再发模型/工具调用。只有 chat 收到**新的用户输入**时才清。
STICKY_KEY = "_run_cancelled"


def mark_cancelled(state: Any, signal: dict | None) -> None:
    """把一次性的 kill_signal 固化成 run 级终态。"""
    try:
        state.hook_state[STICKY_KEY] = dict(signal or {"reason": "cancelled"})
    except (AttributeError, TypeError):
        pass


def clear(state: Any) -> None:
    """清取消状态 —— 只该由「新的用户输入」触发（chat 的 turn 起点）。"""
    try:
        state.hook_state.pop(STICKY_KEY, None)
        state.hook_state.pop("kill_signal", None)
    except (AttributeError, TypeError):
        pass


def signal_for(state: Any) -> dict | None:
    """某个 state 的取消状态：粘性标记优先，其次还没被消费的 kill_signal。"""
    try:
        hs = state.hook_state
    except AttributeError:      # 不是 State（测试替身等）→ 当没取消
        return None
    for key in (STICKY_KEY, "kill_signal"):
        sig = hs.get(key)
        if isinstance(sig, dict) and sig:
            return dict(sig)
    return None


def current_signal() -> dict | None:
    """当前绑定 run 的取消状态（没绑定 / 没取消 → None）。"""
    state = _CURRENT_RUN.get()
    return None if state is None else signal_for(state)


def check(where: str) -> None:
    """咽喉检查：已取消就抛 RunCancelled。"""
    sig = current_signal()
    if sig:
        raise RunCancelled(where, sig)
