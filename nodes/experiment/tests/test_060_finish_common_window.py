"""All declared objects share the pre-check window, independently of method order.

The injected interleaving runs another real recorded writer after the real
read-only check. Neither producer observations nor check verdicts are mocked.
"""
from __future__ import annotations

import asyncio
import shlex
import sys

import pytest

from core.project_workspace import working_directory
from core.state import State
from nodes.experiment.tools import delivery_transaction, finish, ledger, obligations, run_bootstrap, safe_bash


def _command(program):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"


@pytest.mark.parametrize("method", ["formal-file", "file", "glob-before-check", "glob-after-check"])
def test_new_recorded_producer_after_check_requires_fresh_verification(tmp_path, monkeypatch, method):
    task = "Produce result.txt containing 49 and verify its contents before delivery."
    state = State.new("experiment", tmp_path / "runs", session_id="common-window")
    state.hook_state["node_inputs"] = {"experiment_spec": task}
    if method == "formal-file":
        state.hook_state["node_inputs"]["required_outputs"] = [
            {"id": "result", "kind": "file", "path": "runtime/result.txt"},
        ]
    with asyncio.Runner() as runner:
        established = runner.run(run_bootstrap.establish(state))
        assert ledger.bootstrap_phase(state) == "ready", established
        root = working_directory(state) / "runtime"
        target = root / "result.txt"

        async def write_result(text):
            result = await safe_bash._safe_run_bash(
                state, cmd=_command(f"from pathlib import Path; Path('result.txt').write_text({text!r})"),
                cwd=str(root), timeout=20,
            )
            assert result.get("returncode") == 0, result
            assert finish._producer_rows(ledger.open_ledger(state).records(), target)

        runner.run(write_result("49\n"))
        frozen_a = obligations.obligations_path(state).read_bytes()
        actual_check = finish._check_runner
        checks_observed = []

        async def check_then_write(actual_state, ob):
            result = await actual_check(actual_state, ob)
            assert result["executed"] is True and result["exit_code"] == 0, result
            checks_observed.append(result)
            await write_result("50\n")
            return result

        monkeypatch.setattr(finish, "_check_runner", check_then_write)
        check = {"kind": "check", "cwd": "runtime", "command": _command(
            "from pathlib import Path; assert Path('result.txt').read_text() == '49\\n'"
        ), "expect_exit": 0, "timeout_s": 20}
        checks = [check]
        if method == "file":
            checks.insert(0, {"kind": "file", "path": "runtime/result.txt"})
        elif method.startswith("glob"):
            glob = {"kind": "glob", "pattern": "runtime/*.txt", "min_count": 1}
            checks = [glob, check] if method == "glob-before-check" else [check, glob]
        result = runner.run(finish._finish(
            state, outcome="success", summary="The result was checked.",
            verification=[{"requirement": task, "checks": checks}],
        ))
        assert len(checks_observed) == 1
        assert target.read_text() == "50\n"
        assert obligations.obligations_path(state).read_bytes() == frozen_a
        assert result.get("status") == "success_unsupported", result
        assert any(gap.get("gap") == "output_changed_during_check" for gap in result.get("gaps", [])), result
        assert delivery_transaction.read_manifest(state) is None
        # A remains fixed; repair and a fresh actual check may close normally.
        monkeypatch.setattr(finish, "_check_runner", actual_check)
        runner.run(write_result("49\n"))
        retried = runner.run(finish._finish(
            state, outcome="success", summary="A fresh check now verifies the repaired result.",
            verification=[{"requirement": task, "checks": checks}],
        ))
        assert retried.get("status") == "success" and retried.get("outcome") == "success", retried
        assert obligations.obligations_path(state).read_bytes() == frozen_a
