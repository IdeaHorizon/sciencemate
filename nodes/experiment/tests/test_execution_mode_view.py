"""All execution-mode consumers must use the receipt-backed read view."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from core.state import State
from nodes.experiment import hooks
from nodes.experiment.tools.execution_route import (
    _declare_execution_route,
    _execution_scope_projection,
)
from nodes.experiment.tools.operation_completion import (
    _operation_closure_artifact_save_gate,
    operation_closure_status,
)
from nodes.experiment.tools.preprocessing_boundary import (
    scan_preprocessing_boundary,
)
import nodes.experiment.tools.run_contract as run_contract
from nodes.experiment.tools.run_contract import (
    _classify_experiment_scope,
    load_execution_mode_view,
)


def _receipt_scientific_with_stale_operation_cache(tmp_path: Path) -> State:
    """Return a valid prereg-bound run whose mutable cache is stale."""
    state = State.new("experiment", tmp_path)
    prereg_id = state.save_artifact(
        "pre_registration",
        "mode-view-prereg",
        "# frozen preregistration\n",
        metadata={
            "run_role": "primary",
            "stage": "simulation",
            "execution_mode": "scientific",
        },
    )["id"]
    state.mark_frozen(prereg_id)
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "experiment_focus": "Run the preregistered scientific measurement.",
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="toolchain_build",
        reason="Exercise receipt authority over a stale mode cache.",
    ))
    assert classified["status"] == "success", classified
    assert classified["classification"]["mode"] == "scientific"
    # Simulate an interrupted/stale compatibility-cache update.  The immutable
    # transcript receipt and scope event remain scientific and authoritative.
    state.hook_state["experiment_execution_scope"] = {
        "mode": "operational",
        "category": "toolchain_build",
    }
    assert state.hook_state["_request_mode"] == "scientific"
    return state


def _scientific_route() -> dict:
    return {
        "schema_version": 2,
        "goal": "Run the preregistered measurement",
        "evidence_refs": ["artifact://mode-view-prereg"],
        "steps": [{
            "id": "measure",
            "goal": "Produce the preregistered measurement",
            "after": [],
            "action": {"tool": "submit_job", "program": "./measure"},
            "effects": ["scientific_execution", "external_job"],
            "workdir_role": "run_root",
            "expected_outputs": [],
        }],
    }


def _record_preprocessing_write(state: State) -> None:
    state.append_transcript(
        "tool_call",
        turn=1,
        name="safe_execute_python",
        args={"code": "from ase.build import bulk\nbulk('Si').write('POSCAR')"},
    )


def test_view_does_not_turn_an_unclassified_default_into_a_classification(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)

    view = load_execution_mode_view(state)

    assert view == {
        "mode": None,
        "classified": False,
        "classification_present": False,
        "status": "absent",
        "source": "unclassified",
    }


def test_view_never_scans_the_project_artifact_catalog(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)

    def unexpected_scan(*_args, **_kwargs):
        raise AssertionError("execution-mode view must not scan project artifacts")

    state.list_artifacts = unexpected_scan

    view = load_execution_mode_view(state)

    assert view["classified"] is False
    assert view["mode"] is None


def test_view_preserves_a_consistent_operation_classification(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Build and mechanically verify a local package.",
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="toolchain_build",
        reason="Exercise the consistent operation path.",
    ))
    assert classified["status"] == "success", classified

    view = load_execution_mode_view(state)

    assert view["mode"] == "operational"
    assert view["classified"] is True
    assert view["source"] == "classification_result"


def test_view_uses_durable_operation_when_cache_is_stale_scientific(
    tmp_path: Path,
) -> None:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Build and mechanically verify a local package.",
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="toolchain_build",
        reason="Exercise the durable classification projection.",
    ))
    assert classified["status"] == "success", classified
    state.hook_state["experiment_execution_scope"] = {
        "mode": "scientific",
        "category": None,
    }

    view = load_execution_mode_view(state)

    assert view["mode"] == "operational"
    assert view["classified"] is True
    assert view["source"] == "classification_result"


def test_bound_receipt_forces_scientific_over_operational_projection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state = State.new("experiment", tmp_path)
    monkeypatch.setattr(
        run_contract,
        "_reduce_run_acceptance_receipts",
        lambda _state: {
            "passed": True,
            "status": "bound",
            "receipt": {
                "governing_task_input_binding": {
                    "artifact_id": "pre_registration__mode-view-prereg",
                    "version": 1,
                    "content_hash": "a" * 64,
                },
            },
            "durable_scope_projection": {
                "mode": "operational",
                "category": "toolchain_build",
            },
        },
    )

    view = load_execution_mode_view(state)

    assert view["mode"] == "scientific"
    assert view["classified"] is True
    assert view["source"] == "run_acceptance_receipt"


def test_scope_projection_reports_receipt_forced_scientific_source(
    tmp_path: Path,
) -> None:
    state = _receipt_scientific_with_stale_operation_cache(tmp_path)

    projection = _execution_scope_projection(state, required=False)

    assert projection["scope_status"] == "classified"
    assert projection["scope_mode"] == "scientific"
    assert projection["scope_source"] == "run_acceptance_receipt"


def test_route_declaration_ignores_stale_operational_cache(
    tmp_path: Path,
) -> None:
    state = _receipt_scientific_with_stale_operation_cache(tmp_path)

    result = asyncio.run(_declare_execution_route(
        state,
        route=_scientific_route(),
    ))

    assert result.get("error_code") != "route_scope_effect_mismatch", result


def test_skill_router_uses_receipt_forced_scientific_mode(
    tmp_path: Path,
) -> None:
    state = _receipt_scientific_with_stale_operation_cache(tmp_path)

    messages = hooks._experiment_skill_router_on_turn_end(
        SimpleNamespace(state=state, turn=1),
    )

    assert messages is not None
    assert "load_skill(name='scientific-results')" in messages[0].content
    assert "record_operation_completion" not in messages[0].content


def test_save_gate_does_not_treat_stale_cache_as_operation(
    tmp_path: Path,
) -> None:
    state = _receipt_scientific_with_stale_operation_cache(tmp_path)

    result = _operation_closure_artifact_save_gate(
        state,
        {"type": "raw_results"},
    )

    assert result == {}


def test_operation_closure_status_does_not_open_for_stale_operation_cache(
    tmp_path: Path,
) -> None:
    state = _receipt_scientific_with_stale_operation_cache(tmp_path)

    status = operation_closure_status(state)

    assert status == {
        "closure_id": f"{state.run_id}:operation",
        "kind": "empty",
        "sealed": False,
        "owned_artifact_count": 0,
    }


def test_preprocessing_accounting_does_not_use_stale_operation_cache(
    tmp_path: Path,
) -> None:
    state = _receipt_scientific_with_stale_operation_cache(tmp_path)
    _record_preprocessing_write(state)

    report = scan_preprocessing_boundary(state)

    assert report["n_hits"] >= 1
    assert report["n_unaccounted"] >= 1
    assert "operation_scope" not in report["accounting_reasons"]
