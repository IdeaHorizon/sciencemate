from __future__ import annotations

import asyncio
from pathlib import Path

from core.loop_hooks import HookContext
from core.state import State
from nodes.experiment import hooks
from nodes.experiment.tools.execution_route import _declare_execution_route


def _state(tmp_path: Path) -> State:
    return State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="route-legacy-retirement",
    )


def _v2_route() -> dict:
    return {
        "schema_version": 2,
        "goal": "构建并验证小型程序",
        "evidence_refs": ["test:canonical-v2-route"],
        "steps": [{
            "id": "build",
            "goal": "使用已核对的构建入口",
            "after": [],
            "action": {"tool": "submit_job", "program": "make"},
            "effects": ["workspace_write", "process_tree", "external_job"],
            "workdir_role": "build_root",
            "expected_outputs": [],
        }],
    }


def test_canonical_v2_disables_legacy_route_type_and_milestone_state(
    tmp_path, monkeypatch,
):
    state = _state(tmp_path)
    declared = asyncio.run(_declare_execution_route(state, route=_v2_route()))
    assert declared["status"] == "success"

    def legacy_probe_must_not_run(_command):
        raise AssertionError("canonical v2 不应再进入旧 route_type 观察")

    monkeypatch.setattr(hooks, "_observed_route", legacy_probe_must_not_run)
    context = HookContext(
        harness=None,
        state=state,
        messages=[],
        turn=2,
        tool_call_records=[{
            "name": "safe_run_bash",
            "args": {"cmd": "make -j2"},
            "result": {"status": "success", "returncode": 0, "cmd": "make -j2"},
        }],
    )

    hooks.strategic_review_on_turn_end(context)

    assert hooks._declared_route_type(state) is None
    assert hooks._OBSERVED_ROUTE_KEY not in state.hook_state
    assert "build_state" not in state.hook_state
    assert state.hook_state[hooks._NO_PROGRESS_KEY] == 0


def test_legacy_run_keeps_observed_route_compatibility(tmp_path):
    state = _state(tmp_path)
    context = HookContext(
        harness=None,
        state=state,
        messages=[],
        turn=2,
        tool_call_records=[{
            "name": "safe_run_bash",
            "args": {"cmd": "make -j2"},
            "result": {"status": "success", "returncode": 0, "cmd": "make -j2"},
        }],
    )

    hooks.strategic_review_on_turn_end(context)

    assert state.hook_state[hooks._OBSERVED_ROUTE_KEY] == "project_native_manual"
