"""All formal-input readers honor the exact Core-selected task revision."""
from __future__ import annotations

import asyncio
from copy import deepcopy

from core.ledger import record_version
from core.project_workspace import working_directory
from core.state import State
from core.task_contract import PreregAssignment, TaskContractLog
from nodes.experiment.tools import ledger, run_bootstrap, run_contract


def _state(tmp_path):
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs",
                      project_id="p", session_id="owner")
    state.hook_state["node_inputs"] = {
        "scope": "operation", "required_outputs": [],
        "prereg_assignment": {"kind": "none", "reason": "stale caller hint"},
    }
    return state


def _prereg(state, *, frozen=True):
    saved = state.save_artifact("pre_registration", "prereg_task_authority", "# protocol\n")
    if frozen:
        state.mark_frozen(saved["id"])
    version = state.artifact_versions(saved["id"])[-1]
    return {"artifact_id": saved["id"], "version": record_version(version),
            "content_hash": version["content_hash"]}


def _bind(state, assignment):
    revision = TaskContractLog(state.project_root / "tasks").append(
        task_instance_uuid="task-exact", objective="run this task", target_node="experiment",
        prereg_assignment=assignment, actor="parent", reason="explicit dispatch",
    )
    state.task_instance_uuid = revision.task_instance_uuid
    state.task_contract_digest = revision.digest
    state.task_contract_revision = revision.revision
    state.parent_dispatch_id = "dispatch-exact"
    return revision


def _exact(binding):
    return PreregAssignment.exact(binding["artifact_id"], version=str(binding["version"]),
                                 content_hash=binding["content_hash"])


def test_bootstrap_reads_core_selected_binding_without_rewriting_caller_inputs(tmp_path):
    state = _state(tmp_path)
    binding = _prereg(state)
    revision = _bind(state, _exact(binding))
    original = deepcopy(state.hook_state["node_inputs"])
    derived = run_bootstrap.derive_scope(state)
    assert derived["prereg_status"] == "bound_valid", derived
    assert derived["scope"] == "scientific"
    assert derived["prereg"] == binding
    assert derived["task_contract"]["digest"] == revision.digest
    assert state.hook_state["node_inputs"] == original


def test_contract_and_binding_witness_read_the_same_selected_revision(tmp_path):
    state = _state(tmp_path)
    binding = _prereg(state)
    _bind(state, _exact(binding))
    contract = run_contract.load_run_contract(state)
    witness = run_contract.observe_current_prereg_binding_witness(state)
    assert contract["prereg_assignment"]["kind"] == "bound", contract
    assert contract["prereg_assignment"]["artifact_id"] == binding["artifact_id"]
    assert witness["passed"] is True, witness
    assert witness["binding"] == binding, witness


def test_core_pending_does_not_accept_a_mutable_bound_hint(tmp_path):
    state = _state(tmp_path)
    binding = _prereg(state)
    _bind(state, None)
    state.hook_state["node_inputs"]["prereg_assignment"] = {"kind": "bound", **binding}
    derived = run_bootstrap.derive_scope(state)
    assert derived["prereg_status"] == "omitted", derived
    assert derived["task_assignment_kind"] == "pending_assignment"
    assert derived["formal_input_authority"] == "unavailable"


def test_core_explicit_none_does_not_adopt_an_unselected_catalog_prereg(tmp_path):
    state = _state(tmp_path)
    binding = _prereg(state)
    _bind(state, PreregAssignment.none("this task has no prereg"))
    state.hook_state["node_inputs"]["prereg_assignment"] = {"kind": "bound", **binding}
    derived = run_bootstrap.derive_scope(state)
    assert derived["prereg_status"] == "none", derived
    assert derived["prereg"] is None


def test_unavailable_selected_contract_never_falls_back_to_node_inputs(tmp_path):
    state = _state(tmp_path)
    state.task_instance_uuid = "task-missing"
    state.task_contract_digest = "f" * 64
    state.task_contract_revision = 1
    derived = run_bootstrap.derive_scope(state)
    assert derived["formal_input_authority"] == "unavailable", derived
    assert derived["startup_blocker"]["code"] == "task_contract_unavailable"


