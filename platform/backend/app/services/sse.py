"""建流式响应**只有这一条路** —— 因为流必须能被关机中断。

## 根因（2026-08-23 实测，三次生产重启都撞它）

uvicorn 的优雅关机顺序是：先关监听 socket → **等在途连接排空** → 才跑 lifespan
的收尾段。而 SSE 流是一条按设计就不会自己结束的连接：

    execution.py 的流   只在 run 走到终态时 break
    chat.py 的流        只在 done/error 时 break，否则永远发 keepalive

`running` / `waiting_permission` / `waiting_human` 都不是终态。所以只要有人挂在
一个没跑完的会话页上，SIGTERM 之后进程就永远退不掉。三步判别实验：

    A 无入站连接           → SIGTERM 后 1 秒干净退出
    B 挂一条 SSE 流        → 20 秒还活着（端口已放、库连接还攥着）
    C 在 B 的状态下断客户端 → 1 秒后端自己退了

后果不只是"关不掉"：lifespan 的 `finally`（停采集器、`detach_all()` 放开
harness worker）**一次都没执行过**，因为根本没走到那一步。

## 为什么是一个模块而不是在那两个循环里各加一行

那两处是**今天认识的**长活循环。加两行 = 名单式修法，第三个 `while True` 写进来
照样把关机卡死，而且不报错（[[护栏要扫盘，不要写名单]]）。

所以把**合法的那条路命名出来**：流式响应一律经过 `sse_response`，它替所有流
统一接上关机。再配一条扫盘测试钉住"`StreamingResponse` 只许在本模块里构造"——
新端点绕过去就红，不靠人记得。

## 断流要有交代，不能默默 EOF

前端对"没有终止帧就断开"的处理是抛 `ExecutionReadContractError`，UI 文案是
「返回了本版应用无法安全展示的数据」—— 对着一次重启说这句话是错的。所以关机时
发一个 `event: reconnect` 帧：它不是终态、不谎称 run 结束，只是如实说
"服务端要走了，从你自己的游标接着来"。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping

from fastapi.responses import StreamingResponse

from app.services.lifecycle import is_shutting_down, wait_for_shutdown

if TYPE_CHECKING:  # pragma: no cover - 只做类型
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

SSE_MEDIA_TYPE = "text/event-stream"

#: 关机时发给客户端的最后一帧。**不是** `end` —— `end` 的语义是"这条 run 走完了"，
#: 拿它冒充重启就是让前端把一条还在跑的 run 记成终态。
RECONNECT_FRAME = (
    "event: reconnect\n"
    f"data: {json.dumps({'reason': 'server_shutdown'}, separators=(',', ':'))}\n\n"
)

_DEFAULT_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    # 浏览器默认请求 gzip/br；开发态 Next 代理会压缩 SSE，并把小事件缓冲到
    # 压缩块攒够后才交给浏览器。identity 保证每条进度事件立即可见。
    "Content-Encoding": "identity",
}


async def until_shutdown(source: AsyncIterator[str]) -> AsyncIterator[str]:
    """把任意 SSE 生成器包成"关机就收手"的那一种。

    每次取下一块都与关机赛跑：先到关机就发 `reconnect` 帧收尾，源生成器按正常
    的 `aclose()` 路径关掉（它的 `finally` 照跑，观察者注销、队列退订都不丢）。
    """
    iterator = source.__aiter__()
    # ── 只接管「开流之后才开始的关机」（2026-08-23）──────────────────────────
    #
    # 这个机制的职责很窄：让 uvicorn **排空阶段**等着的那些连接能结束。而排空
    # 等的正是"信号到达时已经开着的连接"—— 信号之后 uvicorn 根本不再 accept，
    # 所以"开流时已经在关机中"在生产里不是一条真实路径。
    #
    # 把它也接管过来会造成真实损失：chat 流的生成器体里有副作用（起 worker、
    # 登记 detached execution），赛跑瞬间判关机赢就等于那一步一次没跑过 ——
    # `test_a_restart_leaves_the_run_recoverable_not_failed` 立刻转红：重启打断
    # 的那一轮不再留下 `staleReason: app_server_shutdown`，用户拿到的是
    # 「改改请求再试」，对着一件没发生的事给建议。
    #
    # 我第一版正是这么写的（外加一条"已在关机就直接发帧返回"的短路），被那条
    # 既有测试当场抓住。留下这段是因为它看起来更"干净"，下一个人很可能再写一遍。
    #
    # 「那把 worker 交给 `shutdown_detached_executions()` 收尾不就行了」——不行，
    # 它是 `task.cancel()`，不是等它跑完。切早了那条失败记录就是真的没了。
    #
    # 这一支不设上限，它的界在**进程层**：启动脚本的 `--timeout-graceful-shutdown`
    # （扫盘测试钉着每个启动点都得有）。两条修复正是在这里合上的 —— 主修管
    # 「信号之后开着的连接」，兜底管这条窄路和将来任何绕过本模块的写法。
    if is_shutting_down():
        async for chunk in _plain(iterator):
            yield chunk
        return

    shutdown = asyncio.ensure_future(wait_for_shutdown())
    try:
        while True:
            nxt = asyncio.ensure_future(iterator.__anext__())
            # 让源生成器先起步再赛跑：副作用发生在它的第一个 await 之前。
            await asyncio.sleep(0)
            if not nxt.done():
                await asyncio.wait({nxt, shutdown}, return_when=asyncio.FIRST_COMPLETED)
            if nxt.done():
                try:
                    chunk = nxt.result()
                except StopAsyncIteration:
                    return
                yield chunk
                continue
            # 关机先到：取消在飞的那一次取值，发一帧交代，收手。
            nxt.cancel()
            await asyncio.gather(nxt, return_exceptions=True)
            yield RECONNECT_FRAME
            return
    finally:
        shutdown.cancel()
        await asyncio.gather(shutdown, return_exceptions=True)
        await _aclose(iterator)


async def _plain(iterator) -> AsyncIterator[str]:
    try:
        while True:
            try:
                yield await iterator.__anext__()
            except StopAsyncIteration:
                return
    finally:
        await _aclose(iterator)


async def _aclose(iterator) -> None:
    aclose = getattr(iterator, "aclose", None)
    if aclose is not None:
        await aclose()


def sse_response(
    source: AsyncIterator[str],
    *,
    release: "AsyncSession | None",
    headers: Mapping[str, str] | None = None,
) -> StreamingResponse:
    """建一条 SSE 响应 —— **全仓唯一**允许构造 StreamingResponse 的地方。

    `release` 是必填的表态：这条流**要不要攥着请求级的数据库会话**。

    FastAPI 的 `Depends(get_db)` 在响应**结束**后才收尾；一条可以活几小时的
    事件流因此会把一个开着事务的连接攥到底（2026-09-09 node20：7 条
    `idle in transaction` 各挂着 `SELECT runs.id …`，全是浏览器开着的事件流，
    连接池被白占）。只读的流把会话传进来，第一个字节发出前就归还；真要在流里
    写库的（chat 那条一整轮都在写）传 `None`，并在调用点写明为什么。
    """
    merged = dict(_DEFAULT_HEADERS)
    if headers:
        merged.update(headers)

    async def _released() -> AsyncIterator[str]:
        if release is not None:
            await release.close()
        async for chunk in source:
            yield chunk

    return StreamingResponse(
        until_shutdown(_released()),
        media_type=SSE_MEDIA_TYPE,
        headers=merged,
    )
