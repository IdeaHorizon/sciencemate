"""060 S1a：run 开始时框架建立 A，模型不参与（governing §2、§3、§6）。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import ledger, obligations, run_bootstrap


def _state(tmp_path: Path, inputs: dict) -> State:
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    state.hook_state["node_inputs"] = inputs
    return state


def test_a_bound_prereg_makes_the_run_scientific_without_asking_the_model(tmp_path):
    from core.ledger import record_version, sha256_text
    state = _state(tmp_path, {"requested_work": "跑 MOM6 CTRL"})
    saved = state.save_artifact("pre_registration", "prereg__x", "# prereg\n", metadata={"execution_mode": "scientific"})
    state.mark_frozen(saved["id"])
    rec = state.artifact_versions(saved["id"])[-1]
    state.hook_state["node_inputs"]["prereg_assignment"] = {
        "kind": "bound", "artifact_id": saved["id"], "version": record_version(rec),
        "content_hash": str(rec.get("content_hash") or sha256_text("# prereg\n"))}
    derived = run_bootstrap.derive_scope(state)
    assert derived["scope"] == "scientific" and "预注册" in derived["reason"], derived
    assert derived["prereg"]["artifact_id"] == saved["id"]


def test_a_legacy_prereg_id_input_is_a_loud_failure_not_a_second_resolver(tmp_path):
    """legacy/mixed 是 durable scientific blocker，不桥接、也不锁死 bootstrap。"""
    state = _state(tmp_path, {"prereg_assignment": {"kind": "none", "reason": "x"},
                              "prereg_artifact_id": "prereg__x", "requested_work": "跑"})
    derived = run_bootstrap.derive_scope(state)
    assert derived["scope"] == "scientific"
    assert derived["prereg_status"] == "mixed_conflict"
    assert derived["startup_blocker"]["code"] == "mixed_prereg_input_unsupported"


def test_an_explicit_operation_input_is_taken_as_operation(tmp_path):
    state = _state(tmp_path, {"prereg_assignment": {"kind": "none", "reason": "非科学任务"},
                              "scope": "operation", "operation_category": "toolchain_build"})
    derived = run_bootstrap.derive_scope(state)
    assert derived["scope"] == "operation" and derived["category"] == "toolchain_build"
    assert derived["prereg_status"] == "none"
    assert derived["formal_input_authority"] == "unbound"


def test_an_ambiguous_task_is_left_undecided_rather_than_defaulted(tmp_path):
    state = _state(tmp_path, {"prereg_assignment": {"kind": "none", "reason": "没有预注册"},
                              "experiment_spec": "跑一下这个东西看看"})
    derived = run_bootstrap.derive_scope(state)
    assert derived["scope"] is None and "不猜" in derived["reason"]
    established = asyncio.run(run_bootstrap.establish(state))
    assert established["scope_status"] == "undecided"
    assert "experiment_execution_scope" not in state.hook_state  # 没有替模型/用户定性
    page = run_bootstrap.one_page(state, established)
    assert "## 待澄清" not in page
    assert "不必补分类手续" in page


def test_establish_opens_the_ledger_freezes_obligations_and_writes_the_one_page(tmp_path):
    state = _state(tmp_path, {
        "prereg_assignment": {"kind": "none", "reason": "非科学的本地构建"},
        "scope": "operation", "operation_category": "toolchain_build",
        "experiment_spec": "从只读源码构建 hello 并跑最小 smoke",
        "required_outputs": [
            {"id": "exe", "kind": "file", "path": "build/bin/hello"},
            {"id": "smoke", "kind": "check", "command": "build/bin/hello",
             "expect_stdout_contains": "HF_TOOLCHAIN_SMOKE_OK"},
        ]})
    established = asyncio.run(run_bootstrap.establish(state))
    assert established["scope_status"] == "success"
    assert established["obligations"]["status"] == "frozen"
    assert established["ledger"]["read_only"] is False
    kinds = [r["kind"] for r in ledger.open_ledger(state).records()]
    assert kinds[0] == "run_bootstrap" and "obligations_frozen" in kinds
    assert "envelope_frozen" in kinds  # 授权包络也是 A 的一部分，随 A 一起冻结
    page = run_bootstrap.one_page(state, established)
    assert "从只读源码构建 hello" in page
    assert "`build/bin/hello`" in page and "HF_TOOLCHAIN_SMOKE_OK" in page
    assert "调 `finish`" in page
    # 一页纸里没有 v2 的仪式词
    for word in ("route", "receipt", "attempt", "classify", "finalize", "verify_external"):
        assert word not in page.lower(), word
    # 幂等：再来一次不重复冻结
    again = asyncio.run(run_bootstrap.establish(state))
    assert again["obligations"]["status"] == "already_frozen"


def test_a_free_text_task_is_preserved_without_inventing_success_criteria(tmp_path):
    state = _state(tmp_path, {"prereg_assignment": {"kind": "none", "reason": "x"},
                              "scope": "operation", "operation_category": "other",
                              "experiment_spec": "把这个装上"})
    established = asyncio.run(run_bootstrap.establish(state))
    assert established["obligations"]["status"] == "frozen"
    page = run_bootstrap.one_page(state, established)
    assert "没有点名可核的交付物" not in page
    assert "具体业务要求确实含糊" in page
    assert obligations.frozen(state)[0] == {
        "id": "task_text", "kind": "task", "text": "把这个装上", "required": True,
        "source": "node_inputs.experiment_spec",
    }  # 冻结原文，不把未解析的业务条件编成文件或检查命令


def test_bootstrap_materialises_the_writable_roots_and_leaves_read_only_ones_alone(tmp_path):
    """一页纸说"你能写这里"，那些目录就必须真的存在。

    活体证据（第四次方法预览，2026-09-23）：build_root 与 managed_source_root
    没有被建出来，沙箱契约以"受权路径角色尚未物化"拒了 3 条构建命令。v2 里这些
    目录是 declare_execution_route 顺带建的，v3 没有那一步。
    """
    from nodes.experiment.tools.path_roles import collect_path_roles

    baseline = tmp_path / "readonly_src"
    baseline.mkdir()
    (baseline / "main.c").write_text("int main(void){return 0;}\n")
    state = _state(tmp_path, {
        "prereg_assignment": {"kind": "none", "reason": "本地构建"},
        "scope": "operation", "operation_category": "toolchain_build",
        "experiment_spec": "构建",
        "path_roles": {"source_baseline_root": {"path": str(baseline), "writable": False}},
        "required_outputs": [{"id": "exe", "kind": "file", "path": "build/bin/hello"}]})
    asyncio.run(run_bootstrap.establish(state))
    for role in collect_path_roles(state):
        if role.writable and not role.container_only:
            assert Path(role.path).is_dir(), f"{role.role} 没有被物化：{role.path}"
    assert baseline.is_dir() and sorted(p.name for p in baseline.iterdir()) == ["main.c"]


@pytest.mark.parametrize("assignment", [
    None,
    "bad",
    {},
    {"kind": "none"},
    {"kind": "none", "reason": "x", "extra": True},
    {"kind": "pending", "extra": True},
    {"kind": "bound", "artifact_id": "x", "version": True, "content_hash": "0" * 64},
    {"kind": "bound", "artifact_id": "x", "version": 1, "content_hash": "0" * 64,
     "source": "explicit_node_input"},
])
def test_typed_prereg_union_is_exact_and_invalid_shapes_become_ready_blockers(tmp_path, assignment):
    state = _state(tmp_path, {"prereg_assignment": assignment, "scope": "operation",
                              "experiment_spec": "x", "required_outputs": []})
    established = asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    authority = run_bootstrap.load_run_authority(state)
    assert authority["scope"] == "scientific"
    assert authority["prereg_status"] == "invalid"
    assert authority["formal_input_authority"] == "unavailable"
    assert authority["startup_blocker"]["code"] == "prereg_assignment_invalid"


@pytest.mark.parametrize("inputs,scope,prereg_status,blocked", [
    ({}, None, "omitted", False),
    ({"scope": "operation"}, "operation", "omitted", False),
    ({"prereg_assignment": {"kind": "none", "reason": "x"}}, None, "none", False),
    ({"prereg_assignment": {"kind": "none", "reason": "x"}, "scope": "scientific"},
     "scientific", "none", False),
    ({"prereg_assignment": {"kind": "pending"}}, "scientific", "pending", True),
    ({"prereg_artifact_id": "", "prereg_version": 0}, "scientific", "legacy_unsupported", True),
])
def test_omitted_none_pending_and_key_present_legacy_have_distinct_a_projections(
    tmp_path, inputs, scope, prereg_status, blocked,
):
    state = _state(tmp_path, {**inputs, "experiment_spec": "x", "required_outputs": []})
    asyncio.run(run_bootstrap.establish(state))
    authority = run_bootstrap.load_run_authority(state)
    assert ledger.bootstrap_phase(state) == "ready"
    assert authority["scope"] == scope and authority["prereg_status"] == prereg_status
    assert bool(authority["startup_blocker"]) is blocked


def test_scope_is_monotonic_and_retry_conflict_is_a_durable_blocker(tmp_path, monkeypatch):
    state = _state(tmp_path, {"prereg_assignment": {"kind": "none", "reason": "x"},
                              "scope": "operation", "experiment_spec": "x", "required_outputs": []})
    real = obligations.freeze
    calls = {"n": 0}
    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("injected")
        return real(*args, **kwargs)
    monkeypatch.setattr(obligations, "freeze", flaky)
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "initializing"
    state.hook_state["node_inputs"]["scope"] = "scientific"
    asyncio.run(run_bootstrap.establish(state))
    authority = run_bootstrap.load_run_authority(state)
    assert ledger.bootstrap_phase(state) == "ready"
    assert authority["scope"] == "operation"
    assert authority["startup_blocker"]["code"] == "scope_resolution_conflict"
    bootstrap_rows = [r for r in ledger.open_ledger(state).records() if r["kind"] == "run_bootstrap"]
    assert len(bootstrap_rows) == 1 and bootstrap_rows[0]["scope"] == "operation"


def test_reopen_on_a_later_turn_replays_durable_initializing_state(tmp_path, monkeypatch):
    from core.loop_hooks import HookContext
    state = _state(tmp_path, {"prereg_assignment": {"kind": "none", "reason": "x"},
                              "scope": "operation", "experiment_spec": "x", "required_outputs": []})
    real = obligations.freeze
    calls = {"n": 0}
    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("injected")
        return real(*args, **kwargs)
    monkeypatch.setattr(obligations, "freeze", flaky)
    run_bootstrap.experiment_v3_bootstrap.on_turn_start(
        HookContext(harness=None, state=state, messages=[], turn=1))
    assert ledger.bootstrap_phase(state) == "initializing"
    reopened = State.reopen("experiment", state.root.parent, state.run_id, session_id="owner")
    reopened.hook_state["node_inputs"] = state.hook_state["node_inputs"]
    messages = run_bootstrap.experiment_v3_bootstrap.on_turn_start(
        HookContext(harness=None, state=reopened, messages=[], turn=7)) or []
    assert ledger.bootstrap_phase(reopened) == "ready"
    assert messages and "启动完成" in messages[0].content
    rows = ledger.open_ledger(reopened).records()
    assert len([r for r in rows if r["kind"] == "run_bootstrap"]) == 1


def test_materialized_root_failure_keeps_bootstrap_initializing_until_retry(tmp_path, monkeypatch):
    from nodes.experiment.tools.path_roles import collect_path_roles
    state = _state(tmp_path, {"scope": "operation", "experiment_spec": "x", "required_outputs": []})
    target = next(Path(r.path) for r in collect_path_roles(state)
                  if r.writable and not r.container_only and not Path(r.path).exists())
    real_mkdir = Path.mkdir
    def flaky_mkdir(self, *args, **kwargs):
        if self == target:
            raise OSError("injected")
        return real_mkdir(self, *args, **kwargs)
    monkeypatch.setattr(Path, "mkdir", flaky_mkdir)
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "initializing"
    monkeypatch.setattr(Path, "mkdir", real_mkdir)
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready"
