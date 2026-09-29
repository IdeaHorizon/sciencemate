"""祖先可写角色（包住 run_root 的 approved_write_root）不能把写权限带回 run_root。"""
from __future__ import annotations

import asyncio

import pytest

from core import sandbox
from nodes.experiment.tools import safe_bash
from nodes.experiment.tools.subprocess_policy import python_sandbox_roots
from nodes.experiment.tests.test_scientific_python_runroot_readonly import (
    _evict_attempt,
    _scientific_primary_state,
)


def _approve_ancestor(state, run_root):
    ancestor = run_root.parent
    # 与人工批准 scope-guard 暂停后走的是同一个登记函数。
    registered = safe_bash._register_approved_write_root(
        state, str(ancestor / "notes.txt"), "write")
    assert registered == str(ancestor), registered
    return ancestor


def test_ancestor_approved_root_is_not_replayed_writable(tmp_path) -> None:
    state, run_root = _scientific_primary_state(tmp_path)
    (run_root / ".python-scratch" / "tmp").mkdir(parents=True)
    ancestor = _approve_ancestor(state, run_root)
    writable, _readonly = python_sandbox_roots(state, str(ancestor))
    assert ancestor not in writable, writable
    assert all(
        p == run_root / ".python-scratch" or not run_root.is_relative_to(p)
        for p in writable
    ), writable


def test_ancestor_approved_root_cwd_cannot_write_run_root(tmp_path) -> None:
    available, reason = sandbox.availability()
    if not available:
        pytest.skip(f"native write-boundary backend unavailable: {reason}")
    state, run_root = _scientific_primary_state(tmp_path)
    ancestor = _approve_ancestor(state, run_root)
    target = run_root / "via-ancestor.txt"
    code = (
        "print('entered-ancestor', flush=True)\n"
        f"target = {str(target)!r}\n"
        "writer = open\n"
        "with writer(target, 'w', encoding='utf-8') as stream:\n"
        "    stream.write('computed-by-lightweight-python')\n"
    )
    try:
        result = asyncio.run(safe_bash._safe_execute_python(
            state, code, cwd=str(ancestor), timeout=30))
        assert "entered-ancestor" in str(result.get("stdout_tail") or ""), result
        assert not target.exists(), result
    finally:
        _evict_attempt(state)
