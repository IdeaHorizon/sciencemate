"""051: two closure seams found by the 09-22 live acceptance, and one route-declaration friction.

Both seams are the same shape: a *second* success (after a local expected-outputs
correction, or after a reopen + retry) was real but no receipt let the run record
it, so the closure could only be ``partial``.  Fixes open exits; they add no gate:

* p2 — a local exact-output correction reuses the original attempt, but the
  build closure's producer-receipt check only read ``route_step_outcome`` /
  external verification events.  The frozen correction witness now carries the
  same identity rows and counts as that attempt's receipt.
* p5 — a step that failed, was reopened by an amendment with ``recovery_basis``
  and then succeeded on a second submission could not close as success because
  the finalized failed submission stayed in the auto-derived refs.  Reopened
  finalized attempts are excluded from the success refs, disclosed next to
  cancelled/superseded ones; route completion still requires the reopened step
  to end verified.
* friction — every one of the three live runs paid one extra route revision for
  "submit_job 步骤必须显式声明 external_job effect".  The effect is implied by
  the tool and is now derived at declaration (derivation only ever adds).

All three are red on 83c6a1ef.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tests.test_local_exact_output_correction import _state
from nodes.experiment.tools import execution_action_census as census
from nodes.experiment.tools import operation_completion as oc
from nodes.experiment.tools import resource_manager as rm
from nodes.experiment.tools.execution_action_census import satisfying_attempt_output_receipts
from nodes.experiment.tools.execution_route import (
    _declare_execution_route,
    begin_route_step_attempt,
    build_route_snapshot,
    finish_route_step_attempt,
    load_canonical_route,
    resolve_execution_context,
)
from nodes.experiment.tools.operation_completion import _record_operation_completion
from nodes.experiment.tools.run_contract import _classify_experiment_scope


# ── friction：submit_job 步骤的 external_job 由声明期派生 ────────────────────

def _build_route(effects: list[str]) -> dict:
    return {
        "schema_version": 2,
        "goal": "构建并最小运行示例程序",
        "evidence_refs": ["https://example.invalid/official-build-guide"],
        "steps": [{
            "id": "build",
            "goal": "使用项目入口构建",
            "after": [],
            "action": {"tool": "submit_job", "program": "cmake"},
            "effects": effects,
            "workdir_role": "build_root",
            "expected_outputs": ["hf_toolchain_smoke"],
        }],
    }


def test_submit_job_step_gets_its_external_job_effect_derived_at_declaration(tmp_path):
    state = _state(tmp_path)
    result = asyncio.run(_declare_execution_route(
        state, route=_build_route(["workspace_write", "process_tree"])))
    assert result["status"] == "success", result
    frozen = load_canonical_route(state)
    assert frozen["status"] == "ready", frozen
    effects = frozen["route"]["steps"][0]["effects"]
    assert "external_job" in effects, effects
    assert effects.count("external_job") == 1
    # 派生是幂等的：再声明一次（模型照旧不写那个词）不会变成修订也不会重复。
    again = asyncio.run(_declare_execution_route(
        state, route=_build_route(["workspace_write", "process_tree"])))
    assert again["status"] == "success", again
    assert load_canonical_route(state)["route"]["steps"][0]["effects"] == effects


def test_declared_external_job_effect_is_kept_verbatim(tmp_path):
    """写了的仍然原样接受——派生只补缺，不改写。"""
    state = _state(tmp_path)
    result = asyncio.run(_declare_execution_route(
        state, route=_build_route(["workspace_write", "process_tree", "external_job"])))
    assert result["status"] == "success", result
    assert load_canonical_route(state)["route"]["steps"][0]["effects"] == [
        "workspace_write", "process_tree", "external_job"]


# ── p2：本地纠正后的 build 能记 success ──────────────────────────────────────

def _local_build_route(expected_outputs: list[str]) -> dict:
    return {
        "schema_version": 2,
        "goal": "produce out/result.txt through the frozen one-step route",
        "evidence_refs": ["https://example.invalid/official-build-guide"],
        "steps": [{
            "id": "build",
            "goal": "write the product",
            "after": [],
            "action": {"tool": "safe_run_bash", "program": "python3"},
            "effects": ["process_tree", "workspace_write"],
            "workdir_role": "run_root",
            "expected_outputs": expected_outputs,
        }],
    }


def _run_local_step_with_wrong_output_base(state: State, workdir: Path) -> tuple[dict, Path]:
    """One real-shaped local attempt (admission → produce → settle → outcome), the way
    nodes/experiment/tests/_mechanical_execution.py does it, minus its success asserts:
    the declared expected_outputs base is wrong, so the outcome is expected_outputs_missing."""
    action = {
        "tool": "safe_run_bash", "program": "python3", "route_step_id": "build",
        "read_only": False, "dry_run": False,
        "observed_effects": ["process_tree", "workspace_write"],
        "workdir_roles": ["run_root"], "payload_digest": "d" * 64,
    }
    decision = dict(resolve_execution_context(state, action))
    decision.update({
        "workdir_role_observed": True,
        "workdir_resolution_status": "explicit",
        "resolved_workdir": str(workdir),
    })
    assert decision["decision"] == "matched_ready_step", decision
    binding = begin_route_step_attempt(state, decision, tool="safe_run_bash", action=action)
    assert binding and not binding.get("binding_error"), binding
    token = census.begin_execution_action(state, action, decision, route_binding=binding)
    assert token["status"] == "success", token
    time.sleep(0.01)
    product = workdir / "out" / "result.txt"
    product.parent.mkdir(parents=True, exist_ok=True)
    product.write_text("ok")
    settled = census.settle_execution_action(
        state, token, payload_spawned=True, job_submitted=False,
        proof_source="test_managed_execution_boundary",
        result={"status": "success", "returncode": 0})
    assert settled["passed"] is True, settled
    finish_route_step_attempt(state, binding, result={"status": "success", "returncode": 0})
    return binding, product


def test_local_correction_receipt_lets_a_build_close_as_success(tmp_path):
    state = _state(tmp_path)                                  # operation / toolchain_build
    assert asyncio.run(_declare_execution_route(
        state, route=_local_build_route(["build/out/result.txt"])))["status"] == "success"
    workdir = Path(state.root)
    binding, product = _run_local_step_with_wrong_output_base(state, workdir)
    before = build_route_snapshot(state)["steps"]["build"]
    assert before["state"] == "failed" and before["reason"] == "expected_outputs_missing", before

    corrected = asyncio.run(_declare_execution_route(
        state,
        route=_local_build_route(["out/result.txt"]),
        amendment_reason="declared base build/ did not exist; correct only expected_outputs",
        recovery_basis={
            "attempt_id": binding["attempt_id"],
            "failure_class": "expected_output",
            "diagnosis": "the declared path base did not match the local output",
            "evidence_refs": [],
        },
    ))
    assert corrected["status"] == "success", corrected
    after = build_route_snapshot(state)["steps"]["build"]
    assert after["state"] == "verified" and after["recovered_from_local_attempt"] is True, after
    assert after["attempt_id"] == binding["attempt_id"]

    # 纠正复用的 attempt 现在有产物收据：正是它写下的那个文件。
    receipts = satisfying_attempt_output_receipts(state)
    assert [r["attempt_id"] for r in receipts] == [binding["attempt_id"]], receipts
    assert receipts[0]["source_event"] == "declared_route_recovery_receipt"
    assert [row["path"] for row in receipts[0]["output_observations"]] == [str(product.resolve())]

    state.hook_state["_request_mode"] = "operation"
    closed = asyncio.run(_record_operation_completion(
        state,
        task_kind="build",
        objective="produce out/result.txt through the frozen one-step route",
        outcome="success",
        checks=[{"name": "product_present", "passed": True,
                 "evidence": {"path": str(product), "content": product.read_text()}}],
        artifact_paths=[str(product)],
    ))
    assert closed["status"] == "success", closed
    assert closed.get("outcome") == "success", closed
    assert "outcome_demoted_from" not in closed, closed
    log = state.read_artifact(closed["experiment_log_artifact_id"])
    frozen_input = (log.get("metadata") or {}).get("operation_closure_input") or {}
    assert "outcome_demoted_from" not in frozen_input, frozen_input


def test_correction_receipt_is_dropped_once_the_step_definition_changes_again(tmp_path):
    """收据跟着投影走：纠正后再改步骤定义（不带 recovery_basis），投影不再把它算作
    复用的 verified 步骤，冻结在 lineage 里的旧见证就不能再当产物收据用。"""
    state = _state(tmp_path)
    assert asyncio.run(_declare_execution_route(
        state, route=_local_build_route(["build/out/result.txt"])))["status"] == "success"
    workdir = Path(state.root)
    binding, product = _run_local_step_with_wrong_output_base(state, workdir)
    assert asyncio.run(_declare_execution_route(
        state, route=_local_build_route(["out/result.txt"]),
        amendment_reason="correct only expected_outputs",
        recovery_basis={"attempt_id": binding["attempt_id"], "failure_class": "expected_output",
                        "diagnosis": "path base", "evidence_refs": []},
    ))["status"] == "success"
    assert [r["attempt_id"] for r in satisfying_attempt_output_receipts(state)] == [binding["attempt_id"]]

    redefined = _local_build_route(["out/result.txt"])
    redefined["steps"][0]["action"]["program"] = "bash"          # a different step now (still bounded)
    assert asyncio.run(_declare_execution_route(
        state, route=redefined, amendment_reason="switch the build entry to make",
    ))["status"] == "success"
    step = build_route_snapshot(state)["steps"]["build"]
    assert step["state"] != "verified", step
    assert satisfying_attempt_output_receipts(state) == []


def test_local_correction_receipt_still_refuses_a_file_the_attempt_did_not_write(tmp_path):
    """收据只覆盖见证过的文件：事后另写的产物照旧无归属（P0a v4 不变量不松）。"""
    state = _state(tmp_path)
    assert asyncio.run(_declare_execution_route(
        state, route=_local_build_route(["build/out/result.txt"])))["status"] == "success"
    workdir = Path(state.root)
    binding, product = _run_local_step_with_wrong_output_base(state, workdir)
    assert asyncio.run(_declare_execution_route(
        state, route=_local_build_route(["out/result.txt"]),
        amendment_reason="correct only expected_outputs",
        recovery_basis={"attempt_id": binding["attempt_id"], "failure_class": "expected_output",
                        "diagnosis": "path base", "evidence_refs": []},
    ))["status"] == "success"
    stray = workdir / "out" / "stray.bin"
    stray.write_bytes(b"written after the attempt by something else")
    state.hook_state["_request_mode"] = "operation"
    closed = asyncio.run(_record_operation_completion(
        state, task_kind="build", objective="x", outcome="success",
        checks=[{"name": "product_present", "passed": True, "evidence": {}}],
        artifact_paths=[str(product), str(stray)],
    ))
    assert closed["status"] == "error", closed
    assert closed["error_code"] == "operation_build_artifact_not_produced_by_satisfying_attempt"
    assert [row["path"] for row in closed["unattributed_paths"]] == [str(stray.resolve())]


# ── p5：重开后的重试成功能记 success ─────────────────────────────────────────

def _submission(**overrides) -> dict:
    result = {
        "status": "success", "dry_run": False, "scheduler": "local", "job_id": "4242",
        "job_name": "retry-job", "namespace": None, "launch_host": "node20",
        "scheduler_cluster": None, "resource_uid": None, "submission_nonce": "nonce-4242",
        "container_runtime_id": "c" * 64, "process_group_id": None,
        "process_start_ticks": None, "workdir": "/tmp/work",
    }
    result.update(overrides)
    return result


def _terminal_health(*_args, **_kwargs) -> dict:
    return {"status": "success", "scheduler_phase": "terminal",
            "health_state": "terminal", "workflow_status": "awaiting_analysis"}


def _op_state(tmp_path: Path) -> State:
    state = State.new("experiment", tmp_path)
    state.project_root = tmp_path / "project"
    state.hook_state.setdefault("node_inputs", {
        "experiment_focus": "retry inside the run after a failed managed build",
        "prereg_assignment": {"kind": "none", "reason": "no governing preregistration"},
    })
    asyncio.run(_classify_experiment_scope(
        state, scope="operation", operation_category="job_observation",
        reason="051 seam fixture"))
    return state


@pytest.fixture()
def per_job_scheduler(monkeypatch):
    """终态桩：first 失败、其它成功；lifecycle 由 job_id 决定。"""
    monkeypatch.setattr(rm, "probe_external_job_health", _terminal_health)
    monkeypatch.setattr(rm, "_task_external_jobs", lambda _state: [])

    def lifecycle(_state, payload, **_kw):
        if payload.get("job_id") == "first":
            return {"status": "finalized", "resolution": "exact"}
        return {"status": None}

    monkeypatch.setattr(rm, "lifecycle_for_submission", lifecycle)

    def evidence(record, *_a, **_k):
        failed = record.get("job_id") == "first"
        return {"verified": True, "succeeded": not failed, "returncode": 1 if failed else 0}

    monkeypatch.setattr(oc, "_external_job_success_evidence", evidence)


def _close(state: State) -> dict:
    return asyncio.run(oc._record_operation_completion(
        state, task_kind="external_job",
        objective="close the managed operation honestly", outcome="success"))


def test_reopened_finalized_failure_is_excluded_from_success_refs_and_disclosed(
        tmp_path, per_job_scheduler):
    state = _op_state(tmp_path)
    state.save_artifact("job_submission", "first", json.dumps(_submission(
        job_id="first", submission_nonce="nonce-first", route_attempt_id="route-first")))
    # 带 recovery_basis 的修订重开了那一步：这是 execution_route 写下的事件形状。
    state.append_transcript(
        "declared_route_recovery_basis", attempt_id="route-first", route_step_id="build",
        observed_failure_class="operation_failed", failure_class="environment",
        diagnosis="fixed.flag was missing", evidence_refs=["artifact:diag"],
    )
    state.save_artifact("job_submission", "second", json.dumps(_submission(
        job_id="second", submission_nonce="nonce-second", route_attempt_id="route-second")))

    completion = _close(state)

    assert completion["status"] == "success", completion
    log = state.read_artifact(completion["experiment_log_artifact_id"])
    metadata = log.get("metadata") or {}
    assert {ref["job_id"] for ref in metadata.get("external_job_refs") or []} == {"second"}
    frozen_input = metadata.get("operation_closure_input") or {}
    assert "outcome_demoted_from" not in frozen_input, frozen_input
    excluded = frozen_input.get("external_jobs_excluded") or []
    assert [row["reference"]["job_id"] for row in excluded] == ["first"]
    assert excluded[0]["lifecycle_status"] == "superseded_by_route_reopen"
    assert excluded[0]["route_attempt_id"] == "route-first"


def test_a_finalized_failure_that_was_never_reopened_still_blocks_success(tmp_path, per_job_scheduler):
    """没有重开就没有取代：失败照旧进 refs，success 照旧被拒（不放过失败）。"""
    state = _op_state(tmp_path)
    state.save_artifact("job_submission", "first", json.dumps(_submission(
        job_id="first", submission_nonce="nonce-first", route_attempt_id="route-first")))
    state.save_artifact("job_submission", "second", json.dumps(_submission(
        job_id="second", submission_nonce="nonce-second", route_attempt_id="route-second")))

    completion = _close(state)

    assert completion["status"] == "error", completion
    assert completion["error_code"] == "external_job_success_unverified", completion
