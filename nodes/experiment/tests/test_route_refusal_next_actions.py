"""拒绝时附结构化下一步（收敛任务书 K8，缩小范围的三处）。

1. 纯 expected_outputs 修正的判定不放宽，但拒绝时列出与上一版不同的字段（缺陷 #4：
   sgd_checkpoint 重试 5 次都没发现顺手改了 run_matrix.goal，耗 441 秒）。
2. execution_route_required 附步骤草稿：照草稿声明后，原调用带上 route_step_id 能精确绑定。
3. 缺 amendment_reason 时回显补上模板后的原调用。
不给 recovery_basis 生成草稿。
"""
from __future__ import annotations

import asyncio
import json

from nodes.experiment.tools import execution_route as er
from test_execution_route import _enforce, _route, _single_build_step_route, _state
from test_external_route_projection import _repoint_failure

_UNROUTED_PYTHON = {
    "tool": "safe_run_bash", "program": "python", "read_only": False,
    "observed_effects": ["process_tree", "workspace_write"],
    "workdir_roles": ["run_root"], "dry_run": False,
}
_UNROUTED_BUILD = {
    "tool": "submit_job", "program": "/tmp/compile", "read_only": False,
    "observed_effects": ["external_job", "process_tree", "workspace_write"],
    "workdir_roles": ["build_root"], "dry_run": False,
}


def _declare_draft(state, draft: dict) -> dict:
    arguments = json.loads(json.dumps(draft["arguments"]))
    # 048 v5：草稿里的 amendment_reason 是占位符，照抄会被 route_amendment_reason_required
    # 拒（文案一直这么承诺，现在成真）——按文案要求换成真实依据后再声明。
    if str(arguments.get("amendment_reason") or "").startswith("<"):
        arguments["amendment_reason"] = "按拒绝文案追加这一步"
    return asyncio.run(er._declare_execution_route(state, **arguments))


def _decision(state, action: dict, step_id: str) -> dict:
    return er.resolve_execution_context(state, {**action, "route_step_id": step_id})


# ── 2. execution_route_required 的步骤草稿 ───────────────────────────────────


def test_without_a_route_the_draft_is_a_single_step_route_that_binds_the_call(tmp_path):
    state = _state(tmp_path)

    blocked = _enforce(state, _UNROUTED_PYTHON)

    assert blocked["reason"] == "execution_route_required", blocked
    draft = blocked["next_action"]
    route = draft["arguments"]["route"]
    assert draft["tool"] == "declare_execution_route"
    assert "amendment_reason" not in draft["arguments"]
    assert er.validate_route_v2(route, declaring=True)["valid"]
    assert _declare_draft(state, draft)["status"] == "success"
    step_id = route["steps"][0]["id"]
    assert step_id in draft["then"]
    assert _decision(state, _UNROUTED_PYTHON, step_id)["decision"] == "matched_ready_step"


def test_with_a_frozen_route_the_draft_appends_the_step_as_an_amendment(tmp_path):
    state = _state(tmp_path)
    asyncio.run(er._declare_execution_route(state, route=_single_build_step_route()))
    existing_steps = er.load_canonical_route(state)["route"]["steps"]

    blocked = _enforce(state, _UNROUTED_BUILD)

    assert blocked["reason"] == "execution_route_required", blocked
    draft = blocked["next_action"]
    steps = draft["arguments"]["route"]["steps"]
    assert draft["arguments"]["amendment_reason"]
    assert steps[:len(existing_steps)] == existing_steps
    assert steps[-1]["id"] not in {step["id"] for step in existing_steps}
    declared = _declare_draft(state, draft)
    assert declared["status"] == "success", declared
    assert _decision(state, _UNROUTED_BUILD, steps[-1]["id"])["decision"] == (
        "matched_ready_step")


def test_an_unobserved_workdir_role_is_listed_as_something_to_fill_in(tmp_path):
    state = _state(tmp_path)

    draft = _enforce(state, {**_UNROUTED_PYTHON, "workdir_roles": []})["next_action"]

    assert "workdir_role" in draft["fill_in"], draft
    assert er.validate_route_v2(draft["arguments"]["route"], declaring=True)["valid"]


# ── 3. 缺 amendment_reason ───────────────────────────────────────────────────


def test_a_missing_amendment_reason_echoes_the_call_to_retry(tmp_path):
    state = _state(tmp_path)
    asyncio.run(er._declare_execution_route(state, route=_route()))

    refused = asyncio.run(er._declare_execution_route(state, route=_route(goal="修订目标")))

    assert refused["error_code"] == "route_amendment_reason_required", refused
    retry = refused["retry_call"]
    assert retry["tool"] == "declare_execution_route"
    assert retry["arguments"]["route"]["goal"] == "修订目标"
    arguments = dict(retry["arguments"], amendment_reason="官方文档证据表明需要调整目标")
    assert asyncio.run(er._declare_execution_route(state, **arguments))["status"] == "success"


# ── 1. 纯 expected_outputs 修正的差异字段 ──────────────────────────────────


def _correction(state, route, binding, *, step_goal=None, route_goal=None) -> dict:
    revised = json.loads(json.dumps(route))
    revised["steps"][0]["expected_outputs"] = [
        f"jobs/{binding['attempt_id']}_solver/solver.out"]
    if step_goal is not None:
        revised["steps"][0]["goal"] = step_goal
    if route_goal is not None:
        revised["goal"] = route_goal
    return asyncio.run(er._declare_execution_route(
        state, route=revised, amendment_reason="stdout 实际落在受管作业目录，改指向真实位置",
        recovery_basis={"attempt_id": binding["attempt_id"], "failure_class": "expected_output",
                        "diagnosis": "expected_outputs 路径写错，作业本身成功",
                        "evidence_refs": []}))


def test_a_correction_that_also_touched_goals_names_the_fields_and_is_not_relaxed(tmp_path):
    state, route, binding, _reference, _workdir, _job = _repoint_failure(tmp_path)
    step_id = route["steps"][0]["id"]

    refused = _correction(state, route, binding, step_goal="无意改动的步骤说明",
                          route_goal="无意改动的路线目标")

    assert refused["error_code"] == "route_recovery_basis_required", refused
    violations = "；".join(refused["violations"])
    assert f"route.steps[{step_id}].goal" in violations, refused
    assert "route.goal" in violations, refused
    assert f"route.steps[{step_id}].expected_outputs" not in violations.split("只保留")[0]
    # 判定不放宽：把两处改回原样，同一次纠正就通过。
    assert _correction(state, route, binding)["status"] == "success"



def test_a_blocked_route_gets_no_draft_it_could_not_declare(tmp_path):
    """blocked 路线的修订要 recovery_basis，草稿不生成它；给了照抄只会被拒（第三会话复审 0915 P3）。"""
    state, _route_, _binding, _reference, _workdir, _job = _repoint_failure(tmp_path)
    assert er.build_route_snapshot(state)["route_state"] == "blocked"
    decision = er.resolve_execution_context(state, dict(_UNROUTED_PYTHON))

    assert er._route_step_draft(state, dict(_UNROUTED_PYTHON), decision) is None
