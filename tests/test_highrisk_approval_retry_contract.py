"""Job identities and strict denials remain valid after operation approvals.

Approval execution/recovery replaces the retired model-retry contract; its
behavioral regressions live in test_operation_approval.py.
"""
import pytest
from shared.lib import dangerous_commands as _dc
from nodes.experiment.tools.resource_manager import _local_container_status

def test_local_job_status_error_names_the_valid_id_shape() -> None:
    err = _local_container_status("ka_lj_slow_rep1")["stderr"]

    assert "ka_lj_slow_rep1" in err, "没回显它到底传了什么"
    assert "submit_job" in err, "没说合法值从哪儿来"
    assert "hf-" in err and "PID" in err

def test_a_host_pid_is_never_a_local_job_identity() -> None:
    import os
    assert _local_container_status(str(os.getpgrp()))["ok"] is False

def test_local_job_status_rejects_a_reused_container_name(monkeypatch) -> None:
    from core import sandbox

    monkeypatch.setattr(sandbox, "inspect_container", lambda _name: {
        "exists": True,
        "running": True,
        "managed": True,
        "id": "b" * 64,
        "kind": "job",
        "namespace": "harness",
    })
    result = _local_container_status("hf-" + "a" * 20, "c" * 64)
    assert result["ok"] is False
    assert "不匹配" in result["stderr"]

def test_local_job_status_rejects_an_unmanaged_same_name_container(monkeypatch) -> None:
    from core import sandbox

    monkeypatch.setattr(sandbox, "inspect_container", lambda _name: {
        "exists": True,
        "running": True,
        "managed": False,
        "id": "b" * 64,
    })
    result = _local_container_status("hf-" + "a" * 20, "b" * 64)
    assert result["ok"] is False
    assert "受管标签" in result["stderr"]

def test_local_job_status_accepts_the_namespace_bound_identity(monkeypatch) -> None:
    from core import sandbox

    monkeypatch.setenv("HARNESS_SANDBOX_NAMESPACE", "node20")
    monkeypatch.setattr(sandbox, "inspect_container", lambda _name: {
        "exists": True,
        "running": False,
        "managed": True,
        "id": "b" * 64,
        "kind": "job",
        "namespace": "node20",
    })

    result = _local_container_status("hf-node20-" + "a" * 20, "b" * 64)

    assert result["ok"] is True
    assert result["stdout"] == "NOT_RUNNING"

def test_denial_wording_is_not_swallowed_by_a_loose_approve_word(tmp_path):
    from core import approvals
    from core.state import State
    state = State.new("experiment", tmp_path)
    payload = _dc.build_pause_payload(state, tool="run_bash", text="test", category="test", preview="test")["pause_event"]
    assert approvals.choice_for(payload, "拒绝") == "deny"
    with pytest.raises(approvals.ApprovalError):
        approvals.choice_for(payload, "不要批准")
