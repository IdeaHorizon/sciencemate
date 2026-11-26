"""Build-system probes cannot escape cancellation or write outside their run."""

import asyncio
import inspect
import shlex
import sys

import pytest

from core.state import State
from nodes.experiment.tools import build_graph


@pytest.mark.asyncio
async def test_a_stopped_run_never_starts_another_build_probe(tmp_path, monkeypatch):
    state = State.new("experiment", tmp_path / "runs")
    state.kill_event = asyncio.Event()
    state.kill_event.set()
    monkeypatch.setattr(build_graph, "sandbox_mount_roots", lambda _: ([tmp_path], []))
    marker = tmp_path / "must-not-exist"
    with pytest.raises(asyncio.CancelledError):
        result = build_graph._run(
            f"printf effect > {shlex.quote(str(marker))}", state=state, cwd=str(tmp_path)
        )
        if inspect.isawaitable(result):
            await result
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["signal", "task_cancel"])
async def test_inflight_probe_reaps_its_real_payload_before_cancel_returns(
    tmp_path, monkeypatch, how
):
    state = State.new("experiment", tmp_path / "runs")
    state.kill_event = asyncio.Event()
    monkeypatch.setattr(build_graph, "sandbox_mount_roots", lambda _: ([tmp_path], []))
    marker = tmp_path / "payload.pid"
    program = "import os,time;from pathlib import Path;Path('payload.pid').write_text(str(os.getpid()));time.sleep(60)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"
    task = asyncio.create_task(
        build_graph._run(command, state=state, cwd=str(tmp_path), timeout=15)
    )
    try:
        async with asyncio.timeout(8):
            while not marker.exists() and not task.done():
                await asyncio.sleep(0.02)
        assert marker.exists(), await task
        pid = int(marker.read_text())
        if how == "signal":
            state.kill_event.set()
        else:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 8)
        from shared.lib.process_control import alive

        assert not alive(pid)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
