"""Cancelling the owning coroutine must leave no running command (#1379)."""
import asyncio
import sys
from types import SimpleNamespace

import pytest

from shared.lib import process_control
from shared.lib.cancellable_subprocess import spawn_and_wait


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
async def test_task_cancellation_reaps_its_process_before_propagating(
    tmp_path, monkeypatch, managed,
):
    marker = tmp_path / "pid"
    args = (sys.executable, "-c",
            "import os,time;from pathlib import Path;"
            f"Path({str(marker)!r}).write_text(str(os.getpid()));time.sleep(60)")
    events = []
    if managed:
        from core import isolation

        class Launch:
            argv = args
            cwd = str(tmp_path)
            env = None
            unmet = ()
            stop_argv = ()

            def terminate(self):
                events.append("terminate")
                return False

            def cleanup(self):
                events.append("cleanup")

        monkeypatch.setattr(isolation, "select_backend", lambda: SimpleNamespace(
            name="test", prepare=lambda spec, state: Launch()))
        monkeypatch.setattr(isolation, "record_enforcement_once", lambda *a: None)

    task = asyncio.create_task(spawn_and_wait(
        *args, state=SimpleNamespace(kill_event=None), timeout=30,
        cwd=str(tmp_path), writable_roots=[tmp_path] if managed else None))
    pid = None
    try:
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(0.01)
        pid = int(marker.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not process_control.alive(pid), "cancel returned while the command was still alive"
        if managed:
            assert events == ["terminate", "cleanup"]
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if pid is not None:
            process_control.Group.of(pid).kill()


@pytest.mark.asyncio
async def test_cancellation_during_process_creation_keeps_the_owner(tmp_path, monkeypatch):
    created, release = asyncio.Event(), asyncio.Event()
    original = asyncio.create_subprocess_exec
    child = None

    async def delayed_spawn(*args, **kwargs):
        nonlocal child
        child = await original(*args, **kwargs)
        created.set()
        await release.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    task = asyncio.create_task(spawn_and_wait(
        sys.executable, "-c", "import time;time.sleep(60)",
        state=SimpleNamespace(kill_event=None), timeout=30))
    try:
        await asyncio.wait_for(created.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not process_control.alive(child.pid)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if child is not None:
            process_control.Group.of(child.pid).kill()
