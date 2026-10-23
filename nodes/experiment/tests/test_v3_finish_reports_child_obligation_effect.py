"""#1086 / #1099：封存交付派生 Core 任务结局与 child obligation effect。

core.obligation_effect.record_obligation_effect 此前生产代码零调用方，executor 写进
summary 的 child_obligation_effect 恒为 None：父侧永远读不到「这一趟只是
operational_subtask_only」，纯构建 child completed 就可能被当成科学目标做完了。

走真实 bootstrap → safe_write_file → finish（模型那一侧的注册入口）→
core.executor.finalize_run，读 summary 里那一项；不打桩被测的任何一跳。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core import task_outcome as outcome_api
from core.agent_loop import LoopResult
from core.executor import finalize_run
from core.harness import NodeHarness
from core.llm import LLMClient
from core.obligation_effect import (
    CONTRIBUTION_NONE,
    EFFECT_NONE,
    EFFECT_OPERATIONAL,
    ChildObligationEffect,
)
from core.state import State
from core.task_outcome import task_outcome
from nodes.experiment.tools import delivery_transaction as tx
from nodes.experiment.tools import contract_audit, finish, ledger, run_bootstrap, safe_bash
from nodes.experiment.tools.path_roles import collect_path_roles


@pytest.fixture(autouse=True)
def _bootstrap_core():
    from core.bootstrap import bootstrap
    from core.pause import clear_all

    bootstrap()
    yield
    clear_all()


def _run_root(state: State) -> Path:
    return next(Path(r.path) for r in collect_path_roles(state)
                if r.role == "run_root" and r.writable and not r.container_only)


def _summary(state: State) -> dict:
    harness = NodeHarness(node_type="experiment", system_prompt="t", tools=[], max_turns=5,
                          required_outputs=[])
    return asyncio.run(finalize_run(state, harness, LoopResult(final_text="done", turns=1, tool_calls=[]),
                                    LLMClient(api_key="k", model="m", base_url="http://x")))


def _operation_run(tmp_path: Path) -> tuple[State, Path]:
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    target = _run_root(state) / "hello.bin"
    state.hook_state["node_inputs"] = {
        "prereg_assignment": {"kind": "none", "reason": "纯构建"},
        "scope": "operation", "operation_category": "toolchain_build",
        "experiment_spec": "构建并交付 hello.bin",
        "required_outputs": [{"id": "bin", "kind": "file", "path": str(target)}],
    }
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready"
    return state, target


def test_a_pure_build_success_tells_the_parent_it_was_only_an_operational_subtask(tmp_path):
    state, target = _operation_run(tmp_path)
    assert asyncio.run(safe_bash._safe_write_file(state, path=str(target), content="ok"))["status"] == "success"
    receipt = asyncio.run(finish._finish_for_model(
        state, outcome="success", summary="构建完成",
        deliverables=[{"path": str(target), "obligation_id": "bin"}]))
    assert receipt["status"] == "success" and receipt["outcome"] == "success", receipt

    summary = _summary(state)
    assert summary["status"] == "completed"
    assert summary["node_task_outcome"] == {
        "outcome": "success", "detail": "构建完成", "reported_by": "experiment.finish",
    }
    effect = ChildObligationEffect.from_dict(summary["child_obligation_effect"])
    assert effect is not None
    assert effect.upstream_goal_effect == EFFECT_OPERATIONAL
    assert effect.scientific_contribution == CONTRIBUTION_NONE
    assert not effect.closes_scientific_objective
    assert effect.source_receipt["closure_id"] == receipt["closure_id"]
    assert effect.source_receipt["outcome"] == "success"
    assert effect.source_receipt["scope"] == "operation"


@pytest.mark.parametrize("outcome,status", [
    ("failed", "incomplete"), ("partial", "incomplete"),
    ("blocked", "blocked"), ("cancelled", "incomplete"),
])
def test_a_nonsuccess_delivery_reports_its_task_outcome(tmp_path, outcome, status):
    state, _target = _operation_run(tmp_path)
    blocker = {"summary": "缺少外部构建依赖"} if outcome == "blocked" else None
    receipt = asyncio.run(finish._finish_for_model(
        state, outcome=outcome, summary="未交齐构建结果", blocker=blocker))
    assert receipt["status"] == "success" and receipt["outcome"] == outcome, receipt

    summary = _summary(state)
    assert summary["status"] == status
    assert summary["node_task_outcome"] == {
        "outcome": outcome, "detail": "未交齐构建结果", "reported_by": "experiment.finish",
    }
    # partial 可以交付 operational 子结果，但不等于任务成功或科学目标闭合。
    effect = summary["child_obligation_effect"]
    assert effect["upstream_goal_effect"] == (EFFECT_OPERATIONAL if outcome == "partial" else EFFECT_NONE)
    assert effect["scientific_contribution"] == CONTRIBUTION_NONE


def test_a_refused_success_records_no_effect(tmp_path):
    """没有封存就没有效应：被拒的 success 不能先替子 run 声明它关掉了什么。"""
    state, _target = _operation_run(tmp_path)
    refused = asyncio.run(finish._finish_for_model(state, outcome="success", summary="还没写出文件"))
    assert refused["status"] == "success_unsupported", refused
    summary = _summary(state)
    assert summary["child_obligation_effect"] is None
    assert summary["node_task_outcome"] is None


@pytest.mark.parametrize("outcome", ["success", "failed", "partial", "blocked", "cancelled"])
def test_a_replayed_finish_after_reopen_carries_the_sealed_effect(tmp_path, outcome):
    """effect 只挂在内存的 hook_state 上；同 run 恢复后重放 finish 读回同一份封存。"""
    state, target = _operation_run(tmp_path)
    asyncio.run(safe_bash._safe_write_file(state, path=str(target), content="ok"))
    args = dict(outcome=outcome, summary="实际交付",
                deliverables=[{"path": str(target), "obligation_id": "bin"}])
    if outcome == "blocked":
        args["blocker"] = {"summary": "缺少外部构建依赖"}
    first = asyncio.run(finish._finish_for_model(state, **args))
    assert first["status"] == "success", first
    reopened = State.reopen("experiment", tmp_path / "runs", state.run_id,
                            project_id="p", session_id="owner")
    reopened.hook_state["node_inputs"] = dict(state.hook_state["node_inputs"])
    assert task_outcome(reopened) is None
    replay = asyncio.run(finish._finish_for_model(reopened, **args))
    assert replay["closure_id"] == first["closure_id"], replay
    assert replay["revision"] == first["revision"] == 1
    summary = _summary(reopened)
    assert summary["node_task_outcome"]["outcome"] == outcome
    effect = summary["child_obligation_effect"]
    assert effect["upstream_goal_effect"] == (EFFECT_OPERATIONAL if outcome in {"success", "partial"} else EFFECT_NONE)
    assert effect["source_receipt"]["closure_id"] == first["closure_id"]
    assert len([row for row in ledger.open_ledger(reopened).records() if row["kind"] == "finish"]) == 1


def test_interrupted_finish_reports_the_recovered_claim_not_new_arguments(tmp_path, monkeypatch):
    state, _target = _operation_run(tmp_path)

    def interrupted_freeze(*_args, **_kwargs):
        raise OSError("injected before freeze")

    with monkeypatch.context() as patch:
        patch.setattr(state, "mark_frozen", interrupted_freeze)
        with pytest.raises(OSError, match="injected before freeze"):
            asyncio.run(finish._finish_for_model(state, outcome="failed", summary="实际编译失败"))
    pending = tx.read_manifest(state)
    assert pending["commit"] is None
    assert task_outcome(state) is None

    reopened = State.reopen("experiment", tmp_path / "runs", state.run_id,
                            project_id="p", session_id="owner")
    reopened.hook_state["node_inputs"] = dict(state.hook_state["node_inputs"])
    recovered = asyncio.run(finish._finish_for_model(reopened, outcome="success", summary="后来声称成功"))
    assert recovered["status"] == "success" and recovered["outcome"] == "failed", recovered
    assert recovered["closure_id"] == pending["closure_id"]
    summary = _summary(reopened)
    assert summary["status"] == "incomplete"
    assert summary["node_task_outcome"]["outcome"] == "failed"
    assert summary["node_task_outcome"]["detail"] == "实际编译失败"


def test_rejected_revision_preserves_outcome_then_real_correction_updates_it(tmp_path):
    state, target = _operation_run(tmp_path)
    first = asyncio.run(finish._finish_for_model(state, outcome="failed", summary="编译失败"))
    assert first["status"] == "success", first
    recorded = task_outcome(state)
    assert recorded is not None and recorded["outcome"] == "failed"

    refused = asyncio.run(finish._finish_for_model(state, outcome="success", summary="文件尚未产生"))
    assert refused["status"] == "success_unsupported", refused
    assert task_outcome(state) == recorded
    assert tx.read_manifest(state)["revision"] == 1

    assert asyncio.run(safe_bash._safe_write_file(state, path=str(target), content="ok"))["status"] == "success"
    corrected = asyncio.run(finish._finish_for_model(
        state, outcome="success", summary="已完成修复",
        deliverables=[{"path": str(target), "obligation_id": "bin"}]))
    assert corrected["status"] == "success" and corrected["revision"] == 2, corrected
    summary = _summary(state)
    assert summary["status"] == "completed"
    assert summary["node_task_outcome"]["outcome"] == "success"
    assert summary["node_task_outcome"]["detail"] == "已完成修复"

    revised = asyncio.run(finish._finish_for_model(state, outcome="partial", summary="纠正先前交付主张"))
    assert revised["status"] == "success" and revised["revision"] == 3, revised
    summary = _summary(state)
    assert summary["status"] == "incomplete"
    assert summary["node_task_outcome"]["outcome"] == "partial"
    rows = [row for row in ledger.open_ledger(state).records() if row["kind"] == "finish"]
    assert len(rows) == 3


def test_sealed_success_does_not_clear_an_independent_core_blocker(tmp_path):
    state, target = _operation_run(tmp_path)
    asyncio.run(safe_bash._safe_write_file(state, path=str(target), content="ok"))
    state.hook_state["blockers"] = [{"blocker_id": "independent", "summary": "框架仍有未解决问题"}]
    receipt = asyncio.run(finish._finish_for_model(
        state, outcome="success", summary="构建完成",
        deliverables=[{"path": str(target), "obligation_id": "bin"}]))
    assert receipt["status"] == "success", receipt
    summary = _summary(state)
    assert summary["node_task_outcome"]["outcome"] == "success"
    assert summary["status"] == "blocked"


def test_core_recording_failure_preserves_sealed_evidence_and_replay_repairs_projection(tmp_path, monkeypatch):
    state, _target = _operation_run(tmp_path)
    args = dict(outcome="failed", summary="实际编译失败")

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("injected Core recording failure")

    with monkeypatch.context() as patch:
        patch.setattr(outcome_api, "record_task_outcome", unavailable)
        with pytest.raises(RuntimeError, match="injected Core recording failure"):
            asyncio.run(finish._finish_for_model(state, **args))
    sealed = tx.read_manifest(state)
    assert sealed["commit"] is not None
    assert contract_audit.audit_experiment_delivery(state)["passed"] is True
    assert task_outcome(state) is None

    replay = asyncio.run(finish._finish_for_model(state, **args))
    assert replay["status"] == "success" and replay["reused_manifest"] is True, replay
    assert replay["closure_id"] == sealed["closure_id"] and replay["revision"] == 1
    assert _summary(state)["node_task_outcome"]["outcome"] == "failed"
    assert len([row for row in ledger.open_ledger(state).records() if row["kind"] == "finish"]) == 1


@pytest.mark.parametrize("scope,outcome,closes", [
    ("scientific", "success", True),
    ("scientific", "partial", False),
    ("scientific", "blocked", False),
    ("unknown", "success", False),
    ("operation", "partial", False),
])
def test_only_a_sealed_scientific_success_may_close_the_parents_objective(scope, outcome, closes):
    """派生只往保守一侧取：真 scientific 证据之外，一律不替父侧关掉科学目标。"""
    effect, contribution = finish._child_obligation_effect({"scope": scope, "outcome": outcome})
    record = ChildObligationEffect.from_dict(
        {"upstream_goal_effect": effect, "scientific_contribution": contribution})
    assert record is not None and record.closes_scientific_objective is closes
