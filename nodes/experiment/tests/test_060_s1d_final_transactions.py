"""S1d-final 事务探针（Codex 49）：用真实 State / hook / 工具入口；故障注入只包持久化边界，不替换被测事务。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.ledger import record_version, sha256_text
from core.loop_hooks import HookContext
from core.state import State
from nodes.experiment.tools import delivery_transaction as tx
from nodes.experiment.tools import finish, ledger, obligations, remote_admission, run_bootstrap, safe_bash
from nodes.experiment.tools.path_roles import collect_path_roles
from shared.tools.library.artifacts_extra import run_save_gate

TRIO = ("raw_results", "clean_results", "experiment_log")


def _state(tmp_path: Path, inputs: dict) -> State:
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    state.hook_state["node_inputs"] = inputs
    return state


def _roots(state):
    return {r.role: Path(r.path) for r in collect_path_roles(state)}


def _bound_prereg(state, name="prereg-h1"):
    saved = state.save_artifact("pre_registration", name, f"# {name}\n", metadata={"execution_mode": "scientific"})
    state.mark_frozen(saved["id"])
    rec = state.artifact_versions(saved["id"])[-1]
    return {"kind": "bound", "artifact_id": saved["id"], "version": record_version(rec),
            "content_hash": str(rec.get("content_hash") or sha256_text(f"# {name}\n"))}


def _turn(state, n):
    return run_bootstrap.experiment_v3_bootstrap.on_turn_start(HookContext(harness=None, state=state, messages=[], turn=n)) or []


def _trio_counts(state):
    return {t: len(state.list_artifacts(t, own_only=True)) for t in TRIO}


# 1 ────────────────────────────────────────────────────────────────────────────
def test_undecided_scope_can_finish_blocked_without_becoming_operation(tmp_path):
    state = _state(tmp_path, {"prereg_assignment": {"kind": "none", "reason": "没有预注册"},
                              "experiment_spec": "做点什么", "required_outputs": []})
    _turn(state, 1)
    assert ledger.bootstrap_phase(state) == "ready"
    out = asyncio.run(finish._finish(state, outcome="blocked", summary="调用方没说清 scope",
                                     blocker={"blocker_id": "scope_unresolved", "summary": "等调用方补 scope"}))
    assert out["status"] == "success" and out["outcome"] == "blocked", out
    assert _trio_counts(state) == {t: 1 for t in TRIO}
    for t in TRIO:
        md = state.read_artifact(out["artifact_ids"][t])["metadata"]
        assert md["frozen"] is True and md["delivery_id"] == out["closure_id"]
        assert md["delivery_owner"] == "finish" and md["artifact_role"] == t
    delivery = out["delivery"]
    assert delivery["scope"] == "unknown" and delivery["scope_status"] == "undecided"
    assert delivery["execution_status"] == "not_run" and delivery["result_status"] == "unavailable"
    rows = ledger.open_ledger(state).records()
    assert not any(r.get("scope") in {"operation", "scientific"} for r in rows if r["kind"] in {"run_bootstrap", "scope_resolved"})
    # 已封存且请求未变才是重放；不同 outcome 必须重新核验为后继交付。
    replay = asyncio.run(finish._finish(state, outcome="blocked", summary="调用方没说清 scope",
                                        blocker={"blocker_id": "scope_unresolved", "summary": "等调用方补 scope"}))
    assert replay["status"] == "success" and replay["reused_manifest"] is True
    assert replay["delivery"]["outcome"] == "blocked"


# 2 ────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("point", ["obligations", "envelope"])
def test_bootstrap_failure_is_retried_on_the_next_turn_without_model_repair(tmp_path, monkeypatch, point):
    state = _state(tmp_path, {"prereg_assignment": {"kind": "none", "reason": "x"}, "scope": "operation",
                              "operation_category": "toolchain_build", "experiment_spec": "x",
                              "required_outputs": [{"id": "f", "kind": "file", "path": "outputs/experiment/runtime/f"}]})
    mod, name = {"obligations": (obligations, "freeze"), "envelope": (remote_admission, "freeze_envelope")}[point]
    real = getattr(mod, name); n = {"c": 0}
    def flaky(*a, **k):
        n["c"] += 1
        if n["c"] == 1:
            raise OSError("injected")
        return real(*a, **k)
    monkeypatch.setattr(mod, name, flaky)
    m1 = _turn(state, 1)
    assert ledger.bootstrap_phase(state) == "initializing"
    blob = json.dumps([m.content for m in m1], ensure_ascii=False)
    assert "repair" not in blob.lower()  # 没有模型修复动作
    _turn(state, 2)
    assert ledger.bootstrap_phase(state) == "ready"
    rows = ledger.open_ledger(state).records()
    kinds = [r["kind"] for r in rows]
    assert kinds.count("bootstrap_ready") == 1 and kinds.count("obligations_frozen") == 1
    assert len([r for r in rows if r["kind"] == "run_bootstrap"]) >= 1
    assert all(str(r.get("scope")) == "operation" for r in rows if r["kind"] == "run_bootstrap")  # 同一 authority
    assert len(obligations.frozen(state)) == 4  # 1 deliverable + 三件 artifact，没有重复


# 3 ────────────────────────────────────────────────────────────────────────────
def test_partial_freeze_resume_reuses_the_same_triplet_ids(tmp_path, monkeypatch):
    state = _state(tmp_path, {"prereg_assignment": {"kind": "none", "reason": "x"}, "scope": "operation",
                              "operation_category": "toolchain_build", "experiment_spec": "x",
                              "required_outputs": [{"id": "f", "kind": "file", "path": "outputs/experiment/runtime/f"}]})
    _turn(state, 1)
    clean_id = tx.artifact_id("clean_results", tx.deterministic_names(state)["clean_results"])
    real_freeze = state.mark_frozen
    hit = {"n": 0}

    def flaky_freeze(artifact_id, *args, **kwargs):
        if artifact_id == clean_id and hit["n"] == 0:
            hit["n"] += 1
            raise OSError("injected while freezing clean_results")
        return real_freeze(artifact_id, *args, **kwargs)

    monkeypatch.setattr(state, "mark_frozen", flaky_freeze)
    with pytest.raises(OSError, match="freezing clean_results"):
        asyncio.run(finish._finish(state, outcome="failed", summary="没做成"))
    manifest = finish._read_manifest(state)
    ids = {t: manifest["artifacts"][t]["id"] for t in TRIO}
    assert all(ids.values()), manifest  # 首次 freeze 前三件身份已固定在 manifest 里
    monkeypatch.setattr(state, "mark_frozen", real_freeze)
    reopened = State.reopen("experiment", state.root.parent, state.run_id, session_id="owner")
    reopened.hook_state["node_inputs"] = state.hook_state["node_inputs"]
    second = asyncio.run(finish._finish(reopened, outcome="failed", summary="再来"))
    assert second["status"] == "success" and second["reused_manifest"] is True, second
    assert second["delivery"]["summary"] == "没做成"
    assert second["artifact_ids"] == ids
    assert _trio_counts(reopened) == {t: 1 for t in TRIO}
    assert [r["kind"] for r in ledger.open_ledger(reopened).records()].count("finish") == 1


# 4 ────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("artifact_type", TRIO)
@pytest.mark.parametrize("kind", ["operation", "scientific"])
def test_generic_save_cannot_mint_finish_owned_triplet(tmp_path, artifact_type, kind):
    state = _state(tmp_path, {"experiment_spec": "x", "required_outputs": []})
    if kind == "operation":
        state.hook_state["node_inputs"].update({"prereg_assignment": {"kind": "none", "reason": "x"},
                                                "scope": "operation", "operation_category": "toolchain_build"})
    else:
        state.hook_state["node_inputs"]["prereg_assignment"] = _bound_prereg(state)
    _turn(state, 1)
    assert ledger.bootstrap_phase(state) == "ready"
    refused = asyncio.run(run_save_gate(state, {"type": artifact_type, "name": "forged", "content": "{}"}))
    assert refused is not None, "通用 save 必须在创建之前被拒"
    blob = json.dumps(refused, ensure_ascii=False)
    assert "finish" in blob and "record_operation_completion" not in blob and "classify_experiment_scope" not in blob
    assert _trio_counts(state)[artifact_type] == 0
    if kind == "operation":  # 正控制：真正的 finish 仍能造出三件
        out = asyncio.run(finish._finish(state, outcome="failed", summary="x"))
        assert out["status"] == "success" and _trio_counts(state) == {t: 1 for t in TRIO}


# 5 ────────────────────────────────────────────────────────────────────────────
def test_typed_bound_prereg_drives_every_scope_consumer_to_scientific(tmp_path):
    from nodes.experiment.tools.run_contract import load_execution_mode_view, load_run_contract
    state = _state(tmp_path, {"experiment_spec": "验证 H1", "required_outputs": []})
    state.hook_state["node_inputs"]["prereg_assignment"] = _bound_prereg(state)
    est = asyncio.run(run_bootstrap.establish(state))
    assert est["scope"]["scope"] == "scientific"
    assert finish._run_scope(state) == "scientific"
    assert load_execution_mode_view(state)["mode"] == "scientific"
    assert load_run_contract(state).get("execution_mode") != "operational"
    for t in TRIO:
        assert asyncio.run(run_save_gate(state, {"type": t, "name": "x", "content": "{}"})) is not None
    out = asyncio.run(finish._finish(state, outcome="failed", summary="x"))
    assert out["status"] == "success", out
    assert out["delivery"]["scope"] == "scientific"
    assert out["delivery"]["formal_input"]["status"] == "valid"
    assert out["delivery"]["outcome"] == "failed"


# 6 ────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("inputs", [
    {"prereg_assignment": {"kind": "none", "reason": "x"}, "prereg_artifact_id": "prereg-legacy", "prereg_version": "1",
     "scope": "operation", "operation_category": "toolchain_build"},
    {"prereg_artifact_id": "prereg-legacy", "prereg_version": "1"},
], ids=["mixed", "legacy_only"])
def test_mixed_and_legacy_prereg_become_ready_with_a_durable_scientific_blocker(tmp_path, inputs):
    from nodes.experiment.tools.run_contract import load_execution_mode_view
    state = _state(tmp_path, {**inputs, "experiment_spec": "x",
                              "required_outputs": [{"id": "f", "kind": "file", "path": "outputs/experiment/runtime/f"}]})
    msgs = _turn(state, 1)
    assert ledger.bootstrap_phase(state) == "ready"
    authority = run_bootstrap.load_run_authority(state)
    expected = "mixed_prereg_input_unsupported" if "prereg_assignment" in inputs else "legacy_prereg_input_unsupported"
    assert authority["scope"] == "scientific"
    assert authority["startup_blocker"]["code"] == expected
    assert authority["formal_input_authority"] == "unavailable"
    assert msgs and expected in msgs[0].content
    # compatibility receipt 不会从非法输入伪造；canonical A 在 ledger 中如实是 scientific。
    assert load_execution_mode_view(state).get("classified") is not True
    diagnostic = asyncio.run(safe_bash._safe_write_file(
        state, path=str(_roots(state)["run_root"] / "f"), content="x"))
    assert diagnostic.get("status") == "success", diagnostic  # scope-independent，不全局锁死
    assert _turn(state, 2) == []  # ready 是 durable 的，不重复 bootstrap


# 7 ────────────────────────────────────────────────────────────────────────────
def test_finish_is_the_only_writer_and_reentry_reads_the_same_triplet(tmp_path):
    state = _state(tmp_path, {"prereg_assignment": {"kind": "none", "reason": "x"}, "scope": "operation",
                              "operation_category": "toolchain_build", "experiment_spec": "x", "required_outputs": []})
    _turn(state, 1)
    for t in TRIO:
        assert asyncio.run(run_save_gate(state, {"type": t, "name": "forged", "content": "{}"})) is not None
    first = asyncio.run(finish._finish(state, outcome="failed", summary="没做成"))
    assert first["status"] == "success" and first["outcome"] == "failed"
    again = asyncio.run(finish._finish(state, outcome="failed", summary="没做成"))
    assert again["status"] == "success" and again["reused_manifest"] is True
    assert again["artifact_ids"] == first["artifact_ids"]
    assert again["delivery"] == first["delivery"]
    assert _trio_counts(state) == {t: 1 for t in TRIO}
    for t in TRIO:
        md = state.read_artifact(first["artifact_ids"][t])["metadata"]
        assert md["delivery_owner"] == "finish" and md["delivery_id"] == first["closure_id"]
        assert md["artifact_role"] == t
