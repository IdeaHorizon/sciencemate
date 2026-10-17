"""Scope describes a delivery; it is not an independent success prerequisite.

Use the real Core task contract, bootstrap, action observations, finish
transaction and delivery audit.  The only execution substitute is the bounded
read-only check runner where the check's output is not under test.  A separate
case exercises an actual failed local process followed by a successful retry.
"""
from __future__ import annotations

import asyncio
import copy
import shlex
import sys

import pytest

from core.project_workspace import working_directory
from core.state import State
from core.task_contract import PreregAssignment, TaskContractLog
from nodes.experiment.tools import (
    action_boundary,
    contract_audit,
    delivery_transaction as tx,
    finish,
    ledger,
    run_bootstrap,
    safe_bash,
)


OUTPUT = "runtime/result.txt"


def _run(awaitable):
    return asyncio.run(awaitable)


def _state(tmp_path, *, assignment="none", outputs=None, with_required=True, prereg_frozen=True):
    state = State.new(
        node_type="experiment", base_dir=tmp_path / "runs",
        project_id="p", session_id="owner",
    )
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Produce the requested file and verify its contents.",
    }
    if with_required:
        state.hook_state["node_inputs"]["required_outputs"] = outputs or [
            {"id": "result", "kind": "file", "path": OUTPUT},
        ]
    prereg_assignment = None
    if assignment == "none":
        prereg_assignment = PreregAssignment.none("fixed software acceptance, no prereg")
    elif assignment == "bound":
        saved = state.save_artifact(
            "pre_registration", "scope_neutral_prereg", "Measure value=7.",
            metadata={"execution_contract": {
                "version": 1, "scientific_params": {"value": 7}, "runtime_params": {},
            }},
        )
        if prereg_frozen:
            state.mark_frozen(saved["id"])
        record = state.read_artifact(saved["id"])
        prereg_assignment = PreregAssignment.exact(
            saved["id"], version=str(record["version"]),
            content_hash=record["content_hash"],
        )
    revision = TaskContractLog(state.project_root / "tasks").append(
        task_instance_uuid="scope-neutral-task", objective="Produce the requested result.",
        target_node="experiment", intended_use="Fixed software acceptance, not a scientific claim.",
        prereg_assignment=prereg_assignment, actor="parent", reason="fresh dispatch",
    )
    state.task_instance_uuid = revision.task_instance_uuid
    state.task_contract_digest = revision.digest
    state.task_contract_revision = revision.revision
    state.parent_dispatch_id = "scope-neutral-dispatch"
    established = _run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    assert "scope" not in state.hook_state["node_inputs"]
    return state


def _write(state, *, text="actual result\n", exit_code=0, execution_params=None):
    target = working_directory(state) / OUTPUT
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = action_boundary.begin(
        state, tool="safe_write_file", work_root=str(target.parent),
        output_candidates=[target], execution_params=execution_params,
    )
    assert not isinstance(handle, action_boundary.BoundaryRefusal), handle
    target.write_text(text, encoding="utf-8")
    settled = action_boundary.settle(state, handle, outcome="settled", exit_code=exit_code)
    assert settled["status"] == "recorded", settled
    return handle


def _finish(state, outcome="success", **kwargs):
    return _run(finish._finish(
        state, outcome=outcome, summary="The recorded task result is being delivered.", **kwargs,
    ))


def _assert_sealed(state, result):
    assert result["status"] == "success", result
    assert set(result["artifact_ids"]) == set(tx.TRIO)
    for artifact_id in result["artifact_ids"].values():
        assert state.artifact_head(artifact_id).frozen
    assert contract_audit.audit_experiment_delivery(state)["passed"] is True
    rows = ledger.open_ledger(state).records()
    assert sum(row["kind"] == "finish" for row in rows) == 1


@pytest.mark.parametrize("assignment", ["none", "omitted"])
def test_core_task_without_scope_can_deliver_verified_results(tmp_path, assignment):
    state = _state(tmp_path, assignment=assignment)
    _write(state)

    result = _finish(state)

    _assert_sealed(state, result)
    delivery = result["delivery"]
    assert delivery["scope"] == "unknown"
    assert delivery["scope_status"] == "undecided"
    assert delivery["outcome"] == "success"
    assert delivery["execution_status"] == "attempted"
    assert delivery["result_status"] == "delivered"
    assert delivery["limitations"], "Unresolved scope must remain disclosed."
    assert not delivery["gaps"] and not delivery["contradictions"]
    authority = run_bootstrap.load_run_authority(state)
    assert authority["scope"] is None, "No prereg must not silently mean operation."
    assert authority["prereg_status"] == ("none" if assignment == "none" else "omitted")


