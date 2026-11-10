"""Harness-owned waiting, without model turns or a second job state machine.

The adapter owns physical facts. This file holds only the continuation of an
explicit wait, scoped to the existing run, so restarting a worker can reattach
before asking the model to act again. Submission is deliberately not a wait.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from asyncio import sleep
from pathlib import Path
from time import monotonic

from core.cancellation import RunCancelled, signal_for

_POLL_BACKOFF = (0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
_CANCEL_CHECK_S = 0.2


def _path(state) -> Path:
    return Path(state.root) / "job_wait.json"


def load(state) -> dict | None:
    try:
        value = json.loads(_path(state).read_text())
    except FileNotFoundError:
        return None
    if value["project_id"] != str(state.project_id) or value["run_id"] != state.run_id:
        raise ValueError("Job wait does not belong to this project/run")
    arguments = value["arguments"]
    tool = value["tool"]
    if not (
        (
            tool == "remote_job"
            and arguments.get("action") == "wait"
            and arguments.get("resource_id") == value["identity"].get("resource_id")
            and arguments.get("job_id") == value["identity"].get("job_id")
        )
        or (
            tool == "wait_for_external_job"
            and arguments.get("scheduler") == value["identity"].get("scheduler", "ssh")
            and arguments.get("job_id") == value["identity"].get("job_id")
        )
    ):
        raise ValueError("Continuation must select a read-only wait on the bound job")
    return value


def _write(state, value: dict) -> None:
    path = _path(state)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def register(state, tool: str, arguments: dict, identity: dict) -> None:
    value = {
        "project_id": str(state.project_id),
        "run_id": state.run_id,
        "tool": tool,
        "arguments": arguments,
        "identity": identity,
        "tool_call_id": state.hook_state.get("_current_tool_call_id_being_executed"),
    }
    previous = load(state)
    if previous:
        if all(previous[key] == value[key] for key in ("tool", "arguments", "identity")):
            return
        if not previous.get("finished"):
            raise ValueError("Another job wait already owns this run's continuation")
    _write(state, value)
    state.append_transcript("job_wait_started", tool=tool, identity=identity)


def finish(state) -> None:
    value = load(state)
    if value:
        value["finished"] = True
        _write(state, value)


def acknowledge(state) -> None:
    """Call only after the actual result is in the messages checkpoint."""
    _path(state).unlink(missing_ok=True)


def check_cancelled(state) -> None:
    signal = signal_for(state)
    event = getattr(state, "kill_event", None)
    if not signal and event is not None and event.is_set():
        signal = {"reason": "job wait cancelled"}
    if signal:
        raise RunCancelled("job_wait", signal)


async def cancellable(state, awaitable):
    task = asyncio.ensure_future(awaitable)
    try:
        while not task.done():
            check_cancelled(state)
            await asyncio.wait({task}, timeout=_CANCEL_CHECK_S)
        return task.result()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def poll(
    state, observe, done, *, interval=None, steady_interval=None, max_wait_s=None, initial=None
):
    """Wait on adapter facts; elapsed time consumes no AgentLoop rounds.

    An explicit observation budget may return an active checkpoint. By default
    the harness waits until terminal facts or a diagnostic condition arrives.
    Cancellation interrupts both sleeping and an outstanding status query.
    """
    deadline = None if max_wait_s is None else monotonic() + max_wait_s
    attempt = 0
    result = initial
    while True:
        if result is None:
            result = await cancellable(state, observe())
        if done(result) or (deadline is not None and monotonic() >= deadline):
            return result
        ceiling = steady_interval if steady_interval is not None else _POLL_BACKOFF[-1]
        gap = (
            interval
            if interval is not None
            else min(_POLL_BACKOFF[attempt] if attempt < len(_POLL_BACKOFF) else ceiling, ceiling)
        )
        attempt += 1
        if deadline is not None:
            gap = min(gap, max(0, deadline - monotonic()))
        await cancellable(state, sleep(gap))
        result = None


async def resume(state, messages, *, turn: int) -> None:
    """Resolve only this run's selected wait before another model invocation.

    The original tool call result is the delivery key. If its checkpoint was
    committed before a crash, acknowledge it without another completion. Older
    checkpoints receive a notice, never an invented assistant/tool exchange.
    """
    value = load(state)
    if not value:
        return
    call_id = value.get("tool_call_id")
    delivery = {
        key: value.get(key)
        for key in ("project_id", "run_id", "tool", "arguments", "identity", "tool_call_id")
    }
    marker = "job-wait:" + hashlib.sha256(json.dumps(delivery, sort_keys=True).encode()).hexdigest()
    delivered = any(
        (call_id and m.role == "tool" and m.tool_call_id == call_id) or marker in (m.content or "")
        for m in messages
    )
    if not delivered:
        from core import tool_registry
        from core.agent_loop import _inject_notice, _persist_messages_checkpoint

        result = await tool_registry.execute(value["tool"], state, **value["arguments"])
        _inject_notice(
            state,
            messages,
            turn=turn,
            source="job_wait_resumed",
            text=marker
            + "\nHarness resumed the selected job wait:\n"
            + json.dumps(result, ensure_ascii=False),
        )
        _persist_messages_checkpoint(state, messages, turn=turn)
    acknowledge(state)
