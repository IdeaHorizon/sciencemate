"""path_role_convention hook 测试（保留旧 Python 回调名兼容）。

约定（2026-07-10 meiyu laptop pilot 审计引入）：
  ① fixture/prereg 显式 workdir 最优先（hook 不覆盖，只注入默认值供未声明场景）
  ② 绑定 Project 时默认 <worktree>/experiment/；未绑定时自然回退
     <state.root>/outputs/experiment/（run-local）
  ③ 跨 run workspace 只能通过规范路径角色显式声明
hook 行为：turn-1 注入 experiment_root（仅容器）以及框架已有的
run_root / managed_source_root / build_root（可写，目录仍按需创建）。
"""
from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path

from core.loop_hooks import HookContext
from core.state import State

_NODE_DIR = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "experiment_hooks_workdir_test", _NODE_DIR / "hooks.py")
_hooks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_hooks)


def _ctx(project_id: str | None = None) -> HookContext:
    if project_id:
        home = Path(os.environ["HARNESS_FRAMEWORK_HOME"])
        base = home / "projects" / project_id / "runs"
        base.mkdir(parents=True, exist_ok=True)
    else:
        base = Path(tempfile.mkdtemp(prefix="hf-wdconv-"))
    state = State.new(node_type="experiment", base_dir=base, project_id=project_id)
    return HookContext(harness=None, state=state, messages=[], turn=1)


def _expected_base(ctx: HookContext) -> Path:
    """节点输出目录的期望值只问真相源，不在测试里抄一份路径拼装。

    抄一份的代价实测过：产物落点在 core.paths 改了，测试仍断言旧布局，于是
    "全绿"和"线上产物在哪"脱钩。
    """
    from nodes.experiment.tools.path_roles import experiment_output_dir

    return experiment_output_dir(ctx.state)


def _role_paths(ctx: HookContext, role_name: str) -> set[str]:
    from nodes.experiment.tools.path_roles import collect_path_roles

    return {
        role.path for role in collect_path_roles(ctx.state)
        if role.role == role_name
    }


def test_project_run_injects_run_local_experiment_outputs():
    ctx = _ctx(project_id="proj-wd")
    msgs = _hooks._workdir_convention_on_turn_start(ctx)
    assert msgs, "turn-1 必须注入 workdir 约定消息"
    ws = str(_expected_base(ctx))
    assert ws in msgs[0].content
    assert "run-local" in msgs[0].content
    assert Path(ws).is_dir(), "hook 应自动创建节点输出目录"
    roles = ctx.state.hook_state["path_roles"]
    assert roles["experiment_root"]["path"] == ws
    assert roles["experiment_root"]["container_only"] is True
    assert roles["run_root"]["path"] == str(Path(ws) / "runtime")


def test_no_project_falls_back_to_state_root():
    ctx = _ctx(project_id=None)
    msgs = _hooks._workdir_convention_on_turn_start(ctx)
    assert msgs
    ws = str(Path(ctx.state.root) / "outputs" / "experiment")
    assert ws in msgs[0].content
    assert "run-local" in msgs[0].content
    assert _role_paths(ctx, "managed_source_root") == {
        str(Path(ws) / "runtime" / "source")
    }
    assert _role_paths(ctx, "build_root") == {str(Path(ws) / "build")}
    roles = ctx.state.hook_state["path_roles"]
    assert "managed_source_root" not in roles
    assert "build_root" not in roles
    assert str(Path(ws) / "runtime" / "source") in msgs[0].content
    assert str(Path(ws) / "build") in msgs[0].content


def test_second_turn_idempotent_but_roles_persist():
    ctx = _ctx(project_id="proj-wd2")
    assert _hooks._workdir_convention_on_turn_start(ctx)
    # 第二轮：不再注入消息，规范角色保持不变。
    assert _hooks._workdir_convention_on_turn_start(ctx) is None
    ws = str(_expected_base(ctx))
    assert ctx.state.hook_state["path_roles"]["experiment_root"]["path"] == ws


def test_explicit_workdir_from_node_inputs_wins():
    """legacy workdir 只覆盖 experiment_root，不隐藏框架隔离分配。"""
    ctx = _ctx(project_id="proj-wd-explicit")
    explicit = tempfile.mkdtemp(prefix="hf-wdconv-explicit-")
    ctx.state.hook_state["node_inputs"] = {"workdir": explicit}
    msgs = _hooks._workdir_convention_on_turn_start(ctx)
    assert msgs and explicit in msgs[0].content
    assert "显式声明" in msgs[0].content
    default_ws = str(_expected_base(ctx))
    assert default_ws in msgs[0].content
    roles = ctx.state.hook_state["path_roles"]
    assert roles["experiment_root"]["path"] == explicit
    assert roles["run_root"]["path"] == str(Path(explicit) / "runtime")


