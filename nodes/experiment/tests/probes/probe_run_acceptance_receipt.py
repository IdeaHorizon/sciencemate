"""Self-contained P0a probe; assertions determine the process exit status."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from core.state import State
import shared.tools.run_node as run_node_module
from nodes.experiment.tools import contract_audit
from nodes.experiment.tools.run_contract import (
    _classify_experiment_scope,
    audit_execution_intent_binding,
    resolve_run_acceptance,
)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="experiment-run-acceptance-") as directory:
        root = Path(directory)

        operation = State.new("experiment", root / "operation")
        operation.hook_state["node_inputs"] = {
            "experiment_focus": "Mechanically verify the package before any scientific task.",
        }
        operation_result = asyncio.run(_classify_experiment_scope(
            operation,
            scope="operation",
            operation_category="toolchain_build",
            reason="Exercise the write-once run input acceptance path.",
        ))
        assert operation_result["status"] == "success", operation_result
        operation_receipt = resolve_run_acceptance(
            operation, bind_if_absent=False
        )
        assert operation_receipt["passed"] is True, operation_receipt
        assert (
            operation_receipt["receipt"]["governing_task_input_binding"] is None
        )
        operation_prereg = operation.save_artifact(
            "pre_registration",
            "late_operation",
            "# unrelated late frozen preregistration\n",
            metadata={"run_role": "primary", "execution_mode": "scientific"},
        )["id"]
        operation.mark_frozen(operation_prereg)
        operation_audit = audit_execution_intent_binding(
            operation, require=True
        )
        assert operation_audit["passed"] is True, operation_audit
        assert resolve_run_acceptance(
            operation, bind_if_absent=False
        )["receipt"] == operation_receipt["receipt"]

        scientific = State.new("experiment", root / "scientific")
        scientific.hook_state["node_inputs"] = {
            "experiment_focus": "Explore a scientific question without a preregistration.",
            "prereg_assignment": {
                "kind": "none",
                "reason": "This is an explicitly unpreregistered exploratory run.",
            },
        }
        scientific_result = asyncio.run(_classify_experiment_scope(
            scientific,
            scope="scientific",
            operation_category="other",
            reason="Bind the unpreregistered scientific run before project growth.",
        ))
        assert scientific_result["status"] == "success", scientific_result
        scientific_prereg = scientific.save_artifact(
            "pre_registration",
            "late_scientific",
            "# late governing scientific preregistration\n",
            metadata={"run_role": "primary", "execution_mode": "scientific"},
        )["id"]
        scientific.mark_frozen(scientific_prereg)
        scientific_audit = audit_execution_intent_binding(
            scientific, require=False
        )
        assert scientific_audit["passed"] is True, scientific_audit
        assert scientific_audit["status"] == "bound", scientific_audit
        assert scientific_audit["prereg_assignment"]["kind"] == "none"

        reclassified = State.new("experiment", root / "reclassified")
        for name in ("candidate_a", "candidate_b"):
            prereg_id = reclassified.save_artifact(
                "pre_registration",
                name,
                f"# {name}\n",
                metadata={
                    "run_role": "primary",
                    "execution_mode": "scientific",
                },
            )["id"]
            reclassified.mark_frozen(prereg_id)
        reclassified.hook_state["node_inputs"] = {
            "experiment_focus": "Build a tool unrelated to either preregistration.",
        }
        first = asyncio.run(_classify_experiment_scope(
            reclassified,
            scope="operation",
            operation_category="toolchain_build",
            reason="Accept the mechanical operation without a governing preregistration.",
        ))
        second = asyncio.run(_classify_experiment_scope(
            reclassified,
            scope="scientific",
            operation_category="other",
            reason="Attempt to reinterpret the operation as ambiguous science.",
        ))
        assert first["status"] == "success", first
        assert second["status"] == "error", second
        assert second["error_code"] == "prereg_assignment_required", second
        assert reclassified.hook_state[
            "experiment_execution_scope"
        ]["mode"] == "operational"

        late_dispatch = State.new("experiment", root / "late_dispatch")
        late_dispatch.hook_state["node_inputs"] = {
            "experiment_focus": "Reject a formal child for a preregistration accepted too late.",
        }
        late_result = asyncio.run(_classify_experiment_scope(
            late_dispatch,
            scope="operation",
            operation_category="other",
            reason="Accept the run before any preregistration exists.",
        ))
        assert late_result["status"] == "success", late_result
        late_prereg = late_dispatch.save_artifact(
            "pre_registration",
            "late_dispatch_prereg",
            "# late formal input preregistration\n",
            metadata={
                "run_role": "primary",
                "execution_mode": "scientific",
                "expected_params": {"grid": [12, 12]},
            },
        )["id"]
        late_dispatch.mark_frozen(late_prereg)
        request_spec = json.dumps({
            "request_kind": "formal_input_preparation",
            "source_prereg_artifact_id": late_prereg,
            "scientific_parameters": "frozen_prereg_only",
            "required_assets": [{
                "name": "case/input.dat",
                "format": "text",
                "purpose": "solver input",
            }],
            "acceptance": {
                "file_exists": True,
                "schema": True,
                "units": True,
                "manifest_lineage": True,
            },
        })
        validated = asyncio.run(contract_audit._validate_data_request_spec(
            late_dispatch, request_spec
        ))
        launched: list[dict] = []

        async def record_launch(**kwargs):
            launched.append(kwargs)
            return {"status": "success", "child_run_id": "LAUNCHED"}

        original_run_node = run_node_module._run_node_tool
        run_node_module._run_node_tool = record_launch
        try:
            dispatch = asyncio.run(contract_audit._dispatch_data_request(
                late_dispatch,
                validated.get("spec_id"),
                user_note="formal",
            ))
        finally:
            run_node_module._run_node_tool = original_run_node
        assert dispatch.get("status") == "error", dispatch
        assert launched == [], launched


if __name__ == "__main__":
    main()
