"""One Python source must yield one conservative effect projection.

The projection is a lower bound: recognised effects may tighten policy, while
an absent fact must never be interpreted as proof that the code is read-only or
otherwise safe.  These tests deliberately exercise the public tool entrypoint
instead of merely counting calls in the projection helper.
"""
from __future__ import annotations

import asyncio

import pytest

from nodes.experiment.tests.test_route_shadow_wiring import _state
from nodes.experiment.tests.test_scientific_python_runroot_readonly import (
    _scientific_primary_state,
)
from nodes.experiment.tools import safe_bash


def _count_python_parses(monkeypatch) -> list[str]:
    observed: list[str] = []
    real_parse = safe_bash.ast.parse

    def counted_parse(source, *args, **kwargs):
        observed.append(source)
        return real_parse(source, *args, **kwargs)

    monkeypatch.setattr(safe_bash.ast, "parse", counted_parse)
    return observed


def _successful_executor():
    async def execute(*_args, **_kwargs):
        return {
            "status": "success",
            "returncode": 0,
            "stdout_tail": "ok\n",
            "stderr_tail": "",
        }

    return execute


@pytest.mark.parametrize("code", ["print('projection-success')", "", "  \n"])
def test_safe_execute_python_parses_source_once_on_success(
    tmp_path,
    monkeypatch,
    code,
):
    state = _state(tmp_path)
    runtime = state.root / "outputs" / "experiment" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    parses = _count_python_parses(monkeypatch)
    monkeypatch.setattr(safe_bash, "_exec_and_log", _successful_executor())

    result = asyncio.run(safe_bash._safe_execute_python(
        state,
        code,
        cwd=str(runtime),
    ))

    assert result["status"] == "success"
    assert parses == [code]


def test_archive_route_rejection_reuses_the_single_projection(
    tmp_path,
    monkeypatch,
):
    state = _state(tmp_path)
    runtime = state.root / "outputs" / "experiment" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    code = "import shutil\nshutil.unpack_archive('src.tgz', 'src')"
    parses = _count_python_parses(monkeypatch)

    async def forbidden_executor(*_args, **_kwargs):
        raise AssertionError("route-rejected archive extraction must not spawn")

    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden_executor)

    result = asyncio.run(safe_bash._safe_execute_python(
        state,
        code,
        cwd=str(runtime),
    ))

    assert result["status"] == "error"
    assert result["reason"] == "execution_route_required"
    assert parses == [code]


def test_process_launch_rejection_reads_the_single_projection(
    tmp_path,
    monkeypatch,
):
    state = _state(tmp_path)
    runtime = state.root / "outputs" / "experiment" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    code = "import subprocess\nsubprocess.run(['true'])"
    parses = _count_python_parses(monkeypatch)

    async def forbidden_executor(*_args, **_kwargs):
        raise AssertionError("recognised child-process launch must not spawn")

    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden_executor)

    result = asyncio.run(safe_bash._safe_execute_python(
        state,
        code,
        cwd=str(runtime),
    ))

    assert result["status"] == "error"
    assert result["reason"] == "unmanaged_python_process_launch"
    assert result["blocker"]["calls"] == ["subprocess.run"]
    assert parses == [code]


@pytest.mark.parametrize("code", [None, 0, False, ""])
def test_process_launch_compatibility_helper_keeps_falsy_inputs_empty(code):
    assert safe_bash._python_process_launches(code) == []


def test_highrisk_approval_preview_reuses_the_single_projection(
    tmp_path,
    monkeypatch,
):
    state = _state(tmp_path)
    runtime = state.root / "outputs" / "experiment" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    code = "import shutil\nshutil.rmtree('old-results')"
    parses = _count_python_parses(monkeypatch)

    async def forbidden_executor(*_args, **_kwargs):
        raise AssertionError("unapproved destructive Python must not spawn")

    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden_executor)

    result = asyncio.run(safe_bash._safe_execute_python(
        state,
        code,
        cwd=str(runtime),
    ))

    assert result["status"] == "pause"
    assert result["pause_event"]["metadata"]["tool"] == "safe_execute_python"
    assert parses == [code]


def test_sqlite_error_attribution_reuses_the_single_projection(
    tmp_path,
    monkeypatch,
):
    state, run_root = _scientific_primary_state(tmp_path)
    code = (
        "import sqlite3\n"
        "sqlite3.connect('results.db').execute('select 1').fetchall()"
    )
    parses = _count_python_parses(monkeypatch)

    async def readonly_sqlite_error(*_args, **_kwargs):
        return {
            "status": "error",
            "returncode": 1,
            "stdout_tail": "",
            "stderr_tail": (
                "sqlite3.OperationalError: attempt to write a readonly database"
            ),
        }

    monkeypatch.setattr(safe_bash, "_exec_and_log", readonly_sqlite_error)

    result = asyncio.run(safe_bash._safe_execute_python(
        state,
        code,
        cwd=str(run_root),
    ))

    assert result["status"] == "error"
    assert result["reason"] == "scientific_primary_run_root_readonly"
    assert result.get("recovery")
    assert parses == [code]


def test_python_effect_projection_exposes_only_positive_lower_bound_facts(
    tmp_path,
):
    projection_type = safe_bash.PythonEffectProjection
    projection = safe_bash._project_python_effects(
        "import shutil\n"
        "import subprocess\n"
        "shutil.unpack_archive('src.tgz', 'src')\n"
        "subprocess.run(['true'])\n"
        "open('literal.txt', 'w').write('x')\n"
        "target = 'dynamic.txt'\n"
        "open(target, 'w').write('x')"
    )

    assert isinstance(projection, projection_type)
    assert issubclass(projection_type, tuple)
    with pytest.raises(AttributeError):
        projection.direct_archive_unpack = False

    public_fields = set(projection_type._fields)
    assert any("process" in name and "launch" in name for name in public_fields)
    assert any(
        "archive" in name and ("unpack" in name or "extract" in name)
        for name in public_fields
    )
    assert any("write" in name for name in public_fields)

    assert projection.process_launches == ("subprocess.run",)
    assert projection.direct_archive_unpack is True
    assert projection.has_dynamic_write_target is True
    paths, dynamic = projection.direct_write_paths(str(tmp_path))
    assert paths == [str(tmp_path / "literal.txt")]
    assert dynamic is True

    forbidden_policy_fragments = (
        "allow",
        "authoriz",
        "downgrad",
        "effect_free",
        "no_effect",
        "no_write",
        "policy",
        "read_only",
        "readonly",
        "safe",
        "write_free",
    )
    assert not {
        name
        for name in public_fields
        if any(fragment in name.lower() for fragment in forbidden_policy_fragments)
    }
