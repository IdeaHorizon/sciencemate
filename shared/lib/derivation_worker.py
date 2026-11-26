"""Bound CAS work in a cancellable process; the calling run owns its ledger."""

from __future__ import annotations

import asyncio
import inspect
import json
import tempfile
from functools import wraps
from pathlib import Path
from typing import Any

CAS_TIMEOUT_SECONDS = 30.0
_IN_WORKER = False
_OPERATIONS = frozenset(
    {
        "_check_step",
        "_find_counterexample",
        "_dimensional_check",
        "_limit_check",
        "_interval_check",
    }
)


def bounded_cas(function):
    """Keep parsing, simplification and numerical fallback inside one deadline."""

    @wraps(function)
    async def execute(state, *args, **kwargs):
        if _IN_WORKER:
            return await function(state, *args, **kwargs)
        from core.sandbox import SandboxLimits
        from shared.lib.cancellable_subprocess import spawn_and_wait, the_interpreter_for_model_code

        bound = inspect.signature(function).bind(state, *args, **kwargs)
        bound.apply_defaults()
        arguments = dict(bound.arguments)
        arguments.pop("state")
        arguments.pop("_", None)
        tool = function.__name__.removeprefix("_")
        with tempfile.TemporaryDirectory(prefix="harness-cas-") as directory:
            root = Path(directory)
            source, destination = root / "request.json", root / "response.json"
            source.write_text(
                json.dumps({"operation": function.__name__, "arguments": arguments}),
                encoding="utf-8",
            )
            # Works in source, wheel and bundled runtimes, without depending on
            # the user's cwd or on a second Python installation.
            package_root = str(Path(__file__).resolve().parents[2])
            program = (
                f"import sys; sys.path.insert(0, {package_root!r}); "
                "from shared.lib.derivation_worker import main; main()"
            )
            status, code, _stdout, stderr = await spawn_and_wait(
                the_interpreter_for_model_code(),
                "-c",
                program,
                str(source),
                str(destination),
                state=state,
                cwd=directory,
                timeout=CAS_TIMEOUT_SECONDS,
                writable_roots=[root],
                sandbox_home="own",
                sandbox_limits=SandboxLimits(
                    memory_bytes=2 * 1024**3,
                    cpus=1,
                    pids=32,
                    walltime_seconds=max(1, int(CAS_TIMEOUT_SECONDS)),
                    storage_bytes=64 * 1024**2,
                    output_bytes=4 * 1024**2,
                ),
            )
            if status != "done" or code != 0:
                return {
                    "status": "error",
                    "error_code": "computation_" + ("failed" if status == "done" else status),
                    "verification": {"status": "inconclusive", "method": "none", "tool": tool},
                    "error": (
                        f"{tool} 未完成（{status}, exit={code}，预算 {CAS_TIMEOUT_SECONDS:g}s）。"
                        "未取得验证结论；可以拆小表达式、采用有界数值核验或诚实保留未决。"
                    ),
                    "stderr_tail": stderr.decode("utf-8", "replace")[-1500:],
                }
            try:
                if destination.stat().st_size > 4 * 1024**2:
                    raise ValueError("CAS response exceeds output budget")
                response = json.loads(destination.read_text(encoding="utf-8"))
                result = response["result"]
                events = response["events"]
                if not isinstance(result, dict) or not isinstance(events, list):
                    raise ValueError("invalid CAS response")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return {
                    "status": "error",
                    "error_code": "computation_response_invalid",
                    "error": f"{tool} 未返回完整结果：{exc}。未登记验证成功。",
                }
            from shared.lib import derivation_ledger

            for event in events:
                if event.get("event") != derivation_ledger.EVENT:
                    raise ValueError("Unexpected CAS ledger event")
                derivation_ledger.record(state, **{k: v for k, v in event.items() if k != "event"})
            return result

    return execute


def main():
    """Fixed worker entry point. No runtime state or writable ledger is passed."""
    global _IN_WORKER
    import sys

    from shared.tools.library import derivation_check

    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    operation = request["operation"]
    if operation not in _OPERATIONS:
        raise ValueError("Unknown CAS operation")
    _IN_WORKER = True
    events: list[dict[str, Any]] = []

    class Computation:
        def append_transcript(self, event, **fields):
            events.append({"event": event, **fields})

    result = asyncio.run(
        getattr(derivation_check, operation)(Computation(), **request["arguments"])
    )
    Path(sys.argv[2]).write_text(json.dumps({"result": result, "events": events}), encoding="utf-8")
