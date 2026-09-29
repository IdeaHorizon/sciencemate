"""回归测试：experiment 的 Python 子进程必须加载当前 checkout。"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from core import sandbox

from core.state import State
from nodes.experiment.tools import safe_bash as sb
from nodes.experiment.tools.run_contract import _classify_experiment_scope


def _bind_environment_probe(state: State) -> None:
    """Give real public-tool calls the same v1 upstream receipt as production."""
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Exercise the isolated Python environment probe.",
        "stage": "diagnostic",
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="environment_probe",
        reason="Run the declared isolated Python environment probe without scientific claims.",
    ))
    assert classified["status"] == "success", classified


def test_repo_root_is_readonly_in_python_per_call_roots(tmp_path):
    """The checkout is visible for imports but never enters writable roots."""
    state = State.new("experiment", tmp_path / "state")
    run_root = sb.experiment_output_dir(state, "runtime", create=True)
    writable, readonly = sb._sandbox_roots_for_payload(
        state, str(run_root), sandbox_profile="python")
    repo_root = Path(__file__).resolve().parents[3]

    assert repo_root in readonly
    assert repo_root not in writable
    assert run_root.resolve() in writable
    assert Path(state.root).resolve() in readonly
    assert Path(state.root).resolve() not in writable


def test_current_checkout_imports_without_cwd(monkeypatch, tmp_path):
    """从 run workspace 启动 Python 时，nodes/core 仍来自当前 checkout。"""
    import subprocess
    import sys

    repo_root = str(Path(__file__).resolve().parents[3])
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (repo_root, env.get("PYTHONPATH")) if part
    )
    result = subprocess.run(
        [sys.executable, "-c", "import nodes, core; print(nodes.__file__); print(core.__file__)"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    lines = result.stdout.splitlines()
    assert str(Path(repo_root) / "nodes") in lines[0]
    assert str(Path(repo_root) / "core") in lines[1]


def test_parallel_safe_python_calls_keep_run_local_environment(monkeypatch, tmp_path):
    """两个调用可并发进入执行器，且只通过各自 child_env 传递运行环境。"""
    first = State.new("experiment", tmp_path / "first-state")
    second = State.new("experiment", tmp_path / "second-state")
    first_run = Path(first.root) / "declared-run"
    second_run = Path(second.root) / "declared-run"
    first_run.mkdir()
    second_run.mkdir()
    first.hook_state["path_roles"] = {
        "run_root": {"path": str(first_run), "writable": True},
    }
    second.hook_state["path_roles"] = {
        "run_root": {"path": str(second_run), "writable": True},
    }
    _bind_environment_probe(first)
    _bind_environment_probe(second)

    managed_names = {
        "EXPERIMENT_RUN_ROOT",
        "EXPERIMENT_REPRO_ROOT",
        "EXPERIMENT_REPRO_EVIDENCE",
        "EXPERIMENT_SOURCE_WORKTREE_ROOTS",
        "EXPERIMENT_SOURCE_PATCH_ROOTS",
        "TMPDIR",
        "TMP",
        "TEMP",
        "XDG_CACHE_HOME",
        "MPLCONFIGDIR",
        "PYTHONPYCACHEPREFIX",
        "PYTHONPATH",
        *sb._SAFE_PYTHON_THREAD_ENV,
    }
    for name in managed_names:
        monkeypatch.setenv(name, f"parent-sentinel-{name}")
    parent_env = dict(os.environ)
    observed: dict[str, dict[str, str]] = {}

    async def run_both():
        entered = 0
        both_entered = asyncio.Event()
        release = asyncio.Event()

        async def fake_execute(state, *_args, child_env=None, **_kwargs):
            nonlocal entered
            assert child_env is not None
            label = "first" if state is first else "second"
            observed[label] = child_env
            entered += 1
            if entered == 2:
                both_entered.set()
            await release.wait()
            return {"status": "success", "stdout_tail": "", "stderr_tail": ""}

        monkeypatch.setattr(sb, "_exec_and_log", fake_execute)
        tasks = (
            asyncio.create_task(sb._safe_execute_python(first, "pass")),
            asyncio.create_task(sb._safe_execute_python(second, "pass")),
        )
        await asyncio.wait_for(both_entered.wait(), timeout=2)
        assert all(not task.done() for task in tasks)
        assert dict(os.environ) == parent_env
        release.set()
        return await asyncio.gather(*tasks)

    results = asyncio.run(run_both())

    assert [result["status"] for result in results] == ["success", "success"]
    assert set(observed) == {"first", "second"}
    first_env = observed["first"]
    second_env = observed["second"]
    assert first_env is not second_env
    assert first_env["EXPERIMENT_RUN_ROOT"] == str(first_run)
    assert second_env["EXPERIMENT_RUN_ROOT"] == str(second_run)

    first_scratch = (
        Path(sb.experiment_output_dir(first, "runtime", create=False))
        / ".python-scratch"
    )
    second_scratch = (
        Path(sb.experiment_output_dir(second, "runtime", create=False))
        / ".python-scratch"
    )
    for child_env, scratch in (
        (first_env, first_scratch),
        (second_env, second_scratch),
    ):
        assert child_env["TMPDIR"] == str(scratch / "tmp")
        assert child_env["TMP"] == str(scratch / "tmp")
        assert child_env["TEMP"] == str(scratch / "tmp")
        assert child_env["XDG_CACHE_HOME"] == str(scratch / "cache")
        assert child_env["MPLCONFIGDIR"] == str(scratch / "cache" / "matplotlib")
        assert child_env["PYTHONPYCACHEPREFIX"] == str(scratch / "cache" / "pycache")
        assert {
            child_env[name] for name in sb._SAFE_PYTHON_THREAD_ENV
        } == {"4"}
        assert child_env["PYTHONPATH"].split(os.pathsep)[0] == str(
            Path(__file__).resolve().parents[3]
        )

    assert first_env["TMPDIR"] != second_env["TMPDIR"]
    assert first_env["XDG_CACHE_HOME"] != second_env["XDG_CACHE_HOME"]
    assert dict(os.environ) == parent_env


def test_safe_python_container_imports_checkout_without_parent_secret(
    monkeypatch, tmp_path,
):
    available, reason = sandbox.availability()
    if not available:
        pytest.skip(f"mandatory Docker sandbox is unavailable: {reason}")

    state = State.new("experiment", tmp_path / "production-state")
    _bind_environment_probe(state)
    monkeypatch.setenv("SECRET_SENTINEL", "must-not-enter-container")
    code = (
        "import os, nodes, core\n"
        "print(nodes.__file__)\n"
        "print(core.__file__)\n"
        "print('secret=' + str(os.environ.get('SECRET_SENTINEL')))\n"
    )
    try:
        result = asyncio.run(sb._safe_execute_python(state, code, timeout=30))
        assert result["status"] == "success", result
        output = result["stdout_tail"]
        repo_root = Path(__file__).resolve().parents[3]
        assert str(repo_root / "nodes") in output
        assert str(repo_root / "core") in output
        assert "secret=None" in output
    finally:
        if isinstance(getattr(state, "sandbox_manifest", None), dict):
            sandbox.evict_state_attempt(state)
