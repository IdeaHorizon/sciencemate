"""060 S1b：授权包络、作业归属、边界披露（governing §4 I8、§8）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import capture, ledger, remote_admission as ra, scheduler_adapters as sa


def _state(tmp_path: Path, envelope: dict | None = None) -> State:
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    inputs = {"requested_work": "submit"}
    if envelope is not None:
        inputs["authorization_envelope"] = envelope
    state.hook_state["node_inputs"] = inputs
    ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.record(state, "run_bootstrap", scope="operation", scope_status="success")
    ledger.mark_bootstrap_ready(state)
    ra.freeze_envelope(state)
    return state


def test_inside_the_envelope_a_second_submission_is_not_asked_about_again(tmp_path):
    state = _state(tmp_path, {"cpus": 8, "gpus": 0, "walltime_minutes": 120, "cost_usd": 5})
    req = {"cpus": 4, "gpus": 0, "walltime_minutes": 30, "cost_usd": 1}
    first = ra.check_submission(state, scheduler="slurm", request=req)
    # 首次真实外部提交要问一次
    assert first["decision"] == "ask_once"
    assert [r["scope"] for r in first["reasons"]] == ["first_external_submission"]
    ra.record_authorization(state, scope="first_external_submission", granted_by="user")
    ledger.record(state, "intent", nonce="n1", scheduler="slurm", output_root="r")
    second = ra.check_submission(state, scheduler="slurm", request=req)
    assert second["decision"] == "allowed" and second["reasons"] == []


def test_going_beyond_the_envelope_asks_once_and_names_the_dimension(tmp_path):
    state = _state(tmp_path, {"cpus": 8, "gpus": 0, "walltime_minutes": 120})
    ledger.record(state, "intent", nonce="n0", scheduler="slurm", output_root="r")  # 不是首次了
    out = ra.check_submission(state, scheduler="slurm", request={"cpus": 64, "walltime_minutes": 30})
    assert out["decision"] == "ask_once"
    beyond = [r for r in out["reasons"] if r["why"] == "beyond_envelope"]
    assert beyond and beyond[0] == {"scope": "cpus", "why": "beyond_envelope", "requested": 64, "limit": 8}
    ra.record_authorization(state, scope="cpus", granted_by="user", note="用户当轮批准 64 核")
    again = ra.check_submission(state, scheduler="slurm", request={"cpus": 64, "walltime_minutes": 30})
    assert again["decision"] == "allowed"


def test_a_dimension_with_no_declared_limit_is_disclosed_not_silently_allowed(tmp_path):
    state = _state(tmp_path, {"cpus": 8})
    ledger.record(state, "intent", nonce="n0", scheduler="slurm", output_root="r")
    out = ra.check_submission(state, scheduler="slurm", request={"cpus": 4, "gpus": 4})
    assert out["decision"] == "ask_once"
    assert {"scope": "gpus", "why": "no_declared_limit", "requested": 4} in out["reasons"]


def test_a_local_run_does_not_trigger_the_first_external_submission_question(tmp_path):
    state = _state(tmp_path, {"cpus": 8})
    out = ra.check_submission(state, scheduler="local", request={"cpus": 2})
    assert out["decision"] == "allowed"




def test_a_missing_remote_isolation_is_never_disclosed_as_enforced(tmp_path):
    for scheduler in ("slurm", "pbs", "torque"):
        d = ra.isolation_disclosure(scheduler)
        assert d["remote_write_isolation"] == sa.ISOLATION_MISSING and d["enforced"] is False
        assert "没有沙箱" in d["means"]
    local = ra.isolation_disclosure("local")
    assert local["enforced"] is True and "物理写不进去" in local["means"]


def test_the_envelope_is_frozen_once_and_the_model_cannot_widen_it(tmp_path):
    state = _state(tmp_path, {"cpus": 8})
    assert ra.frozen_envelope(state)["limits"] == {"cpus": 8}
    state.hook_state["node_inputs"]["authorization_envelope"] = {"cpus": 1024}
    assert ra.freeze_envelope(state)["status"] == "already_frozen"
    assert ra.frozen_envelope(state)["limits"] == {"cpus": 8}




def test_finish_discloses_the_write_boundary_it_actually_had(tmp_path):
    """adapter 说 missing，收尾件里就必须写 missing。"""
    import asyncio
    import json as _json
    from nodes.experiment.tools import finish, obligations, run_bootstrap

    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    target = Path(state.root) / "run/out.txt"
    state.hook_state["node_inputs"] = {
        "prereg_assignment": {"kind": "none", "reason": "本地任务"},
        "scope": "operation", "operation_category": "toolchain_build",
        "requested_work": "提交一个作业",
        "required_outputs": [{"id": "out", "kind": "file", "path": str(target)}]}
    asyncio.run(run_bootstrap.establish(state))
    led = ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.record(state, "run_bootstrap", scope="operation", scope_status="success")
    ledger.mark_bootstrap_ready(state)
    from nodes.experiment.tools import action_boundary as ab

    handle = ab.begin(state, tool="submit_job", effect_class=ab.EXPERIMENT, output_candidates=[target])
    assert not isinstance(handle, ab.BoundaryRefusal), handle
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("job output\n")
    assert ab.settle(state, handle, outcome="settled", exit_code=0)["status"] == "recorded"
    led.append("intent", nonce="n1", scheduler="slurm", output_root="r")
    led.append("job_bound", nonce="n1", external_id="77")
    led.append("job_terminal", nonce="n1", derived_state="COMPLETED", exit_code="0:0")
    out = asyncio.run(finish._finish(state, outcome="success", summary="作业跑完了"))
    assert out["status"] == "success", out
    clean = _json.loads(state.read_artifact(out["artifact_ids"]["clean_results"])["content"])
    raw_record = state.read_artifact(out["artifact_ids"]["raw_results"])
    raw = _json.loads(raw_record["content"])
    boundary = raw["write_boundary"]
    assert clean["write_boundary"] == boundary
    assert "operation_closure_input" not in raw_record["metadata"]
    assert boundary and boundary[0]["scheduler"] == "slurm"
    assert boundary[0]["enforced"] is False and boundary[0]["remote_write_isolation"] == "missing"
    assert clean["delivery"]["outcome"] == "success"
