"""只读命令或工具名写成路线步骤，永远不会有 attempt，路线到不了 complete（收敛任务书 K10、
缺陷 #1；活体 fetch_public_resource 因此耗了 794 秒）。

声明时拒绝；升级前已冻结的这类路线读取不回头判，由 ROC 在 execution_route_incomplete 里列出
这些步骤和出口：带 amendment_reason 删掉它们，并同时从其他步骤的 after 里删掉（第三会话复审
0914c 探针 k10 两格已实测这个出口只需 amendment_reason）。
"""
from __future__ import annotations

import asyncio

import pytest

from core.state import State
from nodes.experiment.tools import execution_route as er
from nodes.experiment.tools import operation_completion as oc
from nodes.experiment.tools import safe_bash as sb
from test_blocked_operation_closure import _classified_operation, _complete_operation


def _step(step_id, tool, program, effects, after=()):
    return {"id": step_id, "goal": f"{program} step", "after": list(after),
            "action": {"tool": tool, "program": program}, "effects": list(effects),
            "workdir_role": "run_root", "expected_outputs": []}


def _route(*steps):
    return {"schema_version": 2, "goal": "含只读步骤的路线",
            "evidence_refs": ["https://example.invalid/guide"], "steps": list(steps)}


_MANAGED = ["external_job", "process_tree"]


@pytest.mark.parametrize("tool, program, effects, wording", [
    ("safe_run_bash", "sha256sum", [], "是只读命令"),
    ("safe_run_bash", "test", [], "是只读命令"),
    ("safe_run_bash", "fetch_resource", [], "不走路线、不带 route_step_id"),
    ("submit_job", "fetch_resource", _MANAGED, "不走路线、不带 route_step_id"),
    ("safe_run_bash", "env", [], "program 填 env 之后真正执行的程序"),
], ids=["sha256sum", "test", "tool_name_on_bash", "tool_name_on_submit", "env"])
def test_a_step_that_can_never_bind_is_refused_at_declaration(tool, program, effects, wording):
    route = _route(_step("s", tool, program, effects))

    declared = er.validate_route_v2(route, declaring=True)

    assert not declared["valid"], declared
    assert any(wording in error for error in declared["errors"]), declared
    # 读取已冻结版本不回头判（47eecb78 的教训）。
    assert er.validate_route_v2(route)["valid"], er.validate_route_v2(route)


@pytest.mark.parametrize("tool, program, effects", [
    ("safe_run_bash", "python", ["workspace_write"]),
    ("safe_run_bash", "./sha256sum", ["workspace_write"]),
    ("safe_run_bash", "printf", ["workspace_write"]),
    ("submit_job", "solver", _MANAGED),
    ("submit_job", "sha256sum", _MANAGED),
], ids=["python", "path_entry", "printf", "solver_job", "read_only_inside_a_job"])
def test_steps_that_do_bind_still_declare(tool, program, effects):
    declared = er.validate_route_v2(_route(_step("s", tool, program, effects)), declaring=True)

    assert declared["valid"], declared


def test_a_tool_name_inside_a_program_sequence_is_refused_too():
    route = _route(_step("s", "submit_job", "solver", _MANAGED))
    route["steps"][0]["action"]["program_sequence"] = ["solver", "fetch_resource"]

    assert not er.validate_route_v2(route, declaring=True)["valid"]


def test_the_declaration_rule_follows_the_runtime_read_only_judgment():
    """声明期拒绝的只读名，执行期确实走只读快速路径（不建 attempt）；路径入口两边都不算。"""
    for program in ("sha256sum", "wc", "test"):
        assert sb._is_read_only_shell_command(f"{program} a.bin") is True, program
        assert er.unbindable_route_step_reason(
            {"tool": "safe_run_bash", "program": program}) == "read_only_command"
    assert sb._is_read_only_shell_command("./sha256sum a.bin") is False
    assert er.unbindable_route_step_reason(
        {"tool": "safe_run_bash", "program": "./sha256sum"}) is None


def _freeze_legacy_route(state, monkeypatch, route) -> dict:
    """模拟升级前冻结的路线：声明时还没有 K10 这条规则。"""
    with monkeypatch.context() as patched:
        patched.setattr(er, "unbindable_route_step_reason", lambda _action: None)
        declared = asyncio.run(er._declare_execution_route(state, route=route))
    assert declared["status"] == "success", declared
    return declared


def _complete_step(state, declared: dict, step_id: str) -> None:
    ref = declared["route_ref"]
    step = next(item for item in er.load_canonical_route(state)["route"]["steps"]
                if item["id"] == step_id)
    attempt_id = f"attempt-{ref['version']}-{step_id}"
    state.append_transcript(
        "route_step_bound", route_artifact_id=ref["artifact_id"],
        route_version=ref["version"], route_content_hash=ref["content_hash"],
        route_step_id=step_id, step_definition_hash=er.step_definition_hash(step),
        attempt_id=attempt_id, tool="safe_run_bash", resolved_workdir_role="run_root",
        applied_policy="test",
    )
    state.append_transcript(
        "route_step_outcome", attempt_id=attempt_id, outcome="success",
        managed_tool_receipt={"status": "success", "returncode": 0},
    )


@pytest.mark.parametrize("dependent", [False, True], ids=["independent", "run_after_checksum"])
def test_a_frozen_route_with_a_read_only_step_names_the_exit_and_the_exit_works(
    tmp_path, monkeypatch, dependent,
):
    state = State.new("experiment", tmp_path)
    _classified_operation(state)
    checksum = _step("checksum", "safe_run_bash", "sha256sum", [])
    run = _step("run", "safe_run_bash", "python", ["workspace_write"],
                after=["checksum"] if dependent else [])
    declared = _freeze_legacy_route(state, monkeypatch, _route(checksum, run))
    if not dependent:
        _complete_step(state, declared, "run")

    verification = oc._route_completion_verification(state)
    refused = _complete_operation(state)

    assert verification["error_code"] == "execution_route_incomplete", verification
    assert [(item["step_id"], item["reason"]) for item in verification["unbindable_steps"]] == [
        ("checksum", "read_only_command")]
    assert "after" in verification["next_actions"][0]
    assert refused["error_code"] == "execution_route_incomplete", refused
    assert refused["execution_route"]["unbindable_steps"], refused

    amended = asyncio.run(er._declare_execution_route(
        state, route=_route(dict(run, after=[])), amendment_reason="只读步骤不走路线，删掉"))
    assert amended["status"] == "success", amended
    if dependent:
        _complete_step(state, amended, "run")

    completed = _complete_operation(state)
    assert completed["status"] == "success", completed


def test_an_incomplete_route_without_unbindable_steps_keeps_its_old_response(tmp_path):
    state = State.new("experiment", tmp_path)
    _classified_operation(state)
    asyncio.run(er._declare_execution_route(
        state, route=_route(_step("run", "safe_run_bash", "python", ["workspace_write"]))))

    verification = oc._route_completion_verification(state)

    assert verification["error_code"] == "execution_route_incomplete", verification
    assert "unbindable_steps" not in verification and "next_actions" not in verification
