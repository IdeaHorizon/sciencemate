"""Opt-in WSL kernel checks: HARNESS_TEST_WSL_DISTRO=Ubuntu pytest this file.

Only disposable fixture files are touched. No package installation, network,
workload, application restart or live project is involved.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex

import pytest

from core import execution_target, isolation, sandbox
from core.state import State
from nodes.experiment.tools import ledger, run_bootstrap, safe_bash
from shared.lib.cancellable_subprocess import spawn_and_wait


pytestmark = pytest.mark.skipif(
    os.name != "nt" or not os.getenv("HARNESS_TEST_WSL_DISTRO"),
    reason="opt-in real WSL target; set HARNESS_TEST_WSL_DISTRO on Windows",
)


@pytest.fixture
def live_target(monkeypatch, tmp_path):
    selected = execution_target.parse("wsl:" + os.environ["HARNESS_TEST_WSL_DISTRO"])
    monkeypatch.setattr(execution_target, "current", lambda: selected)
    backend = isolation.select_backend()
    assert isolation.Invariant.WRITE_BOUNDARY in backend.capabilities(), backend.unavailable_reason
    facts = backend.describe()
    print(json.dumps({"target": selected.spec, "mount_prefix": facts.get("mount_prefix"),
                      "caps": facts.get("caps"), "reason": facts.get("reason")}, ensure_ascii=False))
    project = tmp_path / "project"
    workspace = project / "experiments"
    working = workspace / "工作区 空格"
    baseline = workspace / "readonly-source"
    outside = tmp_path / "outside"
    for directory in [working, baseline, outside]:
        directory.mkdir(parents=True)
    state = State.new("experiment", tmp_path / "runs", session_id="wsl-native-regression")
    state.project_worktree = project
    state.workspace_root = workspace
    state.workspace_relative_path = "experiments"
    state.hook_state["path_roles"] = {"run_root": str(working), "source_baseline_root": str(baseline)}
    return backend, state, working, baseline, outside


def _run(state, working, baseline, command):
    result = asyncio.run(spawn_and_wait(
        command, state=state, shell=True, cwd=str(working), timeout=20,
        writable_roots=[working], readonly_roots=[baseline],
    ))
    status, rc, stdout, stderr = result
    print(json.dumps({"status": status, "returncode": rc,
                      "stdout": stdout.decode("utf-8", "replace")[-500:],
                      "stderr": stderr.decode("utf-8", "replace")[-800:]}, ensure_ascii=False))
    return result


def test_real_wsl_local_job_script_maps_workdir_logs_and_stage_in(live_target):
    from nodes.experiment.tools import resource_manager as rm

    _, state, working, baseline, _ = live_target
    source = baseline / "input.txt"
    source.write_text("guest-input", encoding="utf-8")
    script = working / "job.sh"
    script.write_text(rm._script_for(
        "local", "cat input/seed.txt > marker.txt", "guest-coordinate-fixture",
        1, 1, 0, 0.5, 1, None, None, None, str(working),
        output_dir=str(working / "logs"),
        stage_in=[{"src": str(source), "dst": "input/seed.txt", "mode": "0644"}],
    ), encoding="utf-8", newline="\n")
    status, rc, stdout, stderr = _run(
        state, working, baseline, "bash job.sh",
    )

    assert (status, rc) == ("done", 0), (stdout, stderr)
    assert (working / "marker.txt").read_text("utf-8") == "guest-input"
    assert (working / "logs").is_dir()
    assert source.read_text("utf-8") == "guest-input"


def test_real_wsl_absolute_and_relative_file_operations(live_target):
    backend, state, working, baseline, _ = live_target
    output = working / "中文 结果.txt"
    absolute = "printf absolute > " + shlex.quote(backend.to_linux(output))
    assert safe_bash._bash_path_effects_guard(state, absolute, str(working)) is None
    assert _run(state, working, baseline, absolute)[:2] == ("done", 0)
    assert output.read_text("utf-8") == "absolute"
    command = (
        "set -e\ncd " + shlex.quote(backend.to_linux(working)) + "\n"
        "printf relative >> '中文 结果.txt'\n"
        "mv '中文 结果.txt' 'renamed 结果.txt'\n"
        "cat 'renamed 结果.txt'\n"
        "rm 'renamed 结果.txt'\n"
        "mkdir '临时 空格'\nrmdir '临时 空格'\n"
        "printf '\\nWSL_FILEOPS_OK\\n'"
    )
    assert safe_bash._bash_path_effects_guard(state, command, str(working)) is None
    status, rc, stdout, _ = _run(state, working, baseline, command)
    assert (status, rc) == ("done", 0)
    assert b"absoluterelative" in stdout and b"WSL_FILEOPS_OK" in stdout
    assert list(working.iterdir()) == []


def test_real_experiment_tool_creates_build_dir_using_wsl_path(live_target):
    backend, state, working, _, _ = live_target
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Create a build directory and write a build-ready marker.",
        "prereg_assignment": {"kind": "none", "reason": "File operation fixture."},
        "scope": "operation",
    }
    bootstrap = asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", bootstrap
    build = working / "build"
    guest_build = shlex.quote(backend.to_linux(build))
    command = (
        f"mkdir -p {guest_build} && cd {guest_build} && "
        "printf build-ready > marker.txt"
    )
    try:
        result = asyncio.run(safe_bash._safe_run_bash(
            state, cmd=command, cwd=str(working), timeout=20,
        ))
        assert result["status"] == "success", result
        assert (build / "marker.txt").read_text("utf-8") == "build-ready"
    finally:
        if state.sandbox_manifest:
            sandbox.evict_state_attempt(state)


@pytest.mark.parametrize("where", ["readonly", "outside"])
def test_real_wsl_wall_denies_undeclared_and_readonly_writes(live_target, where):
    backend, state, working, baseline, outside = live_target
    target = (baseline if where == "readonly" else outside) / "must-not-exist.txt"
    command = "printf forbidden > " + shlex.quote(backend.to_linux(target))
    block = safe_bash._bash_path_effects_guard(state, command, str(working))
    assert block["status"] == ("error" if where == "readonly" else "pause")
    # Independently prove Core's actual wall. This lower-layer negative probe is
    # deliberately sent without any extra writable root and may touch only the
    # disposable target if the wall is broken.
    status, rc, _, _ = _run(state, working, baseline, command)
    assert status == "done" and rc != 0
    assert not target.exists()


@pytest.mark.parametrize("tool", ["bash", "python"])
def test_real_experiment_uses_guest_workdir_and_runtime(live_target, tool):
    backend, state, working, _, outside = live_target
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Verify a target-local interpreter and workdir.",
        "prereg_assignment": {"kind": "none", "reason": "Execution fixture."},
        "scope": "operation",
    }
    bootstrap = asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", bootstrap
    guest_cwd = backend.to_linux(working)
    marker = working / "target-marker.txt"
    try:
        if tool == "bash":
            result = asyncio.run(safe_bash._safe_run_bash(
                state, "printf bash > target-marker.txt", cwd=guest_cwd, timeout=20,
            ))
            expected = "bash"
        else:
            code = (
                "import os, sys, tempfile\n"
                "assert sys.platform == 'linux' and not sys.executable.endswith('.exe')\n"
                "assert os.path.isabs(tempfile.gettempdir())\n"
                "open('target-marker.txt', 'w').write(sys.platform)\n"
            )
            result = asyncio.run(safe_bash._safe_execute_python(
                state, code, cwd=guest_cwd, timeout=20,
            ))
            expected = "linux"
        assert result["status"] == "success", result
        assert marker.read_text("utf-8") == expected
        for forbidden in (backend.to_linux(outside), "/tmp", guest_cwd + "/missing"):
            if tool == "bash":
                rejected = asyncio.run(safe_bash._safe_run_bash(
                    state, "printf forbidden > forbidden.txt", cwd=forbidden, timeout=20,
                ))
            else:
                rejected = asyncio.run(safe_bash._safe_execute_python(
                    state, "open('forbidden.txt', 'w').write('no')", cwd=forbidden, timeout=20,
                ))
            assert rejected["reason"] == safe_bash.WORKDIR_UNAVAILABLE, rejected
        assert not (outside / "forbidden.txt").exists()
        assert not (working / "forbidden.txt").exists()
        assert not (working / "missing").exists()
    finally:
        if state.sandbox_manifest:
            sandbox.evict_state_attempt(state)


def test_real_wsl_python_with_native_cwd(live_target):
    _, state, working, _, _ = live_target
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Verify the Python runtime target independently of workdir mapping.",
        "prereg_assignment": {"kind": "none", "reason": "Execution fixture."},
        "scope": "operation",
    }
    bootstrap = asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", bootstrap
    try:
        result = asyncio.run(safe_bash._safe_execute_python(
            state, "import sys; print(sys.platform)", cwd=str(working), timeout=20,
        ))
        assert result["status"] == "success", result
        assert result["stdout_tail"].strip() == "linux"
    finally:
        if state.sandbox_manifest:
            sandbox.evict_state_attempt(state)


@pytest.mark.parametrize(("link_target", "allowed"), [
    ("../../potentials/CH.airebo", True),
    ("../../../outside", False),
    ("../../../repository/potentials/CH.airebo", False),
    ("/etc/passwd", False),
])
def test_real_wsl_git_acquisition_inventories_and_imports_links(live_target, monkeypatch, link_target, allowed):
    from nodes.experiment.tools import resource_fetch as fetch

    backend, state, working, _, outside = live_target
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Acquire a fixture checkout and verify link containment.",
        "prereg_assignment": {"kind": "none", "reason": "Acquisition fixture."},
        "scope": "operation",
    }
    bootstrap = asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", bootstrap
    fixture_commit = "a" * 40
    monkeypatch.setattr(fetch, "_git_head", lambda repo: fixture_commit)
    calls = []

    async def fixture_spawn(*argv, **kwargs):
        calls.append((argv[0], kwargs.get("network_access")))
        if argv[0] == "git" and "clone" in argv:
            # Keep the filesystem behavior real, with no external fetch needed.
            path = backend.to_linux(argv[-1])
            code = (
                "import os, pathlib, sys\n"
                "root = pathlib.Path(sys.argv[1])\n"
                "(root / 'bench/POTENTIALS').mkdir(parents=True)\n"
                "(root / 'potentials').mkdir()\n"
                "(root / 'potentials/CH.airebo').write_text('potential')\n"
                "os.symlink(sys.argv[2], root / 'bench/POTENTIALS/CH.airebo')\n"
            )
            kwargs["network_access"] = False
            return await spawn_and_wait("python3", "-I", "-B", "-c", code, path, link_target, **kwargs)
        if argv[0] == "git" and "rev-parse" in argv:
            assert not kwargs.get("network_access")
            return "done", 0, (fixture_commit + "\n").encode(), b""
        return await spawn_and_wait(*argv, **kwargs)

    monkeypatch.setattr(fetch, "spawn_and_wait", fixture_spawn)
    dest = working / "checkout"
    try:
        result = asyncio.run(fetch._fetch_resource(
            state, url="https://github.com/example/fixture", kind="git",
            destination=str(dest), expected_commit=fixture_commit, timeout_seconds=25,
        ))
        if not allowed:
            assert result["status"] == "error" and "symlink" in result["error"], result
            assert not dest.exists()
        else:
            assert result["status"] == "success", result
            status, rc, stdout, _ = _run(state, working, working, "cat checkout/bench/POTENTIALS/CH.airebo")
            assert (status, rc, stdout) == ("done", 0, b"potential")
            assert result["size_bytes"] == len(b"potential")
        assert not list(working.glob(".harness-acquire-*"))
        assert list(outside.iterdir()) == []
        assert ("python3", False) in calls
        assert ("python3", True) not in calls
    finally:
        if state.sandbox_manifest:
            sandbox.evict_state_attempt(state)
