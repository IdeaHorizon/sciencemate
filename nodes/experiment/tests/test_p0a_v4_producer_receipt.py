"""P0a v4: a declared build product must map to the frozen output receipt of the
attempt that satisfied the obligation — identity, not mtime.

Codex's review of v3 (044/23) reproduced five live counterexamples against the
mtime-window rule, all confirmed here before this rewrite:

* a symlink or hardlink made *after* the window impersonated an in-window
  product (``realpath`` erased the lexical identity, hardlinks share the inode);
* a real product staged *inside* the attempt with ``cp -p`` / ``tar`` /
  ``rsync -t`` kept its old mtime and was refused;
* a file written 11 ms before the route-backed attempt slipped inside the 50 ms
  lower tolerance;
* a paused dispatch never converged after ``reconcile_data_dispatch``;
* the dispatch receipt event's persistence failure was swallowed.

v4 replaces the window with a receipt.  ``finish_route_step_attempt`` and
``record_external_route_execution_verification`` freeze, per verified
expected_output, the lexical path, kind, dev/ino/size and sha256
(``output_observations``).  ROC(build) accepts a declared product only when a
satisfying attempt's receipt lists that lexical path with the same identity.
There is no window predicate and no tolerance left to tune.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import time
from pathlib import Path

from core.state import State
from nodes.experiment.tools import execution_action_census as census
from nodes.experiment.tools import (
    execution_route,
    operation_completion,
    run_contract,
    safe_bash,
)


def _pending_state(tmp_path: Path) -> tuple[State, Path]:
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {"experiment_focus": "Build the assigned tool."}
    classified = asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="toolchain_build", reason="build only"))
    assert classified["status"] == "success", classified
    run_root = Path(state.root) / "work" / "CTRL"
    run_root.mkdir(parents=True, exist_ok=True)
    state.hook_state["path_roles"] = {
        "experiment_root": str(run_root.parent), "run_root": str(run_root)}
    return state, run_root


def _write_product(path: Path, content: str = "#!/bin/sh\nexit 0\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


def _route_backed_attempt(
    state: State, run_root: Path, *, produce=None, expected_outputs=None,
    expect_outcome: str = "success",
) -> dict:
    """One route-backed local step.

    ``produce`` (a callable) runs inside the attempt; ``expected_outputs`` (relative
    to run_root) is what the step declares — only declared, verified outputs get a
    receipt entry.  Returns the outcome event.
    """
    events, _ = execution_route._read_transcript_events(state)
    if not any(e.get("event") == "declared_route_recorded" for e in events):
        route = {
            "schema_version": 2, "goal": "Unpack and stage the tool.",
            "evidence_refs": ["test:p0a-v4"],
            "steps": [{
                "id": "unpack", "goal": "Unpack the archive.", "after": [],
                "action": {"tool": "safe_run_bash", "program": "tar"},
                "effects": ["process_tree", "workspace_write"],
                "workdir_role": "run_root",
                "expected_outputs": list(expected_outputs or []),
            }],
        }
        declared = asyncio.run(execution_route._declare_execution_route(state, route=route))
        assert declared["status"] == "success", declared
    action = {
        "tool": "safe_run_bash", "program": "tar", "route_step_id": "unpack",
        "read_only": False, "dry_run": False,
        "observed_effects": ["process_tree", "workspace_write"],
        "workdir_roles": ["run_root"], "payload_digest": "d" * 64,
    }
    decision = dict(execution_route.resolve_execution_context(state, action))
    decision.update({"workdir_role_observed": True, "workdir_resolution_status": "explicit",
                     "resolved_workdir": str(run_root)})
    assert execution_route.enforce_execution_route(state, action, decision) is None, decision
    binding = execution_route.begin_route_step_attempt(
        state, decision, tool="safe_run_bash", action=action)
    token = census.begin_execution_action(state, action, decision, route_binding=binding)
    assert token["status"] == "success", token
    if produce is not None:
        produce()
    settled = census.settle_execution_action(
        state, token, payload_spawned=True, job_submitted=False,
        proof_source="test_boundary", result={"status": "success", "returncode": 0})
    assert settled["passed"], settled
    finished = execution_route.finish_route_step_attempt(
        state, binding, result={"status": "success", "returncode": 0})
    assert finished.get("outcome") == expect_outcome, finished
    return finished


def _roc_build(state: State, product: Path) -> dict:
    return asyncio.run(operation_completion._record_operation_completion(
        state, task_kind="build", objective="build the assigned tool", outcome="success",
        checks=[{"name": "binary_present", "passed": True, "evidence": {"path": str(product)}}],
        artifact_paths=[str(product)]))


def _refused_as_not_produced(closed: dict, *, reason: str | None = None) -> dict:
    assert closed["status"] == "error", closed
    assert closed["error_code"] == "operation_build_artifact_not_produced_by_satisfying_attempt", closed
    if reason is not None:
        assert [row["reason"] for row in closed["unattributed_paths"]] == [reason], closed
    return closed


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── the four v3 regressions, re-pinned on the receipt ────────────────────────

def test_shell_fake_product_after_satisfying_attempt_is_refused(tmp_path):
    state, run_root = _pending_state(tmp_path)
    _route_backed_attempt(state, run_root)
    (run_root / "fake-src").write_text("#!/bin/sh\necho fake\n")
    fake = run_root / "bin" / "solver"
    fake.parent.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(safe_bash._safe_run_bash(
        state, f"cp {run_root}/fake-src {fake}", cwd=str(run_root)))
    assert result["status"] == "success", result
    _refused_as_not_produced(_roc_build(state, fake), reason="no_producer_receipt")
    assert state.list_artifacts("raw_results") == []


def test_python_fake_product_after_satisfying_attempt_is_refused(tmp_path):
    state, run_root = _pending_state(tmp_path)
    _route_backed_attempt(state, run_root)
    fake = run_root / "bin" / "solver"
    code = (
        "import pathlib\n"
        f"p = pathlib.Path({str(fake)!r}); p.parent.mkdir(parents=True, exist_ok=True)\n"
        "p.write_text('#!/bin/sh\\necho fake\\n'); p.chmod(0o755)\n"
    )
    result = asyncio.run(safe_bash._safe_execute_python(state, code, cwd=str(run_root)))
    assert result["status"] == "success", result
    _refused_as_not_produced(_roc_build(state, fake), reason="no_producer_receipt")


def test_product_written_before_any_route_backed_attempt_is_refused_and_never_incidental(tmp_path):
    state, run_root = _pending_state(tmp_path)
    (run_root / "fake-src").write_text("#!/bin/sh\necho fake\n")
    fake = run_root / "bin" / "solver"
    fake.parent.mkdir(parents=True, exist_ok=True)
    early = asyncio.run(safe_bash._safe_run_bash(
        state, f"cp {run_root}/fake-src {fake}", cwd=str(run_root)))
    assert early["status"] == "success", early
    _route_backed_attempt(state, run_root)
    obligation = census.operation_execution_obligation(state)
    early_rows = [
        a for a in obligation["action_census"]["actions"]
        if a["tool"] == "safe_run_bash" and a["compatibility_kind"] == "incidental"
    ]
    assert len(early_rows) == 1, obligation["action_census"]["actions"]
    assert early_rows[0]["preceded_first_route_backed_terminal"] is True
    _refused_as_not_produced(_roc_build(state, fake), reason="no_producer_receipt")


def test_placeholder_overwritten_by_the_satisfying_attempt_is_accepted(tmp_path):
    state, run_root = _pending_state(tmp_path)
    product = run_root / "bin" / "solver"
    placeholder = asyncio.run(safe_bash._safe_write_file(
        state, str(product), "#!/bin/sh\necho placeholder\n"))
    assert placeholder["status"] == "success", placeholder
    _route_backed_attempt(
        state, run_root, produce=lambda: _write_product(product),
        expected_outputs=["bin/solver"])
    assert product.read_text() == "#!/bin/sh\nexit 0\n"
    closed = _roc_build(state, product)
    assert closed["status"] == "success", closed
    assert len(state.list_artifacts("raw_results")) == 1


# ── Codex 23 号的三维反例：位置 / 值 / 包裹 ───────────────────────────────────

def test_symlink_made_after_the_attempt_cannot_impersonate_its_product(tmp_path):
    state, run_root = _pending_state(tmp_path)
    target = run_root / "real_target"
    _route_backed_attempt(
        state, run_root, produce=lambda: _write_product(target),
        expected_outputs=["real_target"])
    declared = run_root / "bin" / "solver"
    declared.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, declared)
    closed = _refused_as_not_produced(_roc_build(state, declared), reason="no_producer_receipt")
    assert closed["unattributed_paths"][0]["current_identity"]["kind"] == "symlink"


def test_hardlink_made_after_the_attempt_cannot_impersonate_its_product(tmp_path):
    state, run_root = _pending_state(tmp_path)
    target = run_root / "real_target"
    _route_backed_attempt(
        state, run_root, produce=lambda: _write_product(target),
        expected_outputs=["real_target"])
    declared = run_root / "bin" / "solver"
    declared.parent.mkdir(parents=True, exist_ok=True)
    os.link(target, declared)
    _refused_as_not_produced(_roc_build(state, declared), reason="no_producer_receipt")


def test_rename_after_the_attempt_cannot_impersonate_its_product(tmp_path):
    state, run_root = _pending_state(tmp_path)
    target = run_root / "real_target"
    _route_backed_attempt(
        state, run_root, produce=lambda: _write_product(target),
        expected_outputs=["real_target"])
    declared = run_root / "bin" / "solver"
    declared.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(target), str(declared))
    _refused_as_not_produced(_roc_build(state, declared), reason="no_producer_receipt")


def test_preserved_mtime_product_staged_inside_the_attempt_is_accepted(tmp_path):
    """cp -p / tar / rsync -t keep the source mtime; identity, not mtime, decides."""
    state, run_root = _pending_state(tmp_path)
    source = run_root / "prebuilt_src"
    _write_product(source)
    old = time.time() - 3600
    os.utime(source, (old, old))
    product = run_root / "bin" / "solver"

    def _stage() -> None:
        product.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, product)

    _route_backed_attempt(state, run_root, produce=_stage, expected_outputs=["bin/solver"])
    assert os.stat(product).st_mtime < time.time() - 3000
    closed = _roc_build(state, product)
    assert closed["status"] == "success", closed


def test_product_written_milliseconds_before_the_attempt_is_not_admitted(tmp_path):
    """No tolerance: a pre-existing, untouched file is never this attempt's product."""
    state, run_root = _pending_state(tmp_path)
    product = run_root / "bin" / "solver"
    _write_product(product)
    # (a) undeclared: nothing in the receipt names it.
    _route_backed_attempt(state, run_root)
    assert not hasattr(census, "satisfying_attempt_windows")
    assert not hasattr(census, "path_within_satisfying_window")
    assert "_WINDOW_LOWER_TOLERANCE_NS" not in vars(census)
    _refused_as_not_produced(_roc_build(state, product), reason="no_producer_receipt")


