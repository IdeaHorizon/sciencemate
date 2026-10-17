"""Node execution probes after Core-selected formal-input corrections.

These tests keep the authority boundary explicit: an existing run may only
re-observe the exact Core-selected contract, while a different revision is
visible only when Core selects it before the fresh run bootstrap.  Fixtures
write that selection through TaskContractLog; they do not exercise the parent
dispatcher or prove same-run amendment authorization.
"""
from __future__ import annotations

import asyncio
import shlex
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from core.ledger import record_version
from core.project_workspace import working_directory
from core.state import State
from core.task_contract import PreregAssignment, TaskContractLog
from nodes.experiment.tools import finish, ledger, run_bootstrap, safe_bash
from nodes.experiment.tools.path_roles import collect_path_roles

OUTPUT_RELATIVE = "runtime/recovered_square.txt"
EXPECTED_RESULT = "squared_value=49"
EXECUTION_CONTRACT = {
    "version": 1,
    "scientific_params": {"value": 7},
    "runtime_params": {},
}


def _run(awaitable):
    return asyncio.run(awaitable)


def _state(tmp_path: Path, *, project_id: str) -> State:
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id=project_id,
        session_id="owner",
    )
    state.hook_state["node_inputs"] = {
        "scope": "scientific",
        "experiment_spec": "Run the Core-selected preregistered square calculation.",
        "required_outputs": [
            {"id": "square_file", "kind": "file", "path": OUTPUT_RELATIVE},
            {
                "id": "square_check",
                "kind": "check",
                "command": (
                    f"grep -Fx {shlex.quote(EXPECTED_RESULT)} "
                    f"{shlex.quote(OUTPUT_RELATIVE)}"
                ),
                "expect_exit": 0,
                "expect_stdout_contains": EXPECTED_RESULT,
            },
        ],
        # Deliberately non-authoritative.  Core's selected revision below must
        # remain the source read by bootstrap, actions, and finish.
        "prereg_assignment": {
            "kind": "none",
            "reason": "stale mutable caller hint",
        },
    }
    return state


def _save_prereg(state: State, *, frozen: bool, value: int = 7) -> dict:
    execution_contract = {
        **EXECUTION_CONTRACT,
        "scientific_params": {"value": value},
    }
    saved = state.save_artifact(
        "pre_registration",
        "authorized_correction",
        f"# Preregistered square calculation\n\nvalue={value}\n",
        metadata={"execution_contract": execution_contract},
    )
    if frozen:
        state.mark_frozen(saved["id"], {"freeze_reason": "formal task fixture"})
    version = state.artifact_versions(saved["id"])[-1]
    return {
        "artifact_id": saved["id"],
        "version": record_version(version),
        "content_hash": version["content_hash"],
    }


def _exact(binding: dict) -> PreregAssignment:
    return PreregAssignment.exact(
        binding["artifact_id"],
        version=str(binding["version"]),
        content_hash=binding["content_hash"],
    )


def _select(state: State, revision) -> None:
    state.task_instance_uuid = revision.task_instance_uuid
    state.task_contract_digest = revision.digest
    state.task_contract_revision = revision.revision
    state.parent_dispatch_id = f"dispatch-r{revision.revision}"


def _run_root(state: State) -> Path:
    roots = {
        role.role: Path(role.path)
        for role in collect_path_roles(state)
        if role.writable and not role.container_only
    }
    return roots["run_root"]


def _experiment_actions(state: State) -> list[dict]:
    return [
        row
        for row in ledger.open_ledger(state).records()
        if row.get("kind") == "action_started"
        and row.get("effect_class") == "experiment"
    ]


