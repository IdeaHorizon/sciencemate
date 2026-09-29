# nodes/experiment/tests/test_scientific_python_runroot_wall_bypass.py（B-N1 部分）
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
    # writer = open 对 AST 写目标分析不可见，调用会走到 OS 能力而不是早期提示
    return (
        "import os\n"
        f"print('entered-{marker}', flush=True)\n"
        "writer = open\n"
        f"with writer({path_expr}, 'w', encoding='utf-8') as stream:\n"
        "    stream.write('computed-by-lightweight-python')\n"
    )


def test_managed_source_root_under_run_root_is_not_replayed_writable(tmp_path) -> None:
    state, run_root = _scientific_primary_state(tmp_path)
    (run_root / ".python-scratch" / "tmp").mkdir(parents=True)
    source = run_root / "source"  # framework default managed_source_root
    source.mkdir()
    writable, _readonly = python_sandbox_roots(state, str(source))
    assert _only_scratch_inside_run_root(writable, run_root), writable


def test_managed_source_root_under_run_root_rejects_real_write(tmp_path) -> None:
    _require_native_backend()
    state, run_root = _scientific_primary_state(tmp_path)
    source = run_root / "source"
    source.mkdir()
    target = source / "results.txt"
    try:
        result = asyncio.run(safe_bash._safe_execute_python(
            state, _alias_write("'results.txt'", "nested-source"),
            cwd=str(source), timeout=30))
        assert "entered-nested-source" in str(result.get("stdout_tail") or ""), result
        assert not target.exists(), result
    finally:
        _evict_attempt(state)


def test_literal_target_under_nested_source_rejected_from_build_root(tmp_path) -> None:
    """不只是 cwd：AST 认出的字面目标同样会重新打开嵌套角色。"""
    _require_native_backend()
    state, run_root = _scientific_primary_state(tmp_path)
    source = run_root / "source"
    source.mkdir()
    build_root = experiment_output_dir(state, "build", create=True).resolve()
    target = source / "literal.txt"
    try:
        result = asyncio.run(safe_bash._safe_execute_python(
            state, f"open({str(target)!r}, 'w').write('forbidden')\n",
            cwd=str(build_root), timeout=30))
        assert not target.exists(), result
    finally:
        _evict_attempt(state)

# 实测：f5acfb52 上 bwrap 与 Landlock 三条都红；原型修法（scientific-primary 下跳过落在 run_root 内的所有可写角色）下两后端全绿。