def test_declared_but_untouched_preexisting_file_fails_the_attempt_itself(tmp_path):
    """(b) declared: the route verifier sees an unchanged baseline → the attempt is
    not a satisfying one at all, so the closure cannot lean on it."""
    state, run_root = _pending_state(tmp_path)
    product = run_root / "bin" / "solver"
    _write_product(product)
    finished = _route_backed_attempt(
        state, run_root, expected_outputs=["bin/solver"], expect_outcome="failed")
    assert finished["failure_class"] == "expected_outputs_missing"
    assert finished["unchanged_expected_outputs"] == ["bin/solver"]
    assert census.satisfying_attempt_output_receipts(state) == []
    closed = _roc_build(state, product)
    assert closed["status"] == "error", closed
    assert closed["error_code"] in {
        "operation_build_artifact_not_produced_by_satisfying_attempt",
        "operation_real_execution_required", "execution_route_incomplete",
    }, closed


def test_content_rewritten_after_the_attempt_is_refused_as_changed(tmp_path):
    state, run_root = _pending_state(tmp_path)
    product = run_root / "bin" / "solver"
    _route_backed_attempt(
        state, run_root, produce=lambda: _write_product(product),
        expected_outputs=["bin/solver"])
    with product.open("a") as handle:
        handle.write("echo tampered\n")
    _refused_as_not_produced(_roc_build(state, product), reason="content_changed_since_attempt")