def test_same_exact_binding_recovery_flows_to_action_and_finish_without_redo(
    tmp_path: Path,
):
    state = _state(tmp_path, project_id="authorized-correction-recovery")
    binding = _save_prereg(state, frozen=False)
    revision = TaskContractLog(state.project_root / "tasks").append(
        task_instance_uuid="task-authorized-correction",
        objective="run the preregistered square calculation",
        target_node="experiment",
        prereg_assignment=_exact(binding),
        actor="parent",
        reason="formal dispatch",
    )
    _select(state, revision)

    first = _run(run_bootstrap.establish(state))
    assert first["scope"]["prereg_status"] == "invalid", first
    assert first["scope"]["formal_input_authority"] == "unavailable", first
    assert first["scope"]["startup_blocker"] is not None

    # The artifact identity and Core task revision do not change.  A normal
    # turn-start re-observes availability and appends one authority refresh.
    state.mark_frozen(binding["artifact_id"], {"freeze_reason": "input became readable"})
    messages = run_bootstrap._on_turn_start(SimpleNamespace(state=state, turn=2))
    assert messages and "正式输入已就绪" in messages[0].content

    records = ledger.open_ledger(state).records()
    refresh = [row for row in records if row.get("kind") == "run_authority_refreshed"]
    assert len(refresh) == 1, records
    authority = run_bootstrap.load_run_authority(state)
    assert authority["prereg_status"] == "bound_valid", authority
    assert authority["startup_blocker"] is None
    assert authority["task_contract"]["digest"] == revision.digest
    assert authority["prereg"] == binding

    target = working_directory(state) / OUTPUT_RELATIVE
    program = "value = 7; print(f'squared_value={value * value}')"
    executed = _run(
        safe_bash._safe_run_bash(
            state,
            cmd=(
                f"{shlex.quote(sys.executable)} -c {shlex.quote(program)} "
                f"> {shlex.quote(str(target))}"
            ),
            cwd=str(_run_root(state)),
            timeout=30,
            stage="simulation",
            execution_params={
                key: EXECUTION_CONTRACT[key]
                for key in ("scientific_params", "runtime_params")
            },
        )
    )
    assert executed.get("status") == "success", executed
    assert executed.get("returncode") == 0, executed
    assert target.read_text(encoding="utf-8").strip() == EXPECTED_RESULT

    actions = _experiment_actions(state)
    assert len(actions) == 1, actions
    context = actions[0]["execution_context"]
    assert context["authority_seq"] == refresh[0]["seq"]
    assert context["task_contract"]["digest"] == revision.digest
    assert context["prereg_binding"] == binding
    assert context["parameter_comparison"]["passed"] is True

    result = _run(
        finish._finish(
            state,
            outcome="success",
            summary="The recovered exact preregistration governed the checked calculation.",
            deliverables=[
                {"path": OUTPUT_RELATIVE, "obligation_id": "square_file"},
            ],
        )
    )
    assert result["status"] == "success", result
    assert result["required_satisfied"] == "2/2"
    assert result["delivery"]["formal_input"] == {
        "status": "valid",
        "binding": binding,
        "error": None,
    }

    output_identity = (target.stat().st_mtime_ns, target.read_bytes())
    records_before_replay = ledger.open_ledger(state).records()
    assert run_bootstrap._on_turn_start(SimpleNamespace(state=state, turn=3)) is None
    replay = _run(
        finish._finish(
            state,
            outcome="success",
            summary="The recovered exact preregistration governed the checked calculation.",
            deliverables=[
                {"path": OUTPUT_RELATIVE, "obligation_id": "square_file"},
            ],
        )
    )
    assert replay["status"] == "success" and replay["reused_manifest"] is True
    assert (target.stat().st_mtime_ns, target.read_bytes()) == output_identity
    assert len(_experiment_actions(state)) == 1
    assert ledger.open_ledger(state).records() == records_before_replay


def test_fresh_bootstrap_uses_core_selected_revision_despite_stale_node_inputs(
    tmp_path: Path,
):
    state = _state(tmp_path, project_id="authorized-correction-fresh-dispatch")
    first_binding = _save_prereg(state, frozen=True, value=7)
    contracts = TaskContractLog(state.project_root / "tasks")
    first_revision = contracts.append(
        task_instance_uuid="task-fresh-revision",
        objective="run preregistration version one",
        target_node="experiment",
        prereg_assignment=_exact(first_binding),
        actor="parent",
        reason="first formal dispatch",
    )

    amended = state.save_artifact(
        "pre_registration",
        "authorized_correction",
        "# Preregistered square calculation\n\nvalue=8\n",
        metadata={
            "execution_contract": {
                **EXECUTION_CONTRACT,
                "scientific_params": {"value": 8},
            }
        },
        amendment_reason="parent approved the corrected value",
    )
    state.mark_frozen(amended["id"], {"freeze_reason": "approved amendment"})
    second_version = state.artifact_versions(amended["id"])[-1]
    second_binding = {
        "artifact_id": amended["id"],
        "version": record_version(second_version),
        "content_hash": second_version["content_hash"],
    }
    second_revision = contracts.append(
        task_instance_uuid=first_revision.task_instance_uuid,
        objective="run preregistration version two",
        target_node="experiment",
        prereg_assignment=_exact(second_binding),
        actor="parent",
        reason="fresh formal dispatch selects the approved amendment",
        parent_revision_digest=first_revision.digest,
    )

    stale_inputs = deepcopy(state.hook_state["node_inputs"])
    stale_inputs["prereg_assignment"] = {"kind": "bound", **first_binding}
    state.hook_state["node_inputs"] = stale_inputs
    _select(state, second_revision)

    established = _run(run_bootstrap.establish(state))
    authority = run_bootstrap.load_run_authority(state)
    assert established["scope"]["prereg_status"] == "bound_valid", established
    assert authority["task_contract"]["revision"] == second_revision.revision
    assert authority["task_contract"]["digest"] == second_revision.digest
    assert authority["prereg"] == second_binding
    assert state.hook_state["node_inputs"]["prereg_assignment"] == {
        "kind": "bound",
        **first_binding,
    }