def test_default_run_declares_source_build_and_runtime_roots():
    """默认 run 公开源码、构建、安装与运行目录，但不复制权威角色。"""
    ctx = _ctx(project_id="proj-wd-default-layout")

    messages = _hooks._workdir_convention_on_turn_start(ctx)

    base = _expected_base(ctx)
    assert _role_paths(ctx, "managed_source_root") == {
        str(base / "runtime" / "source")
    }
    assert _role_paths(ctx, "build_root") == {str(base / "build")}
    assert _role_paths(ctx, "run_root") == {str(base / "runtime")}
    assert str(base / "runtime" / "source") in messages[0].content
    assert str(base / "build") in messages[0].content
    assert str(base / "build" / "install") in messages[0].content


def test_natural_goal_gets_run_local_source_and_build_roots_without_hint():
    """自然任务不需要上游先猜 requires_build_root 或 stage。"""
    ctx = _ctx(project_id="proj-wd-source-build")
    ctx.state.hook_state["node_inputs"] = {
        "experiment_focus": "Download and build an unknown application.",
    }

    msgs = _hooks._workdir_convention_on_turn_start(ctx)

    base = _expected_base(ctx)
    runtime = base / "runtime"
    build = base / "build"
    assert _role_paths(ctx, "managed_source_root") == {str(runtime / "source")}
    assert _role_paths(ctx, "build_root") == {str(build)}
    roles = ctx.state.hook_state["path_roles"]
    assert "managed_source_root" not in roles
    assert "build_root" not in roles
    assert str(runtime / "source") in msgs[0].content
    assert str(build) in msgs[0].content
    assert not (runtime / "source").exists()
    assert not build.exists()
    assert "不得探索、复用或清理 project workspace" in msgs[0].content


def test_multiple_build_roots_are_all_shown_and_require_disambiguation():
    ctx = _ctx(project_id="proj-wd-ambiguous-build")
    explicit = Path(tempfile.mkdtemp(prefix="hf-wdconv-build-"))
    ctx.state.hook_state["node_inputs"] = {
        "path_roles": {
            "build_root": {"path": str(explicit), "writable": True},
        },
    }

    msgs = _hooks._workdir_convention_on_turn_start(ctx)

    default_build = _expected_base(ctx) / "build"
    assert str(explicit) in msgs[0].content
    assert str(default_build) in msgs[0].content
    assert "执行时必须显式消歧" in msgs[0].content


def test_requires_build_root_uses_same_build_path_as_runtime_contract():
    """旧提示可保留兼容，但不能改变本来就存在的 framework 默认值。"""
    ctx = _ctx(project_id="proj-wd-build-root")
    ctx.state.hook_state["node_inputs"] = {"requires_build_root": True}

    _hooks._workdir_convention_on_turn_start(ctx)

    expected = _expected_base(ctx) / "build"
    assert _role_paths(ctx, "build_root") == {str(expected)}
    assert "build_root" not in ctx.state.hook_state["path_roles"]


def test_transcript_event_written():
    import json
    ctx = _ctx(project_id="proj-wd3")
    _hooks._workdir_convention_on_turn_start(ctx)
    tr = Path(ctx.state.root) / "transcript.jsonl"
    events = [json.loads(line) for line in tr.read_text().splitlines()]
    events = [e for e in events if e.get("event") == "path_role_convention_injected"]
    assert len(events) == 1
    assert events[0]["project_level"] is False



def test_default_stage_workdirs_use_the_project_experiment_workspace(tmp_path):
    from nodes.experiment.tools.path_roles import default_stage_workdir

    state = State.new("experiment", tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    state.project_worktree = worktree

    assert default_stage_workdir(state, "diagnostic", create=True) == worktree / "experiments"
    assert default_stage_workdir(state, "toolchain_build", create=True) == worktree / "experiments" / "build"
    assert default_stage_workdir(state, "simulation", create=True) == worktree / "experiments" / "runtime"



def test_default_stage_workdirs_are_shared_but_build_env_control_planes_are_run_local(tmp_path):
    from nodes.experiment.tools.env_provision import env_path_for_state
    from nodes.experiment.tools.path_roles import default_stage_workdir

    runs = tmp_path / "runs"
    runs.mkdir()
    first = State.new("experiment", runs)
    second = State.new("experiment", runs)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    first.project_worktree = worktree
    second.project_worktree = worktree

    for stage, expected in (("diagnostic", worktree / "experiments"),
                            ("toolchain_build", worktree / "experiments" / "build"),
                            ("simulation", worktree / "experiments" / "runtime")):
        assert default_stage_workdir(first, stage, create=True) == expected
        assert default_stage_workdir(second, stage, create=True) == expected
    assert first.root != second.root
    assert env_path_for_state(first) != env_path_for_state(second)
    assert str(first.root) in str(env_path_for_state(first))
    assert str(second.root) in str(env_path_for_state(second))