def test_receipt_records_lexical_path_inode_and_sha256_from_the_attempt(tmp_path):
    state, run_root = _pending_state(tmp_path)
    product = run_root / "bin" / "solver"
    finished = _route_backed_attempt(
        state, run_root, produce=lambda: _write_product(product),
        expected_outputs=["bin/solver"])
    rows = finished["output_observations"]
    assert finished["output_observations_truncated"] is False
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["path"] == execution_route.output_lexical_key(str(product))
    assert row["spec"] == "bin/solver"
    assert row["kind"] == "file"
    assert row["st_ino"] == os.stat(product).st_ino
    assert row["size_bytes"] == product.stat().st_size
    assert row["sha256"] == _sha256(product)
    receipts = census.satisfying_attempt_output_receipts(state)
    assert [r["source_event"] for r in receipts] == ["route_step_outcome"]
    assert receipts[0]["output_observations"] == rows
    # the same helper judges the current identity at closure time
    ok, reason = execution_route.output_identity_matches(
        row, execution_route.output_identity(str(product)))
    assert ok and reason is None


def test_identity_matches_reports_kind_change_before_inode_facts():
    """Mutation R13 (kind check removed) drew 0 red through ROC: a real kind swap
    always changes the inode too, so ``identity_changed`` masked it. The shared
    helper still owes the more specific reason — pin it directly, as 035a's
    evaluator will consume this function and its reasons."""
    recorded = {"kind": "file", "st_dev": 1, "st_ino": 2, "size_bytes": 3,
                "sha256": "a" * 64, "ctime_ns": 1, "link_target": None}
    as_symlink = {**recorded, "kind": "symlink", "sha256": None, "link_target": "x"}
    assert execution_route.output_identity_matches(recorded, as_symlink) == (
        False, "kind_changed_since_attempt")
    as_directory = {**recorded, "kind": "directory", "sha256": None}
    assert execution_route.output_identity_matches(recorded, as_directory) == (
        False, "kind_changed_since_attempt")
    assert execution_route.output_identity_matches(recorded, dict(recorded)) == (True, None)
    assert execution_route.output_identity_matches(
        {**recorded, "kind": "missing"}, dict(recorded)) == (False, "not_present_at_attempt_end")


