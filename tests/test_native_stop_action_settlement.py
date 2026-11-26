"""A stopped Experiment action must settle after its actual process tree exits."""

import asyncio
import shlex
import sys

import pytest

from core import isolation
from core.state import State
from nodes.experiment.tools import ledger, safe_bash
from shared.lib import process_control


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["signal", "task_cancel"])
async def test_stop_reaps_payload_and_descendant_before_action_is_settled(
    tmp_path, monkeypatch, how
):
    native = isolation.native_backend_name()
    if native is None:
        pytest.skip("requires a native sandbox")
    backend = isolation.select_backend(native)
    if isolation.Invariant.WRITE_BOUNDARY not in backend.capabilities():
        pytest.skip("native write boundary unavailable")
    monkeypatch.setenv(isolation.EXECUTOR_ENV, native)
    isolation._reset_for_tests()
    state = State.new("experiment", tmp_path / "runs")
    state.kill_event = asyncio.Event()
    ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    work = tmp_path / "work"
    work.mkdir()
    # This test exercises execution/settlement after admission, with the real
    # sandbox and process owner. Frozen scientific-manifest checks are separate.
    monkeypatch.setattr(safe_bash, "resolve_required_workdir", lambda *a: (str(work), None))
    monkeypatch.setattr(safe_bash, "_frozen_attempt_capability_gap", lambda *a: None)
    monkeypatch.setattr(safe_bash, "_ensure_hardened_attempt_manifest", lambda *a: None)
    script = work / "payload.py"
    script.write_text(
        "import os,subprocess,sys,time\n"
        "from pathlib import Path\n"
        "Path('parent.pid').write_text(str(os.getpid()))\n"
        "subprocess.Popen([sys.executable,'-I','-c',"
        "\"import os,time;from pathlib import Path;"
        "Path('child.pid').write_text(str(os.getpid()));time.sleep(60)\"])\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    task = asyncio.create_task(
        safe_bash._exec_and_log(
            state,
            f"{shlex.quote(sys.executable)} -I {shlex.quote(str(script))}",
            timeout=30,
            cwd=str(work),
            sandbox_roots=([work], []),
            sandbox_write_targets=[str(work)],
        )
    )
    pids = []
    try:
        async with asyncio.timeout(15):
            while not (work / "child.pid").exists() and not task.done():
                await asyncio.sleep(0.02)
        assert (work / "child.pid").exists(), await task
        pids = [int((work / name).read_text()) for name in ("parent.pid", "child.pid")]
        if how == "signal":
            state.kill_event.set()
            result = await asyncio.wait_for(task, 10)
            assert result["status"] == "cancelled", result
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
        assert all(not process_control.alive(pid) for pid in pids), pids
        rows = ledger.open_ledger(state).records()
        starts = [row for row in rows if row["kind"] == "action_started"]
        ends = [row for row in rows if row["kind"] == "action_settled"]
        assert len(starts) == len(ends) == 1
        assert starts[0]["action_id"] == ends[0]["action_id"]
        assert ends[0]["state"] == "cancelled"
        # Recovery uses the existing immutable ledger; no unclosed action.
        assert all(
            action["state"] == "cancelled"
            for action in ledger.project_actions(ledger.open_ledger(state).records())
        )
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for name in ("parent.pid", "child.pid"):
            marker = work / name
            if marker.exists():
                process_control.Group.of(int(marker.read_text())).kill()
        isolation._reset_for_tests()
