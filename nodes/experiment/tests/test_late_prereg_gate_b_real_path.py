"""021 v4 审查：迟到 prereg 的 formal Data dispatch 必须在门 B 被拒（任务书 §5.4 M3）。

不打桩审计，走真实的 classify → validate_data_request_spec → dispatch_data_request；
只把拉起子 run 的 run_node 换成记录调用的替身。自 v2 放开 operation 的 null 收据之后，
门 B 是 operation run 的决定性关口：把它改成「自己跟自己比」，子 run 会真的被拉起。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import shared.tools.run_node as run_node_module
from core.state import State
from nodes.experiment.tools import contract_audit
from nodes.experiment.tools.run_contract import _classify_experiment_scope


def _spec(prereg_id: str) -> str:
    return json.dumps({
        "request_kind": "formal_input_preparation",
        "source_prereg_artifact_id": prereg_id,
        "scientific_parameters": "frozen_prereg_only",
        "required_assets": [{"name": "case/input.dat", "format": "text", "purpose": "solver input"}],
        "acceptance": {"file_exists": True, "schema": True, "units": True, "manifest_lineage": True},
    })


@pytest.mark.parametrize("scope", ["operation", "scientific"])
def test_late_prereg_formal_dispatch_never_launches_the_child(tmp_path: Path, monkeypatch, scope: str) -> None:
    launched: list[dict] = []

    async def fake_run_node(**kwargs):
        launched.append(kwargs)
        return {"status": "success", "child_run_id": "LAUNCHED"}

    monkeypatch.setattr(run_node_module, "_run_node_tool", fake_run_node)
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "late prereg formal dispatch",
        "prereg_assignment": {
            "kind": "none",
            "reason": "No preregistration governed this run at dispatch time.",
        },
    }
    classified = asyncio.run(_classify_experiment_scope(
        state, scope=scope, operation_category="other", reason="accepted before any prereg existed"))
    assert classified["status"] == "success", classified
    prereg_id = state.save_artifact(
        "pre_registration", "late", "# late\n",
        metadata={"run_role": "primary", "execution_mode": "scientific", "expected_params": {"grid": [12, 12]}},
    )["id"]
    state.mark_frozen(prereg_id)

    validated = asyncio.run(contract_audit._validate_data_request_spec(state, _spec(prereg_id)))
    dispatched = asyncio.run(contract_audit._dispatch_data_request(
        state, validated.get("spec_id"), user_note="formal"))

    assert dispatched.get("status") == "error", dispatched
    assert launched == [], "a run accepted before this prereg existed launched a formal Data child for it"
