"""路线完成后的诊断可达性与拒绝文案必须符合真实工具边界。"""
from __future__ import annotations

import asyncio

import pytest

from nodes.experiment.tests.test_route_shadow_wiring import _state
from nodes.experiment.tools import execution_route, safe_bash


_RESOLVER_ERROR_MESSAGE = (
    "路线解析器异常，{tool} 的本次调用已在目录物化和启动前拒绝；"
    "该异常路径同时阻断 safe_execute_python 与 safe_run_bash，"
    "不要切换工具或重复尝试。请调用 report_blocker，交由 framework "
    "owner 修复。"
)

_INTENT_BINDING_BASE = (
    "上游任务输入或其冻结 prereg 绑定已漂移/不可核验；本次真实动作在目录、"
    "脚本和进程产生前拒绝。"
)

_INTENT_BINDING_SUFFIX = (
    "不得把原任务替换为 proxy、简化方法或新研究问题。请恢复原输入，"
    "或由上游重派新 run。"
)


def _two_python_step_route(*, parallel: bool = False) -> dict:
    def step(step_id: str, after: list[str]) -> dict:
        return {
            "id": step_id,
            "goal": f"run Python step {step_id}",
            "after": after,
            "action": {
                "tool": "safe_execute_python",
                "program": "python",
            },
            "effects": ["workspace_write"],
            "workdir_role": "run_root",
            "expected_outputs": [],
        }

    return {
        "schema_version": 2,
        "goal": "exercise repeated Python entrypoints",
        "evidence_refs": ["user_original_input"],
        "steps": [
            step("a", []),
            step("b", [] if parallel else ["a"]),
        ],
    }


def _two_bash_step_route() -> dict:
    def step(step_id: str, after: list[str]) -> dict:
        return {
            "id": step_id,
            "goal": f"run Bash step {step_id}",
            "after": after,
            "action": {
                "tool": "safe_run_bash",
                "program": "touch",
            },
            "effects": ["workspace_write"],
            "workdir_role": "run_root",
            "expected_outputs": [],
        }

    return {
        "schema_version": 2,
        "goal": "preserve repeated Bash entrypoint matching",
        "evidence_refs": ["user_original_input"],
        "steps": [
            step("a", []),
            step("b", ["a"]),
        ],
    }


def _successful_executor(monkeypatch) -> None:
    async def fake_exec(*_args, **_kwargs):
        return {
            "status": "success",
            "returncode": 0,
            "stdout_tail": "ok\n",
            "stderr_tail": "",
        }

    monkeypatch.setattr(safe_bash, "_exec_and_log", fake_exec)


def test_completed_repeated_python_steps_do_not_poison_later_diagnostics(
    tmp_path,
    monkeypatch,
) -> None:
    state = _state(tmp_path)
    _successful_executor(monkeypatch)
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_two_python_step_route(),
    ))
    assert declared["status"] == "success", declared

    for step_id in ("a", "b"):
        completed = asyncio.run(safe_bash._safe_execute_python(
            state,
            f"print({step_id!r})",
            route_step_id=step_id,
        ))
        assert completed["status"] == "success", completed
    assert execution_route.build_route_snapshot(state)["route_state"] == "complete"

    for attempt in range(3):
        diagnostic = asyncio.run(safe_bash._safe_execute_python(
            state,
            f"print('diagnostic-{attempt}')",
        ))
        assert diagnostic["status"] == "success", diagnostic
        assert diagnostic.get("reason") != "route_attempts_exhausted"


def test_completed_repeated_bash_steps_keep_base_ambiguity(
    tmp_path,
    monkeypatch,
) -> None:
    state = _state(tmp_path)
    _successful_executor(monkeypatch)
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_two_bash_step_route(),
    ))
    assert declared["status"] == "success", declared

    for step_id in ("a", "b"):
        completed = asyncio.run(safe_bash._safe_run_bash(
            state,
            f"touch {step_id}.txt",
            route_step_id=step_id,
        ))
        assert completed["status"] == "success", completed
    assert execution_route.build_route_snapshot(state)["route_state"] == "complete"

    spawned = []

    async def forbidden(*_args, **_kwargs):
        spawned.append(True)
        raise AssertionError("completed repeated Bash steps must remain ambiguous")

    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden)
    result = asyncio.run(safe_bash._safe_run_bash(
        state,
        "touch extra.txt",
    ))

    assert result["status"] == "error", result
    assert result["reason"] == "execution_route_step_ambiguous", result
    assert result["candidate_step_ids"] == ["a", "b"]
    assert spawned == []


def test_two_ready_python_steps_remain_genuinely_ambiguous(
    tmp_path,
    monkeypatch,
) -> None:
    state = _state(tmp_path)
    _successful_executor(monkeypatch)
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_two_python_step_route(parallel=True),
    ))
    assert declared["status"] == "success", declared

    result = asyncio.run(safe_bash._safe_execute_python(
        state,
        "print('which step?')",
    ))

    assert result["status"] == "error", result
    assert result["reason"] == "execution_route_step_ambiguous", result
    assert result["candidate_step_ids"] == ["a", "b"]


