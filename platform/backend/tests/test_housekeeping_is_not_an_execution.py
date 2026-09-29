"""平台杂活不算"用户的研究在跑"。

现场：会话自动命名（`_autoname`）和聊天执行（`run_worker`）落进同一个无标签
的注册表，于是 `detached_execution_count()` 把两者一起数。后果不是"数字不好
看"——`test_sse_observer_disconnect_does_not_cancel_server_owned_execution`
单跑必红（数到 2）、整套跑才碰巧是 1，而 xdist 一分派就随机红。

一个计数器回答两个问题，就一定有一方拿到错的答案。
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.local_execution import (
    EXECUTION_KIND,
    HOUSEKEEPING_KIND,
    detached_execution_count,
    retain_detached_execution,
    shutdown_detached_executions,
)


@pytest.mark.asyncio
async def test_housekeeping_does_not_inflate_the_execution_count() -> None:
    started = asyncio.Event()

    async def _never_finishes() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(_never_finishes())
    retain_detached_execution(task, kind=HOUSEKEEPING_KIND)
    await started.wait()
    try:
        assert detached_execution_count() == 0, "杂活不该被数成研究执行"
        assert detached_execution_count(kind=HOUSEKEEPING_KIND) == 1
        assert detached_execution_count(kind=None) == 1
    finally:
        await shutdown_detached_executions()


@pytest.mark.asyncio
async def test_shutdown_still_collects_housekeeping() -> None:
    """分类是**计数**的口径，不是生命周期的口径 —— 关服时一个都不许漏。"""
    started = asyncio.Event()

    async def _never_finishes() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(_never_finishes())
    retain_detached_execution(task, kind=HOUSEKEEPING_KIND)
    await started.wait()
    await shutdown_detached_executions()
    assert task.cancelled() or task.done()
    assert detached_execution_count(kind=None) == 0


@pytest.mark.asyncio
async def test_default_kind_is_the_users_execution() -> None:
    """不传 kind 的调用方（聊天执行那条路）语义必须与从前逐字相同。"""
    started = asyncio.Event()

    async def _never_finishes() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(_never_finishes())
    retain_detached_execution(task)
    await started.wait()
    try:
        assert detached_execution_count() == 1
        assert detached_execution_count(kind=EXECUTION_KIND) == 1
    finally:
        await shutdown_detached_executions()
