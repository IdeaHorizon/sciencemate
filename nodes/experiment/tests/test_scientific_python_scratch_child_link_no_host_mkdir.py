"""scratch 子目录链接必须在宿主 mkdir 之前被拒：run_root 与链接目标都不能多出目录。"""
from __future__ import annotations

import asyncio

import pytest

from nodes.experiment.tools import safe_bash
from nodes.experiment.tests.test_scientific_python_runroot_readonly import (
    _scientific_primary_state,
)


@pytest.mark.parametrize("link_kind", ["relative-parent", "absolute-outside"])
def test_scratch_child_link_rejected_before_host_mkdir(
    tmp_path, monkeypatch, link_kind,
) -> None:
    state, run_root = _scientific_primary_state(tmp_path)
    scratch = run_root / ".python-scratch"
    scratch.mkdir()
    outside = tmp_path / "outside-target"
    outside.mkdir()
    link_target = ".." if link_kind == "relative-parent" else str(outside)
    (scratch / "cache").symlink_to(link_target, target_is_directory=True)
    spawned = []

    async def forbidden(*_args, **_kwargs):
        spawned.append(True)
        raise AssertionError("unsafe scratch child must fail before spawn")

    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden)
    result = asyncio.run(safe_bash._safe_execute_python(
        state, "print('must not run')", cwd=str(run_root)))

    assert result["status"] == "error", result
    assert result["reason"] == "sandbox_path_contract_invalid", result
    assert spawned == []
    # 宿主侧 mkdir(scratch/cache/matplotlib|pycache) 不能穿过链接落地。
    for leaked in ("tmp", "matplotlib", "pycache"):
        assert not (run_root / leaked).exists(), leaked
    assert list(outside.iterdir()) == []