def test_truncated_receipt_refuses_with_its_own_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(execution_route, "_OUTPUT_OBSERVATION_CAP", 1)
    state, run_root = _pending_state(tmp_path)
    first, second = run_root / "bin" / "a", run_root / "bin" / "b"
    finished = _route_backed_attempt(
        state, run_root,
        produce=lambda: (_write_product(first), _write_product(second)),
        expected_outputs=["bin/*"])
    assert finished["output_observations_truncated"] is True
    assert len(finished["output_observations"]) == 1
    closed = _refused_as_not_produced(_roc_build(state, second), reason="producer_receipt_truncated")
    assert closed["producer_receipts"][0]["truncated"] is True


# ── dispatch：收据随基线事件入账、reconcile 单调推进、落账失败结构化返回 ───────

def test_dispatch_receipt_is_projected_from_the_base_event_and_reconcile_converges(tmp_path):
    state, _run_root = _pending_state(tmp_path)
    state.append_transcript(
        "data_request_dispatched", spec_id="spec-1",
        data_dispatch_receipt={"dispatch_state": "returned", "child_status": "completed",
                               "child_run_id": "child-1"},
        input_delivery_ledger_artifact_id="ledger")
    state.append_transcript(
        "data_request_dispatched", spec_id="spec-2",
        data_dispatch_receipt={"dispatch_state": "paused_pending_result",
                               "child_status": "paused", "child_run_id": "child-2"},
        input_delivery_ledger_artifact_id="ledger")
    rows = [a for a in census.reduce_execution_action_census(state)["actions"]
            if a["tool"] == "dispatch_data_request"]
    assert [a["terminal_status"] for a in rows] == ["success", "unknown"]
    assert {a["compatibility_kind"] for a in rows} == {"receipted"}
    before = census.operation_execution_obligation(state)
    assert "accounted_action_terminal_unknown" in before["failure_reasons"]

    # the real reconcile tool appends exactly this event on success
    state.append_transcript(
        "data_dispatch_pause_reconciled", status="success", spec_id="spec-2",
        data_run_id="child-2", child_status="completed", terminal_outcome="dataset",
        all_child_artifacts=[], dataset_artifact_id="dataset__x")
    rows = [a for a in census.reduce_execution_action_census(state)["actions"]
            if a["tool"] == "dispatch_data_request"]
    assert [a["terminal_status"] for a in rows] == ["success", "success"]
    assert rows[1]["reconciled_by_event"] == "data_dispatch_pause_reconciled"
    after = census.operation_execution_obligation(state)
    assert "accounted_action_terminal_unknown" not in after["failure_reasons"]
    assert "data_dispatch_receipt_recorded" not in vars(census).get("_FETCH_RECEIPT_EVENTS", {})


