"""A WSL shell must never borrow the host Git Bash mount table."""
from __future__ import annotations

import os
import shlex

import pytest

from core import execution_target, isolation
from core.isolation import wsl
from core.state import State
from nodes.experiment.tools import bash_paths, safe_bash


pytestmark = pytest.mark.skipif(os.name != "nt", reason="WSL requires a Windows host")


@pytest.fixture
def wsl_paths(monkeypatch):
    backend = wsl.WslBackend("Ubuntu")
    # Exercise Core's real mapping, without starting a distro for unit tests.
    backend._mount_prefix = "/mnt/"
    monkeypatch.setattr(execution_target, "current", lambda: execution_target.parse("wsl:Ubuntu"))
    monkeypatch.setattr(wsl, "WslBackend", lambda distro: backend)
    monkeypatch.setattr(isolation, "_target_backend", lambda: backend)
    def wrong_runtime(_path):
        pytest.fail("A WSL operand reached the host MSYS converter")
    monkeypatch.setattr(bash_paths, "_msys_absolute_path", wrong_runtime)
    return backend


def _linux(backend, path):
    return backend.to_linux(path)


def test_mounted_absolute_and_relative_redirect_agree(tmp_path, wsl_paths):
    target = tmp_path / "工作区 空格" / "out.txt"
    absolute = "printf ready > " + shlex.quote(_linux(wsl_paths, target))
    relative = "printf ready > '工作区 空格/out.txt'"
    assert safe_bash._analyze_shell_path_effects(absolute, str(tmp_path)) == [
        ("redirect", str(target)),
    ]
    assert safe_bash._analyze_shell_path_effects(relative, str(tmp_path)) == [
        ("redirect", str(target)),
    ]
    assert not target.exists(), "projection must not execute or create output"


def test_cd_then_relative_redirect_keeps_wsl_coordinates(tmp_path, wsl_paths):
    directory = tmp_path / "工作区 空格" / "build"
    command = "cd " + shlex.quote(_linux(wsl_paths, directory)) + " && printf ready > ../out.txt"
    assert safe_bash._analyze_shell_path_effects(command, str(tmp_path)) == [
        ("redirect", str(directory.parent / "out.txt")),
    ]


def test_relative_operand_can_cross_wsl_drive_mounts(wsl_paths):
    command = "cd /mnt/c && touch ../d/out.txt"
    assert safe_bash._analyze_shell_path_effects(command, "C:\\") == [
        ("touch", "D:\\out.txt"),
    ]
    assert safe_bash._analyze_shell_path_effects(
        "touch ../d/out.txt", "C:\\",
    ) == [("touch", "D:\\out.txt")]


def test_cd_through_guest_mount_parent_keeps_relative_path_resolvable(wsl_paths):
    command = "cd /mnt && cd c && mkdir build && printf ready > build/out.txt"
    assert safe_bash._analyze_shell_path_effects(command, "C:\\") == [
        ("mkdir", "C:\\build"),
        ("redirect", "C:\\build\\out.txt"),
    ]


def test_build_without_output_selector_uses_guest_cwd(wsl_paths):
    assert safe_bash._analyze_shell_path_effects(
        "cd /mnt && cd c && make", "D:\\",
    ) == [("make", "C:\\")]


def test_relative_write_from_guest_only_directory_is_unknown(wsl_paths):
    assert safe_bash._analyze_shell_path_effects(
        "cd /tmp && touch output.txt", "C:\\",
    ) == [("touch", safe_bash.UNRESOLVED)]


def test_windows_shell_syntax_and_host_variables_are_not_interpreted_in_wsl(
    tmp_path, monkeypatch, wsl_paths,
):
    monkeypatch.setenv("EXPERIMENT_WSL_PATH_FIXTURE", "wrong-host-name")
    command = r"""touch 'C:\other\out.txt' '%EXPERIMENT_WSL_PATH_FIXTURE%'"""
    assert safe_bash._analyze_shell_path_effects(command, str(tmp_path)) == [
        ("touch", safe_bash.UNRESOLVED),
    ]


def test_core_measured_nondefault_mount_prefix_is_used(tmp_path, wsl_paths):
    wsl_paths._mount_prefix = "/host-drives/"
    target = tmp_path / "result.txt"
    command = "touch " + shlex.quote(_linux(wsl_paths, target))
    assert safe_bash._analyze_shell_path_effects(command, str(tmp_path)) == [
        ("touch", str(target)),
    ]


@pytest.mark.parametrize("path", [
    "/home/lujy/miniconda3/pkgs/.wtest", "/home/lujy/.cache/item",
    "/tmp/item", "/etc/item", "/usr/item", "/c/Users/item",
])
def test_guest_only_or_non_wsl_mount_never_becomes_host_path(wsl_paths, path):
    assert bash_paths.normalize_bash_path(path) is None
    effects = safe_bash._analyze_shell_path_effects("touch " + shlex.quote(path))
    assert effects == [("touch", safe_bash.UNRESOLVED)]


def test_dotdot_is_normalized_in_linux_before_drive_mapping(wsl_paths):
    # Linux crosses from the c mount to the d mount here; Windows C:\.. does not.
    assert bash_paths.normalize_bash_path("/mnt/c/../d/out.txt") == "D:\\out.txt"


