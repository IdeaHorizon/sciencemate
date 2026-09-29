"""021 v4 review: residual-risk regressions (review-side, not delivered code)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from core.loop_hooks import HookContext
from core.state import State
from nodes.experiment import hooks
from nodes.experiment.tests._mechanical_execution import (
    record_completed_local_mechanical_action,
)
from nodes.experiment.tools import execution_route as er
from nodes.experiment.tools.operation_completion import _record_operation_completion
from nodes.experiment.tools.run_contract import (
    _classify_experiment_scope,
    audit_execution_intent_binding,
    load_run_contract,
    resolve_run_acceptance,
)


def _prereg(state: State, name: str, **metadata) -> str:
    meta = {"run_role": "primary", "execution_mode": "scientific",
            "stage": "simulation", "expected_params": {"grid": 12}}
    meta.update(metadata)
    artifact_id = state.save_artifact(
        "pre_registration", name, f"# {name}\n", metadata=meta)["id"]
    state.mark_frozen(artifact_id)
    return artifact_id


def _classify(state: State, scope: str) -> dict:
    return asyncio.run(_classify_experiment_scope(
        state, scope=scope, operation_category="toolchain_build",
        reason="review-side classification"))


_FORMAL_ROUTE = {
    "schema_version": 2, "goal": "formal measurement",
    "evidence_refs": ["test:review-021v4"],
    "steps": [{
        "id": "solve", "goal": "formal solve", "after": [],
        "action": {"tool": "submit_job", "program": "./solver"},
        "effects": ["workspace_write", "process_tree", "scientific_execution",
                    "external_job"],
        "workdir_role": "run_root", "expected_outputs": [],
    }],
}


def test_operation_ledger_scope_blocks_scientific_step_despite_stale_scientific_cache(
    tmp_path: Path, monkeypatch,
) -> None:
    """R-B: execution_route must read the same canonical scope as run_contract.

    Unpreregistered science is corrected to operation; the ledger records the
    correction but the hook cache keeps the old scientific value (the exact
    interruption state pinned by
    test_scope_ledger_wins_when_cache_update_is_interrupted[True]).  Two
    unrelated preregs then appear.  The run is canonically operational with a
    null receipt, so a declared scientific_execution step must not be admitted.
    """
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Measure a new quantity.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This exploratory review fixture has no governing preregistration.",
        },
    }
    assert _classify(state, "scientific")["status"] == "success"
    stale = json.loads(json.dumps(state.hook_state["experiment_execution_scope"]))
    assert _classify(state, "operation")["status"] == "success"
    state.hook_state["experiment_execution_scope"] = stale          # stale cache
    state.hook_state["_request_mode"] = "scientific"
    _prereg(state, "other_a")
    _prereg(state, "other_b")
    assert load_run_contract(state)["execution_mode"] == "operational"
    assert audit_execution_intent_binding(state, require=True)["scope_mode"] == "operational"
    assert er._run_execution_mode(state) == "operational"

    asyncio.run(er._declare_execution_route(state, route=json.loads(json.dumps(_FORMAL_ROUTE))))
    decision = er.resolve_execution_context(state, {
        "tool": "submit_job", "program": "./solver", "route_step_id": "solve",
        "read_only": False,
        "observed_effects": ["external_job", "process_tree",
                             "scientific_execution", "workspace_write"],
        "workdir_roles": ["run_root"], "dry_run": False})
    block = er.execution_route_block(decision, phase="pre_materialization")

    # v4: block is None -> submit_job runs execution_class=simulation with no
    # prereg binding and no deviation record (base records prereg_ambiguous).
    assert block is not None
    assert block["reason"] == "execution_scope_route_mismatch"


def test_null_receipt_visibility_witness_survives_prereg_disappearance(
    tmp_path: Path, monkeypatch,
) -> None:
    """R-C (mutation MF): the accepted visibility witness must be unioned in.

    The live catalog is made to stop showing the two preregs this null receipt
    saw at acceptance; the operation's null decision must still not become
    unpreregistered science.
    """
    from nodes.experiment.tools import run_contract as rc

    state = State.new("experiment", tmp_path)
    _prereg(state, "seen_a")
    _prereg(state, "seen_b")
    state.hook_state["node_inputs"] = {"experiment_focus": "Build an unrelated tool."}
    assert _classify(state, "operation")["status"] == "success"
    receipt = resolve_run_acceptance(state, bind_if_absent=False)["receipt"]
    assert len(receipt["unbound_prereg_visibility_witness"]["frozen"]) == 2
    monkeypatch.setattr(rc, "_scan_prereg_catalog", lambda _state, **_kw: {
        "passed": True, "frozen": [], "bound_versions": {}, "pending_amendments": [],
        "unfrozen_preregs": [], "declared_not_found": False,
        "visibility_witness": {"frozen": [], "pending": []}})

    second = _classify(state, "scientific")

    assert second["status"] == "error", second
    assert second["error_code"] == "prereg_assignment_required"
    assert second["assignment_status"] == "pending"
    assert {
        item["artifact_id"] for item in second["candidate_bindings"]
    } == {"pre_registration__seen_a", "pre_registration__seen_b"}


def test_valid_receipt_without_matching_scope_never_takes_mode_from_cache(tmp_path: Path) -> None:
    """R-D (mutation ME): with a valid receipt the cache never authorizes scope."""
    state = State.new("experiment", tmp_path)
    _prereg(state, "a")
    _prereg(state, "b")
    state.hook_state["node_inputs"] = {"experiment_focus": "Build an unrelated tool."}
    accepted = resolve_run_acceptance(state, bind_if_absent=True, requested_mode="operational")
    assert accepted["passed"] is True and "durable_scope_projection" not in accepted
    before = load_run_contract(state)["execution_mode"]
    state.hook_state["experiment_execution_scope"] = {"mode": "operational", "category": "other"}

    assert load_run_contract(state)["execution_mode"] == before


def test_bound_receipt_rejects_forged_operational_scope_projection(tmp_path: Path) -> None:
    """R-E (mutation MQ, 12 risk 4b): bound receipt + operational scope is invalid."""
    state = State.new("experiment", tmp_path)
    prereg_id = _prereg(state, "bound")
    state.hook_state["node_inputs"] = {"prereg_artifact_id": prereg_id,
                                       "experiment_focus": "Run the bound study."}
    accepted = resolve_run_acceptance(state, bind_if_absent=True, requested_mode="scientific")
    receipt = accepted["receipt"]
    state.append_transcript(
        "experiment_scope_classified", mode="operational", category="toolchain_build",
        reason="forged", invocation={},
        run_acceptance_receipt_digest=receipt["receipt_digest"],
        intent_schema_version=receipt["schema_version"],
        intent_digest=receipt["intent_digest"], intent_source=receipt["intent_source"],
        intent_status=receipt["intent_status"], binding_source=receipt["binding_source"],
        governing_task_input_binding=receipt["governing_task_input_binding"])
    resolved = resolve_run_acceptance(state, bind_if_absent=False)
    assert resolved["passed"] is True
    assert "durable_scope_projection" not in resolved

    audit = audit_execution_intent_binding(state, require=True)

    assert audit["passed"] is False
    assert audit["status"] in {
        "scope_unbound",
        "scope_acceptance_projection_changed",
    }


def test_sole_unrelated_prereg_stays_non_authorizing_for_operation_build(
    tmp_path: Path, monkeypatch,
) -> None:
    """R-A: a visible sole prereg is observation, never operation authority.

    The omitted assignment remains pending.  A complete route-backed mechanical
    build may use the temporary compatibility lane and close honestly without
    claiming or executing the unrelated scientific protocol.
    """
    state = State.new("experiment", tmp_path)
    _prereg(state, "another_task")
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Build the solver toolchain; no scientific measurement.",
        "stage": "toolchain_build"}
    classified = _classify(state, "operation")
    assert classified["status"] == "success", classified
    assert classified["classification"]["mode"] == "operational"
    receipt = resolve_run_acceptance(state, bind_if_absent=False)["receipt"]
    assert receipt["prereg_assignment"] == {"kind": "pending"}
    assert receipt["governing_task_input_binding"] is None

    evidence = Path(state.root) / "configure.stdout"
    # P0a v4：声明的 build 产物必须映射到满足义务的 attempt 收尾时冻结的身份收据——
    # 这里的 configure 输出要由该步骤声明（expected_outputs）并在 attempt 内写出
    # （produce=）。原写法在 v3 之前从没被断言过（红 0 条 = 盲区），不是被钉住的选择。
    record_completed_local_mechanical_action(
        state,
        step_id="configure",
        program="cmake",
        produce=lambda: evidence.write_text("configure ok\n", encoding="utf-8"),
        expected_outputs=["configure.stdout"],
    )

    completion = asyncio.run(_record_operation_completion(
        state, task_kind="build", objective="configure the solver", outcome="success",
        checks=[{"name": "rc", "passed": True, "evidence": {"returncode": 0}}],
        artifact_paths=[str(evidence)]))
    loop_result = SimpleNamespace(final_text="build ok", status="completed")
    hooks.experiment_contract_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=1), loop_result)

    assert completion["status"] == "success", completion
    assert loop_result.status == "completed"
    assert not any(
        b.get("blocker_id") == "experiment_closure_incomplete"
        for b in state.hook_state.get("blockers", [])
    )
