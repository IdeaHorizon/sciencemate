"""MSYS shell operands and native Core paths must name the same write target."""
from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import safe_bash as sb
from nodes.experiment.tools import bash_paths
from nodes.experiment.tools.bash_semantics import analyze_bash
from nodes.experiment.tools.run_contract import _classify_experiment_scope
from shared.lib.shell import bash_shell, msys_runtime_bin


pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows MSYS paths")


def _msys(path: Path) -> str:
    value = path.as_posix()
    assert len(value) >= 3 and value[1:3] == ":/"
    return "/" + value[0].lower() + value[2:]


def _touch_cwds(command: str, initial: Path) -> set[str]:
    analysis = analyze_bash(command, initial_cwd=str(initial))
    assert not analysis.parse_error
    assert not analysis.analyzer_unavailable
    events = [event for event in analysis.static_path_events if event.head == "touch"]
    assert len(events) == 1
    assert not events[0].cwd.unknown
    return {os.path.normcase(value) for value in events[0].cwd.values}


def test_msys_absolute_redirect_names_native_file(tmp_path):
    target = tmp_path / "folder with spaces" / "result.txt"
    effects = sb._analyze_shell_path_effects(
        "printf ready > " + shlex.quote(_msys(target)), cwd=str(tmp_path),
    )
    assert effects == [("redirect", str(target))]


@pytest.mark.parametrize("operator", ["&&", ";", "||"])
def test_msys_cd_preserves_success_and_failure_cwd(tmp_path, operator):
    target = tmp_path / "folder with spaces"
    command = "cd " + shlex.quote(_msys(target)) + f" {operator} touch result.txt"
    expected = {os.path.normcase(str(target))} if operator == "&&" else (
        {os.path.normcase(str(tmp_path))} if operator == "||" else
        {os.path.normcase(str(tmp_path)), os.path.normcase(str(target))}
    )
    assert _touch_cwds(command, tmp_path) == expected


def test_relative_target_after_msys_cd_uses_shell_cwd(tmp_path):
    working = tmp_path / "source" / "build"
    target = tmp_path / "source" / "result.txt"
    effects = sb._analyze_shell_path_effects(
        "cd " + shlex.quote(_msys(working)) + " && printf ready > ../result.txt",
        cwd=str(tmp_path),
    )
    assert effects == [("redirect", str(target))]


def test_msys_mkdir_then_cd_keeps_one_native_write_directory(tmp_path):
    working = tmp_path / "experiments" / "repro" / "sgd"
    operand = shlex.quote(_msys(working))
    command = f"mkdir -p {operand} && cd {operand} && printf ready > result.txt"
    assert sb._analyze_shell_path_effects(command, cwd=str(tmp_path)) == [
        ("mkdir", str(working)),
        ("redirect", str(working / "result.txt")),
    ]
    assert not working.exists(), "path projection must not create the requested directory"
    shell = bash_shell()
    runtime = msys_runtime_bin(shell)
    assert runtime is not None
    result = subprocess.run(
        [shell, "--noprofile", "--norc", "-c", command], cwd=str(tmp_path),
        env={**os.environ, "PATH": runtime + os.pathsep + os.environ.get("PATH", "")},
        capture_output=True, text=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stderr
    assert (working / "result.txt").read_text(encoding="utf-8") == "ready"


@pytest.mark.parametrize("operand,expected", [
    ("D:/build/result.txt", "D:\\build\\result.txt"),
    ("//server/share/folder/result.txt", "\\\\server\\share\\folder\\result.txt"),
])
def test_native_drive_and_unc_operands_keep_their_coordinates(tmp_path, operand, expected):
    effects = sb._analyze_shell_path_effects(
        "printf ready > " + shlex.quote(operand), cwd=str(tmp_path),
    )
    assert effects == [("redirect", expected)]


def test_relative_operand_uses_explicit_cwd(tmp_path):
    effects = sb._analyze_shell_path_effects(
        "printf ready > build/result.txt", cwd=str(tmp_path),
    )
    assert effects == [("redirect", str(tmp_path / "build" / "result.txt"))]


@pytest.mark.parametrize("root", ["/usr", "/etc", "/opt"])
def test_msys_system_roots_keep_existing_protection(tmp_path, root):
    state = State.new("experiment", tmp_path / "runs")
    effects = sb._analyze_shell_path_effects(f"printf forbidden > {root}/must-not-exist")
    assert sb._classify_write_path(effects[0][1], state)[0] == "protected_global_env"
    assert sb._classify_write_path(sb._norm_path(root + "/must-not-exist"), state)[0] == (
        "protected_global_env"
    )


def test_msys_alias_does_not_grant_external_role_or_bypass_readonly(tmp_path):
    state = State.new("experiment", tmp_path / "runs")
    project = tmp_path / "project"
    workspace = project / "experiments"
    workspace.mkdir(parents=True)
    writable = workspace / "run"
    writable.mkdir()
    protected = workspace / "baseline"
    protected.mkdir()
    external = tmp_path / "user-environment"
    external.mkdir()
    state.project_worktree = project
    state.workspace_root = workspace
    state.workspace_relative_path = "experiments"
    state.hook_state["path_roles"] = {
        "run_root": str(writable),
        "source_baseline_root": str(protected),
        "dependency_root": {"path": str(external), "writable": True},
    }
    for directory, expected in (
        (writable, "safe"),
        (protected, "protected_source_tree"),
        (external, "outside_project_workspace"),
    ):
        target = directory / "result.txt"
        effects = sb._analyze_shell_path_effects(
            "printf ready > " + shlex.quote(_msys(target)), cwd=str(workspace),
        )
        assert effects == [("redirect", str(target))]
        assert sb._classify_write_path(effects[0][1], state)[0] == expected
        assert not target.exists(), "projection must never materialize a shell write"


@pytest.mark.parametrize("scope", ["protected_source_tree", "outside_project_workspace"])
def test_public_shell_does_not_execute_msys_write_without_authority(tmp_path, scope):
    state = State.new("experiment", tmp_path / "runs")
    project = tmp_path / "project"
    workspace = project / "experiments"
    writable = workspace / "run"
    protected = workspace / "baseline"
    external = tmp_path / "external-environment"
    for directory in (writable, protected, external):
        directory.mkdir(parents=True)
    state.project_worktree = project
    state.workspace_root = workspace
    state.workspace_relative_path = "experiments"
    state.hook_state["path_roles"] = {
        "run_root": str(writable),
        "source_baseline_root": str(protected),
    }
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Check the declared Windows shell write boundary.",
        "stage": "diagnostic",
    }
    classified = asyncio.run(_classify_experiment_scope(
        state, scope="operation", operation_category="other",
        reason="Validate the existing write boundary without scientific conclusions.",
    ))
    assert classified["status"] == "success", classified
    directory = protected if scope == "protected_source_tree" else external
    target = directory / "must-not-exist.txt"
    command = "printf forbidden > " + shlex.quote(_msys(target))
    result = asyncio.run(sb._safe_run_bash(state, command, cwd=str(writable), timeout=10))
    if scope == "protected_source_tree":
        assert result["status"] == "error", result
        assert result["blocker"]["scope"] == scope, result
    else:
        assert result["status"] == "pause", result
        assert str(target) in result["pause_event"]["context"], result
    assert not target.exists(), "the denied shell payload must produce no file"


