"""见证器崩了不是拒绝派发的理由（判决拆除·tool_registry:634，2026-09-02）。

写边界的墙在 spawn（core/sandbox）；派发口的 capture_before_tool / enforce_after_tool
只是事后**见证**的拍照与比对。从前拍照一崩就 `workspace_guard_failed` 拒派发任何
工具 —— gitdir 指针坏那类故障会让整条 run 的每次工具调用都死。

现在：工具照跑；结果附 `workspace_witness_failed` + `workspace_witness: unavailable`；
transcript 记 `workspace_witness_failed` 事件。对称面：after 端 baseline 缺席时
报「无见证」而不是空集（见 test_out_of_scope_writes_are_evidence_only）。

把拒绝加回去（任一相）这里必转红。
"""
from __future__ import annotations

import json

import pytest

from core import project_workspace
from core.bootstrap import bootstrap
from core.state import State


@pytest.fixture(autouse=True)
def _setup():
    bootstrap()
    yield


def _events(state, name):
    lines = state.transcript_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(l) for l in lines if l.strip() and json.loads(l).get("event") == name]


@pytest.mark.asyncio
async def test_before_snapshot_crash_still_runs_the_tool(tmp_path, monkeypatch):
    from core.tool_registry import execute

    def _boom(_state):
        raise RuntimeError("fatal: not a git repository (gitdir pointer broken)")

    monkeypatch.setattr(project_workspace, "capture_before_tool", _boom)
    state = State.new(node_type="data", base_dir=tmp_path)

    result = await execute("list_artifacts", state)

    assert result["status"] == "success", result
    assert "gitdir pointer broken" in result["workspace_witness_failed"]
    assert result["workspace_witness"] == "unavailable"
    recorded = _events(state, "workspace_witness_failed")
    assert recorded and recorded[0]["phase"] == "before"
    assert recorded[0]["tool_name"] == "list_artifacts"


@pytest.mark.asyncio
async def test_after_witness_crash_keeps_the_real_result(tmp_path, monkeypatch):
    """工具已跑完、副作用已发生：把真实结果换成 error 会让模型重试已成功的副作用。"""
    from core.tool_registry import execute

    def _boom(_state, _tool_name):
        raise OSError("git status timed out")

    monkeypatch.setattr(project_workspace, "enforce_after_tool", _boom)
    state = State.new(node_type="data", base_dir=tmp_path)

    result = await execute("list_artifacts", state)

    assert result["status"] == "success", result
    assert "git status timed out" in result["workspace_witness_failed"]
    assert result["workspace_witness"] == "unavailable"
    assert _events(state, "workspace_witness_failed")[0]["phase"] == "after"


@pytest.mark.asyncio
async def test_missing_baseline_is_stamped_on_the_result(tmp_path, monkeypatch):
    """after 端拿不到 baseline → 结果盖「无见证」，不盖「无越界」。"""
    from core.tool_registry import execute

    monkeypatch.setattr(
        project_workspace, "enforce_after_tool",
        lambda _s, _t: {"witness_unavailable": True, "paths": [], "note": "本次调用没有越界见证"},
    )
    state = State.new(node_type="data", base_dir=tmp_path)

    result = await execute("list_artifacts", state)

    assert result["status"] == "success"
    assert result["workspace_witness"] == "unavailable"
    assert "没有越界见证" in result["workspace_witness_note"]
    assert "out_of_scope_paths" not in result and "workspace_scope_warning" not in result
