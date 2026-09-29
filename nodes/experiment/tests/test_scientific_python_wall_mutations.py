# nodes/experiment/tests/test_scientific_python_runroot_wall_bypass.py（B-N3 部分）
from __future__ import annotations

import asyncio

import pytest

from core import sandbox
from nodes.experiment.tools import safe_bash
from nodes.experiment.tools.path_roles import experiment_output_dir
from nodes.experiment.tools.subprocess_policy import python_sandbox_roots
from nodes.experiment.tests.test_scientific_python_runroot_readonly import (
    _evict_attempt,
    _scientific_primary_state,
)


def _require_native_backend() -> None:
    available, reason = sandbox.availability()
    if not available:
        pytest.skip(f"native write-boundary backend unavailable: {reason}")


def _only_scratch_inside_run_root(writable, run_root) -> bool:
    scratch = run_root / ".python-scratch"
    return all(p == scratch or not p.is_relative_to(run_root) for p in writable)


def _alias_write(path_expr: str, marker: str) -> str:
    return (
        "import os\n"
        f"print('entered-{marker}', flush=True)\n"
        "writer = open\n"
        f"with writer({path_expr}, 'w', encoding='utf-8') as stream:\n"
        "    stream.write('computed-by-lightweight-python')\n"
    )


def test_plain_subdirectory_cwd_inside_run_root_gets_no_write(tmp_path) -> None:
    """杀死 MX1：把 run_root 真子目录的 cwd 重新放进 writable。"""
    _require_native_backend()
    state, run_root = _scientific_primary_state(tmp_path)
    analysis = run_root / "analysis"
    analysis.mkdir()
    (run_root / ".python-scratch" / "tmp").mkdir(parents=True)
    writable, _readonly = python_sandbox_roots(state, str(analysis))
    assert _only_scratch_inside_run_root(writable, run_root), writable
    target = analysis / "subdir-out.txt"
    try:
        result = asyncio.run(safe_bash._safe_execute_python(
            state, _alias_write("'subdir-out.txt'", "subdir"),
            cwd=str(analysis), timeout=30))
        assert "entered-subdir" in str(result.get("stdout_tail") or ""), result
        assert not target.exists(), result
    finally:
        _evict_attempt(state)


def test_ast_recognized_literal_run_root_target_from_build_root_stays_denied(
    tmp_path,
) -> None:
    """杀死 MX3：AST 认出的目标（authorized_targets）重新打开 run_root。
    交付的 build_root 格用别名，authorized_targets 为空，走不到这条路。"""
    _require_native_backend()
    state, run_root = _scientific_primary_state(tmp_path)
    build_root = experiment_output_dir(state, "build", create=True).resolve()
    target = run_root / "literal-from-build.txt"
    try:
        result = asyncio.run(safe_bash._safe_execute_python(
            state, f"open({str(target)!r}, 'w').write('forbidden')\n",
            cwd=str(build_root), timeout=30))
        assert not target.exists(), result
    finally:
        _evict_attempt(state)

# 实测：两条在 f5acfb52 上 bwrap / Landlock 都绿；MX1 下第一条红，MX3 下第二条红。
