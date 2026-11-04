"""Build graph probes select only their State's existing environment.

Core's launcher is stubbed here to test the call boundary and shell semantics;
native filesystem isolation is covered by the probe-boundary integration tests.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.state import State
from nodes.experiment.tools import build_graph as bg, env_provision as ep


@pytest.fixture
def core_launch(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(bg, "sandbox_mount_roots", lambda state: ([tmp_path], []))

    def prepare(argv, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(argv=argv, env=dict(os.environ),
                               terminate=lambda: None, cleanup=lambda: None)

    monkeypatch.setattr("core.sandbox.prepare_attempt_command", prepare)
    return calls


def _state(tmp_path: Path, value: str | None = None):
    state = State.new("experiment", tmp_path / "runs")
    if value is not None:
        script = ep.env_path_for_state(state)
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(f"export HF_BUILD_GRAPH_SELECTED={value}\n", encoding="utf-8", newline="\n")
    return state


def test_unselected_environment_ignores_another_runs_global_selector(tmp_path, monkeypatch, core_launch):
    foreign = _state(tmp_path, "foreign")
    own = _state(tmp_path)
    monkeypatch.setenv("EXPERIMENT_ACTIVE_BUILD_ENV", str(ep.env_path_for_state(foreign)))
    monkeypatch.setenv("HF_BUILD_GRAPH_SELECTED", "inherited")

    code, output = bg._run('printf "%s" "$HF_BUILD_GRAPH_SELECTED"', state=own, cwd=str(tmp_path))

    assert (code, output) == (0, "inherited")
    assert not ep.env_path_for_state(own).exists()
    assert not own.list_artifacts("build_env")
    assert core_launch[0]["state"] is own


def test_each_run_uses_its_own_existing_script_despite_a_foreign_selector(tmp_path, monkeypatch, core_launch):
    first = _state(tmp_path, "first")
    second = _state(tmp_path, "second")
    monkeypatch.setenv("EXPERIMENT_ACTIVE_BUILD_ENV", str(ep.env_path_for_state(first)))

    first_result = bg._run('printf "%s" "$HF_BUILD_GRAPH_SELECTED"', state=first, cwd=str(tmp_path))
    second_result = bg._run('printf "%s" "$HF_BUILD_GRAPH_SELECTED"', state=second, cwd=str(tmp_path))

    assert first_result == (0, "first")
    assert second_result == (0, "second")
    assert [call["state"] for call in core_launch] == [first, second]
    assert os.environ["EXPERIMENT_ACTIVE_BUILD_ENV"] == str(ep.env_path_for_state(first))


@pytest.mark.parametrize("failure", ["read", "wrap"])
def test_selected_environment_preparation_failure_never_starts_a_host_probe(tmp_path, monkeypatch, core_launch, failure):
    state = _state(tmp_path, "selected")

    def fail(*args, **kwargs):
        raise PermissionError("selected environment denied")

    monkeypatch.setattr(ep, "existing_env_script" if failure == "read" else "wrap_command", fail)
    code, output = bg._run("printf HOST", state=state, cwd=str(tmp_path))

    assert code is None
    assert "PermissionError" in output and "selected environment denied" in output
    assert core_launch == []


def test_selected_setup_failure_does_not_run_the_probe_command(tmp_path, core_launch):
    state = _state(tmp_path, "selected")
    ep.env_path_for_state(state).write_text("return 17\n", encoding="utf-8", newline="\n")

    code, output = bg._run("printf MUST_NOT_RUN", state=state, cwd=str(tmp_path))

    assert code == 17
    assert "MUST_NOT_RUN" not in output
    assert core_launch[0]["state"] is state