def test_real_msys_write_agrees_with_analysis(tmp_path):
    working = tmp_path / "folder with spaces"
    working.mkdir()
    target = working / "result.txt"
    command = "cd " + shlex.quote(_msys(working)) + " && printf ready > result.txt"
    shell = bash_shell()
    result = subprocess.run(
        [shell, "--noprofile", "--norc", "-c", command], cwd=str(tmp_path),
        capture_output=True, text=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8") == "ready"
    assert sb._analyze_shell_path_effects(command, cwd=str(tmp_path)) == [
        ("redirect", str(target)),
    ]


@pytest.mark.parametrize("name", ["apostrophe's", "folder with spaces", "中文目录", "leading-@name"])
def test_converter_preserves_literal_filename_characters(tmp_path, name):
    target = tmp_path / name / "result.txt"
    assert bash_paths.normalize_bash_path(_msys(target)) == str(target)


def test_cross_mount_parent_uses_selected_runtime_mount_table():
    expected = Path(bash_shell()).parents[2] / "Windows"
    assert bash_paths.normalize_bash_path("/c/../Windows") == str(expected)


@pytest.mark.parametrize("operand", [
    "/tmp", "/tmp/result", "/c/../tmp/result", "/TMP/result",
    "/tmp\\result", "/c/..\\tmp/result",
])
def test_payload_dependent_tmp_mount_is_not_guessed_from_parent(operand):
    assert bash_paths.normalize_bash_path(operand) is None


def test_conversion_failure_stays_unknown_without_fabricating_native_path(tmp_path, monkeypatch):
    monkeypatch.setattr(bash_paths, "msys_runtime_bin", lambda _: str(tmp_path))
    effects = sb._analyze_shell_path_effects("printf ready > /c/folder/result.txt", cwd=str(tmp_path))
    assert effects == [("redirect", sb.UNRESOLVED)]
    assert sb._classify_write_path(effects[0][1], None)[0] == "unresolved_target"
    analysis = analyze_bash("cd /c/folder && touch result.txt", initial_cwd=str(tmp_path))
    touches = [event for event in analysis.static_path_events if event.head == "touch"]
    assert touches[0].cwd.unknown
    assert touches[0].cwd.values == ()
    assert sb._exec_target_diagnosis("/c/folder/solver", str(tmp_path))["kind"] == "exec_path_unresolved"
    for command in (
        "cd /c/folder && printf ready > result.txt",
        "cd /c/folder && touch result.txt",
    ):
        effects = sb._analyze_shell_path_effects(command, cwd=str(tmp_path))
        assert effects and all(target == sb.UNRESOLVED for _operation, target in effects)


def test_native_paths_never_spawn_and_conversion_cache_ends_with_analysis(tmp_path, monkeypatch):
    calls = []
    run = bash_paths.subprocess.run

    def observed_run(*args, **kwargs):
        calls.append((args, kwargs))
        return run(*args, **kwargs)

    monkeypatch.setattr(bash_paths.subprocess, "run", observed_run)
    with bash_paths.shell_path_resolution():
        assert bash_paths.normalize_bash_path(str(tmp_path)) == str(tmp_path)
        assert bash_paths.normalize_bash_path("relative", str(tmp_path)) == str(tmp_path / "relative")
        assert calls == []
        operand = _msys(tmp_path / "apostrophe's")
        first = bash_paths.normalize_bash_path(operand)
        assert bash_paths.normalize_bash_path(operand) == first
        assert len(calls) == 1
    with bash_paths.shell_path_resolution():
        assert bash_paths.normalize_bash_path(operand) == first
    assert len(calls) == 2
    args, kwargs = calls[0]
    assert operand not in args[0], "shell operands travel as stdin, never executable argv"
    assert kwargs["input"] == operand + "\n"
    assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert kwargs["timeout"] <= 2


def test_native_core_coordinate_does_not_use_shell_mount_translation(tmp_path, monkeypatch):
    monkeypatch.setattr(bash_paths, "_msys_absolute_path", lambda _: pytest.fail("native path was translated"))
    assert sb._norm_path("/c/native-tool-path") == os.path.abspath("/c/native-tool-path")
    with bash_paths.shell_path_resolution(remote=True):
        assert bash_paths.normalize_bash_path("/c/remote-path") == os.path.abspath("/c/remote-path")


def test_remote_system_roots_never_resolve_through_local_msys(monkeypatch):
    monkeypatch.setattr(
        bash_paths, "_msys_absolute_path", lambda _: pytest.fail("remote path was translated"),
    )
    assert sb._classify_write_path("/usr/lib/result", None, remote=True)[0] == (
        "protected_global_env"
    )
    assert sb._analyze_shell_path_effects("printf ready > /tmp/result", remote=True) == [
        ("redirect", os.path.abspath("/tmp/result")),
    ]


def test_exec_diagnostics_translate_only_explicit_local_shell(tmp_path):
    target = tmp_path / "solver"
    command = shlex.quote(_msys(target)) + " input.dat"
    local = sb.collect_exec_path_targets(command, cwd=str(tmp_path), local_shell=True)
    scheduler = sb.collect_exec_path_targets(command, cwd=str(tmp_path))
    assert local[0]["path"] == str(target)
    assert scheduler[0]["path"] == os.path.abspath(_msys(target))


@pytest.mark.parametrize("name", ["result'", " result "])
@pytest.mark.parametrize("quoting", ["double", "shell"])
def test_decoded_literal_filename_is_not_unquoted_a_second_time(tmp_path, name, quoting):
    target = tmp_path / name
    operand = shlex.quote(_msys(target)) if quoting == "shell" else '"' + _msys(target) + '"'
    effects = sb._analyze_shell_path_effects(
        "printf ready > " + operand, cwd=str(tmp_path),
    )
    assert effects == [("redirect", str(target))]


@pytest.mark.parametrize("metadata_name", [".git", ".GIT"])
@pytest.mark.parametrize("role", ["run_root", "source_worktree_root"])
def test_git_metadata_native_coordinates_stay_protected(tmp_path, metadata_name, role):
    state = State.new("experiment", tmp_path / "runs")
    project = tmp_path / "project"
    workspace = project / "experiments"
    working = workspace / "work"
    metadata = working / metadata_name
    metadata.mkdir(parents=True)
    target = metadata / "must-not-exist.txt"
    state.project_worktree = project
    state.workspace_root = workspace
    state.workspace_relative_path = "experiments"
    state.hook_state["path_roles"] = {role: str(working)}
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Check existing Git metadata protection.", "stage": "diagnostic",
    }
    classified = asyncio.run(_classify_experiment_scope(
        state, scope="operation", operation_category="other",
        reason="Validate the existing metadata boundary without modifying a real repository.",
    ))
    assert classified["status"] == "success", classified
    assert sb._classify_write_path(str(target), state)[0] == "protected_source_tree"
    command = "printf forbidden > " + shlex.quote(_msys(target))
    result = asyncio.run(sb._safe_run_bash(state, command, cwd=str(working), timeout=10))
    assert result["status"] == "error", result
    assert result["blocker"]["scope"] == "protected_source_tree", result
    assert not target.exists(), "metadata writes must be rejected before shell execution"


def test_git_metadata_casefold_applies_only_to_native_windows():
    assert sb._is_git_metadata(r"C:\work\.GIT\config")
    assert not sb._is_git_metadata("/remote/.GIT/config", remote=True)
    assert sb._is_git_metadata("/remote/.git/config", remote=True)
    assert not sb._is_git_metadata(r"C:\work\.git-copy\config")
