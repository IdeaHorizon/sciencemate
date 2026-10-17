"""#917 残余：调用方要 operation、绑定的冻结 prereg 定成 scientific —— v3 bootstrap 要留冲突 witness。

v3 bootstrap 替模型分类时只把生效值 scientific 转交给 classify，classify 里
requested_operation_with_governing_prereg 那条偏离申报永远不触发：冲突被静默吞掉。
验收（#917）：有 prereg + 请求 operation → effective scientific，并保留请求冲突 witness。

走真实 run_bootstrap.establish；不打桩分类与收据。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from core.state import State
from nodes.experiment.tools import ledger, run_bootstrap, run_contract

_KIND = "requested_operation_with_governing_prereg"


def _bound_prereg(state: State, name: str) -> dict:
    from core.ledger import record_version, sha256_text
    saved = state.save_artifact("pre_registration", name, f"# {name}\n",
                                metadata={"execution_mode": "scientific"})
    state.mark_frozen(saved["id"])
    versions = state.artifact_versions(saved["id"])
    return {"kind": "bound", "artifact_id": saved["id"], "version": record_version(versions[-1]),
            "content_hash": str(versions[-1].get("content_hash") or sha256_text(f"# {name}\n"))}


def _establish(tmp_path: Path, **inputs) -> State:
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    if callable(inputs.get("prereg_assignment")):
        inputs["prereg_assignment"] = inputs["prereg_assignment"](state)
    state.hook_state["node_inputs"] = {"experiment_spec": "x", "required_outputs": [], **inputs}
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready"
    return state


def _events(state: State, name: str) -> list[dict]:
    path = Path(state.root) / "transcript.jsonl"
    return [row for row in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                            if line.strip()) if row.get("event") == name]


def test_a_bound_prereg_overriding_a_requested_operation_leaves_a_conflict_witness(tmp_path):
    state = _establish(tmp_path, prereg_assignment=lambda s: _bound_prereg(s, "governing"),
                       scope="operation", operation_category="toolchain_build")

    assert run_bootstrap.load_run_authority(state)["scope"] == "scientific"
    assert run_contract.load_run_contract(state)["execution_mode"] == "scientific"
    deviations = state.hook_state.get(run_contract.PREREG_DEVIATIONS_KEY) or []
    assert [d["kind"] for d in deviations] == [_KIND], deviations
    assert deviations[0]["requested_execution_mode"] == "operational"
    assert deviations[0]["effective_execution_mode"] == "scientific"
    (classified,) = _events(state, "experiment_scope_classified")
    assert classified["mode"] == "scientific"
    assert classified["requested_mode"] == "operational"
    assert classified["requested_category"] == "toolchain_build"
    assert len(_events(state, "prereg_deviation_declared")) == 1

    # 同 run 恢复后重放 bootstrap：幂等，不再申报第二次。
    reopened = State.reopen("experiment", tmp_path / "runs", state.run_id,
                            project_id="p", session_id="owner")
    reopened.hook_state["node_inputs"] = dict(state.hook_state["node_inputs"])
    asyncio.run(run_bootstrap.establish(reopened))
    assert len(_events(reopened, "prereg_deviation_declared")) == 1
    assert run_contract.audit_execution_intent_binding(reopened, require=True)["passed"] is True


def test_a_bound_prereg_without_an_operation_request_declares_no_deviation(tmp_path):
    state = _establish(tmp_path, prereg_assignment=lambda s: _bound_prereg(s, "governing"))
    assert run_bootstrap.load_run_authority(state)["scope"] == "scientific"
    assert not state.hook_state.get(run_contract.PREREG_DEVIATIONS_KEY)
    assert "requested_mode" not in _events(state, "experiment_scope_classified")[0]


def test_an_operation_request_without_a_prereg_is_simply_operation(tmp_path):
    state = _establish(tmp_path, prereg_assignment={"kind": "none", "reason": "纯构建"},
                       scope="operation", operation_category="toolchain_build")
    assert run_bootstrap.load_run_authority(state)["scope"] == "operation"
    assert not state.hook_state.get(run_contract.PREREG_DEVIATIONS_KEY)
    assert _events(state, "experiment_scope_classified")[0]["mode"] == "operational"
