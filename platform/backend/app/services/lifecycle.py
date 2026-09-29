"""App Server 自己知不知道"我正在关机"。

## 为什么需要这一个事实（2026-08-12 实测）

重启 App Server，在跑的那条 run 落成：

    status  = failed
    failure = "Harness runtime process exited before replying (exit code -15)"
    → UI: "Review the request, then continue or revise the request."

`-15` 是 SIGTERM —— **我们自己发的**。研究本身一点问题没有，是平台把它掐了。
而用户拿到的建议是"改改你的请求再试"，那是对着一件没发生的事给的建议。

这又是那条形状：**两件不同的事按同一条规则处理**。

    「这次研究失败了」        → 终态 failed，请修改请求
    「平台重启把它打断了」    → 可恢复态，工作没错，接着做就行

区分这两件事需要的信息**本来就在手里** —— 只是从没被记下来：只有 App Server
知道自己进没进关机流程。子进程那边只看得见一个 `-15`，而 `-15` 可能来自我们
的重启，也可能来自别人（OOM killer、运维手工 kill）。

## 为什么是一个独立模块

`local_execution`（写终态的）和 `main`（跑 lifespan 的）都要用它，互相 import
会成环。一个只有一个布尔值的模块比在任何一边挖个洞都干净。

## 不做的事

这里**不做**优雅停机、不等在途请求、不做超时。它只回答一个问题：现在是不是
关机流程里。做多了就会变成第二套调度逻辑，而 uvicorn 已经有一套了。
"""

from __future__ import annotations

import asyncio
import logging
import signal

logger = logging.getLogger(__name__)

_shutting_down = False

#: 关机事件与它所属的 loop。事件让等待方在**信号到达的那一刻**醒过来，而不是
#: 靠轮询把延迟摊到一个节拍上。loop 要在装信号处理器时就抓住 —— `begin_shutdown`
#: 可能从信号处理器里被调用，那时再去问"当前 loop 是谁"是不可靠的。
_loop: asyncio.AbstractEventLoop | None = None
_event: asyncio.Event | None = None

#: 事件桥没装上时（单测直接调 `begin_shutdown`、非主线程）等待方的兜底节拍。
#: 有它就不会出现"永远醒不过来"，代价只是最坏多等这么久。
_FALLBACK_TICK_S = 0.5


def begin_shutdown() -> None:
    """进入关机流程。"""
    global _shutting_down
    _shutting_down = True
    _wake_waiters()


def _wake_waiters() -> None:
    if _event is None or _loop is None or _event.is_set():
        return
    try:
        _loop.call_soon_threadsafe(_event.set)
    except RuntimeError:
        # loop 已经关了：等待方要么早醒了，要么会走兜底节拍。
        pass


async def wait_for_shutdown() -> None:
    """一直等到关机开始 —— 长活的循环用它来收手。

    ## 为什么这个 await 必须存在

    uvicorn 的优雅关机**先关监听 socket、再等在途连接排空**。一条永远不结束的
    SSE 流就是一条永远排不空的连接 —— 2026-08-23 实测：挂一条流时 SIGTERM 之后
    20 秒进程还在（端口已放、库连接还攥着），把客户端断掉，1 秒就退了。三次
    生产重启撞的都是它，每次只能 SIGKILL 收尾，于是 lifespan 的 `finally`
    （停采集器、终止 harness worker）**一次都没跑过**。

    旗从 2026-08-12 起就有了（见 `watch_for_shutdown_signal`），只是没有任何
    长活循环读过它。这个 await 是给它们的读法。
    """
    if _shutting_down:
        return
    while not _shutting_down:
        event = _ensure_event()
        if event is None:
            await asyncio.sleep(_FALLBACK_TICK_S)
            continue
        try:
            await asyncio.wait_for(event.wait(), timeout=_FALLBACK_TICK_S * 20)
        except (TimeoutError, asyncio.TimeoutError):
            continue


def _ensure_event() -> asyncio.Event | None:
    """本 loop 上的关机事件；跨 loop（测试里常见）就退回兜底节拍。"""
    global _event, _loop
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        return None
    if _event is None or _loop is not running:
        return None
    return _event


def install_shutdown_event() -> None:
    """在 lifespan 启动段调用一次，把事件绑到当前 loop 上。"""
    global _event, _loop
    _loop = asyncio.get_running_loop()
    _event = asyncio.Event()
    if _shutting_down:
        _event.set()


def watch_for_shutdown_signal() -> None:
    """收到 SIGTERM/SIGINT 的**那一刻**就立旗。

    ## 为什么不能只靠 lifespan 的收尾段（2026-08-12 实测）

    时序（同一秒内，两个进程交错写同一份日志）：

        00:55:26.857  新 App Server 启动扫描：Marked 1 orphaned … 并回收 worker
        00:55:26.863  老 App Server 的在途 run：Local execution failed  ← 记成 failed
        00:55:26.927  老 App Server：Shutting down Research Platform    ← 旗到这才立

    uvicorn 的关机顺序是**先停止接受连接、等在途连接排空，最后才跑 lifespan 的
    收尾段**。而子进程早在排空阶段就没了 —— 等 `finally` 执行时，那条 run 已经
    被记成 `failed` 并告诉用户"改改请求再试"。

    旗立在收尾段 = 立在伤害之后。所以钩到信号本身：从"关机被请求"那一刻起，
    这个事实就是真的。

    ## 包住 uvicorn 的 handler，不是替掉

    uvicorn 自己也要收这个信号来启动优雅关机。抢掉它的 handler 会让服务器根本
    不退出 —— 修一个观测问题的代价不能是把关机本身弄坏。所以取出原 handler、
    立完旗再调它。

    必须在 uvicorn 装完 handler **之后**调用（lifespan 启动段正好在那之后）。
    """
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous = signal.getsignal(sig)
        except (ValueError, OSError):  # 非主线程 / 平台不支持
            continue

        def handler(signum, frame, _previous=previous):
            begin_shutdown()
            if callable(_previous):
                _previous(signum, frame)

        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            # 装不上不该拖垮启动：最坏情况退回 lifespan 收尾段那次 `begin_shutdown()`，
            # 也就是修这条之前的行为。
            logger.warning("Could not watch %s for shutdown; falling back to lifespan", sig)


def is_shutting_down() -> bool:
    """现在是不是关机流程里 —— 用来区分"研究失败"和"我们把它掐了"。"""
    return _shutting_down


def reset_for_tests() -> None:
    """测试之间复位。进程内的全局状态泄漏到下一个测试是很难查的那类 bug。"""
    global _shutting_down, _event, _loop
    _shutting_down = False
    _event = None
    _loop = None
