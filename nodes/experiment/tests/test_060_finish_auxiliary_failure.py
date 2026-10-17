"""Real execution: an auxiliary exit must not replace task acceptance.

The same report/check cannot hide a genuinely failed required computation.
No execution, observation, check result or delivery transaction is mocked.
"""
import asyncio
import json
import shlex
import sys

import pytest

from core.state import State
from nodes.experiment.tools import delivery_transaction, finish, ledger, run_bootstrap, safe_bash


def _command(program):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"


@pytest.mark.parametrize("stage_exit", [0, 7], ids=["auxiliary-only", "actual-computation-failed"])
def test_report_error_and_required_computation_are_judged_separately(tmp_path, stage_exit):
    state = State.new("experiment", tmp_path / "runs", session_id="auxiliary-failure")
    runtime = state.root / "outputs/experiment/runtime"
    event_path = ledger.ledger_dir(state) / "events.jsonl"
    # This task explicitly requires the computation to exit normally. The
    # formal check reads its real executor fact, not a report's success claim.
    stage_check = _command(
        "import json; from pathlib import Path; "
        f"rows = [json.loads(line) for line in Path({str(event_path)!r}).read_text().splitlines()]; "
        "aid = next(r['action_id'] for r in rows if r.get('kind') == 'action_started' "
        "and r.get('tool') == 'safe_run_bash'); "
        "code = next(r['exit_code'] for r in rows if r.get('kind') == 'action_settled' "
        "and r.get('action_id') == aid); assert code == 0, ('required computation exit', code)"
    )
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Run the computation to normal exit, verify result 49, and deliver its report.",
        "required_outputs": [{"id": "computation_completed", "kind": "check",
                              "command": stage_check, "expect_exit": 0}],
    }
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready"

    async def run(command):
        return await safe_bash._safe_run_bash(state, cmd=command, cwd=str(runtime), timeout=20)

    computation = asyncio.run(run(_command(
        "from pathlib import Path; Path('result.txt').write_text('49\\n'); "
        f"raise SystemExit({stage_exit})")))
    assert computation.get("returncode") == stage_exit, computation
    report_command = _command(
        "from pathlib import Path; Path('report.json').write_text('{\"result\":49}')"
    ) + "; ls missing-report-directory"
    reported = asyncio.run(run(report_command))
    assert reported.get("returncode") == 2, reported
    report = runtime / "report.json"
    unchanged = (report.read_bytes(), report.stat().st_ino, report.stat().st_mtime_ns)
    verification = [{"requirement": "Verify the computation's result and report.", "checks": [{
        "kind": "check", "command": _command(
            "import json; from pathlib import Path; "
            f"assert Path({str(runtime / 'result.txt')!r}).read_text() == '49\\n'; "
            f"assert json.loads(Path({str(report)!r}).read_text())['result'] == 49"
        ), "expect_exit": 0,
    }]}]
    request = dict(outcome="success", summary="Computed result and checked report.",
                   deliverables=[{"path": str(runtime / "result.txt")}, {"path": str(report)}],
                   verification=verification)
    result = asyncio.run(finish._finish(state, **request))
    assert (report.read_bytes(), report.stat().st_ino, report.stat().st_mtime_ns) == unchanged
    rows = ledger.open_ledger(state).records()
    payloads = [r for r in rows if r.get("kind") == "action_started" and r.get("tool") == "safe_run_bash"]
    assert len(payloads) == 2
    assert [r["action_id"] for r in finish._producer_rows(rows, report)] == [payloads[1]["action_id"]]

    if stage_exit == 0:
        assert result["status"] == "success" and result["outcome"] == "success", result
        raw = json.loads(state.read_artifact(result["artifact_ids"]["raw_results"])["content"])
        assert any(r.get("kind") == "action_settled" and r.get("action_id") == payloads[1]["action_id"]
                   and r.get("exit_code") == 2 for r in raw["ledger_records"])
        assert any("nonzero exits" in text for text in result["delivery"]["limitations"])
    else:
        assert result["status"] == "success_unsupported", result
        gap = next(g for g in result["gaps"] if g.get("obligation") == "computation_completed")
        assert gap["gap"] == "check_failed"
        assert "required computation exit" in gap["evidence"]["stderr_tail"]
        context = next(r for r in result["failed_actions"] if r["action"] == payloads[1]["action_id"])
        assert str(report) in context["paths"] and context["exit_code"] == 2
        assert context["command"] == report_command
        assert "gaps" in result["next_action"]
        repeated = asyncio.run(finish._finish(state, **request))
        assert repeated["status"] == "success_unsupported"
        assert repeated["failed_actions"] == result["failed_actions"]
        assert delivery_transaction.read_manifest(state) is None
        assert len([r for r in ledger.open_ledger(state).records()
                    if r.get("kind") == "action_started" and r.get("tool") == "safe_run_bash"]) == 2