def test_scope_unknown_does_not_hide_available_partial_results(tmp_path):
    state = _state(tmp_path)
    _write(state)

    result = _finish(state, "partial")

    _assert_sealed(state, result)
    assert result["delivery"]["result_status"] == "partial"
    assert result["delivery"]["scope"] == "unknown"
    assert not any(gap.get("name") == "scope_undecided" for gap in result["delivery"]["gaps"])


@pytest.mark.parametrize("problem", ["missing", "unattributed", "changed", "failed"])
def test_unknown_scope_does_not_bypass_output_or_execution_evidence(tmp_path, problem):
    state = _state(tmp_path, outputs=[{"id": "result", "kind": "file", "path": OUTPUT, "non_empty": True}])
    target = working_directory(state) / OUTPUT
    target.parent.mkdir(parents=True, exist_ok=True)
    if problem == "unattributed":
        target.write_text("pre-existing, not produced by a recorded action\n", encoding="utf-8")
    elif problem == "changed":
        _write(state)
        target.write_text("a later unrecorded replacement\n", encoding="utf-8")
    elif problem == "failed":
        _write(state, text="", exit_code=9)

    result = _finish(state)

    assert result["status"] == "success_unsupported", result
    if problem == "failed":
        assert any(row.get("gap") == "empty" for row in result["gaps"])
        assert any(row.get("kind") == "action_settled" and row.get("exit_code") == 9
                   for row in ledger.open_ledger(state).records())
    else:
        expected = {"missing": "missing", "unattributed": "unattributed",
                    "changed": "identity_changed_since_the_action"}[problem]
        assert any(row.get("gap") == expected for row in result["gaps"])
    assert tx.read_manifest(state) is None


def test_unknown_scope_still_cannot_abandon_an_open_action(tmp_path):
    state = _state(tmp_path)
    _write(state)
    handle = action_boundary.begin(state, tool="safe_run_bash", command="still running")
    assert not isinstance(handle, action_boundary.BoundaryRefusal)

    result = _finish(state)

    assert result["error_code"] == "terminal_closure_with_live_or_unknown_effect", result
    assert tx.read_manifest(state) is None


def test_missing_task_acceptance_is_not_replaced_by_scope_neutral_success(tmp_path):
    state = _state(tmp_path, with_required=False)
    _write(state)

    result = _finish(state)

    assert result["status"] == "success_unsupported", result
    assert any(gap.get("gap") == "task_verification_missing" for gap in result["gaps"]), result
    assert tx.read_manifest(state) is None


def test_invalid_bound_formal_input_is_not_excused_as_an_unknown_label(tmp_path):
    state = _state(tmp_path, assignment="bound", prereg_frozen=False)
    _write(state)

    rejected = _finish(state)

    assert rejected["status"] == "success_unsupported", rejected
    assert any(row.get("name") == "formal_input_unavailable" for row in rejected["gaps"])
    assert tx.read_manifest(state) is None
    honest = _finish(state, "partial")
    _assert_sealed(state, honest)
    assert honest["delivery"]["scope"] == "scientific"
    assert honest["delivery"]["formal_input"]["status"] == "invalid"


def test_scope_label_conflict_is_disclosed_without_rejecting_same_task_delivery(tmp_path):
    state = _state(tmp_path)
    state.hook_state["node_inputs"]["scope"] = "operation"
    _run(run_bootstrap.establish(state))
    _write(state)
    state.hook_state["node_inputs"]["scope"] = "scientific"
    _run(run_bootstrap.establish(state))

    result = _finish(state)

    _assert_sealed(state, result)
    assert result["delivery"]["limitations"]
    assert not result["delivery"]["contradictions"]


def test_actual_contract_change_still_cannot_be_hidden_as_a_scope_issue(tmp_path):
    state = _state(tmp_path)
    _write(state)
    state.task_contract_digest = "f" * 64
    _run(run_bootstrap.establish(state))

    result = _finish(state)

    assert result["status"] == "success_unsupported", result
    assert any(row.get("blocker", {}).get("code") == "run_authority_input_changed"
               for row in result["contradictions"])
    assert tx.read_manifest(state) is None


@pytest.mark.parametrize("scope", [None, "operation"])
def test_bound_prereg_producer_conflict_is_checked_independently_of_scope_label(tmp_path, scope):
    state = _state(tmp_path, assignment="bound")
    handle = _write(state, execution_params={"scientific_params": {"value": -7}, "runtime_params": {}})
    source_authority = run_bootstrap.load_run_authority(state)
    authority = copy.deepcopy(source_authority)
    authority["scope"] = scope
    # Exercise the pure consumer with a misleading view, not a forged ledger.
    evaluation = _run(finish.evaluate(state, ledger.open_ledger(state, write=True)))

    _lineage, _gaps, contradictions, _limitations = finish._execution_lineage_findings(
        authority, ledger.open_ledger(state).records(), evaluation,
    )

    assert any(row.get("name") == "producer_parameter_claim_conflict"
               and row.get("action") == handle.action_id for row in contradictions)
    assert run_bootstrap.load_run_authority(state) == source_authority
    actual = _finish(state)
    assert actual["status"] == "success_unsupported", actual


