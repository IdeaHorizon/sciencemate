"""「这个会话在忙」不能是一个能被忘记清掉的布尔。

## 现场（wangd 2026-08-18，会话 c9deb4f2）

用户连发三次「继续吧」，每次都红，detail 是 `The project Harness session
is busy`。取证：worker 进程**活着但空闲**（transcript 17 分钟没写），后端
堵在读它 stdout 上 —— turn 的 RPC 故意没有超时（一次科研轮次可以跑几小时，
把墙钟当取消理由是错的）。平台随后把那条 run 判成 stale，`busy` 却还挂着。
这个会话被锁死到重启后端为止。

## 判据

原来 `busy` 是内存 bool：一处置 True，另一处的 finally 清 False。它的正确性
靠"每条路径都记得清"来维持 —— 而那维持不住，这一晚就是证据。

现在它**从 `_operation_lock` 推导**：锁由 `async with` 持有，return / raise /
取消都必然释放。"忘了清"这条路径从此不存在，而不是"我们更小心了"。

用户原话把这件事说得最准：「这个 session 它就是一堆聊天历史的一个文本文档
是吧？」—— 是。占用是这一次操作的属性，不是那份文档的属性。
"""
from __future__ import annotations

import asyncio

import pytest


def _session():
    from app.services.harness_sessions import _ProjectHarnessSession

    session = object.__new__(_ProjectHarnessSession)
    session._operation_lock = asyncio.Lock()
    return session


def test_occupancy_is_derived_and_cannot_be_set_by_hand() -> None:
    session = _session()
    assert session.conversation_in_flight is False
    with pytest.raises(AttributeError):
        session.conversation_in_flight = True   # type: ignore[misc]


@pytest.mark.asyncio
async def test_a_claim_releases_itself_even_when_the_operation_raises() -> None:
    """异常路径也必须释放 —— 这正是旧实现漏掉的那条。"""
    session = _session()

    with pytest.raises(RuntimeError):
        async with session.claim():
            assert session.conversation_in_flight is True
            raise RuntimeError("turn blew up")
    assert session.conversation_in_flight is False, (
        "抛异常之后会话还占着 —— 下一条消息会被永远判成插话"
    )


@pytest.mark.asyncio
async def test_a_claim_releases_itself_when_cancelled() -> None:
    """取消同理（后端关停、客户端断开都会走到这里）。"""
    session = _session()
    started = asyncio.Event()

    async def _long_operation() -> None:
        async with session.claim():
            started.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(_long_operation())
    await started.wait()
    assert session.conversation_in_flight is True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.conversation_in_flight is False


def test_no_code_path_assigns_occupancy_anymore() -> None:
    """接线：源码里不许再出现占用的赋值。

    只测属性行为不够 —— 得确认没有哪条路径又偷偷加回一个标志（这一晚
    "写了没人调 / 接错入口" 已经各栽过一次）。
    """
    import inspect
    import re

    from app.services import harness_sessions

    src = inspect.getsource(harness_sessions)
    assignments = re.findall(r"\.conversation_in_flight\s*=\s*(?!=)", src)
    assert not assignments, (
        f"又出现了 {len(assignments)} 处占用赋值 —— 占用应当只由 claim() 表达"
    )
