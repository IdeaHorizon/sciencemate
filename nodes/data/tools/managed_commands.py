"""Data adapters use Core's process owner and the run's existing filesystem scope."""

from __future__ import annotations

import asyncio
import math
import subprocess
import sys
from pathlib import Path

from core import cancellation
from core.project_workspace import run_scratch_directory, validate_tool_cwd
from core.sandbox import SandboxLimits, model_tool_roots
from core.state import State
from shared.lib.cancellable_subprocess import spawn_and_wait


async def run_data_command(
    state: State,
    argv: list[str],
    *,
    cwd: str | None = None,
    timeout: float = 120,
    text: bool = True,
    capture_output: bool = True,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """Run one adapter command. There is no host or background-thread fallback."""
    if cancellation.signal_for(state) or (
        state.kill_event is not None and state.kill_event.is_set()
    ):
        raise asyncio.CancelledError
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("adapter timeout must be a positive finite number")
    if not capture_output:
        raise ValueError("adapter commands must capture their output")
    scratch = run_scratch_directory(state)
    scratch.mkdir(parents=True, exist_ok=True)
    writable, readonly = model_tool_roots(state)
    directory = validate_tool_cwd(state, cwd) if cwd else scratch
    spill: dict[str, str] = {}
    status, code, stdout, stderr = await spawn_and_wait(
        *argv,
        state=state,
        timeout=timeout,
        cwd=str(directory),
        writable_roots=writable,
        readonly_roots=readonly,
        spill_dir=scratch,
        spill=spill,
        sandbox_limits=SandboxLimits(
            memory_bytes=2 * 1024**3,
            cpus=2,
            pids=64,
            storage_bytes=8 * 1024**3,
            output_bytes=8 * 1024**2,
        ),
    )
    if status == "cancelled":
        raise asyncio.CancelledError
    if status == "spawn_failed":
        raise OSError(stderr.decode("utf-8", "replace"))

    def complete_output(label: str, tail: bytes) -> bytes:
        path = spill.get(label + "_path")
        if not path:
            return tail
        with Path(path).open("rb") as source:
            raw = source.read(8 * 1024**2 + 1)
        if len(raw) > 8 * 1024**2:
            raise RuntimeError(
                f"adapter {label} exceeds its output budget; complete output: {path}"
            )
        if sys.platform == "win32":
            from shared.lib.native_output import to_utf8

            raw = to_utf8(raw)
        return raw

    stdout = complete_output("stdout", stdout)
    stderr = complete_output("stderr", stderr)
    if status == "timeout":
        raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr)
    # Match subprocess text mode, including Windows universal-newline handling.
    output = (
        stdout.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
        if text
        else stdout
    )
    error = (
        stderr.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
        if text
        else stderr
    )
    result = subprocess.CompletedProcess(argv, code, output, error)
    if check:
        result.check_returncode()
    return result
