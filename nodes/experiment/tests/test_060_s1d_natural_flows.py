"""060 S1d-B：三条自然流程 + 真实 loader 的模型面测试（30 号任务书）。

真实工具调用链，不 monkeypatch 被测边界：bootstrap → 工具 → finish。
"""
from __future__ import annotations

import asyncio
import json
import re
import shlex
from pathlib import Path

import pytest
import yaml

from core.loader import load_harness
from core.loop_hooks import HookContext, get_loop_hook
from core.state import State
from core.tool_registry import _REGISTRY
from nodes.experiment.tools import action_boundary, delivery_transaction as tx, finish, ledger, resource_manager as rm, run_bootstrap, safe_bash
from nodes.experiment.tools.path_roles import collect_path_roles

V2_PROCEDURAL = {"classify_experiment_scope", "declare_execution_route", "declare_execution_envelope",
                 "verify_external_job_execution", "finalize_external_job", "reconcile_external_submission",
                 "record_operation_completion", "preview_experiment_contract", "supersede_closure_draft",
                 "supersede_experiment_log", "record_unknown_orphan", "resolve_unknown_orphan",
                 "run_node"}  # preflight_build_resources 是纯建议（从不阻塞），留在自然循环里
BANNED = re.compile(r"\broute\b|route_step_id|\battempt\b|\breceipt\b|\bamend|verify_external_job_execution"
                    r"|finalize_external_job|recovery_basis|analysis_eligible|classify", re.I)
V2_TRANSCRIPT = re.compile(r"route|receipt|attempt|finalize_external_job|verify_external_job|classify", re.I)


def _roots(state):
    return {r.role: Path(r.path) for r in collect_path_roles(state) if r.writable and not r.container_only}


def _fixture_path(state, path: str) -> str:
    """Fixture layout labels name real role roots, not tool-relative paths."""
    for prefix, role in (("outputs/experiment/build/", "build_root"),
                         ("outputs/experiment/runtime/", "run_root")):
        if path.startswith(prefix):
            return str(_roots(state)[role] / path[len(prefix):])
    return path


def _v3_state(tmp_path: Path, required: list[dict], spec: str) -> State:
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    required = [dict(row) for row in required]
    for row in required:
        if "path" in row:
            row["path"] = _fixture_path(state, row["path"])
        if row.get("kind") == "check" and str(row.get("command", "")).startswith("outputs/experiment/"):
            row["command"] = shlex.quote(_fixture_path(state, row["command"]))
    state.hook_state["node_inputs"] = {
        "prereg_assignment": {"kind": "none", "reason": "本地 operation"},
        "scope": "operation", "operation_category": "toolchain_build",
        "experiment_spec": spec, "required_outputs": required}
    est = asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", est
    return state


def _transcript_events(state):
    p = Path(state.root) / "transcript.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _sandbox_ok(out):
    return not (out.get("status") != "success" and "sandbox" in str(out.get("error", "")).lower())


# ── 流程 1：本地 build operation ────────────────────────────────────────────

def test_flow1_local_build_runs_naturally_and_closes_success(tmp_path):
    state = _v3_state(tmp_path,
                      [{"id": "bin", "kind": "file", "path": "outputs/experiment/build/hello.sh"},
                       {"id": "smoke", "kind": "check", "command": "outputs/experiment/build/hello.sh",
                        "expect_stdout_contains": "HF_SMOKE_OK"}],
                      "在 build 根里写一个最小脚本并跑一次")
    build = _roots(state)["build_root"]
    w = asyncio.run(safe_bash._safe_write_file(state, path=str(build / "hello.sh"),
                                               content="#!/bin/sh\necho HF_SMOKE_OK\n"))
    assert w.get("status") == "success", w
    r = asyncio.run(safe_bash._safe_run_bash(state, cmd=f"chmod +x {build}/hello.sh && {build}/hello.sh",
                                             cwd=str(build), timeout=60))
    if not _sandbox_ok(r):
        pytest.skip(str(r.get("error"))[:120])
    assert r.get("status") == "success" and "HF_SMOKE_OK" in (r.get("stdout_tail") or ""), r
    out = asyncio.run(finish._finish(state, outcome="success", summary="写了脚本并跑通 smoke",
                                     deliverables=[{"path": str(build / "hello.sh"), "obligation_id": "bin"}]))
    assert out["status"] == "success" and out["outcome"] == "success", out
    assert out["required_satisfied"] == "2/2" and out["gaps"] == [] and out["contradictions"] == []
    # 每项 deliverable 追得到 action observation
    rows = ledger.open_ledger(state).records()
    obs = ledger.project_output_observations(rows)
    assert any(str(build / "hello.sh") in (o.get("created") or []) for o in obs)
    # 没有 v2 tool/route 事件、没有拒绝
    ev = "\n".join(e.get("event", "") for e in _transcript_events(state))
    assert not re.search(r"route_step|execution_route|declared_route|route_binding", ev), ev
    kinds = [x["kind"] for x in rows]
    assert "finish_refused" not in kinds
    # 三件一次冻结
    for t in ("raw_results", "clean_results", "experiment_log"):
        assert state.read_artifact(out["artifact_ids"][t])["metadata"]["frozen"] is True



def test_flow1b_a_deliverable_rewritten_outside_any_action_is_not_attributed(tmp_path):
    """谎言形状：action 产出的文件事后被无记录地改写（手改、旁路进程），仍拿 action 当产地。
    finish 只认冻结在 action 收尾时的身份；身份变了就是 identity_changed，不能 success。"""
    state = _v3_state(tmp_path,
                      [{"id": "bin", "kind": "file", "path": "outputs/experiment/build/hello.sh"}],
                      "写一个最小脚本")
    build = _roots(state)["build_root"]
    w = asyncio.run(safe_bash._safe_write_file(state, path=str(build / "hello.sh"),
                                               content="#!/bin/sh\necho HF_SMOKE_OK\n"))
    assert w.get("status") == "success", w
    (build / "hello.sh").write_text("#!/bin/sh\necho REWRITTEN_WITHOUT_AN_ACTION\n")  # 旁路改写
    out = asyncio.run(finish._finish(state, outcome="success", summary="写了脚本",
                                     deliverables=[{"path": str(build / "hello.sh"), "obligation_id": "bin"}]))
    assert out["status"] != "success" or out["outcome"] != "success", out
    gaps = json.dumps(out.get("gaps") or [], ensure_ascii=False)
    assert "identity_changed_since_the_action" in gaps, out


