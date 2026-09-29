"""Regression coverage for immutable upstream intent at Experiment boundaries."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.loop_hooks import HookContext
from core.state import State
from nodes.experiment import hooks
from nodes.experiment.tools import operation_completion as operation_completion_module
from nodes.experiment.tools.contract_audit import (
    audit_experiment_contract,
    audit_operation_log_contract,
)
from nodes.experiment.tools.execution_route import (
    _declare_execution_route,
    execution_route_block,
    resolve_execution_context,
)
from nodes.experiment.tools.operation_completion import (
    _record_operation_completion,
    operation_child_obligation_projection,
)
from nodes.experiment.tools.run_contract import (
    _classify_experiment_scope,
    audit_execution_intent_binding,
    load_run_contract,
    resolve_run_acceptance,
)


def _scientific_state(tmp_path: Path) -> tuple[State, str]:
    state = State.new("experiment", tmp_path)
    prereg_id = state.save_artifact(
        "pre_registration",
        "bound_goal",
        """## Research Questions
### Q1: preserve the requested scientific target
- output_kind: numeric
```yaml
- metric: target_metric
  comparison: >=
  threshold: 1
```
""",
        metadata={
            "run_role": "secondary",
            "execution_mode": "scientific",
            "stage": "simulation",
            "execution_contract": {
                "version": 1,
                "scientific_params": {"target": "native solver"},
                "runtime_params": {},
            },
        },
    )["id"]
    state.mark_frozen(prereg_id)
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "stage": "simulation",
        "experiment_focus": "Run the declared native solver; do not replace it with a proxy.",
    }
    return state, prereg_id


def _classify_scientific(state: State) -> dict:
    return asyncio.run(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="Execute the frozen scientific target with its declared native method.",
    ))


def test_scope_binds_upstream_intent_and_exact_prereg_snapshot(tmp_path: Path) -> None:
    state, prereg_id = _scientific_state(tmp_path)

    result = _classify_scientific(state)
    scope = state.hook_state["experiment_execution_scope"]
    acceptance = resolve_run_acceptance(state, bind_if_absent=False)
    audit = audit_execution_intent_binding(state, require=True)

    assert result["status"] == "success", result
    assert scope["intent_schema_version"] == 2
    assert scope["intent_digest"]
    assert acceptance["passed"] is True, acceptance
    assert acceptance["receipt"]["governing_task_input_binding"]["artifact_id"] == prereg_id
    assert acceptance["receipt"]["governing_task_input_binding"]["version"] == 1
    assert acceptance["receipt"]["governing_task_input_binding"]["content_hash"]
    assert acceptance["receipt"]["prereg_assignment"]["kind"] == "bound"
    assert audit["passed"] is True, audit
    assert audit["status"] == "bound"


def test_typed_none_science_ignores_late_catalog_for_route_planning(
    tmp_path: Path,
) -> None:
    """A typed-none scope keeps later catalog entries non-authorizing."""
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Execute the declared scientific target without replacing it.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This exploratory scientific run has no governing preregistration.",
        },
    }
    assert asyncio.run(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="Classify the declared scientific work before any preregistration exists.",
    ))["status"] == "success"
    late = state.save_artifact(
        "pre_registration",
        "late_authority",
        "# frozen late preregistration",
        metadata={
            "run_role": "primary",
            "execution_mode": "scientific",
            "stage": "simulation",
        },
    )
    state.mark_frozen(late["id"])

    audit = audit_execution_intent_binding(state, require=False)
    route = {
        "schema_version": 2,
        "goal": "Execute the declared scientific target through a managed route.",
        "evidence_refs": ["test:late-prereg"],
        "steps": [{
            "id": "run",
            "goal": "Run the declared target.",
            "after": [],
            "action": {"tool": "submit_job", "program": "solver"},
            "effects": ["workspace_write", "process_tree", "external_job"],
            "workdir_role": "run_root",
            "expected_outputs": [],
        }],
    }
    planned = asyncio.run(_declare_execution_route(state, route))

    assert audit["passed"] is True, audit
    assert audit["status"] == "bound"
    assert audit["prereg_assignment"]["kind"] == "none"
    assert planned["status"] == "success", planned


@pytest.mark.parametrize("late_kind", ["frozen", "pending_amendment"])
@pytest.mark.parametrize("reopen", [False, True])
def test_typed_none_science_can_be_corrected_despite_late_catalog(
    tmp_path: Path,
    late_kind: str,
    reopen: bool,
) -> None:
    node_inputs = {
        "experiment_focus": "Run an unpreregistered scientific measurement.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This exploratory measurement has no governing preregistration.",
        },
    }
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = dict(node_inputs)
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="Classify the scientific work before any preregistration exists.",
    ))
    assert classified["status"] == "success", classified

    late = state.save_artifact(
        "pre_registration",
        "late_authority",
        "# frozen late preregistration",
        metadata={
            "run_role": "primary",
            "execution_mode": "scientific",
            "stage": "simulation",
        },
    )
    state.mark_frozen(late["id"])
    if late_kind == "pending_amendment":
        state.save_artifact(
            "pre_registration",
            "late_authority",
            "# pending replacement",
            amendment_reason="Revise the scientific protocol.",
        )
    before = audit_execution_intent_binding(state, require=False)
    assert before["passed"] is True, before
    assert before["status"] == "bound"
    assert before["prereg_assignment"]["kind"] == "none"

    active = state
    if reopen:
        active = State.reopen("experiment", state.root.parent, state.run_id)
        active.hook_state["node_inputs"] = dict(node_inputs)
        assert "experiment_execution_scope" not in active.hook_state

    downgrade = asyncio.run(_classify_experiment_scope(
        active,
        scope="operation",
        operation_category="other",
        reason="Correct the typed-none run from scientific to operation.",
    ))
    after = audit_execution_intent_binding(active, require=True)
    evidence = Path(active.root) / "operation.txt"
    evidence.write_text("late catalog remained non-authorizing\n", encoding="utf-8")
    completion = asyncio.run(_record_operation_completion(
        active,
        task_kind="generic",
        objective="record the corrected mechanical operation",
        outcome="success",
        checks=[{
            "name": "typed_none_assignment",
            "passed": True,
            "evidence": {"non_authorizing_late_prereg": late["id"]},
        }],
        artifact_paths=[str(evidence)],
    ))

    assert downgrade["status"] == "success", downgrade
    assert downgrade["classification"]["mode"] == "operational"
    assert load_run_contract(active)["execution_mode"] == "operational"
    assert after["passed"] is True, after
    assert after["status"] == "bound"
    assert after["scope_mode"] == "operational"
    assert completion["status"] == "success", completion
    assert completion["closure_id"]


@pytest.mark.parametrize("write_before_raise", [False, True])
def test_scope_ledger_wins_when_cache_update_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_before_raise: bool,
) -> None:
    node_inputs = {
        "experiment_focus": "Run an unpreregistered scientific measurement.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This exploratory measurement has no governing preregistration.",
        },
    }
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = dict(node_inputs)
    first = asyncio.run(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="Persist the initial scientific scope.",
    ))
    assert first["status"] == "success", first
    original_append = state.append_transcript

    def interrupted_append(event: str, **payload):
        if (
            event == "experiment_scope_classified"
            and payload.get("mode") == "operational"
        ):
            if write_before_raise:
                original_append(event, **payload)
            raise OSError("injected scope ledger interruption")
        return original_append(event, **payload)

    monkeypatch.setattr(state, "append_transcript", interrupted_append)
    with pytest.raises(OSError, match="injected scope ledger interruption"):
        asyncio.run(_classify_experiment_scope(
            state,
            scope="operation",
            operation_category="other",
            reason="Attempt a persisted scope correction.",
        ))
    monkeypatch.setattr(state, "append_transcript", original_append)

    acceptance = resolve_run_acceptance(state, bind_if_absent=False)
    durable_mode = acceptance["durable_scope_projection"]["mode"]
    if write_before_raise:
        assert durable_mode == "operational"
        assert state.hook_state["experiment_execution_scope"]["mode"] == "scientific"
        before_retry = audit_execution_intent_binding(state, require=True)
        assert before_retry["passed"] is True, before_retry
        assert before_retry["scope_mode"] == "operational"
        assert load_run_contract(state)["execution_mode"] == "operational"
        retried = asyncio.run(_classify_experiment_scope(
            state,
            scope="operation",
            operation_category="other",
            reason="Resume the already-persisted correction.",
        ))
        assert retried["status"] == "success", retried
        assert retried["idempotent"] is True
        assert state.hook_state["experiment_execution_scope"]["mode"] == "operational"
        assert audit_execution_intent_binding(
            state, require=True
        )["scope_mode"] == "operational"
        assert load_run_contract(state)["execution_mode"] == "operational"
        return

    assert durable_mode == "scientific"
    assert state.hook_state["experiment_execution_scope"]["mode"] == "scientific"
    state.hook_state["experiment_execution_scope"] = {
        "mode": "operational",
        "category": "other",
    }
    state.hook_state["_request_mode"] = "operation"
    audit = audit_execution_intent_binding(state, require=True)
    assert audit["passed"] is True, audit
    assert audit["scope_mode"] == "scientific"
    assert load_run_contract(state)["execution_mode"] == "scientific"

    evidence = Path(state.root) / "stale-cache.txt"
    evidence.write_text("stale hook cache\n", encoding="utf-8")
    completion = asyncio.run(_record_operation_completion(
        state,
        task_kind="generic",
        objective="must remain scientific",
        outcome="blocked",
        checks=[{"name": "scope", "passed": False, "evidence": {}}],
        artifact_paths=[str(evidence)],
        next_step="retry the durable scientific run",
    ))
    assert completion["status"] == "error", completion
    assert completion["error_code"] == "execution_intent_binding_required"
    assert "closure_id" not in completion


def test_route_blocks_changed_upstream_intent_before_materialization(tmp_path: Path) -> None:
    state, _ = _scientific_state(tmp_path)
    assert _classify_scientific(state)["status"] == "success"

    state.hook_state["node_inputs"]["experiment_focus"] = (
        "Replace the native solver with a lightweight proxy."
    )
    decision = resolve_execution_context(state, {
        "tool": "safe_write_file",
        "program": "write_file",
        "read_only": False,
        "observed_effects": ["workspace_write"],
        "workdir_roles": [],
        "dry_run": False,
    })
    block = execution_route_block(decision, phase="pre_materialization")

    assert decision["intent_binding"]["passed"] is False
    assert decision["intent_binding"]["status"] == "intent_changed"
    assert block is not None
    assert block["reason"] == "experiment_execution_intent_changed"


def test_operation_receipt_is_bound_but_never_claims_upstream_goal_completion(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Mechanically validate the requested solver artifact.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This bounded validation has no governing preregistration.",
        },
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="format_validation",
        reason="Validate the requested solver artifact without drawing scientific conclusions.",
    ))
    evidence = Path(state.root) / "build.stdout"
    evidence.write_text("build succeeded\n", encoding="utf-8")

    completion = asyncio.run(_record_operation_completion(
        state,
        task_kind="generic",
        objective="validate the requested solver artifact",
        outcome="success",
        checks=[{"name": "build_exit", "passed": True, "evidence": {"returncode": 0}}],
        artifact_paths=[str(evidence)],
    ))
    log = state.read_artifact(completion["experiment_log_artifact_id"])
    audit = audit_operation_log_contract(state)

    assert classified["status"] == "success", classified
    assert completion["status"] == "success", completion
    child_obligation = completion["child_obligation"]
    assert completion["upstream_goal_effect"] == "operational_subtask_only"
    assert completion["scientific_contribution"] == "none"
    assert child_obligation["source"] == "frozen_operation_closure"
    assert child_obligation["delivery_status"] == "completed"
    assert child_obligation["parent_goal_completion_claimed"] is False
    assert log["metadata"]["upstream_goal_effect"] == "operational_subtask_only"
    assert log["metadata"]["execution_intent_binding"]["status"] == "bound"
    assert audit["passed"] is True, audit
    assert audit["intent_binding"]["passed"] is True

    events = [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    completion_event = next(
        event
        for event in events
        if event.get("event") == "operation_completion_recorded"
    )
    assert completion_event["child_obligation"] == child_obligation
    assert completion_event["scientific_contribution"] == "none"

    repeated = asyncio.run(_record_operation_completion(
        state,
        task_kind="generic",
        objective="validate the requested solver artifact",
        outcome="success",
        checks=[{
            "name": "build_exit",
            "passed": True,
            "evidence": {"returncode": 0},
        }],
        artifact_paths=[str(evidence)],
    ))
    assert repeated["idempotent"] is True
    assert repeated["child_obligation"] == child_obligation

    loop_result = SimpleNamespace(status="completed", final_text="operation complete")
    hooks.experiment_contract_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=1),
        loop_result,
    )
    assert "## Experiment Child Delivery" in loop_result.final_text
    assert "upstream_goal_effect: operational_subtask_only" in loop_result.final_text
    assert "scientific_contribution: none" in loop_result.final_text
    assert "parent_goal_completion_claimed: false" in loop_result.final_text


@pytest.mark.parametrize("outcome", ["failed", "blocked"])
def test_operation_child_projection_preserves_non_success_delivery_status(
    tmp_path: Path,
    outcome: str,
) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Record an honestly unsuccessful format validation.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This mechanical validation has no governing preregistration.",
        },
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="format_validation",
        reason="Validate a file format without drawing a scientific conclusion.",
    ))
    evidence = Path(state.root) / f"{outcome}.txt"
    evidence.write_text(f"validation {outcome}\n", encoding="utf-8")

    completion = asyncio.run(_record_operation_completion(
        state,
        task_kind="generic",
        objective="validate the requested file format",
        outcome=outcome,
        checks=[{
            "name": "format_valid",
            "passed": False,
            "evidence": {"observed": "invalid header"},
        }],
        artifact_paths=[str(evidence)],
        next_step="provide a valid input file and start a continuation run",
    ))
    projection = operation_child_obligation_projection(state)

    assert classified["status"] == "success", classified
    assert completion["status"] == "success", completion
    assert completion["outcome"] == outcome
    assert completion["child_obligation"]["delivery_status"] == outcome
    assert projection["status"] == "ready", projection
    assert projection["child_obligation"]["outcome"] == outcome
    assert projection["child_obligation"]["delivery_status"] == outcome
    assert projection["child_obligation"]["parent_goal_completion_claimed"] is False


def test_operation_child_projection_rejects_metadata_receipt_disagreement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Record one bounded mechanical validation.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This mechanical validation has no governing preregistration.",
        },
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="format_validation",
        reason="Validate one artifact without drawing a scientific conclusion.",
    ))
    evidence = Path(state.root) / "validation.txt"
    evidence.write_text("validation passed\n", encoding="utf-8")
    completion = asyncio.run(_record_operation_completion(
        state,
        task_kind="generic",
        objective="validate one artifact",
        outcome="success",
        checks=[{
            "name": "format_valid",
            "passed": True,
            "evidence": {"observed": "valid header"},
        }],
        artifact_paths=[str(evidence)],
    ))
    assert classified["status"] == "success", classified
    assert completion["status"] == "success", completion

    original = operation_completion_module._closure_metadata

    def conflicting_metadata(item: dict) -> dict:
        metadata = dict(original(item))
        if item.get("type") == "clean_results":
            metadata["upstream_goal_effect"] = "parent_goal_complete"
        return metadata

    monkeypatch.setattr(
        operation_completion_module,
        "_closure_metadata",
        conflicting_metadata,
    )

    projection = operation_child_obligation_projection(state)

    assert projection == {
        "status": "unavailable",
        "reason": "operation_child_effect_unverifiable",
        "closure_id": f"{state.run_id}:operation",
    }


def test_unrelated_late_prereg_cannot_block_honest_blocked_operation_closure(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Build the requested solver before the scientific run.",
        "stage": "toolchain_build",
    }
    assert asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="toolchain_build",
        reason="Build and verify the solver without drawing scientific conclusions.",
    ))["status"] == "success"
    unrelated = state.save_artifact(
        "pre_registration",
        "another_task",
        "# unrelated frozen preregistration",
        metadata={"run_role": "primary", "execution_mode": "scientific"},
    )
    state.mark_frozen(unrelated["id"])
    evidence = Path(state.root) / "blocked-build.stdout"
    evidence.write_text("dependency unavailable\n", encoding="utf-8")

    completion = asyncio.run(_record_operation_completion(
        state,
        task_kind="build",
        objective="build the requested solver",
        outcome="blocked",
        checks=[{
            "name": "dependency_available",
            "passed": False,
            "evidence": {"dependency": "compiler"},
        }],
        artifact_paths=[str(evidence)],
        next_step="provide the declared compiler and retry",
    ))
    audit = audit_operation_log_contract(state)

    assert completion["status"] == "success", completion
    assert audit["passed"] is True, audit
    assert audit["blocked_closure"] is True
    assert audit["intent_binding"]["passed"] is True


def test_operation_closure_rejects_changed_upstream_intent(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Build the requested solver before the scientific run.",
        "stage": "toolchain_build",
    }
    assert asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="toolchain_build",
        reason="Build and verify the requested solver without drawing scientific conclusions.",
    ))["status"] == "success"
    state.hook_state["node_inputs"]["experiment_focus"] = "Declare a proxy result complete."
    evidence = Path(state.root) / "build.stdout"
    evidence.write_text("build succeeded\n", encoding="utf-8")

    result = asyncio.run(_record_operation_completion(
        state,
        task_kind="build",
        objective="build the requested solver",
        outcome="success",
        checks=[{"name": "build_exit", "passed": True, "evidence": {"returncode": 0}}],
        artifact_paths=[str(evidence)],
    ))

    assert result["status"] == "error"
    assert result["error_code"] == "execution_intent_binding_required"
    assert not list(state.list_artifacts("raw_results"))



def test_operation_closure_rejects_unbound_scope_even_when_inputs_are_present(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["_request_mode"] = "operation"
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Build the requested solver.",
        "stage": "toolchain_build",
    }

    result = asyncio.run(_record_operation_completion(
        state,
        task_kind="build",
        objective="build the requested solver",
        outcome="success",
        checks=[{"name": "build_exit", "passed": True, "evidence": {"returncode": 0}}],
    ))

    assert result["status"] == "error"
    assert result["error_code"] == "execution_intent_binding_required"
    assert result["execution_intent_binding"]["status"] == "scope_unbound"


def test_terminal_contract_blocks_nonempty_inputs_without_scope(tmp_path: Path) -> None:
    state, _ = _scientific_state(tmp_path)

    audit = audit_experiment_contract(state)
    assert audit["execution_intent_binding"]["passed"] is False
    assert audit["execution_intent_binding"]["status"] == "scope_unbound"

    loop_result = SimpleNamespace(final_text="claim the scientific task is complete")
    hooks.experiment_contract_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=1), loop_result
    )

    assert loop_result.status == "blocked"
    assert "experiment_execution_intent_audit: failed" in loop_result.final_text


def test_drifted_inputs_reject_scientific_log_freeze_before_immutable_terminal(tmp_path: Path) -> None:
    from shared.tools.library.artifacts_extra import _freeze_artifact

    state, _ = _scientific_state(tmp_path)
    assert _classify_scientific(state)["status"] == "success"
    log_id = state.save_artifact(
        "experiment_log",
        "drifted_log",
        "## Execution Status\nstatus: completed\n\n## Credibility\ncredibility: questionable\n",
    )["id"]
    state.hook_state["node_inputs"]["experiment_focus"] = (
        "Replace the declared native solver with a proxy after classification."
    )

    frozen = asyncio.run(_freeze_artifact(
        state, log_id, "must reject upstream intent drift before log freeze"
    ))

    assert frozen["status"] == "error"
    assert "execution_intent_binding" in frozen["failed_checks"]



def test_route_declared_before_scope_requires_receipt_refresh_before_execution(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    route = {
        "schema_version": 2,
        "goal": "Build the requested native solver through one managed operation.",
        "evidence_refs": ["test:route-intent"],
        "steps": [{
            "id": "build",
            "goal": "Build the requested native solver.",
            "after": [],
            "action": {"tool": "submit_job", "program": "make"},
            "effects": ["workspace_write", "process_tree", "external_job"],
            "workdir_role": "run_root",
            "expected_outputs": [],
        }],
    }
    planned = asyncio.run(_declare_execution_route(state, route))
    assert planned["status"] == "success", planned

    state.hook_state["node_inputs"] = {
        "experiment_focus": "Build the requested native solver before its scientific run.",
    }
    assert asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="toolchain_build",
        reason="Build the requested native solver without claiming scientific completion.",
    ))["status"] == "success"

    action = {
        "tool": "submit_job",
        "program": "make",
        "route_step_id": "build",
        "observed_effects": ["workspace_write", "process_tree", "external_job"],
        "workdir_roles": ["run_root"],
        "read_only": False,
        "dry_run": False,
    }
    before = resolve_execution_context(state, action)
    assert before["route_intent_binding"]["status"] == "receipt_missing"
    blocked = execution_route_block(before, phase="pre_materialization")
    assert blocked is not None
    assert blocked["reason"] == "route_execution_intent_binding_missing"

    rebound = asyncio.run(_declare_execution_route(
        state, route, amendment_reason="bind route to the newly classified immutable operation input"
    ))
    assert rebound["status"] == "success", rebound
    after = resolve_execution_context(state, action)
    assert after["route_intent_binding"]["passed"] is True, after
    assert execution_route_block(after, phase="pre_materialization") is None
