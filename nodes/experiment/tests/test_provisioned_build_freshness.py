"""E-11：provisioned_build_tracker 停止跨 run 反向背书。

上一 run 遗留的磁盘产物（mtime 早于本 run 起始）不得被 Wire 1 直接推进
verified：标 inherited（与 built 同级、拦下游），出路只有 reused_inputs
精确声明收养或重建刷新。锚不可得时保持旧行为但留痕。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from core.loop_hooks import HookContext
from core.state import State
from nodes.experiment import hooks as H
from nodes.experiment.tools import safe_bash
from nodes.experiment.tools.build_state import (
    ORDER,
    STATE_KEY,
    adopt_inherited_nodes,
    framework_verify_outputs,
    mark_stale_on_env_change,
    normalize_reuse_path,
    resolve_run_started_at,
)


def _state(tmp_path: Path) -> State:
    return State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="freshness-project",
    )


def _ctx(state: State, turn: int = 1) -> HookContext:
    return HookContext(harness=None, state=state, messages=[], turn=turn)


def _events(state: State) -> list[dict]:
    if not state.transcript_path.exists():
        return []
    return [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _build_state_with_output(path: Path, *, node_state: str = "built") -> dict:
    return {
        "schema_version": "1.0",
        "dag_id": "dag",
        "nodes": {
            "lib": {
                "id": "lib", "state": node_state, "deps": [], "blocked_by": [],
                "outputs": [str(path)], "evidence": [],
            },
            "app": {
                "id": "app", "state": "planned", "deps": ["lib"],
                "blocked_by": ["lib"], "outputs": [str(path.parent / "app")],
                "evidence": [],
            },
        },
    }


def _write_stale(path: Path, anchor: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("payload")
    os.utime(path, (anchor - 3600, anchor - 3600))


def test_cross_run_leftover_marks_inherited_not_verified(tmp_path):
    state = _state(tmp_path)
    anchor = time.time()
    state.hook_state["_run_started_at"] = anchor
    out = tmp_path / "build" / "lib.a"
    _write_stale(out, anchor)
    state.hook_state[STATE_KEY] = _build_state_with_output(out)

    H._provisioned_build_tracker_on_turn_end(_ctx(state))

    bs = state.hook_state[STATE_KEY]
    node = bs["nodes"]["lib"]
    assert node["state"] == "inherited"
    assert node["inherited_run_started_at"] == anchor
    assert [r["path"] for r in node["inherited_outputs"]] == [str(out)]
    # 下游被自动拦住（ORDER["inherited"] < ORDER["verified"]）
    assert bs["nodes"]["app"]["blocked_by"] == ["lib"]
    inherited_events = [e for e in _events(state)
                       if e.get("event") == "provisioned_node_inherited"]
    assert len(inherited_events) == 1
    assert inherited_events[0]["run_started_at"] == anchor
    assert inherited_events[0]["outputs"][0]["mtime"] < anchor
    # 同 run 内再跑一轮：状态不变、事实事件不重复（多轮推进零变化）
    H._provisioned_build_tracker_on_turn_end(_ctx(state, turn=2))
    assert state.hook_state[STATE_KEY]["nodes"]["lib"]["state"] == "inherited"
    assert len([e for e in _events(state)
                if e.get("event") == "provisioned_node_inherited"]) == 1


def test_fresh_outputs_still_advance_to_verified(tmp_path):
    state = _state(tmp_path)
    state.hook_state["_run_started_at"] = time.time() - 100
    out = tmp_path / "build" / "lib.a"
    out.parent.mkdir(parents=True)
    out.write_text("payload")          # mtime = now > anchor
    state.hook_state[STATE_KEY] = _build_state_with_output(out)

    H._provisioned_build_tracker_on_turn_end(_ctx(state))

    bs = state.hook_state[STATE_KEY]
    assert bs["nodes"]["lib"]["state"] == "verified"
    assert bs["nodes"]["app"]["blocked_by"] == []
    assert not [e for e in _events(state)
                if e.get("event") == "provisioned_node_inherited"]
    # 再跑一轮照旧（同 run 多轮推进行为不变）
    H._provisioned_build_tracker_on_turn_end(_ctx(state, turn=2))
    assert state.hook_state[STATE_KEY]["nodes"]["lib"]["state"] == "verified"


def test_inherited_outputs_are_recorded_with_actionable_detail(
    tmp_path, monkeypatch,
):
    state = _state(tmp_path)
    source_root = tmp_path / "src"
    build_root = tmp_path / "build"
    source_root.mkdir()
    build_root.mkdir()
    for artifact_type in ("platform_profile", "source_recon"):
        state.save_artifact(artifact_type, f"test_{artifact_type}", "{}")
    lib_out = build_root / "lib.a"
    anchor = 1756000000.0
    graph = {
        "status": "extracted", "actionable": True, "dag_id": "dag",
        "source_root": str(source_root), "build_root": str(build_root),
        "nodes": {
            "lib": {"deps": [], "outputs": [str(lib_out)]},
            "app": {"deps": ["lib"], "outputs": [str(build_root / "app")]},
        },
    }
    state.hook_state[STATE_KEY] = {
        "dag_id": "dag",
        "nodes": {
            "lib": {"id": "lib", "state": "inherited", "deps": [],
                    "blocked_by": [], "outputs": [str(lib_out)],
                    "inherited_run_started_at": anchor,
                    "inherited_outputs": [
                        {"path": str(lib_out), "mtime": anchor - 7200.0,
                         "predates_run": True}]},
            "app": {"id": "app", "state": "planned", "deps": ["lib"],
                    "blocked_by": ["lib"], "outputs": [str(build_root / "app")]},
        },
    }
    monkeypatch.setattr(
        safe_bash, "bench_enabled",
        lambda capability: capability in {"build_gate", "recon", "provision_first"},
    )
    monkeypatch.setattr(safe_bash, "_infer_source_path", lambda *_a, **_k: str(source_root))
    monkeypatch.setattr(safe_bash, "_infer_build_root", lambda *_a, **_k: str(build_root))
    monkeypatch.setattr(safe_bash, "_has_matching_source_recon", lambda *_a, **_k: True)
    monkeypatch.setattr(safe_bash, "_latest_build_graph", lambda *_a, **_k: graph)

    result = safe_bash._build_gate(
        state, "cmake --build . --target app", cwd=str(build_root),
    )

    # 判决拆除（sb:3341 删）：墙拆了，E-11 的诊断价值无代价，整体并入 warning。
    # 这里验的从「拦住并给出可执行错误」变成「不拦，但逐文件 mtime 详单照记」——
    # 时钟偏斜/路径不匹配仍然一轮可定位，判断要不要重建回到调用方手里。
    assert result is None
    warned = [e for e in _events(state)
              if e.get("event") == "build_gate_prereq_warning"]
    assert warned, _events(state)
    last = warned[-1]
    assert last["inherited"] == ["lib"]
    stale = last["inherited_stale_outputs"]
    assert [item["dependency"] for item in stale] == ["lib"]
    assert stale[0]["run_started_at"] == anchor
    assert [rec["path"] for rec in stale[0]["outputs"]] == [str(lib_out)]
    assert stale[0]["outputs"][0]["mtime"] == anchor - 7200.0


def test_adoption_with_full_coverage_promotes_to_verified(tmp_path):
    state = _state(tmp_path)
    anchor = time.time()
    state.hook_state["_run_started_at"] = anchor
    out = tmp_path / "build" / "lib.a"
    _write_stale(out, anchor)
    state.hook_state[STATE_KEY] = _build_state_with_output(out)
    H._provisioned_build_tracker_on_turn_end(_ctx(state))
    assert state.hook_state[STATE_KEY]["nodes"]["lib"]["state"] == "inherited"

    state.save_artifact(
        "experiment_log", "reuse_declaration", "reusing prior build",
        metadata={"reused_inputs": [
            {"path": str(out), "source": "prior run", "reason": "unchanged deps"},
        ]},
    )
    H._provisioned_build_tracker_on_turn_end(_ctx(state, turn=2))

    bs = state.hook_state[STATE_KEY]
    assert bs["nodes"]["lib"]["state"] == "verified"
    assert bs["nodes"]["app"]["blocked_by"] == []
    adopted = [e for e in _events(state)
               if e.get("event") == "reused_inputs_adoption"]
    assert adopted and adopted[0]["node"] == "lib"
    assert adopted[0]["adopted_paths"] == [normalize_reuse_path(out)]


def test_adoption_partial_coverage_blocks_and_records_incomplete(tmp_path):
    state = _state(tmp_path)
    anchor = time.time()
    state.hook_state["_run_started_at"] = anchor
    out_a = tmp_path / "build" / "lib.a"
    out_b = tmp_path / "build" / "lib.mod"
    _write_stale(out_a, anchor)
    _write_stale(out_b, anchor)
    bs = _build_state_with_output(out_a)
    bs["nodes"]["lib"]["outputs"] = [str(out_a), str(out_b)]
    state.hook_state[STATE_KEY] = bs
    H._provisioned_build_tracker_on_turn_end(_ctx(state))

    # 只声明其中一个 → 不放行，且绝不静默失败
    state.save_artifact(
        "experiment_log", "reuse_declaration", "partial declaration",
        metadata={"reused_inputs": [{"path": str(out_a), "source": "prior run"}]},
    )
    H._provisioned_build_tracker_on_turn_end(_ctx(state, turn=2))

    node = state.hook_state[STATE_KEY]["nodes"]["lib"]
    assert node["state"] == "inherited"
    incomplete = [e for e in _events(state)
                  if e.get("event") == "reused_inputs_adoption_incomplete"]
    assert len(incomplete) == 1
    assert incomplete[0]["node"] == "lib"
    assert incomplete[0]["uncovered_paths"] == [normalize_reuse_path(out_b)]
    assert normalize_reuse_path(out_a) in incomplete[0]["declared_paths"]
    # 同一未覆盖事实不逐轮刷屏
    H._provisioned_build_tracker_on_turn_end(_ctx(state, turn=3))
    assert len([e for e in _events(state)
                if e.get("event") == "reused_inputs_adoption_incomplete"]) == 1


def test_adoption_normalizes_symlinked_paths(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real_out = real_dir / "lib.a"
    real_out.write_text("payload")
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir)
    link_out = link_dir / "lib.a"
    # 归一化函数单独可测：symlink 形态与 real 形态收敛到同一 realpath
    assert normalize_reuse_path(link_out) == normalize_reuse_path(real_out)

    bs = {
        "nodes": {
            "lib": {"id": "lib", "state": "inherited", "deps": [],
                    "outputs": [str(link_out)],
                    "inherited_outputs": [
                        {"path": str(link_out), "mtime": 1.0,
                         "predates_run": True}]},
        },
    }
    # 声明用 real 形态、stale 记录是 symlink 形态（beegfs 场景）→ 仍精确覆盖
    result = adopt_inherited_nodes(bs, [str(real_out)], turn=3)
    assert [r["node"] for r in result["adopted"]] == ["lib"]
    assert result["incomplete"] == []
    assert bs["nodes"]["lib"]["state"] == "verified"


def test_resolve_run_started_at_fallback_chain():
    # ① core 锚（hook_state["_run_started_at"]）优先
    st = SimpleNamespace(hook_state={"_run_started_at": 123.5},
                         run_id="999-abc", created_at=456.0)
    assert resolve_run_started_at(st) == 123.5
    # ② 回退 run_id 时间戳前缀
    st = SimpleNamespace(hook_state={}, run_id="1756000000-abcdef")
    assert resolve_run_started_at(st) == 1756000000.0
    # ③ 回退 state.created_at
    st = SimpleNamespace(hook_state={}, run_id="weird-id", created_at=456.0)
    assert resolve_run_started_at(st) == 456.0
    # ④ 全不可得 → 0.0（保持旧行为）
    st = SimpleNamespace(hook_state={}, run_id="weird-id")
    assert resolve_run_started_at(st) == 0.0


def test_anchor_unavailable_keeps_legacy_verify_and_leaves_trace(tmp_path):
    state = _state(tmp_path)
    state.run_id = "anchorless"          # 无 epoch 前缀，也无 _run_started_at
    out = tmp_path / "build" / "lib.a"
    _write_stale(out, time.time())
    state.hook_state[STATE_KEY] = _build_state_with_output(out)

    H._provisioned_build_tracker_on_turn_end(_ctx(state))

    # 旧行为逐字一致：只查存在性，老文件照旧 verified
    assert state.hook_state[STATE_KEY]["nodes"]["lib"]["state"] == "verified"
    traces = [e for e in _events(state)
              if e.get("event") == "provision_run_anchor_unavailable"]
    assert len(traces) == 1
    H._provisioned_build_tracker_on_turn_end(_ctx(state, turn=2))
    assert len([e for e in _events(state)
                if e.get("event") == "provision_run_anchor_unavailable"]) == 1


def test_framework_verify_outputs_without_anchor_is_verbatim_legacy(tmp_path):
    out = tmp_path / "lib.a"
    out.write_text("payload")
    legacy = framework_verify_outputs([str(out)])
    assert legacy == {"ok": True, "outputs": [
        {"path": str(out), "exists": True, "size_bytes": 7}]}
    anchored = framework_verify_outputs([str(out)], run_started_at=time.time() + 60)
    assert anchored["outputs"][0]["predates_run"] is True
    assert anchored["outputs"][0]["mtime"] > 0


def test_mark_stale_on_env_change_also_stales_inherited(tmp_path):
    out = tmp_path / "lib.a"
    bs = _build_state_with_output(out, node_state="inherited")
    bs["env_fingerprint"] = "old-fp"
    bs["nodes"]["lib"]["inherited_outputs"] = [
        {"path": str(out), "mtime": 1.0, "predates_run": True}]
    mark_stale_on_env_change(bs, "new-fp")
    # 环境变了：跨 run 遗留产物与 built 同级，一并失效
    assert bs["nodes"]["lib"]["state"] == "stale"
    assert bs["nodes"]["lib"]["stale_reason"] == "env_fingerprint_changed"


def test_bench_off_group_leaves_tracker_inert(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPERIMENT_BENCH_GROUP", "A")   # provision_first off
    state = _state(tmp_path)
    anchor = time.time()
    state.hook_state["_run_started_at"] = anchor
    out = tmp_path / "build" / "lib.a"
    _write_stale(out, anchor)
    before = _build_state_with_output(out)
    state.hook_state[STATE_KEY] = json.loads(json.dumps(before))

    assert H._provisioned_build_tracker_on_turn_end(_ctx(state)) is None

    assert state.hook_state[STATE_KEY] == before
    assert not state.transcript_path.exists() or not [
        e for e in _events(state)
        if e.get("event", "").startswith(("provisioned_node_inherited",
                                          "provision_run_anchor",
                                          "reused_inputs_adoption"))]


def test_order_places_inherited_below_verified():
    assert ORDER["inherited"] == ORDER["built"]
    assert ORDER["inherited"] < ORDER["verified"]
