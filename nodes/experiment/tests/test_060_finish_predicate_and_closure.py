"""060 S1a：finish 的 success 谓词与三件闭包契约。

谓词：每个 required 有正面证据 ∧ 无确定性矛盾。诚实的非 success 收尾永远可达。
闭包：三件冻结、同一组 closure 元数据、平台 hooks 能读回有效 outcome。
"""
from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import contract_audit, finish, ledger, obligations, run_bootstrap


def _run(coro):
    return asyncio.run(coro)


def _prepared(tmp_path: Path, *, with_check: bool = True):
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="route-project",
                      session_id="owner-session")
    executable = Path(state.root) / "build/bin/hello"
    # required obligations 是 A 的一部分：必须在 run 验收快照之前就在输入里（事后改输入 = 上游变更）
    state.hook_state["node_inputs"] = {
        "fixture": "finish", "requested_work": "构建并最小运行示例程序",
        "prereg_assignment": {"kind": "none", "reason": "This operation has no governing preregistration."},
        "scope": "operation", "operation_category": "toolchain_build",
        "required_outputs": [
            {"id": "exe", "kind": "file", "path": str(executable)},
            *([{"id": "smoke", "kind": "check", "command": shlex.quote(str(executable)),
                "expect_stdout_contains": "HF_TOOLCHAIN_SMOKE_OK"}] if with_check else []),
        ],
    }
    established = asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    led = ledger.open_ledger(state, write=True)
    assert not led.read_only, led.reason
    assert obligations.frozen(state)
    return state, led


def _produce(state, led, rel: str, content: bytes, *, action_id: str = "0001-bash", exit_code: int = 0,
             work_root: str = "build") -> Path:
    """模拟一次 experiment action：走统一边界，前后身份由边界冻结。"""
    from nodes.experiment.tools import action_boundary as ab

    target = Path(state.root) / rel
    handle = ab.begin(state, tool="safe_run_bash", effect_class=ab.EXPERIMENT, command="make",
                      work_root=str(Path(state.root) / work_root), output_candidates=[target])
    assert not isinstance(handle, ab.BoundaryRefusal), handle
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    settled = ab.settle(state, handle, outcome="settled" if exit_code == 0 else "failed",
                        exit_code=exit_code)
    assert settled["status"] == "recorded", settled
    return target


@pytest.fixture
def check_ok(monkeypatch):
    async def runner(state, ob):
        return {"exit_code": 0, "stdout_tail": "HF_TOOLCHAIN_SMOKE_OK\n", "stderr_tail": "", "error": None}
    monkeypatch.setattr(finish, "_check_runner", runner)


def test_success_needs_positive_evidence_for_every_required_and_freezes_the_triplet(tmp_path, check_ok):
    state, led = _prepared(tmp_path)
    executable = _produce(state, led, "build/bin/hello", b"#!/bin/sh\necho HF_TOOLCHAIN_SMOKE_OK\n")
    out = _run(finish._finish(state, outcome="success", summary="构建并运行了 smoke",
                              deliverables=[{"path": str(executable), "obligation_id": "exe"}]))
    assert out["status"] == "success", out
    assert out["required_satisfied"] == "2/2" and out["gaps"] == [] and out["contradictions"] == []
    ids = out["artifact_ids"]
    for artifact_type in ("raw_results", "clean_results", "experiment_log"):
        record = state.read_artifact(ids[artifact_type])
        md = record["metadata"]
        assert md.get("frozen") is True, (artifact_type, md)
        assert md["delivery_id"] == out["closure_id"]
        assert md["delivery_owner"] == "finish"
        assert md["artifact_role"] == artifact_type
        assert md["schema_version"] == 1
    audit = contract_audit.audit_experiment_delivery(state)
    assert audit["passed"] is True and audit["outcome"] == "success", audit
    kinds = [r["kind"] for r in led.records()]
    assert kinds[-1] == "finish" and "action_started" in kinds  # 检查命令也进了账本


def test_a_pre_existing_file_nobody_wrote_this_run_cannot_support_success(tmp_path, check_ok):
    state, led = _prepared(tmp_path, with_check=False)
    target = Path(state.root) / "build/bin/hello"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"stale binary")  # 存在，但账本里没有任何 action 写过它
    out = _run(finish._finish(state, outcome="success", summary="done"))
    assert out["status"] == "success_unsupported"
    assert out["gaps"][0]["gap"] == "unattributed" and out["gaps"][0]["obligation"] == "exe"
    assert {o["outcome"] for o in out["reachable_outcomes"]} >= {"blocked", "cancelled"}
    assert state.list_artifacts("experiment_log", own_only=True) == []  # 没冻结任何东西
    assert not [row for row in led.records() if row.get("kind") == "finish"]


