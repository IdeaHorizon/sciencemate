"""The correct delivery entry must be visible before a save/freeze refusal."""
import asyncio

from core.bootstrap import bootstrap
from core.state import State
from core.tool_registry import _REGISTRY
from nodes.experiment.tools import delivery_transaction, ledger, run_bootstrap, task_verification
from shared.tools.library.artifacts_extra import FREEZE_GATE_CONTRACTS, SAVE_GATE_CONTRACTS


def test_shared_save_and_freeze_describe_the_same_scoped_finish_contract():
    bootstrap(force=True)
    contract = delivery_transaction.MODEL_DELIVERY_CONTRACT
    for kind in delivery_transaction.TRIO:
        assert SAVE_GATE_CONTRACTS[kind] == FREEZE_GATE_CONTRACTS[kind] == contract
    for name in ("save_artifact", "freeze_artifact"):
        assert contract["experiment_v3_delivery"] in _REGISTRY.tools[name].description
    assert "仅对 Experiment v3" in contract["experiment_v3_delivery"]
    assert "其他节点" in contract["experiment_v3_delivery"]


def test_initial_one_page_uses_the_same_delivery_contract(tmp_path):
    state = State.new("experiment", tmp_path / "runs", project_id="guidance", session_id="owner")
    state.hook_state["node_inputs"] = {"experiment_spec": "Run the selftest."}
    established = asyncio.run(run_bootstrap.establish(state))
    page = run_bootstrap.one_page(state, established)
    assert delivery_transaction.MODEL_DELIVERY_CONTRACT["experiment_v3_delivery"] in page
    assert "无需逐字引用" in page
    assert "连续引句" not in str(task_verification.SCHEMA)


def test_clean_action_view_does_not_drop_unknown_provenance_explanation():
    facts = {"unknown": ["/business/result"], "overlapping_actions": {"/business/result": ["other"]},
             "coverage": {"before": [], "after": []}, "scan_errors": ["scan unavailable"],
             "observation_boundary": {"roots": ["/business"], "excluded_roots": []}}
    view = ledger.project_actions([
        {"kind": "action_started", "action_id": "writer", "tool": "safe_run_bash"},
        {"kind": "observation", "action_id": "writer", **facts},
    ])[0]["observation"]
    assert view == facts
