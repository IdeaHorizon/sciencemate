"""An unselected optional failure must not depend on candidate execution order.

Both payloads are real managed subprocesses. The fixed natural task requires
one successful result, not every matching file and not a failure-free history.
No producer observation, check runner or completion verdict is mocked.
"""
from __future__ import annotations

import asyncio
import json
import shlex
import sys

import pytest

from core.project_workspace import working_directory
from core.state import State
from nodes.experiment.tools import delivery_transaction, finish, ledger, obligations, run_bootstrap, safe_bash


TASK = (
    "Produce at least one new valid text result. Additional candidate attempts "
    "are optional: unsuccessful candidates need not be delivered, but retain "
    "their actual failures in the execution record."
)


@pytest.mark.parametrize("good_first", [False, True], ids=["bad-then-good", "good-then-bad"])
def test_sufficient_glob_candidates_do_not_require_repairing_unselected_failure(tmp_path, good_first):
    with asyncio.Runner() as runner:
        state = State.new("experiment", tmp_path / "runs", project_id="glob-ordering", session_id="owner")
        state.hook_state["node_inputs"] = {
            "experiment_spec": TASK,
            "prereg_assignment": {"kind": "none", "reason": "isolated software verification"},
        }
        established = runner.run(run_bootstrap.establish(state))
        assert ledger.bootstrap_phase(state) == "ready", established
        assert all(row["kind"] in {"task", "artifact"} for row in obligations.frozen(state))
        frozen_a = obligations.obligations_path(state).read_bytes()
        output = working_directory(state) / "runtime"
        assert output.is_dir()
        good, bad = output / "z_good.txt", output / "a_bad.txt"
        paths = {"good": good, "bad": bad}
        contents = {"good": "valid candidate\n", "bad": "failed optional candidate\n"}
        expected_exits = {"good": 0, "bad": 7}
        ids = {}
        order = ["good", "bad"] if good_first else ["bad", "good"]
        for label in order:
            program = (
                f"from pathlib import Path; Path({paths[label].name!r}).write_text({contents[label]!r}); "
                f"raise SystemExit({expected_exits[label]})"
            )
            result = runner.run(safe_bash._safe_run_bash(
                state, cmd=f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}",
                cwd=str(output), timeout=20,
            ))
            assert result.get("returncode") == expected_exits[label], result
            rows = ledger.open_ledger(state).records()
            starts = [row for row in rows if row.get("kind") == "action_started"
                      and row.get("tool") == "safe_run_bash"]
            ids[label] = starts[-1]["action_id"]

        assert good.read_bytes() == contents["good"].encode()
        assert bad.read_bytes() == contents["bad"].encode()
        led = ledger.open_ledger(state, write=True)
        rows = led.records()
        payloads = [row for row in rows if row.get("kind") == "action_started"
                    and row.get("tool") == "safe_run_bash"]
        assert [row["action_id"] for row in payloads] == [ids[label] for label in order]
        for label, path in paths.items():
            assert finish._producers(rows, path) == [ids[label]]
            settled = [row for row in rows if row.get("kind") == "action_settled"
                       and row.get("action_id") == ids[label]]
            assert len(settled) == 1 and settled[0]["exit_code"] == expected_exits[label]

        verification = [{"requirement": TASK, "checks": [{
            "kind": "glob", "pattern": str(output / "*.txt"), "min_count": 1,
        }]}]
        evaluation = runner.run(finish.evaluate(state, led, verification))
        predicate = next(row for row in evaluation["checks"] if row["name"] == "task_verification_0")
        evidence = predicate["evidence"]
        assert predicate["passed"] is True
        assert evidence["matched"] == 2 and evidence["min_count"] == 1
        assert evidence["selected"] == [str(good)]
        assert evidence["unselected_count"] == 1
        assert evidence["producers"] == [ids["good"]]
        assert [row["path"] for row in evidence["selected_objects"]] == [str(good)]
        objects = [row for row in evaluation["checks"] if row.get("assessment_kind") == "delivery_object"]
        assert [row["evidence"]["path"] for row in objects] == [str(good)]
        assert not evaluation["gaps"], evaluation
        assert obligations.obligations_path(state).read_bytes() == frozen_a

        closed = runner.run(finish._finish(
            state, outcome="success", summary="One valid candidate was produced; the optional failed candidate is not delivered.",
            verification=verification,
        ))
        rows = led.records()
        assert len([row for row in rows if row.get("kind") == "action_started"
                    and row.get("tool") == "safe_run_bash"]) == 2
        assert any(row.get("kind") == "action_settled" and row.get("action_id") == ids["bad"]
                   and row.get("exit_code") == 7 for row in rows)
        assert good.read_bytes() == contents["good"].encode()
        assert bad.read_bytes() == contents["bad"].encode()
        assert obligations.obligations_path(state).read_bytes() == frozen_a
        print(json.dumps({
            "order": order, "selected": evidence["selected"],
            "unselected_failed_action": ids["bad"], "status": closed.get("status"),
            "gaps": closed.get("gaps", []), "contradictions": closed.get("contradictions", []),
        }, sort_keys=True))
        assert closed.get("status") == "success" and closed.get("outcome") == "success", closed
        assert evaluation["success_supported"] is True, evaluation
        assert delivery_transaction.read_manifest(state) is not None
