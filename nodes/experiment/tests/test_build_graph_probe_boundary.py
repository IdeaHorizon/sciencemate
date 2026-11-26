"""build-graph 探针的环境与挂载面（S09 回归）。

``build_graph`` 跑的是 ``make -pn`` / ``cmake --graphviz`` —— 它们会**真的求值**
项目自带的 Makefile 与 CMakeLists（``$(shell ...)``、``execute_process``），也就是
外部源码。所以这条探针的子进程环境和挂载面都必须是收窄过的。

两个缺陷（2026-09-08 实测）：
1. ``_run`` 拿到 ``prepare_attempt_command`` 交出的 Launch 后自己 ``subprocess.run``，
   却不传 ``env=launch.env`` —— 子进程拿到 harness 的原始环境。实测 payload 能打印
   出 ANTHROPIC_API_KEY 的真值。
2. ``sandbox_mount_roots`` 既不校验路径角色契约，也没有 ``bash_sandbox_roots``
   那道「可写宽祖先根包住只读根就丢弃」的收窄 —— f9d8c7f9 为 bash 关掉的
   baseline-inside-workspace_root 缺口在这条产出面上仍然开着。
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from core.state import State  # noqa: E402
from nodes.experiment.tools.path_roles import (  # noqa: E402
    PathRoleContractError,
    sandbox_mount_roots,
)


def _bound_run(tmp_path: Path, *, nested_baseline: bool):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=worktree, check=True)
    workspace = worktree / "experiment"
    workspace.mkdir()
    baseline = workspace / "src" / "pkg"
    baseline.mkdir(parents=True)
    state = State.new("experiment", tmp_path / "runs")
    state.project_worktree = str(worktree)
    state.workspace_root = str(workspace)
    state.workspace_relative_path = "experiment"
    if nested_baseline:
        state.hook_state["node_inputs"] = {"path_roles": {
            "source_baseline_root": {"path": str(baseline), "writable": False}}}
    return state, workspace, baseline


def test_mount_roots_drop_a_writable_root_that_contains_a_readonly_root(tmp_path):
    state, workspace, baseline = _bound_run(tmp_path, nested_baseline=True)

    writable, readonly = sandbox_mount_roots(state)

    assert workspace.resolve() not in [p.resolve() for p in writable], (
        "a writable root containing a read-only root defeats that root's "
        "--ro-bind through the priority layer")
    assert baseline.resolve() in [p.resolve() for p in readonly]
    assert writable, "narrowing must not empty the writable set"


def test_mount_roots_keep_the_broad_root_when_nothing_is_nested(tmp_path):
    state, workspace, _ = _bound_run(tmp_path, nested_baseline=False)

    writable, _ = sandbox_mount_roots(state)

    assert workspace.resolve() in [p.resolve() for p in writable], (
        "without a nested read-only root the broad root is harmless")


def test_mount_roots_refuse_an_invalid_contract(tmp_path):
    """契约无效时角色的包含关系不可信，据此铸挂载面同样不可信。"""
    state, workspace, baseline = _bound_run(tmp_path, nested_baseline=True)
    build = baseline / "inplace-build"
    build.mkdir()
    state.hook_state["node_inputs"]["path_roles"]["build_root"] = {
        "path": str(build), "writable": True}

    with pytest.raises(PathRoleContractError):
        sandbox_mount_roots(state)


def test_build_probe_child_does_not_inherit_harness_secrets(tmp_path, monkeypatch):
    """探针会求值外部源码；它的子进程不得拿到 harness 的凭据。"""
    from core import isolation
    from core.sandbox import SandboxLimits, prepare_attempt_command

    try:
        isolation.select_backend()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"native isolation backend unavailable: {exc}")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-topsecret")
    work = tmp_path / "w"
    work.mkdir()

    class _S:
        pass

    launch = prepare_attempt_command(
        ["/bin/sh", "-c", 'echo "KEY=$ANTHROPIC_API_KEY"'],
        state=_S(), cwd=work, writable_roots=[work], readonly_roots=[],
        limits=SandboxLimits())

    # build_graph._run 现在传 env=launch.env；这里断言那个 env 本身是脱敏的。
    result = subprocess.run(
        list(launch.argv), capture_output=True, text=True,
        env=getattr(launch, "env", None))

    assert "sk-topsecret" not in result.stdout, (
        "the build probe child inherited a harness credential")


def test_build_graph_run_uses_the_managed_launchers_credential_filter(tmp_path, monkeypatch):
    """Exercise the complete probe, including the launch that actually runs."""
    from nodes.experiment.tools import build_graph
    monkeypatch.setattr(build_graph, "sandbox_mount_roots",
                        lambda state: ([tmp_path], []))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-never-reach-the-probe")
    state = State.new("experiment", tmp_path / "runs")
    code, output = asyncio.run(build_graph._run(
        'printf "KEY=%s" "$ANTHROPIC_API_KEY"', state=state, cwd=str(tmp_path)))
    assert (code, output) == (0, "KEY="), output
