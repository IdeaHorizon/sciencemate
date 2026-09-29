"""P3d：flow cadence `review_curate` —— 裁决顺延给 Analysis，不是跳过。

顺延最容易退化成"跳过审查链"，所以这里把每条边界都钉死：
review 挂了不顺延、curator 没跑完不顺延、顺延之后只有 Analysis 能往下走。
"""
from __future__ import annotations

import json

import pytest

from core.artifact_provenance import forwarded, produced
from core.harness import NodeHarness
from core.loader import load_harness
from shared.tools.run_node import (
    _authorized_action_target,
    _unresolved_flow_next_step,
    _unresolved_flow_reason,
)


def _entry(**over):
    base = {
        "producing_node": "experiment",
        "producing_run_id": "run-1",
        "review_state": "done",
        "review_critique_artifact_id": "review_critique__x",
        "curator_state": "done",
        "decision_state": "deferred_to_analysis",
        "deferred_to_node": "hypothesis",
    }
    base.update(over)
    return base


def _review_provenance(state):
    return forwarded(
        produced("_reviewer", "review-run"),
        via_node_type=state.node_type,
        via_run_id=state.run_id,
    )


# ── harness 语义 ──────────────────────────────────────────────────────


def test_review_curate_still_owes_review_and_flow():
    h = NodeHarness(node_type="x", system_prompt="", post_run_flow="review_curate")
    assert h.owes_post_node_review is True     # reviewer 照跑
    assert h.owes_post_node_flow is True       # curator + 账本照走
    assert h.is_service is False
    assert h.defers_decision_to_analysis is True


def test_other_flows_do_not_defer():
    for flow in ("full", "review_only", "none"):
        h = NodeHarness(node_type="x", system_prompt="", post_run_flow=flow)
        assert h.defers_decision_to_analysis is False


def test_experiment_declares_the_new_cadence():
    assert load_harness("experiment").post_run_flow == "review_curate"


def test_illegal_flow_value_still_fails_loud():
    from core.loader import _resolve_post_run_flow

    with pytest.raises(ValueError):
        _resolve_post_run_flow({"post_run_flow": "review_curate_maybe"})


# ── 顺延 ≠ 放行 ───────────────────────────────────────────────────────


def test_deferred_entry_is_still_unresolved():
    """否则下游门禁直接放行 = 顺延退化成跳过。"""
    reason = _unresolved_flow_reason(_entry())
    assert reason and "Analysis" in reason


def test_only_analysis_is_authorized_while_deferred():
    entry = _entry()
    assert _authorized_action_target(entry) == "hypothesis"


def test_next_step_points_at_analysis():
    step = _unresolved_flow_next_step(_entry())
    assert "hypothesis" in step and "research_state" in step


def test_deferral_does_not_leak_into_other_decision_states():
    assert _authorized_action_target(_entry(decision_state="pending")) is None
    assert _authorized_action_target(
        _entry(decision_state="action_authorized", authorized_target_node="writing")
    ) == "writing"


# ── 红线：review 挂了不许顺延 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_failed_review_still_pauses_for_human(monkeypatch, tmp_path):
    """review 没有有效 critique 时，必须照旧出 REVIEW-FAILED 决策包。

    顺延的前提是"有一份可信的独立审查"；没有审查就没有可顺延的东西。
    """
    from core.state import State
    from shared.tools.library import decision_package as dp

    state = State(run_id="r", node_type="_orchestrator", root=tmp_path / "run")
    state.hook_state["pending_post_node_flow"] = [{
        "producing_node": "experiment", "producing_run_id": "run-1",
        "review_state": "done", "curator_state": "done",
        "decision_state": "pending",
    }]
    result = await dp._present_decision_package(
        state,
        source_node_type="experiment",
        producing_run_id="run-1",
        producing_summary="ran",
        artifact_ids_produced=["experiment_log__x"],
        review_failed_reason="reviewer crashed",
    )
    assert result["status"] == "pause"
    entry = state.hook_state["pending_post_node_flow"][0]
    assert entry["review_state"] == "failed_awaiting_human"
    assert entry["decision_state"] != "deferred_to_analysis"


