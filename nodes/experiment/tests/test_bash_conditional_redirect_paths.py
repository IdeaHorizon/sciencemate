"""A trailing list redirect belongs to the final command's entry cwd."""
from __future__ import annotations

import shlex

import pytest

from nodes.experiment.tools.bash_semantics import analyze_bash


@pytest.mark.parametrize("operator", ["&&", "||"])
def test_list_redirect_uses_branch_entry_cwd(tmp_path, operator):
    destination = tmp_path / "destination"
    analysis = analyze_bash(
        f"cd {shlex.quote(destination.as_posix())} {operator} printf ready > result.txt",
        initial_cwd=str(tmp_path),
    )
    redirects = [event for event in analysis.static_path_events if event.kind == "redirect"]
    assert len(redirects) == 1
    expected = destination if operator == "&&" else tmp_path
    assert redirects[0].cwd.values == (str(expected),)


def test_nested_list_redirect_follows_last_cd(tmp_path):
    destination = tmp_path / "destination"
    analysis = analyze_bash(
        f"cd {shlex.quote(destination.as_posix())} && cd child && printf ready > result.txt",
        initial_cwd=str(tmp_path),
    )
    redirects = [event for event in analysis.static_path_events if event.kind == "redirect"]
    assert len(redirects) == 1
    assert redirects[0].cwd.values == (str(destination / "child"),)


def test_redirect_on_compound_command_keeps_its_entry_cwd(tmp_path):
    destination = tmp_path / "destination"
    analysis = analyze_bash(
        f"{{ cd {shlex.quote(destination.as_posix())}; printf ready; }} > result.txt",
        initial_cwd=str(tmp_path),
    )
    redirects = [event for event in analysis.static_path_events if event.kind == "redirect"]
    assert len(redirects) == 1
    assert redirects[0].cwd.values == (str(tmp_path),)


def test_list_redirect_sees_prior_shell_assignment(tmp_path):
    analysis = analyze_bash("file=result.txt && printf ready > \"$file\"", initial_cwd=str(tmp_path))
    redirects = [event for event in analysis.static_path_events if event.kind == "redirect"]
    assert len(redirects) == 1
    assert redirects[0].target_values == ("result.txt",)
    assert not redirects[0].target_unknown
