"""A mistaken C object must remain withdrawable for a genuinely file-free task.

This is the reverse constraint to the known object-omitted producer gap.  Both
checks and the failed debug writer are real processes. A test-owned external
edit makes its optional debug output unsupported; an old model proposal
does not become a new requirement or permanently taint an identical check.
"""
from __future__ import annotations

import asyncio
import json
import shlex
import sys

import pytest

from core import sandbox
from core.project_workspace import working_directory
from core.state import State
from nodes.experiment.tools import (
    delivery_transaction, finish, ledger, obligations, run_bootstrap, safe_bash,
)


def _command(program):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"


@pytest.mark.parametrize("mistaken_object_source", ["deliverable", "file-method"])
def test_check_only_task_can_withdraw_an_unrelated_changed_debug_object(tmp_path, mistaken_object_source):
    task = "Run an arithmetic self-test confirming that 2 + 2 equals 4; no result file is required."
    state = State.new("experiment", tmp_path / "runs", session_id="owner")
    state.hook_state["node_inputs"] = {"experiment_spec": task}
    with asyncio.Runner() as runner:
        try:
            established = runner.run(run_bootstrap.establish(state))
            assert ledger.bootstrap_phase(state) == "ready", established
            frozen_a = obligations.obligations_path(state).read_bytes()
            assert not any(ob["kind"] in {"file", "glob"} for ob in obligations.frozen(state))
            output = working_directory(state) / "runtime"
            debug = output / "debug.txt"
            produced = runner.run(safe_bash._safe_run_bash(
                state, cwd=str(output), timeout=20,
                cmd=_command("from pathlib import Path; Path('debug.txt').write_text('debug only\\n'); "
                             "raise SystemExit(7)"),
            ))
            assert produced.get("returncode") == 7, produced
            assert debug.read_bytes() == b"debug only\n"
            rows = ledger.open_ledger(state).records()
            producer = finish._producer_rows(rows, debug)[-1]["action_id"]
            failed = next(row for row in rows if row.get("kind") == "action_settled"
                          and row.get("action_id") == producer)
            assert failed["exit_code"] == 7
            # Test-owned external mutation makes the optional debug object unsupported.
            # The actual file-free task must still permit withdrawing that proposal.
            debug.write_bytes(b"external debug edit\n")
            check = {
                "kind": "check", "cwd": str(output), "timeout_s": 20,
                "command": _command("assert 2 + 2 == 4; print('ARITHMETIC_OK')"),
                "expect_exit": 0, "expect_stdout_contains": "ARITHMETIC_OK",
            }
            first_methods = [check]
            if mistaken_object_source == "file-method":
                first_methods = [{"kind": "file", "path": str(debug)}, check]
            first = runner.run(finish._finish(
                state, outcome="success", summary="The arithmetic check passed.",
                verification=[{"requirement": task, "checks": first_methods}],
                deliverables=[{"path": str(debug)}] if mistaken_object_source == "deliverable" else None,
            ))
            assert first.get("status") == "success_unsupported", first
            assert any(row.get("gap") == "identity_changed_since_the_action"
                       for row in first.get("gaps", [])), first
            assert delivery_transaction.read_manifest(state) is None

            corrected = runner.run(finish._finish(
                state, outcome="success", summary="The arithmetic self-test passed; the debug file is not a deliverable.",
                verification=[{"requirement": task, "checks": [check]}],
            ))

            assert corrected.get("status") == "success" and corrected.get("outcome") == "success", corrected
            assert obligations.obligations_path(state).read_bytes() == frozen_a
            assert debug.read_bytes() == b"external debug edit\n"
            current = ledger.open_ledger(state).records()
            assert failed in current
            payloads = [row for row in current if row.get("kind") == "action_started"
                        and row.get("tool") == "safe_run_bash"]
            assert len(payloads) == 1, "Correcting a mistaken object must not rerun its failed writer."
            checks = [row for row in current if row.get("kind") == "action_started"
                      and row.get("tool") == "finish.check"]
            assert len(checks) == 2 and all(row["command"] == check["command"] for row in checks)
            for observed in checks:
                settled = next(row for row in current if row.get("kind") == "action_settled"
                               and row.get("action_id") == observed["action_id"])
                assert settled.get("executed") is True and settled["exit_code"] == 0
            raw = json.loads(state.read_artifact(corrected["artifact_ids"]["raw_results"])["content"])
            clean = json.loads(state.read_artifact(corrected["artifact_ids"]["clean_results"])["content"])
            assert failed in raw["ledger_records"]
            assert raw["declared_deliverables"] == []
            assert next(row for row in clean["actions"] if row["action_id"] == producer)["exit_code"] == 7
            limitations = clean["delivery"]["limitations"]
            assert any(producer in item and "does not prove" in item and "repaired" in item
                       for item in limitations), limitations
            assert delivery_transaction.read_manifest(state) is not None
        finally:
            if state.sandbox_manifest:
                sandbox.evict_state_attempt(state)