def test_same_contract_can_revalidate_an_input_that_was_not_yet_frozen(tmp_path):
    state = _state(tmp_path)
    binding = _prereg(state, frozen=False)
    revision = _bind(state, _exact(binding))
    first = asyncio.run(run_bootstrap.establish(state))
    assert first["scope"]["formal_input_authority"] == "unavailable", first
    state.mark_frozen(binding["artifact_id"])
    second = asyncio.run(run_bootstrap.establish(state))
    authority = run_bootstrap.load_run_authority(state)
    assert authority["prereg_status"] == "bound_valid", (second, authority)
    assert authority["startup_blocker"] is None
    assert authority["task_contract"]["digest"] == revision.digest
    records = ledger.open_ledger(state).records()
    assert sum(row["kind"] == "run_bootstrap" for row in records) == 1
    assert sum(row["kind"] == "run_authority_refreshed" for row in records) == 1
    asyncio.run(run_bootstrap.establish(state))
    assert sum(row["kind"] == "run_authority_refreshed"
               for row in ledger.open_ledger(state).records()) == 1


def test_new_revision_is_not_adopted_without_a_new_core_dispatch(tmp_path):
    state = _state(tmp_path)
    original = _bind(state, None)
    asyncio.run(run_bootstrap.establish(state))
    binding = _prereg(state)
    TaskContractLog(state.project_root / "tasks").append(
        task_instance_uuid=original.task_instance_uuid, parent_revision_digest=original.digest,
        prereg_assignment=_exact(binding), actor="parent", reason="input now ready",
    )
    state.hook_state["node_inputs"]["prereg_assignment"] = {"kind": "bound", **binding}
    again = asyncio.run(run_bootstrap.establish(state))
    assert again["scope"]["prereg_status"] == "omitted", again
    assert again["scope"]["task_assignment_kind"] == "pending_assignment"
    assert again["scope"]["task_contract"]["digest"] == original.digest


def test_input_revalidation_is_automatic_not_another_model_repair_tool(tmp_path):
    from types import SimpleNamespace
    state = _state(tmp_path)
    binding = _prereg(state, frozen=False)
    _bind(state, _exact(binding))
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready"
    state.mark_frozen(binding["artifact_id"])
    messages = run_bootstrap._on_turn_start(SimpleNamespace(state=state, turn=2))
    authority = run_bootstrap.load_run_authority(state)
    assert authority["prereg_status"] == "bound_valid", authority
    assert messages and "正式输入" in messages[0].content
    assert run_bootstrap._on_turn_start(SimpleNamespace(state=state, turn=3)) is None


def test_core_unassigned_prereg_is_not_itself_a_scientific_task_declaration(tmp_path):
    state = _state(tmp_path)
    _bind(state, None)
    # Core field absence is not a caller explicitly requesting a prereg.
    derived = run_bootstrap.derive_scope(state)
    assert derived["scope"] == "operation", derived
    assert derived["prereg_status"] == "omitted"
    assert derived["task_assignment_kind"] == "pending_assignment"
    assert derived["formal_input_authority"] == "unavailable"
    state.hook_state["node_inputs"].pop("scope")
    assert run_bootstrap.derive_scope(state)["scope"] is None


def test_unassigned_unused_prereg_does_not_block_an_operation_delivery(tmp_path):
    from nodes.experiment.tools import action_boundary, contract_audit, finish
    state = _state(tmp_path)
    state.hook_state["node_inputs"]["required_outputs"] = [
        {"id": "out", "kind": "file", "path": "result.txt"},
    ]
    _bind(state, None)
    asyncio.run(run_bootstrap.establish(state))
    target = working_directory(state) / "result.txt"
    handle = action_boundary.begin(state, tool="safe_write_file", output_candidates=[target])
    assert not isinstance(handle, action_boundary.BoundaryRefusal)
    target.write_text("observed operation output\n")
    assert action_boundary.settle(state, handle, outcome="settled", exit_code=0)["status"] == "recorded"
    result = asyncio.run(finish._finish(state, outcome="success", summary="output produced"))
    assert result["status"] == "success", result
    assert result["delivery"]["formal_input"]["status"] == "none"
    # Delivery absence is not an assertion that Core chose explicit_none.
    assert run_bootstrap.load_run_authority(state)["task_assignment_kind"] == "pending_assignment"
    assert run_contract.load_run_contract(state)["prereg_assignment"]["kind"] == "pending"
    assert contract_audit.audit_experiment_delivery(state)["passed"] is True


def test_unassigned_unused_prereg_does_not_block_honest_blocked_delivery(tmp_path):
    from nodes.experiment.tools import finish
    state = _state(tmp_path)
    _bind(state, None)
    asyncio.run(run_bootstrap.establish(state))
    result = asyncio.run(finish._finish(
        state, outcome="blocked", summary="compiler unavailable",
        blocker={"summary": "compiler unavailable"},
    ))
    assert result["status"] == "success", result
    assert result["delivery"]["outcome"] == "blocked"