def _record_interrupted_first_step(state, route_ref: dict) -> None:
    first = execution_route.load_canonical_route(state)["route"]["steps"][0]
    state.append_transcript(
        "route_step_bound",
        route_artifact_id=route_ref["artifact_id"],
        route_version=route_ref["version"],
        route_content_hash=route_ref["content_hash"],
        route_step_id=first["id"],
        step_definition_hash=execution_route.step_definition_hash(first),
        attempt_id="interrupted-a",
        tool="safe_execute_python",
        resolved_workdir_role="run_root",
        applied_policy="low_risk_effectful",
    )


@pytest.mark.parametrize("first_state", ["failed", "interrupted"])
def test_unresolved_repeated_python_steps_never_fall_through_to_spawn(
    tmp_path,
    monkeypatch,
    first_state,
) -> None:
    state = _state(tmp_path)
    declared = asyncio.run(execution_route._declare_execution_route(
        state,
        route=_two_python_step_route(),
    ))
    assert declared["status"] == "success", declared

    if first_state == "failed":
        async def fail_first(*_args, **_kwargs):
            return {
                "status": "error",
                "reason": "fixture_failure",
                "returncode": 1,
                "stdout_tail": "",
                "stderr_tail": "failed\n",
            }

        monkeypatch.setattr(safe_bash, "_exec_and_log", fail_first)
        failed = asyncio.run(safe_bash._safe_execute_python(
            state,
            "raise SystemExit(1)",
            route_step_id="a",
        ))
        assert failed["status"] == "error", failed
    else:
        _record_interrupted_first_step(state, declared["route_ref"])

    snapshot = execution_route.build_route_snapshot(state)
    assert snapshot["steps"]["a"]["state"] == first_state
    assert snapshot["steps"]["b"]["state"] == "pending"
    assert snapshot["ready_step_ids"] == []

    spawned = []

    async def forbidden(*_args, **_kwargs):
        spawned.append(True)
        raise AssertionError("ambiguous unresolved route must stop before spawn")

    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden)
    result = asyncio.run(safe_bash._safe_execute_python(
        state,
        "print('must not spawn')",
    ))

    assert result["status"] == "error", result
    assert result["reason"] == "execution_route_step_ambiguous", result
    assert result["candidate_step_ids"] == ["a", "b"]
    assert spawned == []


def test_resolver_exception_tells_both_tools_they_are_blocked(
    tmp_path,
    monkeypatch,
) -> None:
    state = _state(tmp_path)
    spawned = []

    def resolver_broken(*_args, **_kwargs):
        raise RuntimeError("resolver unavailable")

    async def forbidden(*_args, **_kwargs):
        spawned.append(True)
        raise AssertionError("resolver failure must stop before spawn")

    monkeypatch.setattr(
        execution_route,
        "resolve_execution_context",
        resolver_broken,
    )
    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden)

    python_result = asyncio.run(safe_bash._safe_execute_python(
        state,
        "print('diag')",
    ))
    bash_result = asyncio.run(safe_bash._safe_run_bash(state, "ls"))

    for tool, result in (
        ("safe_execute_python", python_result),
        ("safe_run_bash", bash_result),
    ):
        assert result["status"] == "error", (tool, result)
        assert result["reason"] == "execution_route_resolver_failed", result
        assert result["error"] == _RESOLVER_ERROR_MESSAGE.format(tool=tool)
        assert result["blocker"]["node_action"] == "report_blocker"
    assert spawned == []


@pytest.mark.parametrize(
    ("tool", "diagnostic_hint"),
    [
        (
            "safe_execute_python",
            "safe_execute_python 的本次真实动作已拒绝；如需做不改变状态的诊断，"
            "请改用 safe_run_bash 的精确只读命令。",
        ),
        (
            "safe_run_bash",
            "safe_run_bash 的本次真实动作已拒绝；只有经机械判定为只读的 "
            "safe_run_bash 诊断仍可使用。",
        ),
        (
            "submit_job",
            "submit_job 的本次真实动作已拒绝；只有经机械判定为只读的 "
            "safe_run_bash 诊断仍可使用。",
        ),
    ],
)
def test_intent_binding_error_names_the_actual_read_only_escape(
    tool,
    diagnostic_hint,
) -> None:
    block = execution_route.execution_route_block(
        {
            "decision": "route_not_required",
            "tool": tool,
            "policy": "low_risk_effectful",
            "read_only": False,
            "dry_run": False,
            "observed_effects": ["workspace_write"],
            "effective_effects": ["workspace_write"],
            "scope_required": True,
            "scope_status": "classified",
            "scope_mode": "operational",
            "intent_binding": {
                "passed": False,
                "status": "intent_changed",
            },
        },
        phase="pre_materialization",
    )

    assert block is not None
    assert block["reason"] == "experiment_execution_intent_changed"
    assert block["error"] == (
        _INTENT_BINDING_BASE + diagnostic_hint + _INTENT_BINDING_SUFFIX
    )
    assert block["blocker"]["node_action"] == (
        "restore_bound_inputs_or_start_new_run"
    )