def test_multiple_leading_slashes_are_linux_paths_not_host_unc(wsl_paths):
    assert bash_paths.normalize_bash_path("//mnt/c/out.txt") == "C:\\out.txt"
    assert bash_paths.normalize_bash_path("///home/lujy/out.txt") is None


def test_nested_analysis_reuses_only_the_current_target_observation(monkeypatch, wsl_paths):
    calls = []
    actual = wsl_paths.to_windows
    def observed(path):
        calls.append(path)
        return actual(path)
    monkeypatch.setattr(wsl_paths, "to_windows", observed)
    with bash_paths.shell_path_resolution():
        assert bash_paths.normalize_bash_path("/mnt/c/out.txt") == "C:\\out.txt"
        with bash_paths.shell_path_resolution():
            assert bash_paths.normalize_bash_path("/mnt/c/out.txt") == "C:\\out.txt"
    assert calls == ["/mnt/c/out.txt"]
    assert bash_paths.normalize_bash_path("/mnt/c/out.txt") == "C:\\out.txt"
    assert len(calls) == 2


def test_mapping_failure_does_not_fall_back_to_msys(monkeypatch, wsl_paths):
    def unavailable(_path):
        raise isolation.IsolationContractError("fixture mount observation unavailable")
    monkeypatch.setattr(wsl_paths, "to_windows", unavailable)
    assert bash_paths.normalize_bash_path("/mnt/c/out.txt") is None


def test_unknown_wsl_target_does_not_recommend_fixing_host_cygpath(tmp_path, wsl_paths):
    state = State.new("experiment", tmp_path / "runs")
    block = safe_bash._bash_path_effects_guard(
        state, "touch /home/lujy/miniconda3/pkgs/.wtest", str(state.root),
    )
    assert block["pause_event"]["metadata"]["target"] == safe_bash.UNRESOLVED
    context = block["pause_event"]["context"]
    assert "WSL" in context and "实际写入能力" in context
    assert "cygpath.exe" not in context
    assert "D:\\ScienceMate\\Resources\\git\\home" not in context


def test_readonly_source_is_still_rejected_and_outside_target_still_asks(tmp_path, wsl_paths):
    state = State.new("experiment", tmp_path / "runs")
    project = tmp_path / "project"
    workspace = project / "experiments"
    working = workspace / "run"
    baseline = workspace / "baseline"
    outside = tmp_path / "external"
    for directory in [working, baseline, outside]:
        directory.mkdir(parents=True)
    state.project_worktree = project
    state.workspace_root = workspace
    state.workspace_relative_path = "experiments"
    state.hook_state["path_roles"] = {
        "run_root": str(working), "source_baseline_root": str(baseline),
    }
    target = baseline / "must-not-exist.txt"
    block = safe_bash._bash_path_effects_guard(
        state, "printf denied > " + shlex.quote(_linux(wsl_paths, target)), str(working),
    )
    assert block["status"] == "error"
    assert block["blocker"]["scope"] == "protected_source_tree"
    target = outside / "must-not-exist.txt"
    block = safe_bash._bash_path_effects_guard(
        state, "printf denied > " + shlex.quote(_linux(wsl_paths, target)), str(working),
    )
    assert block["status"] == "pause", block
    assert block["pause_event"]["metadata"]["target"] == str(target)
    assert not target.exists()


def test_remote_analysis_does_not_consult_wsl_or_host_mounts(monkeypatch, wsl_paths):
    def no_target_selection():
        pytest.fail("remote analysis consulted the local execution target")
    monkeypatch.setattr(execution_target, "current", no_target_selection)
    safe_bash._analyze_shell_path_effects("printf ready > /srv/work/out.txt", remote=True)


def test_unsupported_managed_target_has_no_local_path_fallback(monkeypatch, wsl_paths):
    selected = execution_target.parse("ssh:server")
    monkeypatch.setattr(execution_target, "current", lambda: selected)
    assert safe_bash._analyze_shell_path_effects("touch /srv/work/out.txt", "C:\\") == [
        ("touch", safe_bash.UNRESOLVED),
    ]


def test_target_and_mount_observations_do_not_leak_between_calls(monkeypatch, wsl_paths):
    assert bash_paths.normalize_bash_path("/mnt/c/out.txt") == "C:\\out.txt"
    wsl_paths._mount_prefix = "/drives/"
    assert bash_paths.normalize_bash_path("/mnt/c/out.txt") is None
    assert bash_paths.normalize_bash_path("/drives/c/out.txt") == "C:\\out.txt"
    monkeypatch.setattr(execution_target, "current", lambda: execution_target.LOCAL)
    monkeypatch.setattr(bash_paths, "_msys_absolute_path", lambda path: "D:\\git\\home\\out.txt")
    assert bash_paths.normalize_bash_path("/home/out.txt") == "D:\\git\\home\\out.txt"


def test_remote_scope_inside_wsl_context_restores_the_outer_mapper(wsl_paths):
    with bash_paths.shell_path_resolution():
        assert bash_paths.normalize_bash_path("/mnt/c/one.txt") == "C:\\one.txt"
        with bash_paths.shell_path_resolution(remote=True):
            assert not bash_paths.uses_wsl_paths()
        assert bash_paths.normalize_bash_path("/mnt/c/two.txt") == "C:\\two.txt"