def test_flow1c_a_real_build_command_on_a_v3_run_is_not_stopped_by_the_v2_build_gate(tmp_path):
    """v2 的 build gate（侦察 / declared_route 回退）是路线准入仪式；v3 只有边界、沙箱、授权。
    `make` 在没有 Makefile 的 build 根里跑，应当是一次 settled 的失败 action，而不是 build_gate 的拒绝。"""
    state = _v3_state(tmp_path, [{"id": "bin", "kind": "file", "path": "outputs/experiment/build/a.out"}],
                      "构建一个最小程序")
    build = _roots(state)["build_root"]
    r = asyncio.run(safe_bash._safe_run_bash(state, cmd="make -j2 all", cwd=str(build), timeout=60))
    if not _sandbox_ok(r):
        pytest.skip(str(r.get("error"))[:120])
    assert r.get("error_code") not in {"build_gate", "build_gate_unavailable", "build_contract_required",
                                       "declared_route_required", "source_recon_required"}, r
    assert r.get("returncode") not in (None, 0), r  # 真跑了：没有 Makefile 就失败
    ev = "\n".join(e.get("event", "") for e in _transcript_events(state))
    assert "build_gate" not in ev, ev
    rows = ledger.open_ledger(state).records()
    assert [x["kind"] for x in rows if x["kind"] in ("action_started", "action_settled")] == ["action_started", "action_settled"]


def test_scientific_success_needs_task_obligations_but_honest_failure_uses_the_neutral_delivery(tmp_path):
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    state.hook_state["node_inputs"] = {"prereg_assignment": _bound_prereg(state, "prereg-h1"),
                                       "experiment_spec": "验证预注册假设 H1", "required_outputs": []}
    est = asyncio.run(run_bootstrap.establish(state))
    assert est["scope"]["scope"] == "scientific" and ledger.bootstrap_phase(state) == "ready", est
    unsupported = asyncio.run(finish._finish(state, outcome="success", summary="x"))
    assert unsupported.get("status") == "success_unsupported", unsupported
    assert any(row.get("gap") == "task_verification_missing" for row in unsupported["gaps"]), unsupported
    assert not {a.get("type") for a in state.list_artifacts()} & set(tx.TRIO)
    honest = asyncio.run(finish._finish(state, outcome="failed", summary="x"))
    assert honest["status"] == "success", honest
    assert honest["delivery"]["scope"] == "scientific"
    assert honest["delivery"]["formal_input"]["status"] == "valid"
    assert honest["delivery"]["outcome"] == "failed"


# ── Codex 36 P0-D / P1：finish 只消费 ready 的 A；scope 不默认；中途冻结失败可恢复；重入读回完整 ──

