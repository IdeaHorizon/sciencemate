"""#1099（Experiment 侧）：v3 finish 封存的交付要派生出 child obligation effect。

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
from nodes.experiment.tools import finish, ledger, run_bootstrap, safe_bash
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

    effect = ChildObligationEffect.from_dict(_summary(state)["child_obligation_effect"])
    assert effect is not None
    assert effect.upstream_goal_effect == EFFECT_OPERATIONAL
    assert effect.scientific_contribution == CONTRIBUTION_NONE
    assert not effect.closes_scientific_objective
    assert effect.source_receipt["closure_id"] == receipt["closure_id"]
    assert effect.source_receipt["outcome"] == "success"
    assert effect.source_receipt["scope"] == "operation"


def test_a_failed_delivery_reports_that_it_closed_nothing(tmp_path):
    state, _target = _operation_run(tmp_path)
    receipt = asyncio.run(finish._finish_for_model(state, outcome="failed", summary="编译失败"))
    assert receipt["status"] == "success" and receipt["outcome"] == "failed", receipt

    effect = _summary(state)["child_obligation_effect"]
    assert effect["upstream_goal_effect"] == EFFECT_NONE
    assert effect["scientific_contribution"] == CONTRIBUTION_NONE


def test_a_refused_success_records_no_effect(tmp_path):
    """没有封存就没有效应：被拒的 success 不能先替子 run 声明它关掉了什么。"""
    state, _target = _operation_run(tmp_path)
    refused = asyncio.run(finish._finish_for_model(state, outcome="success", summary="还没写出文件"))
    assert refused["status"] == "success_unsupported", refused
    assert _summary(state)["child_obligation_effect"] is None


def test_a_replayed_finish_after_reopen_carries_the_sealed_effect(tmp_path):
    """effect 只挂在内存的 hook_state 上；同 run 恢复后重放 finish 读回同一份封存。"""
    state, target = _operation_run(tmp_path)
    asyncio.run(safe_bash._safe_write_file(state, path=str(target), content="ok"))
    args = dict(outcome="success", summary="构建完成",
                deliverables=[{"path": str(target), "obligation_id": "bin"}])
    first = asyncio.run(finish._finish_for_model(state, **args))
    reopened = State.reopen("experiment", tmp_path / "runs", state.run_id,
                            project_id="p", session_id="owner")
    reopened.hook_state["node_inputs"] = dict(state.hook_state["node_inputs"])
    replay = asyncio.run(finish._finish_for_model(reopened, **args))
    assert replay["closure_id"] == first["closure_id"], replay
    effect = _summary(reopened)["child_obligation_effect"]
    assert effect["upstream_goal_effect"] == EFFECT_OPERATIONAL
    assert effect["source_receipt"]["closure_id"] == first["closure_id"]


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
