# nodes/experiment/tests/test_scientific_python_runroot_wall_bypass.py（B-N2 部分，与 B-N1 同文件时共用 header/helper）
from __future__ import annotations

import asyncio

import pytest

from core import sandbox
from nodes.experiment.tools import safe_bash
from nodes.experiment.tests.test_scientific_python_runroot_readonly import (
    _evict_attempt,
    _scientific_primary_state,
)


def _require_native_backend() -> None:
    available, reason = sandbox.availability()
    if not available:
        pytest.skip(f"native write-boundary backend unavailable: {reason}")


def _alias_write(path_expr: str, marker: str) -> str:
    return (
        "import os\n"
        f"print('entered-{marker}', flush=True)\n"
        "writer = open\n"
        f"with writer({path_expr}, 'w', encoding='utf-8') as stream:\n"
        "    stream.write('computed-by-lightweight-python')\n"
    )


def test_scratch_hardlink_planted_by_bash_cannot_rewrite_run_root(tmp_path) -> None:
    """touch + ln 是 scientific-primary 下 bash 可以做的普通文件操作；
    硬链接让 scratch 路径和 run_root 文件共享 inode。"""
    _require_native_backend()
    state, run_root = _scientific_primary_state(tmp_path)
    target = run_root / "results.txt"
    try:
        warm = asyncio.run(safe_bash._safe_execute_python(
            state, "print('warm')", cwd=str(run_root), timeout=30))
        assert warm["status"] == "success", warm
        planted = asyncio.run(safe_bash._safe_run_bash(
            state,
            "touch results.txt && ln results.txt .python-scratch/tmp/planted.txt",
            cwd=str(run_root), timeout=30))
        assert planted["status"] == "success", planted
        assert target.read_bytes() == b""
        result = asyncio.run(safe_bash._safe_execute_python(
            state,
            _alias_write("os.path.join(os.environ['TMPDIR'], 'planted.txt')",
                         "hardlink"),
            cwd=str(run_root), timeout=30))
        assert result["status"] == "error", result
        assert result["reason"] == "sandbox_path_contract_invalid", result
        planted_path = run_root / ".python-scratch" / "tmp" / "planted.txt"
        assert str(planted_path) in result["error"]
        assert "delete that path and retry" in result["error"]
        assert target.read_bytes() == b"", result
    finally:
        _evict_attempt(state)


def test_scratch_child_symlink_fails_before_host_mkdir_or_spawn(
    tmp_path,
    monkeypatch,
) -> None:
    state, run_root = _scientific_primary_state(tmp_path)
    scratch = run_root / ".python-scratch"
    scratch.mkdir()
    child = scratch / "cache"
    child.symlink_to("..", target_is_directory=True)
    spawned = []

    async def forbidden(*_args, **_kwargs):
        spawned.append(True)
        raise AssertionError("unsafe scratch child must fail before spawn")

    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden)
    result = asyncio.run(safe_bash._safe_execute_python(
        state,
        "print('must not run')",
        cwd=str(run_root),
    ))

    assert result["status"] == "error", result
    assert result["reason"] == "sandbox_path_contract_invalid", result
    assert str(child) in result["error"]
    assert "delete that path and retry" in result["error"]
    assert spawned == []

# 实测：f5acfb52 上 bwrap / Landlock 都红（results.txt 被写成 b'computed-by-lightweight-python'）；原型修法下两后端都绿。
