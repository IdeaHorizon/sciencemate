"""Capability probes must share one exact, operand-free judgement.

This boundary is intentionally tested in one place: route classification and
write-target projection must not drift into separate ideas of what a probe is.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.state import State
from nodes.experiment.tools import safe_bash as sb


@pytest.mark.parametrize("flag", ["--version", "--help", "-V"])
def test_rsync_capability_probe_has_no_write_target(flag: str) -> None:
    tokens = ["rsync", flag]

    assert sb._rsync_write_targets(tokens, "/run") == []
    assert sb._analyze_shell_path_effects(f"rsync {flag}", "/run") == []


@pytest.mark.parametrize("flag", ["--version", "--help", "-V"])
def test_rsync_capability_probe_never_requests_path_authorization(
    tmp_path: Path,
    flag: str,
) -> None:
    state = State.new("experiment", tmp_path / "state")
    run_root = tmp_path / "run"
    run_root.mkdir()
    state.hook_state["path_roles"] = {
        "run_root": {"path": str(run_root), "writable": True},
    }

    assert sb._bash_path_effects_guard(
        state,
        f"rsync {flag}",
        cwd=str(run_root),
        remote=True,
        allow_authorization=False,
    ) is None


@pytest.mark.parametrize("flag", ["--version", "--help", "-V"])
def test_rsync_probe_flag_with_operands_still_projects_destination(
    flag: str,
) -> None:
    """Seeing a probe flag must never erase real source/destination operands."""
    assert sb._rsync_write_targets(
        ["rsync", flag, "/src", "/dst"], "/run",
    ) == ["/dst"]


@pytest.mark.parametrize(
    ("args", "flags", "prefixes", "expected"),
    [
        (("--version",), frozenset({"--version"}), (), True),
        (("--version", "/src", "/dst"), frozenset({"--version"}), (), False),
        ((), frozenset({"--version"}), (), False),
        (("--verbose",), frozenset({"--version"}), (), False),
        (("-print-search-dirs",), frozenset(), ("-print-",), True),
    ],
)
def test_exact_capability_probe_accepts_only_probe_arguments(
    args: tuple[str, ...],
    flags: frozenset[str],
    prefixes: tuple[str, ...],
    expected: bool,
) -> None:
    assert sb._is_exact_capability_probe(
        args,
        flags=flags,
        flag_prefixes=prefixes,
    ) is expected


@pytest.mark.parametrize(
    ("site", "invoke", "expected_args", "expected_result"),
    [
        (
            "tar write targets",
            lambda: sb._tar_write_targets(["tar", "--version"], "/run"),
            ("--version",),
            ([], False),
        ),
        (
            "build write targets",
            lambda: sb._is_build_probe(["make", "--version"]),
            ("--version",),
            True,
        ),
        (
            "compiler write targets",
            lambda: sb._compiler_write_targets(["gcc", "--version"], "/run"),
            ("--version",),
            [],
        ),
        (
            "major-run classification",
            lambda: sb._is_major_run("mpirun --version"),
            ("--version",),
            False,
        ),
        (
            "rsync write targets",
            lambda: sb._rsync_write_targets(["rsync", "--version"], "/run"),
            ("--version",),
            [],
        ),
        (
            "strict route classification",
            lambda: sb._is_strict_capability_probe("rsync", ["--version"]),
            ("--version",),
            True,
        ),
    ],
    ids=["tar", "build", "compiler", "major-run", "rsync", "strict-route"],
)
def test_capability_probe_sites_delegate_to_shared_exact_judgement(
    monkeypatch: pytest.MonkeyPatch,
    site: str,
    invoke: Callable[[], Any],
    expected_args: tuple[str, ...],
    expected_result: Any,
) -> None:
    """A local copy of the old predicate must make its corresponding case red."""
    original = sb._is_exact_capability_probe
    observed: list[tuple[str, ...]] = []

    def recording_probe(
        args: list[str] | tuple[str, ...],
        *,
        flags: frozenset[str],
        flag_prefixes: tuple[str, ...] = (),
    ) -> bool:
        observed.append(tuple(args))
        return original(
            args,
            flags=flags,
            flag_prefixes=flag_prefixes,
        )

    monkeypatch.setattr(sb, "_is_exact_capability_probe", recording_probe)

    assert invoke() == expected_result, site
    assert expected_args in observed, (
        f"{site} stopped using the shared exact capability-probe judgement"
    )


@pytest.mark.parametrize(
    ("invoke", "expected"),
    [
        (lambda: sb._is_build_probe(["make", "--version", "target"]), False),
        (
            lambda: sb._compiler_write_targets(
                ["gcc", "--version", "source.c"], "/run",
            ),
            ["/run"],
        ),
        (lambda: sb._is_major_run("mpirun --version ./solver"), True),
    ],
    ids=["build", "compiler", "major-run"],
)
def test_probe_like_commands_with_operands_are_not_exempt(
    invoke: Callable[[], Any],
    expected: Any,
) -> None:
    assert invoke() == expected


@pytest.mark.parametrize(
    "command",
    [
        "make --version target",
        "gcc --version -o solver",
    ],
)
def test_probe_flag_with_build_operands_remains_a_major_build(
    command: str,
) -> None:
    assert sb._is_major_build(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "make -n",
        "make -n all",
        "make --dry-run install",
        "ninja -n all",
        "make -n -j8",
        "cd build && make -n install",
    ],
)
def test_build_dry_run_mode_with_operands_is_not_a_major_build(
    command: str,
) -> None:
    assert sb._is_major_build(command) is False


@pytest.mark.parametrize(
    "command",
    [
        "make --version 2>&1 | head -1",
        "ninja --version 2>&1",
        "make --version 2>/dev/null",
        "make --version | ninja --version",
    ],
)
def test_redirected_and_piped_build_probes_are_not_major_builds(
    command: str,
) -> None:
    """Shell plumbing is not an operand and each pipeline stage stands alone."""
    assert sb._is_major_build(command) is False


@pytest.mark.parametrize(
    "command",
    [
        "make --version target 2>/dev/null",
        "make --version 2>&1 | ninja -j2",
        "make -j8 -n",
        "make all -n",
        "make -C -n all",
        "ninja -t cleandead -n",
    ],
)
def test_probe_like_builds_with_operands_or_late_dry_run_remain_builds(
    command: str,
) -> None:
    assert sb._is_major_build(command) is True


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (["make", "-v"], True),
        (["make", "-n"], True),
        (["ninja", "--version"], True),
        (["ninja", "-n"], True),
        (["cmake", "--help"], True),
        (["./compile", "--help"], True),
        (["ninja", "-v"], False),
        (["make", "help"], False),
        (["ninja", "help"], False),
        (["cmake", "-v"], False),
        (["make", "--", "-n"], False),
        (["make", "-j8", "-n"], False),
        (["make", "all", "-n"], False),
        (["make", "-C", "-n", "all"], False),
        (["ninja", "-t", "cleandead", "-n"], False),
        (["case.build", "--version"], False),
    ],
)
def test_build_probe_flags_are_tool_specific(
    tokens: list[str],
    expected: bool,
) -> None:
    assert sb._is_build_probe(tokens) is expected


def test_build_write_target_projection_delegates_to_shared_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Write projection must not grow a second, drifting build-probe rule."""
    original = sb._is_build_probe
    observed: list[tuple[str, ...]] = []

    def recording_probe(tokens: list[str]) -> bool:
        observed.append(tuple(tokens))
        return original(tokens)

    monkeypatch.setattr(sb, "_is_build_probe", recording_probe)

    op, targets, _ = sb._extract_write_targets(
        "make --version 2>&1", "/run",
    )

    assert op == "make"
    assert targets == []
    assert observed == [("make", "--version", "2>&1")]


def test_build_write_target_projection_keeps_real_build_and_redirect_output() -> None:
    _, build_targets, _ = sb._extract_write_targets(
        "make --version target 2>/dev/null", "/run",
    )
    _, redirect_targets, _ = sb._extract_write_targets(
        "make --version > version.txt", "/run",
    )

    assert build_targets == ["/run"]
    assert redirect_targets == ["/run/version.txt"]
