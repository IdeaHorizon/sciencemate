"""嵌套只读路径角色的墙必须真的立着（commit f9d8c7f9 的回归）。

不可变的 ``source_baseline_root`` 嵌在 ``workspace_root`` 之下是**契约合法**的：
``path_roles.validate_path_roles`` 的嵌套检查明确豁免 State 持有的 workspace_root
（它是框架的所有权边界，不是授予写入的应用角色）。2026-09-08 实测发现，这种布局
下基线在 OS 层面是**可写的** —— 记录说受保护，实际写得进去。

机制：``core.isolation._native.write_layers`` 把可写根分两层 —— 可写根自身落在某个
只读根内部时归 priority，否则归 broad；bwrap 的绑定次序是
``broad(rw) → readonly(ro) → priority(rw)``。Project 绑定的 run 里
``readonly_overrides_for`` 返回整个 project worktree，而 workspace_root 就住在里面，
于是它落进 priority，在只读之后才绑，反过来盖掉了嵌套基线的 ``--ro-bind``。

f9d8c7f9 两处修复：(1) ``bash_sandbox_roots`` 丢弃**包含**只读覆盖的 Core 宽祖先根；
(2) 不传 cwd 时默认落 run_root 而非 workspace_root —— 后者是前者的前提，因为
workspace_root 不再可写就不能再拿它给默认 cwd 兜底。该提交没有随附测试，本文件补上。

注意：本文件的布局里基线与 run_root 是**兄弟**，所以 pytest 的 ``tmp_path``（住在
/tmp，而 /tmp 属 ``scratch_roots()``）不影响判据 —— /tmp 落 broad 先绑，基线的
``--ro-bind`` 后绑覆盖它，而 priority 里的 run_root 并不包含基线。嵌套（基线在
run_root **内部**）是另一个尚未修复的缺陷，不在本文件范围。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from core.project_workspace import _NODE_WORKSPACES  # noqa: E402
from core.state import State  # noqa: E402
from nodes.experiment.tools.safe_bash import _default_run_workdir  # noqa: E402
from nodes.experiment.tools.subprocess_policy import bash_sandbox_roots  # noqa: E402


def _project_bound_run(tmp_path: Path, *, with_baseline: bool):
    """一个绑定了 project worktree 的 run；可选地在 workspace 内声明不可变基线。"""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=worktree, check=True)
    # C0（2026-09-12）起节点目录按研究语义命名：experiment 的目录是 experiments/。
    # 取框架自己的对照表，不再手写目录名。
    workspace = worktree / _NODE_WORKSPACES["experiment"]
    workspace.mkdir()
    baseline = workspace / "src" / "pkg-1.0"
    baseline.mkdir(parents=True)
    (baseline / "orig.c").write_text("original\n", encoding="utf-8")

    state = State.new("experiment", tmp_path / "runs")
    state.project_worktree = str(worktree)
    state.workspace_root = str(workspace)
    state.workspace_relative_path = _NODE_WORKSPACES["experiment"]
    if with_baseline:
        state.hook_state["node_inputs"] = {"path_roles": {
            "source_baseline_root": {"path": str(baseline), "writable": False},
        }}
    return state, workspace, baseline


def test_workspace_root_is_dropped_when_it_contains_a_protected_overlay(tmp_path):
    """宽祖先根一旦包住只读覆盖就不能再交给后端 —— 否则 priority 层会盖掉它。"""
    state, workspace, baseline = _project_bound_run(tmp_path, with_baseline=True)

    writable, readonly = bash_sandbox_roots(state, cwd=_default_run_workdir(state))

    assert workspace.resolve() not in [p.resolve() for p in writable], (
        "workspace_root contains an immutable baseline; handing it over writable "
        "lets the priority layer override the baseline's --ro-bind")
    assert any(baseline.resolve() == p.resolve() for p in readonly), (
        "the baseline must still be declared read-only")
    assert writable, (
        "narrowing must not empty the writable set — that is the "
        "'command has no authorized writable root' dead end")


def test_workspace_root_survives_when_nothing_protected_is_nested(tmp_path):
    """没有嵌套只读覆盖时不得收窄 —— 把影响面限制在真正有问题的布局上。"""
    state, workspace, _ = _project_bound_run(tmp_path, with_baseline=False)

    writable, _ = bash_sandbox_roots(state, cwd=str(workspace))

    assert any(workspace.resolve() == p.resolve() for p in writable), (
        "without a nested protected overlay the broad root is harmless and "
        "must keep its previous behaviour")


def test_default_workdir_is_the_run_root_not_the_workspace_root(tmp_path):
    """默认工作目录必须是声明的 run_root。

    这既是上面那条收窄的前提（workspace_root 不再可写就不能给默认 cwd 兜底），
    也是节点自己的规矩：skills/hpc-build/SKILL.md「P3 cwd 正确：workdir 必须是
    声明的 build_root 或 run_root」。
    """
    state, workspace, _ = _project_bound_run(tmp_path, with_baseline=True)

    workdir = Path(_default_run_workdir(state)).resolve()

    assert workdir != workspace.resolve()
    assert workdir.name == "runtime" and workspace.resolve() in workdir.parents
    writable, _ = bash_sandbox_roots(state, cwd=str(workdir))
    assert any(workdir == p.resolve() or workdir.is_relative_to(p.resolve())
               for p in writable), "default cwd must sit inside a writable root"


def test_immutable_baseline_is_unwritable_through_the_real_backend(tmp_path):
    """判据落在 OS 上，不落在返回值上 —— 返回值说受保护、实际写得进去正是本缺陷。"""
    from core import isolation, sandbox

    try:
        backend = isolation.select_backend()
    except Exception as exc:  # pragma: no cover - 取决于宿主能力
        pytest.skip(f"native isolation backend unavailable: {exc}")

    state, _workspace, baseline = _project_bound_run(tmp_path, with_baseline=True)
    workdir = _default_run_workdir(state)
    writable, readonly = bash_sandbox_roots(state, cwd=workdir)

    target = baseline / "orig.c"
    spec = isolation.CommandSpec(
        argv=("/bin/sh", "-c", f"echo TAMPERED > {target}"),
        cwd=str(workdir),
        writable_roots=tuple(writable),
        readonly_roots=tuple(readonly),
        limits=sandbox.SandboxLimits(),
        environment=None,
        network_access=False,
    )
    launch = backend.prepare(spec, state=state)
    result = subprocess.run(
        list(launch.argv), capture_output=True, text=True,
        env=getattr(launch, "env", None), cwd=getattr(launch, "cwd", None))

    assert result.returncode != 0, (
        "the write into the immutable baseline succeeded; the declared "
        "read-only boundary does not exist on the host")
    assert target.read_text(encoding="utf-8") == "original\n", (
        "the immutable baseline was modified on the host")


def test_default_workdir_command_can_still_write(tmp_path):
    """收窄不得把正常命令一起拦掉 —— 默认 cwd 必须仍然可写。"""
    from core import isolation, sandbox

    try:
        backend = isolation.select_backend()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"native isolation backend unavailable: {exc}")

    state, _workspace, _baseline = _project_bound_run(tmp_path, with_baseline=True)
    workdir = _default_run_workdir(state)
    writable, readonly = bash_sandbox_roots(state, cwd=workdir)

    spec = isolation.CommandSpec(
        argv=("/bin/sh", "-c", "echo ok > ./probe.txt && cat ./probe.txt"),
        cwd=str(workdir),
        writable_roots=tuple(writable),
        readonly_roots=tuple(readonly),
        limits=sandbox.SandboxLimits(),
        environment=None,
        network_access=False,
    )
    launch = backend.prepare(spec, state=state)
    result = subprocess.run(
        list(launch.argv), capture_output=True, text=True,
        env=getattr(launch, "env", None), cwd=getattr(launch, "cwd", None))

    assert result.returncode == 0, f"default-cwd write failed: {result.stderr[:300]}"
    assert "ok" in result.stdout