def test_blocked_reconcile_projects_as_failed_dispatch(tmp_path):
    state, _run_root = _pending_state(tmp_path)
    state.append_transcript(
        "data_dispatch_pause_reconciled", status="success", spec_id="spec-3",
        data_run_id="child-3", child_status="blocked", terminal_outcome="blocked",
        all_child_artifacts=[], blocked_report_id="preprocessing_blocked_report__x")
    rows = [a for a in census.reduce_execution_action_census(state)["actions"]
            if a["tool"] == "dispatch_data_request"]
    # no dispatched event survived (its persistence failure was returned, not
    # swallowed): the reconcile event alone is the dispatch's account.
    assert [a["terminal_status"] for a in rows] == ["failed"]


# ── 写入成功但结算失败：出口必须是真实存在的入口 ───────────────────────────

def test_write_committed_but_settlement_uncertain_names_a_real_exit(tmp_path, monkeypatch):
    state, run_root = _pending_state(tmp_path)
    monkeypatch.setattr(
        census, "settle_execution_action",
        lambda *_a, **_k: {"passed": False, "error_code": "simulated"})
    target = run_root / "late.txt"
    result = asyncio.run(safe_bash._safe_write_file(state, str(target), "x\n"))
    assert target.read_text() == "x\n"          # 副作用已经发生
    assert result["status"] == "error", result
    assert result["error_code"] == "write_committed_but_census_settlement_uncertain"
    assert result["side_effect_committed"] is True
    assert result["payload_must_not_rerun"] is True
    next_action = result["next_action"]
    # 与 census 自己的持久化失败同一形状：runtime-owned，不是模型工具
    assert next_action["owner"] == "experiment_runtime"
    assert next_action["action"] == "retry_missing_census_phase_only"
    assert next_action["model_callable"] is False
    assert result["model_next_action"]["action"] == "report_blocker_and_end_current_run"
    registered = set(safe_bash._REGISTRY.tools)
    assert "reconcile_execution_action_census" not in registered
    assert next_action["action"] in registered or next_action["model_callable"] is False


def test_settlement_failure_passes_through_the_census_next_action(tmp_path, monkeypatch):
    state, run_root = _pending_state(tmp_path)
    runtime_exit = {"owner": "experiment_runtime", "action": "retry_missing_census_phase_only",
                    "phase": "terminal", "model_callable": False, "after_phase": "spawn_observation"}
    monkeypatch.setattr(
        census, "settle_execution_action",
        lambda *_a, **_k: {"passed": False, "error_code": "execution_action_census_persistence_failed",
                           "next_action": dict(runtime_exit)})
    result = asyncio.run(safe_bash._safe_write_file(state, str(run_root / "n.txt"), "x\n"))
    assert result["status"] == "error"
    assert result["next_action"] == runtime_exit


# ── M4：obligation 对象带派生自的冻结收据身份（非空、可核、reload 稳定） ──────