def test_open_or_unknown_jobs_block_success_but_a_superseded_failure_does_not(tmp_path, check_ok):
    state, led = _prepared(tmp_path, with_check=False)
    _produce(state, led, "build/bin/hello", b"bin")
    led.append("intent", nonce="n1", scheduler="slurm", output_root="r1")
    led.append("job_bound", nonce="n1", external_id="11")
    led.append("job_terminal", nonce="n1", derived_state="FAILED", exit_code="1:0", raw="11|FAILED|1:0")
    led.append("intent", nonce="n2", scheduler="slurm", output_root="r2", retry_of="n1")
    led.append("job_bound", nonce="n2", external_id="12")
    out = _run(finish._finish(state, outcome="success", summary="done"))
    # n2 还在跑：活作业先于 success 谓词挡住收尾；失败的 n1 要由成功重试 n2 supersede。
    assert out["status"] == "live_external_effect"
    assert out["error_code"] == "terminal_closure_with_live_or_unknown_effect"
    assert out["live_jobs"] == [{"nonce": "n2", "state": "submitted",
                                  "scheduler": "slurm", "external_id": "12"}]
    led.append("job_terminal", nonce="n2", derived_state="COMPLETED", exit_code="0:0", raw="12|COMPLETED|0:0")
    out = _run(finish._finish(state, outcome="success", summary="done"))
    assert out["status"] == "success", out
    assert out["contradictions"] == []


def test_failed_required_check_blocks_success_while_failed_action_stays_recorded(tmp_path, monkeypatch):
    state, led = _prepared(tmp_path)
    _produce(state, led, "build/bin/hello", b"bin", action_id="0001-bash", exit_code=2, work_root="build")

    async def runner(state, ob):
        return {"exit_code": 1, "stdout_tail": "boom", "stderr_tail": "", "error": None}
    monkeypatch.setattr(finish, "_check_runner", runner)
    out = _run(finish._finish(state, outcome="success", summary="done"))
    assert out["status"] == "success_unsupported"
    assert any(g["gap"] == "check_failed" for g in out["gaps"])
    assert any(row.get("kind") == "action_settled" and row.get("exit_code") == 2
               for row in led.records())
    assert any(o["outcome"] == "failed" for o in out["reachable_outcomes"])


def test_honest_non_success_outcomes_always_freeze_with_gaps_disclosed(tmp_path, check_ok):
    state, led = _prepared(tmp_path, with_check=False)
    out = _run(finish._finish(state, outcome="failed", summary="构建失败", next_step="修 CMakeLists 后重跑"))
    assert out["status"] == "success" and out["outcome"] == "failed", out
    assert out["gaps"] and out["gaps"][0]["gap"] == "missing"
    clean = json.loads(state.read_artifact(out["artifact_ids"]["clean_results"])["content"])
    assert clean["delivery"]["outcome"] == "failed"
    assert clean["delivery"]["gaps"][0]["obligation"] == "exe"
    assert clean["evaluation"]["success_supported"] is False


def test_blocked_needs_a_blocker_and_a_read_only_session_cannot_finish(tmp_path, check_ok):
    state, led = _prepared(tmp_path, with_check=False)
    out = _run(finish._finish(state, outcome="blocked", summary="集群不可达"))
    assert out["status"] == "error" and "blocker" in out["error"]
    from core.state import State
    other = State.reopen(run_id=state.run_id, node_type="experiment", base_dir=tmp_path / "runs",
                         project_id="route-project", session_id="someone-else")
    out = _run(finish._finish(other, outcome="partial", summary="x"))
    assert out["status"] == "error" and out["error_code"] == "not_run_owner"


# ── P0-D：活作业不许被任何 outcome 的收尾留成孤儿 ─────────────────────────────

def test_a_live_external_job_blocks_terminal_closure_for_every_outcome(tmp_path, check_ok):
    """"诚实的非成功总有出口"说的是 outcome 语义，不是"有活作业也可以终结"。"""
    state, led = _prepared(tmp_path, with_check=False)
    led.append("intent", nonce="n1", scheduler="slurm", output_root="r")
    led.append("job_bound", nonce="n1", external_id="55")
    for outcome in ("success", "partial", "failed", "blocked", "cancelled"):
        kwargs = {"blocker": {"summary": "x"}} if outcome == "blocked" else {}
        out = _run(finish._finish(state, outcome=outcome, summary="收个尾", **kwargs))
        assert out["status"] == "live_external_effect", (outcome, out)
        assert out["error_code"] == "terminal_closure_with_live_or_unknown_effect"
        assert out["live_jobs"][0]["state"] == "submitted"
        assert {item["action"] for item in out["exits"]} == {
            "wait_for_external_job", "cancel_job", "report_blocker",
        }
        assert out["retryable"] is True and out["owner"] == "experiment_runtime"
    assert state.list_artifacts("experiment_log", own_only=True) == []  # 一件都没冻


def test_once_the_job_reaches_a_terminal_state_the_honest_closure_goes_through(tmp_path, check_ok):
    state, led = _prepared(tmp_path, with_check=False)
    led.append("intent", nonce="n1", scheduler="slurm", output_root="r")
    led.append("job_bound", nonce="n1", external_id="55")
    led.append("job_terminal", nonce="n1", derived_state="CANCELLED", exit_code="0:15")
    out = _run(finish._finish(state, outcome="cancelled", summary="按计划停了"))
    assert out["status"] == "success" and out["outcome"] == "cancelled"
    assert out["artifact_ids"]["experiment_log"]
    delivery = out["delivery"]
    assert delivery["outcome"] == "cancelled"
    assert delivery["execution_status"] == "attempted"
    assert delivery["result_status"] == "unavailable"
    assert delivery["blocker"] is None


def test_a_run_with_no_external_job_still_closes_honestly_in_one_call(tmp_path, check_ok):
    state, led = _prepared(tmp_path, with_check=False)
    out = _run(finish._finish(state, outcome="failed", summary="构建失败", next_step="修 CMakeLists"))
    assert out["status"] == "success" and out["outcome"] == "failed"
