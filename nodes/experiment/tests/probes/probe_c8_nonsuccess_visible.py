"""C8 probe: honest non-success operation closure must reach Core status.

The probe intentionally drives the real operation completion and on-end audit
paths.  Its exit status is the gate: all assertions must pass.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from core.agent_loop import LoopResult
from core.executor import finalize_run
from core.harness import NodeHarness
from core.loop_hooks import HookContext
from core.state import State
from nodes.experiment import hooks
from nodes.experiment.tools.operation_completion import _record_operation_completion
from nodes.experiment.tools.run_contract import _classify_experiment_scope


def _operation_state(root: Path) -> State:
    state = State.new("experiment", root)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Execute the declared operation and retain its evidence.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This operation has no governing preregistration.",
        },
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="format_validation",
        reason="Validate a fixed operation without deriving a scientific conclusion.",
    ))
    assert classified["status"] == "success", classified
    return state


def _close_and_audit(
    root: Path,
    *,
    outcome: str,
    check_passed: bool,
) -> tuple[State, dict, LoopResult]:
    state = _operation_state(root)
    evidence = root / "operation.stdout"
    evidence.write_text(
        "returncode=" + ("0" if check_passed else "1") + "\n",
        encoding="utf-8",
    )
    completion = asyncio.run(_record_operation_completion(
        state,
        task_kind="generic",
        objective="verify an operational task",
        outcome=outcome,
        checks=[{
            "name": "operation_check",
            "passed": check_passed,
            "evidence": {"returncode": 0 if check_passed else 1},
        }],
        artifact_paths=[str(evidence)],
        next_step=("inspect failure and retry safely" if not check_passed else ""),
    ))
    assert completion["status"] == "success", completion
    audit = hooks._audit_operation_log(state)
    assert audit["passed"] is True, audit
    loop_result = LoopResult(
        final_text="operation closed",
        turns=1,
        tool_calls=[],
        messages=[],
        status="completed",
    )
    hooks.experiment_contract_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=1),
        loop_result,
    )
    return state, completion, loop_result


def _assert_nonsuccess_visible(
    root: Path,
    *,
    requested: str,
    check_passed: bool,
    expected_effective: str,
) -> None:
    state, completion, loop_result = _close_and_audit(
        root,
        outcome=requested,
        check_passed=check_passed,
    )
    assert completion["outcome"] == expected_effective, completion
    if expected_effective == "partial":
        assert completion["outcome_demoted_from"] == "success", completion
    blockers = state.hook_state.get("blockers") or []
    assert any(
        item.get("blocker_id") == "experiment_operation_nonsuccess_outcome"
        for item in blockers
        if isinstance(item, dict)
    ), blockers
    assert loop_result.status == "blocked", loop_result
    summary = asyncio.run(finalize_run(
        state,
        NodeHarness(node_type="experiment", required_outputs=[]),
        loop_result,
        llm=None,
    ))
    assert summary["status"] == "blocked", summary


def _assert_success_unblocked(root: Path) -> None:
    state, completion, loop_result = _close_and_audit(
        root,
        outcome="success",
        check_passed=True,
    )
    assert completion["outcome"] == "success", completion
    assert state.hook_state.get("blockers", []) == []
    assert loop_result.status == "completed", loop_result
    summary = asyncio.run(finalize_run(
        state,
        NodeHarness(node_type="experiment", required_outputs=[]),
        loop_result,
        llm=None,
    ))
    assert summary["status"] == "completed", summary


def _assert_existing_read_error_unchanged(root: Path) -> None:
    state = State.new("experiment", root)
    artifact = state.save_artifact(
        "job_submission",
        "corrupt_receipt",
        json.dumps({
            "status": "success",
            "dry_run": False,
            "scheduler": "local",
            "job_id": "4242",
        }),
    )
    path = state.find_artifact_path(artifact["id"])
    assert path is not None
    path.write_text("{not valid JSON", encoding="utf-8")
    loop_result = LoopResult(
        final_text="",
        turns=1,
        tool_calls=[],
        messages=[],
        status="completed",
    )

    hooks.external_job_handoff_on_end(
        HookContext(harness=None, state=state, messages=[], turn=1),
        loop_result,
    )

    blocker = next(
        item
        for item in state.hook_state.get("blockers") or []
        if item.get("blocker_id") == "experiment_job_submission_read_error"
    )
    assert blocker == {
        "blocker_id": "experiment_job_submission_read_error",
        "category": "closure",
        "summary": (
            "unable to read authoritative external job submission records; "
            "cannot verify that external work is finalized"
        ),
        "retryable_after_change": True,
    }, blocker
    assert state.hook_state["experiment_downstream_blocked"]["reason"].startswith(
        "authoritative job_submission records unavailable:"
    )
    assert loop_result.status == "blocked"


def main() -> None:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="probe-c8-") as temp:
        root = Path(temp)
        cases = [
            ("failed", lambda: _assert_nonsuccess_visible(
                root / "failed", requested="failed", check_passed=False,
                expected_effective="failed",
            )),
            ("blocked", lambda: _assert_nonsuccess_visible(
                root / "blocked", requested="blocked", check_passed=False,
                expected_effective="blocked",
            )),
            ("partial", lambda: _assert_nonsuccess_visible(
                root / "partial", requested="success", check_passed=False,
                expected_effective="partial",
            )),
            ("success", lambda: _assert_success_unblocked(root / "success")),
            ("existing_read_error", lambda: _assert_existing_read_error_unchanged(
                root / "read-error"
            )),
        ]
        for name, run in cases:
            try:
                run()
            except Exception as exc:
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
                print(f"{name}: FAIL ({type(exc).__name__}: {exc})")
            else:
                print(f"{name}: PASS")
    assert not failures, failures
    print("probe_c8_nonsuccess_visible: PASS")


if __name__ == "__main__":
    main()