def test_finish_refuses_when_bootstrap_is_only_half_done(tmp_path, monkeypatch):
    from nodes.experiment.tools import obligations
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    state.hook_state["node_inputs"] = {"prereg_assignment": {"kind": "none", "reason": "x"}, "scope": "operation",
                                       "operation_category": "toolchain_build", "experiment_spec": "x",
                                       "required_outputs": [{"id": "f", "kind": "file", "path": "outputs/experiment/runtime/f"}]}
    monkeypatch.setattr(obligations, "freeze", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "initializing"
    out = asyncio.run(finish._finish(state, outcome="failed", summary="x"))
    assert out.get("error_code") == "bootstrap_incomplete", out
    assert not {a.get("type") for a in state.list_artifacts()} & {"raw_results", "clean_results", "experiment_log"}
    assert not ledger.open_ledger(state).root.joinpath("obligations.json").exists()


def test_finish_does_not_default_an_undecided_scope_to_operation(tmp_path):
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    state.hook_state["node_inputs"] = {"prereg_assignment": {"kind": "none", "reason": "x"}, "experiment_spec": "做点什么",
                                       "required_outputs": [{"id": "f", "kind": "file", "path": "outputs/experiment/runtime/f"}]}
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready"
    # 拒绝的依据是 required 产物没生成，不是缺 scope；也不默认 operation。
    out = asyncio.run(finish._finish(state, outcome="success", summary="x"))
    assert out.get("status") == "success_unsupported", out
    assert out["gaps"] and out["gaps"][0]["obligation"] == "f"
    assert any(item["outcome"] == "blocked" for item in out.get("reachable_outcomes", []))
    # 诚实的非成功不需要 scope：三件套照样冻结，scope 以 typed unknown 写入
    honest = asyncio.run(finish._finish(state, outcome="failed", summary="没做成"))
    assert honest["status"] == "success" and honest["outcome"] == "failed", honest
    delivery = honest["delivery"]
    assert delivery["scope"] == "unknown" and delivery["scope_status"] == "undecided"


def test_a_scope_decided_later_by_the_caller_is_resolved_and_recorded_not_overridden_by_the_first_record(tmp_path):
    """Codex 45 P0-A：第一条 bootstrap 记录不能永远压住后续合法解析；resolver 只有一个（derive_scope）。"""
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    state.hook_state["node_inputs"] = {"prereg_assignment": {"kind": "none", "reason": "x"}, "experiment_spec": "做点什么",
                                       "required_outputs": [{"id": "f", "kind": "file", "path": str(_roots(state)["run_root"] / "f")}]}
    asyncio.run(run_bootstrap.establish(state))
    assert [r for r in ledger.open_ledger(state).records() if r["kind"] == "run_bootstrap"][0]["scope"] is None
    state.hook_state["node_inputs"]["scope"] = "operation"  # 调用方补了 scope（authoritative 输入）
    resolved = asyncio.run(run_bootstrap.establish(state))
    assert resolved["scope"]["scope"] == "operation"
    target = _roots(state)["run_root"] / "f"
    asyncio.run(safe_bash._safe_write_file(state, path=str(target), content="ok"))
    out = asyncio.run(finish._finish(state, outcome="success", summary="x", deliverables=[{"path": str(target), "obligation_id": "f"}]))
    assert out["status"] == "success" and out["outcome"] == "success", out
    kinds = [r["kind"] for r in ledger.open_ledger(state).records()]
    assert "scope_resolved" in kinds
    assert out["delivery"]["scope"] == "operation"


def test_a_bound_prereg_beats_a_declared_operation_scope_and_generic_saves_of_the_trio_are_refused(tmp_path):
    """Codex 45 P0-C 复现链：prereg_artifact_id 输入 + scope=operation 声明 → 唯一 resolver 判 scientific；
    v3 run 的 raw/clean/log 通用 save 被 closure owner 拒，不看旧 mode。"""
    from shared.tools.library.artifacts_extra import run_save_gate
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    # 45 号复现链用的是 legacy 键；48 号起 legacy 键直接大声失败（见上面的参数化测试）。
    # 这里用 typed bound prereg + 声明 scope=operation：唯一 resolver 仍判 scientific，不看声明。
    state.hook_state["node_inputs"] = {"prereg_assignment": _bound_prereg(state, "prereg-1"),
                                       "scope": "operation", "experiment_spec": "验证 H1", "required_outputs": []}
    est = asyncio.run(run_bootstrap.establish(state))
    assert est["scope"]["scope"] == "scientific", est["scope"]
    for t in ("raw_results", "clean_results", "experiment_log"):
        refused = asyncio.run(run_save_gate(state, {"type": t, "name": f"x_{t}", "content": "{}"}))
        assert refused is not None and "finish" in json.dumps(refused, ensure_ascii=False), (t, refused)
    assert not {a.get("type") for a in state.list_artifacts()} & {"raw_results", "clean_results", "experiment_log"}
    out = asyncio.run(finish._finish(state, outcome="failed", summary="x"))
    assert out["status"] == "success", out
    assert out["delivery"]["scope"] == "scientific"
    assert out["delivery"]["formal_input"]["status"] == "valid"
    assert out["delivery"]["outcome"] == "failed"


def test_bootstrap_replays_itself_next_turn_until_ready_without_inventing_a_repair_action(tmp_path, monkeypatch):
    """Codex 45 P0-B：turn 1 冻结失败 → initializing；hook 下一轮重放同一个幂等 establish → ready。"""
    from nodes.experiment.tools import obligations
    from core.loop_hooks import HookContext
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    state.hook_state["node_inputs"] = {"prereg_assignment": {"kind": "none", "reason": "x"}, "scope": "operation",
                                       "operation_category": "toolchain_build", "experiment_spec": "x",
                                       "required_outputs": [{"id": "f", "kind": "file", "path": "outputs/experiment/runtime/f"}]}
    real = obligations.freeze
    calls = {"n": 0}
    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        return real(*a, **k)
    monkeypatch.setattr(obligations, "freeze", flaky)
    hook = run_bootstrap.experiment_v3_bootstrap
    m1 = hook.on_turn_start(HookContext(harness=None, state=state, messages=[], turn=1)) or []
    assert ledger.bootstrap_phase(state) == "initializing"
    assert m1 and "启动未完成" in m1[0].content
    refused = asyncio.run(safe_bash._safe_write_file(state, path=str(_roots(state)["run_root"] / "f"), content="x"))
    assert refused.get("error_code") == "bootstrap_incomplete", refused
    assert "repair_bootstrap" not in json.dumps(refused)
    m2 = hook.on_turn_start(HookContext(harness=None, state=state, messages=[], turn=2)) or []
    assert ledger.bootstrap_phase(state) == "ready", m2
    assert m2 and "启动完成" in m2[0].content
    m3 = hook.on_turn_start(HookContext(harness=None, state=state, messages=[], turn=3))
    assert not m3  # ready 之后零字
    ok = asyncio.run(safe_bash._safe_write_file(state, path=str(_roots(state)["run_root"] / "f"), content="x"))
    assert ok.get("status") == "success", ok


def test_a_python_boundary_refusal_is_attributed_to_safe_execute_python(tmp_path, monkeypatch):
    """Codex 45 P1：边界拒绝 / 超时的 provenance 也要记 safe_execute_python，不是 safe_run_bash。"""
    from nodes.experiment.tools import obligations
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    state.hook_state["node_inputs"] = {"prereg_assignment": {"kind": "none", "reason": "x"}, "scope": "operation",
                                       "operation_category": "toolchain_build", "experiment_spec": "x",
                                       "required_outputs": [{"id": "f", "kind": "file", "path": "outputs/experiment/runtime/f"}]}
    monkeypatch.setattr(obligations, "freeze", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "initializing"
    r = asyncio.run(safe_bash._safe_execute_python(state, code="print(1)", timeout=30))
    assert r.get("error_code") == "bootstrap_incomplete", r
    assert "safe_execute_python" in str(r.get("error")) and "safe_run_bash" not in str(r.get("error")), r


def test_a_closure_whose_first_freeze_failed_halfway_is_recoverable_not_reminted(tmp_path, monkeypatch):
    state = _v3_state(tmp_path, [{"id": "bin", "kind": "file", "path": "outputs/experiment/build/hello.sh"}], "写脚本")
    build = _roots(state)["build_root"]
    asyncio.run(safe_bash._safe_write_file(state, path=str(build / "hello.sh"), content="#!/bin/sh\necho ok\n"))
    clean_id = tx.artifact_id("clean_results", tx.deterministic_names(state)["clean_results"])
    real_freeze = state.mark_frozen
    calls = {"n": 0}

    def flaky_freeze(artifact_id, *args, **kwargs):
        if artifact_id == clean_id and calls["n"] == 0:  # raw 已冻，clean 失败
            calls["n"] += 1
            raise OSError("simulated clean freeze failure")
        return real_freeze(artifact_id, *args, **kwargs)

    monkeypatch.setattr(state, "mark_frozen", flaky_freeze)
    with pytest.raises(OSError, match="clean freeze failure"):
        asyncio.run(finish._finish(state, outcome="success", summary="x",
                                   deliverables=[{"path": str(build / "hello.sh"), "obligation_id": "bin"}]))
    manifest = tx.read_manifest(state)
    raw_id_first = manifest["artifacts"]["raw_results"]["id"]
    monkeypatch.setattr(state, "mark_frozen", real_freeze)
    # 重入：从同一 manifest 补冻，不重铸 raw；交付内容仍是第一次 proposal。
    second = asyncio.run(finish._finish(state, outcome="failed", summary="ignored on recovery"))
    assert second["status"] == "success" and second["reused_manifest"] is True, second
    assert second["delivery"]["outcome"] == "success"
    assert second["artifact_ids"]["raw_results"] == raw_id_first
    assert len(state.list_artifacts("raw_results", own_only=True)) == 1
    for t in tx.TRIO:
        assert state.read_artifact(second["artifact_ids"][t])["metadata"]["frozen"] is True
    # The pending transaction ignored recovery arguments above. Once sealed,
    # replay means the original complete request, not a new delivery claim.
    third = asyncio.run(finish._finish(state, outcome="success", summary="x",
                                      deliverables=[{"path": str(build / "hello.sh"), "obligation_id": "bin"}]))
    assert third["status"] == "success" and third["reused_manifest"] is True
    assert third["artifact_ids"] == second["artifact_ids"]
    assert [r["kind"] for r in ledger.open_ledger(state).records()].count("finish") == 1


def test_reentry_reads_back_declared_outcome_and_blocker(tmp_path):
    state = _v3_state(tmp_path, [{"id": "bin", "kind": "file", "path": "outputs/experiment/build/hello.sh"}], "写脚本")
    blocker = {"blocker_id": "planned_cancellation", "summary": "用户要求停止"}
    first = asyncio.run(finish._finish(state, outcome="cancelled", summary="按计划停止", blocker=blocker))
    assert first["status"] == "success" and first["delivery"]["outcome"] == "cancelled", first
    again = asyncio.run(finish._finish(state, outcome="cancelled", summary="按计划停止", blocker=blocker))
    assert again["status"] == "success" and again["reused_manifest"] is True, again
    assert again["delivery"] == first["delivery"]
    assert again["delivery"]["blocker"] == blocker
    # A different honest claim is not a replay: preserve cancelled v1 while
    # recording the amended failed explanation as v2 of the same identities.
    corrected = asyncio.run(finish._finish(state, outcome="failed", summary="任务未完成，交付已有失败事实"))
    assert corrected["status"] == "success" and corrected["revision"] == 2, corrected
    assert corrected["delivery"]["outcome"] == "failed"
    assert corrected["artifact_ids"] == first["artifact_ids"]
    archived = json.loads(tx.history_manifest_path(state, 1).read_text(encoding="utf-8"))
    assert archived["delivery"] == first["delivery"]
    assert archived["delivery"]["blocker"] == blocker


# ── Codex 36 P0-C：机器可读的下一动作不再点名退役工具 ───────────────────────────

def test_cancel_of_an_already_ended_job_points_the_machine_field_at_finish():
    from nodes.experiment.tools import resource_manager as rm
    record = {"scheduler": "slurm", "job_id": "9001"}
    # B 只结算事实，不替 C 选 outcome：NODE_FAIL + exit 0 不能被建议成 success
    odd = rm._cancel_already_ended_response(record, lifecycle_status="open", observed={"state": "NODE_FAIL", "exit_code": 0})
    assert odd["next_tool"]["name"] == "finish", odd
    args = odd["next_tool"]["arguments"]
    assert args["outcome"] not in {"success", "failed", "partial"}  # 留给模型按任务判断
    assert args["terminal_facts"]["state"] == "NODE_FAIL" and args["terminal_facts"]["exit_code"] == 0
    assert "9001" in args["summary"]


# ── Codex 44：工作计划是模型自己的，缺失不阻断；恢复时带回并标注非权威 ─────────

def test_a_missing_working_plan_never_blocks_an_action_or_an_honest_finish(tmp_path):
    state = _v3_state(tmp_path, [{"id": "bin", "kind": "file", "path": "outputs/experiment/build/hello.sh"}], "写脚本")
    assert not str(getattr(state, "scratchpad", "") or "").strip()  # 白板是空的：没有计划
    build = _roots(state)["build_root"]
    w = asyncio.run(safe_bash._safe_write_file(state, path=str(build / "hello.sh"), content="#!/bin/sh\necho ok\n"))
    assert w.get("status") == "success", w
    out = asyncio.run(finish._finish(state, outcome="failed", summary="没跑通"))
    assert out.get("status") == "success" and out.get("outcome") == "failed", out
    for r in (w, out):  # 没有任何"缺计划"形状的拒绝或缺口
        blob = json.dumps({k: r.get(k) for k in ("error", "error_code", "gaps", "contradictions", "reason")}, ensure_ascii=False)
        assert "plan" not in blob.lower() and "计划" not in blob, blob


def test_the_one_pager_brings_back_the_models_own_plan_labelled_as_non_authoritative(tmp_path):
    from core import whiteboard
    state = _v3_state(tmp_path, [{"id": "bin", "kind": "file", "path": "outputs/experiment/build/hello.sh"}], "多阶段构建")
    whiteboard.write(state, "阶段 1 配置 → 阶段 2 构建 → 阶段 3 smoke；失败则回到阶段 1", turn=3)
    page = run_bootstrap.one_page(state, asyncio.run(run_bootstrap.establish(state)))
    assert "阶段 2 构建" in page
    assert "你自己的工作假设，不是框架事实" in page
    assert not re.search(r"declared_route|route_step_id|\battempt\b|\breceipt\b", page), page


def _bound_prereg(state, name: str, *, freeze: bool = True, hash_override: str | None = None) -> dict:
    """typed prereg_assignment（kind=bound）指向一份真实（默认冻结）的 pre_registration artifact。"""
    from core.ledger import record_version, sha256_text
    saved = state.save_artifact("pre_registration", name, f"# {name}\n", metadata={"execution_mode": "scientific"})
    if freeze:
        state.mark_frozen(saved["id"])
    versions = state.artifact_versions(saved["id"])
    version = record_version(versions[-1])
    content_hash = hash_override or str(versions[-1].get("content_hash") or sha256_text(f"# {name}\n"))
    return {"kind": "bound", "artifact_id": saved["id"], "version": version, "content_hash": content_hash}


def _operation_inputs(spec: str = "x", required=None) -> dict:
    return {"prereg_assignment": {"kind": "none", "reason": "本地任务"}, "scope": "operation",
            "operation_category": "toolchain_build", "experiment_spec": spec,
            "required_outputs": required if required is not None else [{"id": "f", "kind": "file", "path": "outputs/experiment/runtime/f"}]}


# ── Codex 48 §1：typed prereg 是唯一形状；不匹配 / 未冻结 / legacy 在消费前就失败 ─────────

@pytest.mark.parametrize("shape,expect", [
    ("legacy", "legacy_prereg_input_unsupported"),
    ("hash_mismatch", "prereg_content_hash_mismatch"),
    ("unfrozen", "prereg_not_frozen"),
    ("unknown_kind", "prereg_assignment_invalid"),
])
def test_an_invalid_prereg_input_is_a_ready_durable_scientific_blocker(tmp_path, shape, expect):
    from core.loop_hooks import HookContext
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    if shape == "legacy":
        inputs = {"prereg_artifact_id": "prereg-legacy", "prereg_version": "1", "scope": "operation"}
    elif shape == "hash_mismatch":
        inputs = {"prereg_assignment": _bound_prereg(state, "p", hash_override="0" * 64)}
    elif shape == "unfrozen":
        inputs = {"prereg_assignment": _bound_prereg(state, "p", freeze=False)}
    else:
        inputs = {"prereg_assignment": {"kind": "frozen", "artifact_id": "x"}}
    state.hook_state["node_inputs"] = {**inputs, "experiment_spec": "验证 H1", "required_outputs": []}
    msgs = run_bootstrap.experiment_v3_bootstrap.on_turn_start(HookContext(harness=None, state=state, messages=[], turn=1)) or []
    assert ledger.bootstrap_phase(state) == "ready"
    authority = run_bootstrap.load_run_authority(state)
    assert authority["scope"] == "scientific"
    assert authority["startup_blocker"]["code"] == expect
    assert authority["formal_input_authority"] == "unavailable"
    assert msgs and expect in msgs[0].content, msgs
    handle = action_boundary.begin(state, tool="diagnostic_probe", effect_class=action_boundary.SUPPORT)
    assert isinstance(handle, action_boundary.ActionHandle), handle
    assert action_boundary.settle(state, handle, outcome="completed")["status"] == "recorded"
    diagnostic = asyncio.run(safe_bash._safe_write_file(
        state, path=str(_roots(state)["run_root"] / "f"), content="x"))
    assert diagnostic.get("status") == "success", diagnostic
    out = asyncio.run(finish._finish(state, outcome="failed", summary="x"))
    assert out["status"] == "success", out
    delivery = out["delivery"]
    assert delivery["scope"] == "scientific" and delivery["outcome"] == "failed"
    assert delivery["formal_input"]["binding"] is None
    assert delivery["formal_input"]["error"]["code"] == expect
    assert set(delivery["formal_input"]["error"]) == {"code", "reason"}
    assert {a.get("type") for a in state.list_artifacts()} >= set(tx.TRIO)


def test_scope_unresolved_does_not_add_a_classification_ritual_to_actions(tmp_path):
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    state.hook_state["node_inputs"] = {"experiment_spec": "做点什么",
                                       "required_outputs": [{"id": "f", "kind": "file", "path": "outputs/experiment/runtime/f"}]}
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready"
    root = _roots(state)["run_root"]
    for i in range(2):
        w = asyncio.run(safe_bash._safe_write_file(state, path=str(root / f"f{i}"), content="x"))
        assert w.get("status") == "success", w
    rows = ledger.open_ledger(state).records()
    assert not any(r["kind"] == "scope_clarification_offered" for r in rows)
    from nodes.experiment.tools import ledger_hooks
    view = ledger_hooks.build_delta_view(state)
    assert "scope：未定" in view
    assert "success 收尾不可达" not in view
    assert "不要求补分类手续" in view


# ── Codex 48 §2/§3 fault injection：bootstrap 每个写点、closure 每一步后失败，重入收敛不重复 ──

@pytest.mark.parametrize("point", ["obligations", "envelope", "ready_marker"])
def test_bootstrap_converges_after_a_failure_at_every_write_point(tmp_path, monkeypatch, point):
    from nodes.experiment.tools import obligations, remote_admission
    from core.loop_hooks import HookContext
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    state.hook_state["node_inputs"] = _operation_inputs()
    targets = {"obligations": (obligations, "freeze"), "envelope": (remote_admission, "freeze_envelope"),
               "ready_marker": (ledger, "mark_bootstrap_ready")}
    mod, name = targets[point]
    real = getattr(mod, name); calls = {"n": 0}
    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(f"injected at {point}")
        return real(*a, **k)
    monkeypatch.setattr(mod, name, flaky)
    hook = run_bootstrap.experiment_v3_bootstrap
    hook.on_turn_start(HookContext(harness=None, state=state, messages=[], turn=1))
    assert ledger.bootstrap_phase(state) == "initializing", point
    hook.on_turn_start(HookContext(harness=None, state=state, messages=[], turn=2))
    hook.on_turn_start(HookContext(harness=None, state=state, messages=[], turn=3))  # 多余的重入也收敛
    assert ledger.bootstrap_phase(state) == "ready", point
    rows = ledger.open_ledger(state).records()
    kinds = [r["kind"] for r in rows]
    assert kinds.count("bootstrap_ready") == 1 and kinds.count("obligations_frozen") == 1 and kinds.count("envelope_frozen") == 1, kinds
    assert len(obligations.frozen(state)) >= 1
    ob_file = ledger.ledger_dir(state) / "obligations.json"
    assert ob_file.exists() and not ob_file.with_suffix(".tmp").exists()
    # 之后一切正常
    w = asyncio.run(safe_bash._safe_write_file(state, path=str(_roots(state)["run_root"] / "f"), content="x"))
    assert w.get("status") == "success", w


@pytest.mark.parametrize("step", ["save:raw_results", "save:clean_results", "save:experiment_log",
                                  "freeze:raw_results", "freeze:clean_results", "freeze:experiment_log"])
def test_closure_converges_after_a_failure_at_every_step(tmp_path, monkeypatch, step):
    state = _v3_state(tmp_path, [{"id": "bin", "kind": "file", "path": "outputs/experiment/build/hello.sh"}], "写脚本")
    build = _roots(state)["build_root"]
    asyncio.run(safe_bash._safe_write_file(state, path=str(build / "hello.sh"), content="#!/bin/sh\necho ok\n"))
    kind, target = step.split(":")
    if kind == "save":
        real_save = state.save_artifact; hit = {"n": 0}
        def flaky_save(artifact_type, name, content, **kw):
            if artifact_type == target and hit["n"] == 0:
                hit["n"] += 1
                raise OSError(f"injected at save {target}")
            return real_save(artifact_type, name, content, **kw)
        monkeypatch.setattr(state, "save_artifact", flaky_save)
        with pytest.raises(OSError):
            asyncio.run(finish._finish(state, outcome="success", summary="x",
                                       deliverables=[{"path": str(build / "hello.sh"), "obligation_id": "bin"}]))
    else:
        real_freeze = state.mark_frozen; hit = {"n": 0}
        target_id = tx.artifact_id(target, tx.deterministic_names(state)[target])
        def flaky_freeze(artifact_id, *args, **kwargs):
            if artifact_id == target_id and hit["n"] == 0:
                hit["n"] += 1
                raise OSError(f"injected at freeze {target}")
            return real_freeze(artifact_id, *args, **kwargs)
        monkeypatch.setattr(state, "mark_frozen", flaky_freeze)
        with pytest.raises(OSError, match=f"freeze {target}"):
            asyncio.run(finish._finish(state, outcome="success", summary="x",
                                       deliverables=[{"path": str(build / "hello.sh"), "obligation_id": "bin"}]))
    ids_before = {t: a.get("id") for t, a in ((finish._read_manifest(state) or {}).get("artifacts") or {}).items() if a.get("id")}
    second = asyncio.run(finish._finish(state, outcome="success", summary="x",
                                        deliverables=[{"path": str(build / "hello.sh"), "obligation_id": "bin"}]))
    assert second["status"] == "success" and second["reused_manifest"] is True, second
    for t, i in ids_before.items():
        assert second["artifact_ids"][t] == i, (t, i, second)  # 已创建的身份不变
    for t in ("raw_results", "clean_results", "experiment_log"):
        assert len(state.list_artifacts(t, own_only=True)) == 1, t  # 没有重复草稿
        assert state.read_artifact(second["artifact_ids"][t])["metadata"]["frozen"] is True
    third = asyncio.run(finish._finish(state, outcome="success", summary="x",
                                      deliverables=[{"path": str(build / "hello.sh"), "obligation_id": "bin"}]))
    assert third["status"] == "success" and third["reused_manifest"] is True
    assert third["artifact_ids"] == second["artifact_ids"]
    assert [r["kind"] for r in ledger.open_ledger(state).records()].count("finish") == 1
# ── 流程 2：WRF-015 假成功形状 ───────────────────────────────────────────────

def test_flow2_wrapper_exit_zero_without_the_binary_cannot_be_success(tmp_path):
    state = _v3_state(tmp_path,
                      [{"id": "wrf_exe", "kind": "file", "path": "outputs/experiment/build/main/wrf.exe"}],
                      "构建 WRF 并交付 wrf.exe")
    build = _roots(state)["build_root"]
    r = asyncio.run(safe_bash._safe_run_bash(
        state, cmd=f"mkdir -p {build}/main && echo 'make: *** [wrf.exe] Error 31' >&2; exit 0",
        cwd=str(build), timeout=60))
    if not _sandbox_ok(r):
        pytest.skip(str(r.get("error"))[:120])
    assert r.get("status") == "success" and r.get("returncode") == 0  # wrapper 说成功
    first = asyncio.run(finish._finish(state, outcome="success", summary="构建完成"))
    assert first["status"] == "success_unsupported", first
    assert first["gaps"][0]["obligation"] == "wrf_exe" and first["gaps"][0]["gap"] == "missing"
    assert not state.list_artifacts("experiment_log", own_only=True)
    second = asyncio.run(finish._finish(state, outcome="failed", summary="wrapper 返回 0 但 wrf.exe 没有产出",
                                        next_step="按 make 报错修构建"))
    assert second["status"] == "success" and second["outcome"] == "failed", second
    clean = json.loads(state.read_artifact(second["artifact_ids"]["clean_results"])["content"])
    assert clean["delivery"]["gaps"][0]["obligation"] == "wrf_exe"


# ── 流程 3：受管作业生命周期 ────────────────────────────────────────────────

def test_flow3_live_job_blocks_closure_until_the_scheduler_reports_terminal(tmp_path, monkeypatch):
    """Codex 45 P0-D：终态由产品自己结算（status / health / wait / cancel 共用一个 owner），测试不手写账本。"""
    state = _v3_state(tmp_path,
                      [{"id": "log", "kind": "file", "path": "outputs/experiment/runtime/job.log"}],
                      "提交一个作业")
    run_root = _roots(state)["run_root"]
    calls: list[list[str]] = []
    phase = {"now": "running"}

    def fake_run(argv, timeout=10):
        calls.append(list(argv))
        head = argv[0]
        if head == "sbatch":
            return {"ok": True, "stdout": "777", "stderr": "", "returncode": 0}
        if head == "squeue":
            return {"ok": True, "stdout": ("777|RUNNING|0:10|node1" if phase["now"] == "running" else ""), "stderr": "", "returncode": 0}
        if head == "sacct":
            row = "777|COMPLETED|0:0|main|node1|1|1G|65|" if phase["now"] == "done" else "777|RUNNING|0:0|main|node1|1|1G|10|"
            return {"ok": True, "stdout": row + "\n", "stderr": "", "returncode": 0}
        if head == "scancel":
            return {"ok": True, "stdout": "", "stderr": "", "returncode": 0}
        return {"ok": False, "stdout": "", "stderr": f"unexpected {head}", "returncode": 1}

    monkeypatch.setattr(rm, "_run", fake_run)
    sub = rm._submit_sync(runtime_root=run_root, scheduler="slurm", command="echo hi > job.log", job_name="j",
                          mpi_ranks=1, cpus_per_rank=1, gpus=0, memory_gb=1.0, storage_gb=1.0,
                          walltime_minutes=5, queue=None, nodelist=None, image=None, workdir=str(run_root),
                          dry_run=False, namespace=None, stage_in=None, state=state)
    assert sub.get("job_id") == "777", sub
    # 工具层在 _submit_sync 成功后持久化的提交记录（这里按同一形状写；远端真实提交在 S1c 才放行）
    record = {**sub, "output_roots": sub.get("output_roots") or [str(run_root)]}
    state.save_artifact("job_submission", f"job_submission_{state.run_id}_{sub['submission_nonce']}",
                        json.dumps(record, ensure_ascii=False, default=str),
                        metadata={"scheduler": "slurm", "dry_run": False, "output_roots": record["output_roots"]})
    kinds = [x["kind"] for x in ledger.open_ledger(state).records()]
    assert kinds.index("intent") < kinds.index("job_bound")
    # 作业还在跑：读状态不结算，任何 outcome 都不能收尾
    running = asyncio.run(rm._job_status(state, "slurm", "777"))
    assert running.get("status") == "success" and "terminal_settlement" not in running, running
    for outcome in ("success", "partial", "failed"):
        out = asyncio.run(finish._finish(state, outcome=outcome, summary="试图收尾"))
        assert out["status"] == "live_external_effect", (outcome, out)
    # 作业写出产物并自然结束（sacct 正向终态）：job_status 第一次读到就结算
    (run_root / "job.log").write_text("hi\n")
    phase["now"] = "done"
    done = asyncio.run(rm._job_status(state, "slurm", "777"))
    settled = done.get("terminal_settlement") or {}
    assert settled.get("settled") is True and settled.get("derived_state") == "COMPLETED" and settled.get("exit_code") == 0, done
    rows = ledger.open_ledger(state).records()
    terminals = [r for r in rows if r["kind"] == "job_terminal"]
    assert len(terminals) == 1 and terminals[0]["external_id"] == "777"
    obs = ledger.project_output_observations(rows)
    assert any(str(run_root / "job.log") in (o.get("created") or []) for o in obs), obs
    assert ledger.open_jobs(rows) == []
    # 其余入口共用同一结算：health 与 cancel 都读到 already，不再写第二条
    health = asyncio.run(rm._check_external_job_health(state, "slurm", "777"))
    assert (health.get("terminal_settlement") or {}).get("already") is True, health
    cancel = asyncio.run(rm._cancel_job(state, "slurm", "777", reason="stop"))
    assert cancel.get("status") == "already_ended", cancel
    assert not any(c[0] == "scancel" for c in calls)
    rows = ledger.open_ledger(state).records()
    assert len([r for r in rows if r["kind"] == "job_terminal"]) == 1
    assert not any(r["kind"] == "job_cancelled" for r in rows)
    # 现在可以诚实收尾；产物追得到作业 action
    ok = asyncio.run(finish._finish(state, outcome="success", summary="作业完成",
                                    deliverables=[{"path": str(run_root / "job.log"), "obligation_id": "log"}]))
    assert ok["status"] == "success" and ok["outcome"] == "success", ok
    assert any("first durable settlement observation" in item
               for item in ok["delivery"]["limitations"])
    ev = "\n".join(e.get("event", "") for e in _transcript_events(state))
    assert "verify_external_job_execution" not in ev and "finalize_external_job" not in ev


def test_queue_absence_without_accounting_proof_is_not_settled_as_terminal(tmp_path, monkeypatch):
    """squeue 里不见了只说明调度器不再持有它；sacct 读不出就没有正向终态证据 → 不写 job_terminal，作业留在开放列表交 reconcile。"""
    state = _v3_state(tmp_path, [{"id": "log", "kind": "file", "path": "outputs/experiment/runtime/job.log"}], "提交一个作业")
    run_root = _roots(state)["run_root"]
    def fake_run(argv, timeout=10):
        if argv[0] == "sbatch":
            return {"ok": True, "stdout": "778", "stderr": "", "returncode": 0}
        if argv[0] == "squeue":
            return {"ok": True, "stdout": "", "stderr": "", "returncode": 0}
        if argv[0] == "sacct":
            return {"ok": False, "stdout": "", "stderr": "sacct: error: Problem talking to the database", "returncode": 1}
        return {"ok": False, "stdout": "", "stderr": "unexpected", "returncode": 1}
    monkeypatch.setattr(rm, "_run", fake_run)
    sub = rm._submit_sync(runtime_root=run_root, scheduler="slurm", command="echo hi", job_name="j",
                          mpi_ranks=1, cpus_per_rank=1, gpus=0, memory_gb=1.0, storage_gb=1.0,
                          walltime_minutes=5, queue=None, nodelist=None, image=None, workdir=str(run_root),
                          dry_run=False, namespace=None, stage_in=None, state=state)
    state.save_artifact("job_submission", f"job_submission_{state.run_id}_{sub['submission_nonce']}",
                        json.dumps({**sub, "output_roots": [str(run_root)]}, ensure_ascii=False, default=str),
                        metadata={"scheduler": "slurm", "dry_run": False, "output_roots": [str(run_root)]})
    st = asyncio.run(rm._job_status(state, "slurm", "778"))
    assert st.get("status") == "success" and not (st.get("terminal_settlement") or {}).get("settled"), st
    rows = ledger.open_ledger(state).records()
    assert not any(r["kind"] == "job_terminal" for r in rows)
    assert ledger.open_jobs(rows), "没有正向终态证据时作业必须留在开放列表"
    out = asyncio.run(finish._finish(state, outcome="success", summary="x"))
    assert out["status"] == "live_external_effect", out

# ── finish 幂等重入 ─────────────────────────────────────────────────────────

def test_a_second_finish_on_a_sealed_run_reads_back_instead_of_reminting(tmp_path):
    state = _v3_state(tmp_path, [{"id": "f", "kind": "file", "path": "outputs/experiment/runtime/f.txt"}], "x")
    target = _roots(state)["run_root"] / "f.txt"
    w = asyncio.run(safe_bash._safe_write_file(state, path=str(target), content="ok"))
    assert w.get("status") == "success", w
    first = asyncio.run(finish._finish(state, outcome="success", summary="done"))
    assert first["status"] == "success"
    again = asyncio.run(finish._finish(state, outcome="success", summary="done"))
    assert again["status"] == "success" and again["reused_manifest"] is True
    assert again["artifact_ids"] == first["artifact_ids"]
    assert again["delivery"] == first["delivery"]
    assert again["delivery"]["summary"] == "done"
    assert len(state.list_artifacts("experiment_log", own_only=True)) == 1


# ── cancelled 在唯一 delivery envelope 与三件 payload 中保持同一语义 ────────────

def test_cancelled_is_identical_in_result_manifest_and_all_three_payloads(tmp_path):
    state = _v3_state(tmp_path, [{"id": "f", "kind": "file", "path": "outputs/experiment/runtime/f.txt"}], "x")
    blocker = {"blocker_id": "planned_cancellation", "summary": "用户要求停止"}
    out = asyncio.run(finish._finish(
        state, outcome="cancelled", summary="按计划停", blocker=blocker,
    ))
    assert out["status"] == "success"
    delivery = out["delivery"]
    assert delivery["outcome"] == "cancelled" and delivery["blocker"] == blocker
    assert tx.read_manifest(state)["delivery"] == delivery
    raw_record = state.read_artifact(out["artifact_ids"]["raw_results"])
    md = raw_record["metadata"]
    assert md["delivery_id"] == out["closure_id"] and md["artifact_role"] == "raw_results"
    assert not ({"outcome", "platform_status", "blocker_id", "operation_closure_input"} & set(md))
    raw = json.loads(raw_record["content"])
    clean = json.loads(state.read_artifact(out["artifact_ids"]["clean_results"])["content"])
    assert raw["delivery"] == delivery and clean["delivery"] == delivery
    log = state.read_artifact(out["artifact_ids"]["experiment_log"])["content"]
    assert "outcome: `cancelled`" in log


# ── 真实 loader 的模型面 ────────────────────────────────────────────────────

def test_loader_surface_has_finish_and_no_v2_procedural_tools_and_no_banned_words():
    h = load_harness("experiment")
    assert "finish" in h.tools
    assert not (set(h.tools) & V2_PROCEDURAL), set(h.tools) & V2_PROCEDURAL
    text = h.system_prompt + "\n".join(h.rules)
    assert not BANNED.search(text), BANNED.search(text)
    for name in h.tools:
        d = _REGISTRY.tools.get(name)
        if d is None or not str(getattr(d, "allowed_node_types", None) or ["experiment"]) or name in {"save_artifact", "freeze_artifact", "dispatch_data_request", "reconcile_data_dispatch"}:
            continue  # shared 工具的描述归 framework owner，节点内交接件登记（不越界改）
        assert "route_step_id" not in (d.parameters_schema or {}).get("properties", {}), name
        if d.allowed_node_types == ["experiment"]:
            assert not BANNED.search(d.description or ""), (name, BANNED.search(d.description or "").group(0))
    assert "experiment_v3_bootstrap" in h.loop_hooks and "experiment_delta_view" in h.loop_hooks
    for gone in ("experiment_contract_audit", "external_job_handoff", "external_job_reconciliation",
                 "route_block_resolution", "sediment_closure_advisor", "secondary_experiment_log_recovery",
                 "execution_supervisor", "execution_control", "turn_one_briefing"):
        assert gone not in h.loop_hooks, gone


def test_turn_one_injects_only_the_one_page_briefing(tmp_path):
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="o")
    state.hook_state["node_inputs"] = {"prereg_assignment": {"kind": "none", "reason": "x"},
                                       "scope": "operation", "operation_category": "toolchain_build",
                                       "experiment_spec": "构建 hello",
                                       "required_outputs": [{"id": "b", "kind": "file", "path": "outputs/experiment/build/hello"}]}
    h = load_harness("experiment")
    hook = get_loop_hook("experiment_v3_bootstrap")
    ctx = HookContext(harness=h, state=state, messages=[], turn=1)
    msgs = hook.on_turn_start(ctx) or []
    assert len(msgs) == 1
    page = msgs[0].content
    assert "本 run 的目标与约束" in page and "调 `finish`" in page
    assert not V2_TRANSCRIPT.search(page), V2_TRANSCRIPT.search(page).group(0)
    assert ledger.bootstrap_phase(state) == "ready"
    assert (hook.on_turn_start(HookContext(harness=h, state=state, messages=[], turn=2)) or []) == []