@pytest.mark.asyncio
async def test_successful_review_defers_without_pausing(tmp_path):
    from core.state import State
    from shared.tools.library import decision_package as dp

    state = State(run_id="r", node_type="_orchestrator", root=tmp_path / "run")
    state.save_artifact("review_critique", "x", json.dumps({
        "verdict": "approve",
        "recommended_action": {"action": "proceed",
                               "feedback_to_next_run": "结果可信，继续"},
        "findings": [],
    }), {"produced_by_node_type": "_reviewer"}, provenance=_review_provenance(state))
    state.hook_state["pending_post_node_flow"] = [{
        "producing_node": "experiment", "producing_run_id": "run-1",
        "review_state": "done", "review_critique_artifact_id": "review_critique__x",
        "curator_state": "done", "decision_state": "pending",
    }]
    result = await dp._present_decision_package(
        state,
        source_node_type="experiment",
        producing_run_id="run-1",
        producing_summary="ran",
        artifact_ids_produced=["experiment_log__x"],
        review_critique_artifact_id="review_critique__x",
    )
    assert result["status"] == "success"
    assert result["deferred_to"] == "hypothesis"
    entry = state.hook_state["pending_post_node_flow"][0]
    assert entry["decision_state"] == "deferred_to_analysis"
    lines = (state.root / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    assert any("decision_deferred_to_analysis" in line for line in lines), \
        "顺延必须留痕，否则事后无从追责"


@pytest.mark.asyncio
async def test_full_flow_node_still_pauses(tmp_path):
    """hypothesis 是 full —— 它的决策照旧问人。"""
    from core.state import State
    from shared.tools.library import decision_package as dp

    state = State(run_id="r", node_type="_orchestrator", root=tmp_path / "run")
    state.save_artifact("review_critique", "y", json.dumps({
        "verdict": "approve",
        "recommended_action": {"action": "proceed",
                               "feedback_to_next_run": "预注册可用"},
        "findings": [],
    }), {"produced_by_node_type": "_reviewer"}, provenance=_review_provenance(state))
    state.hook_state["pending_post_node_flow"] = [{
        "producing_node": "hypothesis", "producing_run_id": "run-9",
        "review_state": "done", "review_critique_artifact_id": "review_critique__y",
        "curator_state": "done", "decision_state": "pending",
    }]
    result = await dp._present_decision_package(
        state,
        source_node_type="hypothesis",
        producing_run_id="run-9",
        producing_summary="ran",
        artifact_ids_produced=["pre_registration__x"],
        review_critique_artifact_id="review_critique__y",
    )
    assert result["status"] == "pause"


# ── 顺延的 flow 必须关得掉（2026-08-17 死锁回放）────────────────────────
#
# 现场：experiment run 顺延给 Analysis → 起 hypothesis → hypothesis 跑完 →
# flow 没关 → 调度器想起 writing 被拦，报错写着"已授权的 None 正在节点 None
# 执行，等它结束" → 只好重新呈递 → 又顺延 → 又起 hypothesis……11 圈、5.5 小时。
#
# 病根：闭合/失败重置读 `authorized_target_node`（只有人工授权路径写），顺延
# 路径写的是 `deferred_to_node`。同一个事实两份判据。


def _in_flight_deferred_entry(**over):
    """顺延 → 已起 hypothesis 之后，entry 在盘上的样子。"""
    return _entry(
        decision_state="action_in_progress",
        action_target_node="hypothesis",
        action_prior_state="deferred_to_analysis",
        action_attempt_count=1,
        action_started_at="2026-08-17T19:20:05+00:00",
        **over,
    )


def test_deferred_action_is_findable_for_closing():
    from shared.tools.run_node import _find_in_progress_entry

    flow = [_in_flight_deferred_entry()]
    assert _find_in_progress_entry(flow, "hypothesis") is flow[0]
    # 别的节点不该匹配上 —— 否则 writing 一跑就把 experiment 的 flow 关了
    assert _find_in_progress_entry(flow, "writing") is None


def test_legacy_entry_without_stamp_still_closes():
    """本次修复之前落盘的 entry（没有 action_target_node）也必须能被认出来。

    这就是 2026-08-17 卡死的那条：deferred_to_node='hypothesis'，
    authorized_target_node 从来没被写过。
    """
    from shared.tools.run_node import _find_in_progress_entry

    legacy = _entry(decision_state="action_in_progress", action_attempt_count=11)
    assert "action_target_node" not in legacy
    assert legacy.get("authorized_target_node") is None
    assert _find_in_progress_entry([legacy], "hypothesis") is legacy


def test_blocker_names_the_node_instead_of_none():
    """报错文案里出现 None = 调度器拿不到任何可行动信息。"""
    reason = _unresolved_flow_reason(_in_flight_deferred_entry())
    assert "None" not in reason, reason
    assert "hypothesis" in reason
    step = _unresolved_flow_next_step(_in_flight_deferred_entry())
    assert "None" not in step, step


def test_failed_action_falls_back_to_deferred_not_authorized(tmp_path):
    """顺延的那一轮失败了，要退回"顺延"，不能伪装成"人工已授权"。"""
    from core.state import State
    from shared.tools.run_node import _reset_authorized_action

    state = State(run_id="r", node_type="_orchestrator", root=tmp_path / "run")
    state.root.mkdir(parents=True, exist_ok=True)
    state.hook_state["pending_post_node_flow"] = [_in_flight_deferred_entry()]
    _reset_authorized_action(state, "hypothesis", reason="child status='failed'")
    entry = state.hook_state["pending_post_node_flow"][0]
    assert entry["decision_state"] == "deferred_to_analysis"
    # 退回之后 Analysis 可以再起；别的节点照旧拦着
    assert _authorized_action_target(entry) == "hypothesis"


def test_restart_recovery_also_restores_deferred(tmp_path):
    from core.state import State
    from shared.tools.run_node import recover_interrupted_decision_actions

    state = State(run_id="r", node_type="_orchestrator", root=tmp_path / "run")
    state.root.mkdir(parents=True, exist_ok=True)
    state.hook_state["pending_post_node_flow"] = [_in_flight_deferred_entry()]
    assert recover_interrupted_decision_actions(state) == 1
    assert state.hook_state["pending_post_node_flow"][0]["decision_state"] \
        == "deferred_to_analysis"


@pytest.mark.asyncio
async def test_represent_does_not_clobber_an_in_flight_action(tmp_path):
    """重呈递不得把"已经起了"抹回"还没起" —— 那正是 11 圈空转的引擎。"""
    from core.state import State
    from shared.tools.library import decision_package as dp

    state = State(run_id="r", node_type="_orchestrator", root=tmp_path / "run")
    state.save_artifact("review_critique", "x", json.dumps({
        "verdict": "approve",
        "recommended_action": {"action": "proceed", "feedback_to_next_run": "ok"},
        "findings": [],
    }), {"produced_by_node_type": "_reviewer"}, provenance=_review_provenance(state))
    state.hook_state["pending_post_node_flow"] = [_in_flight_deferred_entry(
        producing_run_id="run-1",
        review_critique_artifact_id="review_critique__x",
    )]
    result = await dp._present_decision_package(
        state,
        source_node_type="experiment",
        producing_run_id="run-1",
        producing_summary="ran",
        artifact_ids_produced=["experiment_log__x"],
        review_critique_artifact_id="review_critique__x",
    )
    assert result["status"] == "error"
    entry = state.hook_state["pending_post_node_flow"][0]
    assert entry["decision_state"] == "action_in_progress"
    assert entry["action_attempt_count"] == 1, "重呈递不该让它再起一次"