def test_child_obligation_source_receipt_hash_is_real_and_verifiable(tmp_path):
    state, run_root = _pending_state(tmp_path)
    product = run_root / "bin" / "solver"
    _route_backed_attempt(
        state, run_root, produce=lambda: _write_product(product),
        expected_outputs=["bin/solver"])
    closed = _roc_build(state, product)
    assert closed["status"] == "success", closed
    projection = operation_completion.operation_child_obligation_projection(state)
    obligation = projection["child_obligation"]
    source = obligation["source_receipt"]
    raw_id = state.list_artifacts("raw_results")[0]["id"]
    record = state.read_artifact(raw_id)
    assert source["artifact_id"] == raw_id
    assert source["closure_id"] == obligation["closure_id"]
    digest = source["content_hash"]
    assert isinstance(digest, str) and len(digest) == 64 and int(digest, 16) >= 0
    # 逐字节核回：哈希就是账本钉住的正文哈希
    assert digest == record["content_hash"]
    assert digest == hashlib.sha256(str(record["content"]).encode("utf-8")).hexdigest()
    # State reload 后同一份
    reopened = State.reopen("experiment", tmp_path, state.run_id)
    again = operation_completion.operation_child_obligation_projection(reopened)
    assert again["child_obligation"]["source_receipt"]["content_hash"] == digest
    # 篡改后不匹配
    tampered = str(record["content"]) + "\n<!-- tampered -->"
    assert hashlib.sha256(tampered.encode("utf-8")).hexdigest() != digest


# ── 沿用 v3 已接受的两条：B3 第二分支、M5 页脚 ────────────────────────────────

def test_legacy_v1_receipt_conflict_branch_carries_the_same_migration_exit(monkeypatch):
    monkeypatch.setattr(
        run_contract, "_scientific_null_receipt_conflict",
        lambda _state, _receipt: {"kind": "missing", "visible_preregs": []})
    guarded = run_contract._acceptance_for_requested_mode(
        object(),
        {"passed": True, "receipt": {"schema_version": 1, "binding_source": "none"}},
        requested_mode="scientific",
    )
    assert guarded["status"] == "run_authority_scientific_prereg_binding_missing", guarded
    assert guarded["legacy_run"] is True
    assert "started before the upgrade" in guarded["reason"]
    assert "new run" in guarded["reason"]
    modern = run_contract._acceptance_for_requested_mode(
        object(),
        {"passed": True, "receipt": {"schema_version": 2, "prereg_assignment": {"kind": "pending"}}},
        requested_mode="scientific",
    )
    assert "legacy_run" not in modern


class _LoopResult:
    def __init__(self, status: str) -> None:
        self.status = status
        self.final_text = "model narrative"


def test_child_delivery_footer_carries_run_terminal_status_and_block_annotation(tmp_path):
    from nodes.experiment import hooks

    obligation = {
        "closure_id": "run:operation", "delivery_status": "completed",
        "upstream_goal_effect": "operational_subtask_only", "scientific_contribution": "none",
    }
    ended_badly = _LoopResult("max_turns")
    hooks._append_operation_child_delivery_summary(ended_badly, obligation)
    assert "- delivery_status: completed" in ended_badly.final_text
    assert "- run_terminal_status: max_turns" in ended_badly.final_text
    assert "did not end completed" in ended_badly.final_text

    completed = _LoopResult("completed")
    hooks._append_operation_child_delivery_summary(completed, obligation)
    assert "- run_terminal_status: completed" in completed.final_text
    assert "did not end completed" not in completed.final_text

    state = State.new("experiment", tmp_path)
    hooks._block_experiment_completion(
        state, completed, blocker_id="closure:test", reason="test",
        failed_checks=["experiment_verdict_audit"], summary="test block")
    assert "- run_terminal_status: blocked" in completed.final_text
    assert "- blocked_after_delivery: closure:test" in completed.final_text
    hooks._block_experiment_completion(
        state, completed, blocker_id="closure:test", reason="test",
        failed_checks=["experiment_verdict_audit"], summary="test block")
    assert completed.final_text.count("blocked_after_delivery") == 1
