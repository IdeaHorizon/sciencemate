"""写入时验过的 supersession 依据必须随事实一起冻住，不得按当前 route 重算。

背景：``_active_attempt_projection_events`` 判定一条投影事件能否取代同 attempt
的旧失败事件，靠的是 ``_validated_expected_outputs_correction``。那个函数读的是
**当前** canonical route，外加 route metadata 里**单槽**的 recovery 收据 ——
换一份收据、或改掉它指名的 step，一条写入那一刻完全有效的 supersession 就会
在之后的读取里失效，两条事件同时"复活"。

节点规则：持久事实各有权威来源，派生视图必须能从权威事实重建；exact-output
correction 要"保留旧、新声明及依据"。依据不随事实冻住，派生视图就不是重建，
是重新裁决。

这里钉住的不变量（2026-09-13 起按方案 C，用户定）：
- 事件自带 ``supersession_basis_validated`` 与当时的步骤契约 hash：与该步骤无关的合法
  修订不反悔这条 supersession；
- 钉住只担保那份契约。该步骤的执行契约后来变了，丢弃这条钉住、保留它取代的失败——
  active 仍只有一条，不会两条同时复活；
- 没有契约 hash 的旧钉住，比较当时核过的输出与当前声明；没有钉住字段的旧事件走
  lineage 重算。owner=experiment；删除条件：不再需要读 2026-09-09 之前产生的 run 事件。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from core.state import State
from nodes.experiment.tools.execution_route import (
    _active_attempt_projection_events,
    _declare_execution_route,
    load_canonical_route,
    step_execution_contract_hash,
)

ATTEMPT = "attempt-1"
_IDENTITY = {
    "scheduler": "slurm", "namespace": "ns", "launch_host": "head01",
    "scheduler_cluster": "c1", "resource_uid": "uid-1", "job_id": "7001",
    "submission_nonce": ATTEMPT, "process_group_id": "pg-1",
    "process_start_ticks": "999", "container_runtime_id": "",
}


def _failure_event() -> dict[str, Any]:
    return {
        "event": "route_step_external_finalized", "attempt_id": ATTEMPT,
        "route_attempt_id": ATTEMPT, **_IDENTITY,
        "domain_outcome": "operation_completed",
        "evidence_artifact_id": "receipt-1",
        "route_outcome": "failed", "failure_class": "expected_outputs_missing",
        "missing_expected_outputs": ["phantom-output.dat"],
    }


def _superseding_event(*, pinned: bool) -> dict[str, Any]:
    event = {
        "event": "route_step_external_finalized", "attempt_id": ATTEMPT,
        "route_attempt_id": ATTEMPT, **_IDENTITY,
        "domain_outcome": "operation_completed",
        "evidence_artifact_id": "receipt-1",
        "route_outcome": "success",
        "supersedes_failure_class": "expected_outputs_missing",
    }
    if pinned:
        event["supersession_basis_validated"] = True
    return event


def _state_whose_route_no_longer_validates(tmp_path: Path) -> State:
    """真 State，没有 canonical route —— 重算必然返回 None。"""
    return State.new("experiment", tmp_path)


def _bound_event() -> dict[str, Any]:
    return {"event": "route_step_bound", "attempt_id": ATTEMPT, "route_step_id": "run"}


def _state_with_run_step(tmp_path: Path, outputs: list[str]):
    from test_external_route_projection import _route, _state  # noqa: PLC0415

    state = _state(tmp_path)
    route = _route()
    route["steps"][0]["expected_outputs"] = list(outputs)
    assert asyncio.run(_declare_execution_route(state, route=route))["status"] == "success"
    return state, route


def _amend(state: State, route: dict, mutate) -> dict:
    revised = json.loads(json.dumps(route))
    mutate(revised)
    amended = asyncio.run(_declare_execution_route(
        state, route=revised, amendment_reason="合法修订"))
    assert amended["status"] == "success", amended
    return revised


def _current_run_hash(state: State) -> str:
    return step_execution_contract_hash(load_canonical_route(state)["route"]["steps"][0])


def _active(state: State, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _active_attempt_projection_events(
        state, events, event_type="route_step_external_finalized", attempt_id=ATTEMPT)


def test_a_pinned_basis_survives_an_amendment_that_keeps_the_step_contract(tmp_path):
    state, route = _state_with_run_step(tmp_path, ["corrected.out"])
    pinned = {**_superseding_event(pinned=True),
              "supersession_step_execution_contract_hash": _current_run_hash(state)}
    events = [_bound_event(), _failure_event(), pinned]

    _amend(state, route, lambda revised: revised.__setitem__("goal", revised["goal"] + "（措辞）"))

    active = _active(state, events)
    assert [event["route_outcome"] for event in active] == ["success"]


def test_a_pinned_basis_is_dropped_when_the_step_contract_changes(tmp_path):
    state, route = _state_with_run_step(tmp_path, ["corrected.out"])
    pinned = {**_superseding_event(pinned=True),
              "supersession_step_execution_contract_hash": _current_run_hash(state)}
    events = [_bound_event(), _failure_event(), pinned]

    _amend(state, route, lambda revised: revised["steps"][0].__setitem__(
        "expected_outputs", ["other.out"]))

    # 钉住被丢弃、失败保留：只有一条 active，不是两条同时复活。
    assert [event["route_outcome"] for event in _active(state, events)] == ["failed"]


@pytest.mark.parametrize("declared, survives", [
    (["corrected.out"], True), (["other.out"], False),
])
def test_a_legacy_pin_without_a_contract_hash_compares_the_verified_outputs(
    tmp_path, declared, survives,
):
    state, _route = _state_with_run_step(tmp_path, declared)
    legacy_pin = {**_superseding_event(pinned=True), "verified_output_specs": ["corrected.out"]}
    events = [_bound_event(), _failure_event(), legacy_pin]

    outcomes = [event["route_outcome"] for event in _active(state, events)]
    assert outcomes == (["success"] if survives else ["failed"])


def test_a_pin_is_dropped_when_there_is_no_route_to_check_it_against(tmp_path):
    """读不到路线就无从对照钉住的契约：失败关闭，失败保留、不复活成两条。"""
    events = [_failure_event(), _superseding_event(pinned=True)]

    active = _active(_state_whose_route_no_longer_validates(tmp_path), events)
    assert [event["route_outcome"] for event in active] == ["failed"]


def test_a_legacy_event_without_the_pin_falls_back_to_recomputation(tmp_path):
    """兼容面的阴性对照：没有钉住依据的旧事件仍按当前 route 判，行为不变。"""
    events = [_failure_event(), _superseding_event(pinned=False)]

    active = _active_attempt_projection_events(
        _state_whose_route_no_longer_validates(tmp_path), events,
        event_type="route_step_external_finalized", attempt_id=ATTEMPT)

    # 重算返回 None → 不能取代 → 两条都保留，交调用方按 conflict 处理。
    assert len(active) == 2


# ── 跨 attempt 身份冲突：记账，不拒绝 ────────────────────────────────────────
# 受支持路径上构造不出这个前提（submission_nonce 在提交期硬绑成 attempt_id，
# 身份解析期又有一道逐字更强的墙），所以这里直接伪造一条前序事件来走这条线 ——
# 正因为它只可能由外部改写 transcript 造成，节点更不该用一个无出口的拒绝来接。

def _forged_other_attempt_event() -> dict[str, Any]:
    """同一外部 identity，挂在**另一个** attempt 名下的验证事件。"""
    return {
        "event": "route_step_external_execution_verified",
        "attempt_id": "attempt-OTHER", "route_attempt_id": "attempt-OTHER",
        **_IDENTITY,
        "route_outcome": "success", "verification_status": "success",
    }


def test_a_cross_attempt_identity_clash_is_recorded_not_refused(tmp_path):
    """账本记下"这个外部作业也被另一个 attempt 声称验证过"，然后照跑。

    原实现在这里硬拒。它一旦被触发，finalize 与 cancel 双双返回
    route_submission_ambiguous、snapshot 卡在 in_progress，全仓没有任何否定
    通道 —— 无出口死路。节点规则要求每个可达非终态至少有一个合法出口，
    并把"增加拒绝却没有恢复和终态出口"列为不得作为默认方案的症状补丁。
    """
    from test_external_route_projection import (  # noqa: PLC0415
        _events, _state, _submitted_route,
    )
    from nodes.experiment.tools.execution_route import (  # noqa: PLC0415
        _external_receipt_key, record_external_route_execution_verification,
    )

    state = _state(tmp_path)
    _binding, reference = _submitted_route(state)
    # 伪造一条挂在**另一个** attempt 名下、identity 完全相同的验证事件。
    # 只有外部改写 transcript 才做得到：提交期 submission_nonce 被硬绑成
    # attempt_id，两个不同 attempt 拿不到相同的 _external_receipt_key。
    forged = {**reference, "attempt_id": "attempt-OTHER",
              "route_attempt_id": "attempt-OTHER",
              "route_outcome": "success", "verification_status": "success"}
    state.append_transcript(
        "route_step_external_execution_verified", **forged)

    result = record_external_route_execution_verification(
        state, external_job_ref=reference, terminal=True, success_verified=True,
        success_evidence={"verified": True, "succeeded": True,
                          "source": "scheduler_accounting", "returncode": 0})

    assert _external_receipt_key(forged) == _external_receipt_key(reference)
    assert result["status"] == "success", result
    # 冲突是事实，进账本交下游按出处判定；不在这里替它做裁决。
    assert result["cross_attempt_identity_conflict"] == ["attempt-OTHER"]
    written = [
        event for event in _events(
            state, "route_step_external_execution_verified")
        if event.get("attempt_id") != "attempt-OTHER"
    ]
    assert written[-1]["cross_attempt_identity_conflict"] == ["attempt-OTHER"]
