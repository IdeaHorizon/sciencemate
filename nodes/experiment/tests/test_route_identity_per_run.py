"""路线身份按 run 区分（2026-09-14 第三会话复审 §六）。

同一项目工作区的 experiment run 共用本节点账本。路线原先是全项目一个固定身份，而修订
保留原产出方、读取要求产出方是当前 run —— 第二个 run 声明不了也读不到自己的路线，
submit_job 的真实提交一直被拒（main bf177b98 上就有）。探针：
.hf-879-test/acceptance/review-0914-third/probe_crossrun_route(_main)。
"""
from __future__ import annotations

import asyncio

import pytest

from core.state import State
from nodes.experiment.tools import execution_route as er
import test_operation_job_finalization as tojf
from test_external_route_projection import _route


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "hf-home"))


def _workspace(tmp_path):
    worktree = tmp_path / "worktree"
    records = worktree / tojf._NODE_WORKSPACES["experiment"]
    records.mkdir(parents=True)
    return worktree, records


def _bound_state(tmp_path, worktree, records) -> State:
    state = State.new("experiment", tmp_path / "runs")
    state.project_worktree = worktree
    state.workspace_records_dir = records
    state.hook_state["_request_mode"] = "operation"
    state.hook_state["experiment_execution_scope"] = {
        "mode": "operational", "category": "other"}
    return state


@pytest.mark.parametrize("variant", [
    "same_content", "changed_without_reason", "changed_with_reason"])
def test_a_second_run_in_the_same_workspace_declares_and_reads_its_own_route(
    tmp_path, variant,
):
    worktree, records = _workspace(tmp_path)
    first = _bound_state(tmp_path, worktree, records)
    second = _bound_state(tmp_path, worktree, records)
    assert first.run_id != second.run_id
    declared_first = asyncio.run(er._declare_execution_route(first, route=_route()))
    assert declared_first["status"] == "success", declared_first

    route = _route()
    kwargs = {}
    if variant != "same_content":
        route["goal"] = "第二个 run 的目标"
    if variant == "changed_with_reason":
        kwargs["amendment_reason"] = "续做的新路线"
    declared_second = asyncio.run(
        er._declare_execution_route(second, route=route, **kwargs))

    assert declared_second["status"] == "success", declared_second
    assert declared_second["artifact_id"] != declared_first["artifact_id"]
    assert declared_second["route_ref"]["version"] == 1
    loaded_second = er.load_canonical_route(second)
    assert loaded_second["status"] == "ready", loaded_second
    assert loaded_second["route"]["goal"] == route["goal"]
    assert er.build_route_snapshot(second)["ready_step_ids"], er.build_route_snapshot(second)
    # 第一个 run 的路线原样不动。
    loaded_first = er.load_canonical_route(first)
    assert loaded_first["status"] == "ready", loaded_first
    assert loaded_first["route_ref"] == declared_first["route_ref"]


def test_a_run_that_declared_under_the_old_fixed_identity_keeps_its_route(
    tmp_path, monkeypatch,
):
    """升级前在固定名下声明、升级后继续跑的 run：路线照常读、修订仍在同一身份上，
    绑定仍认固定名；同一工作区升级后新开的 run 不碰固定名。"""
    worktree, records = _workspace(tmp_path)
    legacy_run = _bound_state(tmp_path, worktree, records)
    per_run_name = er._canonical_route_name
    # 升级前：固定名。
    monkeypatch.setattr(er, "_canonical_route_name",
                        lambda _state: er.CANONICAL_ROUTE_NAME)
    declared = asyncio.run(er._declare_execution_route(legacy_run, route=_route()))
    assert declared["status"] == "success", declared
    assert declared["artifact_id"] == er.CANONICAL_ROUTE_ARTIFACT_ID

    # 升级到新代码。
    monkeypatch.setattr(er, "_canonical_route_name", per_run_name)
    loaded = er.load_canonical_route(legacy_run)
    assert loaded["status"] == "ready", loaded
    assert loaded["route_ref"]["artifact_id"] == er.CANONICAL_ROUTE_ARTIFACT_ID
    assert {binding[0] for binding in er._known_route_step_bindings(legacy_run)} == {
        er.CANONICAL_ROUTE_ARTIFACT_ID}
    revised = _route()
    revised["goal"] = "升级后修订"
    amended = asyncio.run(er._declare_execution_route(
        legacy_run, route=revised, amendment_reason="升级后修订同一条路线"))
    assert amended["status"] == "success", amended
    assert amended["route_ref"]["artifact_id"] == er.CANONICAL_ROUTE_ARTIFACT_ID
    assert amended["route_ref"]["version"] == 2

    newcomer = _bound_state(tmp_path, worktree, records)
    fresh = asyncio.run(er._declare_execution_route(newcomer, route=_route()))
    assert fresh["status"] == "success", fresh
    assert fresh["artifact_id"] != er.CANONICAL_ROUTE_ARTIFACT_ID
    assert er.load_canonical_route(legacy_run)["route_ref"]["version"] == 2


@pytest.mark.parametrize("name", [
    er.CANONICAL_ROUTE_NAME, f"{er.CANONICAL_ROUTE_NAME}__1789236325-9c5499"])
def test_generic_save_cannot_mint_any_canonical_route_identity(name):
    gate = er._canonical_route_save_gate(None, {"name": name})

    assert "canonical_route_owner" in (gate.get("failures") or {}), gate


def test_generic_save_of_an_unrelated_declared_route_name_is_not_gated():
    assert er._canonical_route_save_gate(None, {"name": "legacy_build_contract"}) == {}