def test_bound_prereg_check_only_conflict_cannot_disappear_under_unknown_scope(tmp_path, monkeypatch):
    state = _state(tmp_path, assignment="bound", outputs=[
        {"id": "check", "kind": "check", "command": "true", "expect_exit": 0},
    ])
    handle = _write(state, execution_params={"scientific_params": {"value": -7}, "runtime_params": {}})

    async def check_ok(_state, _obligation):
        return {"exit_code": 0, "stdout_tail": "", "stderr_tail": "", "error": None}

    monkeypatch.setattr(finish, "_check_runner", check_ok)
    evaluation = _run(finish.evaluate(state, ledger.open_ledger(state, write=True)))
    authority = copy.deepcopy(run_bootstrap.load_run_authority(state))
    authority["scope"] = None

    _lineage, gaps, _contradictions, _limitations = finish._execution_lineage_findings(
        authority, ledger.open_ledger(state).records(), evaluation,
    )

    gap, = [row for row in gaps if row.get("name") == "parameter_lineage_unresolved"]
    assert any(item["action"] == handle.action_id for item in gap["candidates"])


def test_unknown_success_recovers_first_delivery_after_partial_freeze(tmp_path, monkeypatch):
    state = _state(tmp_path)
    _write(state)
    real_freeze = state.mark_frozen
    interrupted = False

    def freeze_then_interrupt(artifact_id, *args, **kwargs):
        nonlocal interrupted
        result = real_freeze(artifact_id, *args, **kwargs)
        if not interrupted:
            interrupted = True
            raise OSError("interrupted after first physical freeze")
        return result

    monkeypatch.setattr(state, "mark_frozen", freeze_then_interrupt)
    with pytest.raises(OSError, match="first physical freeze"):
        _finish(state)
    persisted = tx.read_manifest(state)
    monkeypatch.setattr(state, "mark_frozen", real_freeze)

    recovered = _finish(state, "partial")

    _assert_sealed(state, recovered)
    assert recovered["delivery"] == persisted["delivery"]
    assert recovered["delivery"]["outcome"] == "success"
    assert recovered["delivery"]["scope"] == "unknown"
    for artifact_id in recovered["artifact_ids"].values():
        assert len(state.artifact_versions(artifact_id)) == 1


@pytest.mark.parametrize("exit_codes", [(7, 0), (0, 7)])
def test_retry_projection_uses_recorded_order_not_random_action_identifier(exit_codes):
    # The real-process case exposed this ordering defect.  These two orders
    # make both false rejection and false success deterministic without
    # changing the UUID source, stored history, or finish implementation.
    records = []
    for index, (action_id, exit_code) in enumerate(zip(("action-z", "action-a"), exit_codes)):
        records.extend([
            {"seq": 2 * index + 1, "kind": "action_started", "action_id": action_id,
             "tool": "safe_run_bash", "effect_class": "experiment", "work_root": "/work/result"},
            {"seq": 2 * index + 2, "kind": "action_settled", "action_id": action_id,
             "state": "settled", "exit_code": exit_code},
        ])

    assert [row["action_id"] for row in ledger.project_actions(records)] == ["action-z", "action-a"]
    # Ordering is the reducer's contract.  It does not itself establish that
    # the later action repaired an earlier failure; real output/check evidence
    # is tested separately in test_060_failed_action_recovery.py.


def test_real_failed_local_process_then_retry_delivers_without_scope(tmp_path):
    state = _state(tmp_path)
    target = working_directory(state) / OUTPUT
    target.parent.mkdir(parents=True, exist_ok=True)
    python = shlex.quote(sys.executable)
    program = "from pathlib import Path; Path('result.txt').write_text('successful retry\\n')"

    async def execute_and_finish():
        failed = await safe_bash._safe_run_bash(
            state, cmd=f"{python} -c 'raise SystemExit(7)'", cwd=str(target.parent), timeout=20,
        )
        assert failed.get("returncode") == 7, failed
        retried = await safe_bash._safe_run_bash(
            state, cmd=f"{python} -c {shlex.quote(program)}", cwd=str(target.parent), timeout=20,
        )
        assert retried.get("returncode") == 0, retried
        return await finish._finish(state, outcome="success", summary="A real failed process was retried successfully.")

    result = _run(execute_and_finish())

    _assert_sealed(state, result)
    assert result["delivery"]["scope"] == "unknown"
    assert result["delivery"]["outcome"] == "success"
    settlements = [row for row in ledger.open_ledger(state).records()
                   if row["kind"] == "action_settled"]
    assert [row["exit_code"] for row in settlements] == [7, 0]
