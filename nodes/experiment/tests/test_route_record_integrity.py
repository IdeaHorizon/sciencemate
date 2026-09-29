"""路线记录正文缺失或被改动时，恢复门不能失效（verify 清单 #4；第三会话复审探针
review-0914-third/probe_checklist_1_2_4 的「删除」「追加换行」两格）。

原先：删掉路线文件 → load 报 route_not_declared → 声明入口按初次声明处理；追加一个换行 →
hash 不符、快照 unavailable → 恢复门被跳过。两种都能让失败的外部步骤不带 recovery_basis 重开，
submit_job 随即绑定到它（重复提交，违背不变量 2）；删文件还让 ROC 把路线当成「没声明」。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from nodes.experiment.tools import execution_route as er
from nodes.experiment.tools import operation_completion as oc
from test_external_route_projection import _repoint_failure


def _tamper(state, how: str) -> None:
    path = state.find_artifact_path(er._canonical_route_artifact_id(state))
    if how == "delete":
        path.unlink()
    else:
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")


@pytest.mark.parametrize("how", ["delete", "append_newline"])
def test_a_tampered_route_record_is_not_treated_as_a_fresh_declaration(tmp_path, how):
    state, route, _binding, _reference, _workdir, _job_dir = _repoint_failure(tmp_path)
    route_id = er._canonical_route_artifact_id(state)
    assert er.build_route_snapshot(state)["route_state"] == "blocked"
    _tamper(state, how)

    loaded = er.load_canonical_route(state)
    assert loaded["status"] == "invalid", loaded

    revised = json.loads(json.dumps(route))
    revised["steps"][0]["expected_outputs"] = ["other.out"]
    declared = asyncio.run(er._declare_execution_route(
        state, route=revised, amendment_reason="不带 basis 改输出"))

    assert declared["status"] == "error", declared
    assert declared["error_code"] == "canonical_route_record_integrity_failed"
    assert declared["do_not_resubmit"] is True
    assert any('outcome="blocked"' in step for step in declared["next_actions"])
    assert len(state.artifact_versions(route_id)) == 1   # 没有被当成新声明写下新版本
    step = route["steps"][0]
    decision = er.resolve_execution_context(state, {
        "tool": step["action"]["tool"], "program": step["action"]["program"],
        "read_only": False, "observed_effects": list(step["effects"]),
        "workdir_roles": [step["workdir_role"]]})
    assert decision.get("decision") != "matched_ready_step", decision
    # ROC 也不能把它当成「没声明路线」而跳过路线完成门。
    verification = oc._route_completion_verification(state)
    assert verification["applicable"] is True and verification["ok"] is False, verification


def test_an_intact_route_still_requires_a_recovery_basis_to_reopen_the_failed_step(tmp_path):
    state, route, _binding, _reference, _workdir, _job_dir = _repoint_failure(tmp_path)
    revised = json.loads(json.dumps(route))
    revised["steps"][0]["expected_outputs"] = ["other.out"]

    declared = asyncio.run(er._declare_execution_route(
        state, route=revised, amendment_reason="不带 basis 改输出"))

    assert declared["error_code"] == "route_recovery_basis_required", declared


def test_a_scientific_run_is_not_pointed_at_the_operation_closure_tool(tmp_path):
    """record_operation_completion 是 operation run 的收尾工具；scientific run 登记 blocker 后结束本轮
    （第三会话复审 0914c 971fd66a P3）。"""
    state, route, _binding, _reference, _workdir, _job_dir = _repoint_failure(tmp_path)
    state.hook_state["experiment_execution_scope"] = {"mode": "scientific", "category": "simulation"}
    _tamper(state, "delete")
    revised = json.loads(json.dumps(route))
    revised["steps"][0]["expected_outputs"] = ["other.out"]

    declared = asyncio.run(er._declare_execution_route(
        state, route=revised, amendment_reason="不带 basis 改输出"))

    assert declared["error_code"] == "canonical_route_record_integrity_failed", declared
    assert not any("record_operation_completion(" in step for step in declared["next_actions"])
    assert any("结束本轮" in step for step in declared["next_actions"])
