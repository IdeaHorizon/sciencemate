"""Heavy symbolic work cannot own the parent event loop or outlive cancellation."""

import asyncio
from types import SimpleNamespace

import pytest

from core.state import State
from shared.lib import derivation_ledger, derivation_worker
from shared.tools.library import derivation_check


@pytest.mark.asyncio
async def test_expensive_parse_has_a_hard_deadline_and_reaps_the_process(monkeypatch):
    monkeypatch.setattr(derivation_worker, "CAS_TIMEOUT_SECONDS", 1.5)
    processes = []
    create = asyncio.create_subprocess_exec

    async def track(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", track)
    task = asyncio.create_task(derivation_check._check_step(None, "factorial(1000000000)", "0"))
    ticks = 0
    while not task.done():
        await asyncio.sleep(0.05)
        ticks += 1
    result = await task
    assert ticks >= 3, "the parent loop must remain responsive while CAS runs"
    assert result["error_code"] == "computation_timeout", result
    assert result["verification"]["status"] == "inconclusive"
    assert processes and all(process.returncode is not None for process in processes)


@pytest.mark.asyncio
async def test_cancel_interrupts_computation_without_recording_a_verdict(monkeypatch):
    monkeypatch.setattr(derivation_worker, "CAS_TIMEOUT_SECONDS", 10)
    events = []
    state = SimpleNamespace(
        kill_event=asyncio.Event(), append_transcript=lambda event, **kw: events.append((event, kw))
    )
    task = asyncio.create_task(derivation_check._check_step(state, "factorial(1000000000)", "0"))
    await asyncio.sleep(0.5)
    state.kill_event.set()
    result = await asyncio.wait_for(task, timeout=5)
    assert result["error_code"] == "computation_cancelled", result
    assert result["verification"]["status"] == "inconclusive"
    assert not [event for event, _ in events if event == "derivation_check"]


@pytest.mark.asyncio
async def test_completed_work_is_registered_once_in_the_calling_runs_ledger(tmp_path):
    state = State.new(node_type="_reviewer", base_dir=tmp_path / "runs")
    result = await derivation_check._check_step(state, "(a+b)**2", "a**2+2*a*b+b**2")
    assert result["verification"]["status"] == "verified", result
    entries = derivation_ledger.entries(state)
    assert len(entries) == 1
    assert entries[0]["probe"] == result["verification"]["probe"]
    assert entries[0]["tool"] == "check_step"


@pytest.mark.asyncio
async def test_counterexample_search_uses_one_outer_budget_and_preserves_its_evidence(tmp_path):
    state = State.new(node_type="_reviewer", base_dir=tmp_path / "runs")
    result = await derivation_check._find_counterexample(state, "x+1", "x", budget=64)
    assert result["found"] is True, result
    assert result["verification"]["status"] == "failed"
    assert len(derivation_ledger.entries(state)) == 1
