import ast
import asyncio
import hashlib
import json
import pytest
from pathlib import Path

from core.state import State
from nodes.experiment.tools.contract_audit import (
    _has_explicit_inconclusive_reasoning, audit_citation_binding, audit_experiment_contract,
    audit_experiment_log_integrity, audit_execution_record, audit_operation_log_contract, audit_result_evidence,
    _validate_data_request_spec, _verify_dataset_consumption, _record_data_delivery_outcome,
    _dispatch_data_request, _authorize_experiment_fallback, _verify_experiment_fallback_inputs,
    audit_input_delivery_for_execution,
)

def _freeze(state, artifact_id, reason=""):
    """经唯一的 freeze_artifact 冻结 —— 类型门自动跑（六个特化工具已删）。"""
    from shared.tools.library.artifacts_extra import _freeze_artifact
    return asyncio.run(_freeze_artifact(state=state, artifact_id=artifact_id, reason=reason))


def _save_frozen(state, artifact_type, name, content, metadata=None):
    """夹具级冻结：save + mark_frozen（绕过类型门）。冻结的事实只出自账本的 freeze 行，
    save 行里的 frozen 键会被剥掉（core.ledger.FREEZE_OWNED_METADATA）。"""
    saved = state.save_artifact(artifact_type, name, content, metadata=metadata)
    state.mark_frozen(saved["id"])
    return saved



def test_phantom_citation_blocks_log_freeze_and_workflow_outcome(tmp_path: Path):
    state = _non_scientific_state(tmp_path)
    log_id = state.save_artifact(
        "experiment_log", "phantom_citation",
        "## Execution Status\nstatus: completed_build\nclaim_missing123\n\n## Credibility\ncredibility: questionable",
    )["id"]
    state.append_transcript(
        "citation_validation", artifact_id=log_id, artifact_type="experiment_log",
        passed=False, n_cited=1, n_phantom=1, phantom_ids=["claim_missing123"],
    )
    citation = audit_citation_binding(state)
    assert citation["passed"] is False
    assert citation["n_phantom"] == 1
    frozen = _freeze(state, log_id)
    assert frozen["status"] == "error"
    assert "citation_binding" in frozen["failed_checks"]



def test_markdown_inconclusive_verdict_is_auditable():
    content = """## Hypothesis Verdict

**Verdict: inconclusive**

Reason: this is a platform E2E and has no KB hypothesis claim.
Need: register a formal hypothesis before scientific status update.
"""
    assert _has_explicit_inconclusive_reasoning(content)


def test_plain_inconclusive_verdict_remains_auditable():
    content = """verdict: inconclusive
reason: missing scientific claim
next_step: register hypothesis
"""
    assert _has_explicit_inconclusive_reasoning(content)


def _bound_secondary_scientific_state(tmp_path: Path, *, focus: str) -> State:
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new("experiment", tmp_path)
    prereg_id = _save_frozen(
        state,
        "pre_registration",
        "secondary_diagnostic",
        "# Frozen secondary diagnostic preregistration\n",
        metadata={
            "run_role": "secondary",
            "execution_mode": "scientific",
            "stage": "diagnostic",
        },
    )["id"]
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "experiment_focus": focus,
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="Run the frozen secondary diagnostic fixture without formal scientific closure.",
    ))
    assert classified["status"] == "success", classified
    return state


def _non_scientific_state(tmp_path: Path) -> State:
    return _bound_secondary_scientific_state(
        tmp_path,
        focus="Record a bounded diagnostic execution without a formal scientific conclusion.",
    )


def _non_scientific_log(state: State, name: str = "non_scientific") -> str:
    content = """## Execution Status
status: completed_build
reason: toolchain build completed; no scientific measurements were requested

## Credibility
credibility: reliable
evidence: managed job returned returncode=0 and output was inspected.
"""
    return state.save_artifact(
        "experiment_log", name, content,
        metadata={"run_role": "secondary", "analysis_eligible": False},
    )["id"]


def test_non_scientific_run_needs_no_hypothesis_or_sediment_but_is_reviewable(tmp_path: Path):
    state = _non_scientific_state(tmp_path)
    _non_scientific_log(state)
    audit = audit_experiment_contract(state)
    assert audit["verdict"]["passed"] is True
    assert audit["verdict"]["applicable"] is False
    assert audit["sediment"]["passed"] is True
    assert audit["sediment"]["applicable"] is False
    # KB registration is optional; a missing record must not reopen execution closure.
    assert audit["execution_record"]["passed"] is True
    assert audit["execution_record"]["registration_status"] == "not_registered"

def _freeze_log_with_receipt(state: State, artifact_id: str) -> dict:
    state.append_transcript("tool_call", name="freeze_artifact", args={"artifact_id": artifact_id})
    result = _freeze(state, artifact_id, "freeze before execution registration")
    state.append_transcript("tool_result", name="freeze_artifact", result_preview=result)
    return result


def _register_execution_record(state: State, *, frozen_log_chunk_id: str = "") -> dict:
    from shared.tools.library.kb import _create_experiment

    state.append_transcript(
        "tool_call", name="create_experiment",
        args={"experiment_text": "diagnostic execution", "setup_text": "controlled local diagnostic",
              "outcome": "success", "frozen_log_chunk_id": frozen_log_chunk_id},
    )
    result = asyncio.run(_create_experiment(
        state, experiment_text="diagnostic execution", setup_text="controlled local diagnostic",
        outcome="success", frozen_log_chunk_id=frozen_log_chunk_id,
    ))
    state.append_transcript("tool_result", name="create_experiment", result_preview=result)
    return result


def _scientific_execution_state(tmp_path: Path) -> tuple[State, str]:
    state = _bound_secondary_scientific_state(
        tmp_path,
        focus="Run the frozen secondary diagnostic and retain its execution record.",
    )
    log_id = state.save_artifact(
        "experiment_log", "diagnostic_execution",
        "## Execution Status\nstatus: completed_diagnostic\n"
        "## Credibility\ncredibility: reliable\nevidence: returncode=0\n",
    )["id"]
    return state, log_id


def test_non_scientific_freeze_gate_freezes_canonical_log(tmp_path: Path):
    state = _non_scientific_state(tmp_path)
    log_id = _non_scientific_log(state)
    result = _freeze(state, log_id, "completed non-scientific delivery")
    assert result["status"] == "success"
    assert state.read_artifact(log_id)["metadata"]["frozen"] is True


def _complete_operation(state: State, *, outcome: str = "success", checks=None,
                        next_step: str = "", summary: str = "") -> dict:
    from nodes.experiment.tools.operation_completion import _record_operation_completion
    if checks is None:
        checks = [{"name": "command_returncode", "passed": outcome == "success",
                   "evidence": {"returncode": 0 if outcome == "success" else 1}}]
    evidence = Path(state.root) / "operation_test.stdout"
    evidence.write_text("returncode=" + ("0" if outcome == "success" else "1") + "\n", encoding="utf-8")
    blocker_id = ""
    if outcome == "blocked":
        from shared.tools.library.blockers import _report_blocker
        reported = asyncio.run(_report_blocker(
            state, summary="The declared operation input is unavailable.", category="environment",
            evidence_paths=[str(evidence)], suggested_owner="data",
        ))
        blocker_id = reported["blocker"]["blocker_id"]
    return asyncio.run(_record_operation_completion(
        state, task_kind="generic", objective="verify a bounded non-scientific operation",
        outcome=outcome, checks=checks, artifact_paths=[str(evidence)],
        next_step=next_step, summary=summary, blocker_id=blocker_id,
    ))


def _bind_operation_inputs(state: State) -> None:
    node_inputs = state.hook_state.setdefault("node_inputs", {
        "experiment_focus": "Execute the declared bounded operation test fixture.",
    })
    node_inputs.setdefault("prereg_assignment", {
        "kind": "none",
        "reason": "This operation fixture has no governing preregistration.",
    })


def test_operation_freeze_uses_run_contract_without_mutating_log_metadata(tmp_path: Path):
    from nodes.experiment import hooks
    from nodes.experiment.tools import run_contract

    state = State.new("experiment", tmp_path)
    _bind_operation_inputs(state)
    asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="package_install",
        reason="Install a small package and verify its import without scientific analysis.",
    ))
    completion = _complete_operation(state, summary="command and verification were recorded")
    audit = hooks._audit_operation_log(state)
    log = state.read_artifact(completion["experiment_log_artifact_id"])

    assert completion["status"] == "success", completion
    assert log["metadata"]["frozen"] is True
    assert log["metadata"]["record_kind"] == "operation"
    assert audit["passed"] is True, audit
    assert "operation_log_provenance_normalized" not in state.transcript_path.read_text(encoding="utf-8")


def test_blocked_operation_freezes_and_closes_from_run_contract(tmp_path: Path):
    """A registered blocker is an honest operation receipt with failed verification evidence."""
    from nodes.experiment.tools import run_contract

    state = State.new("experiment", tmp_path)
    _bind_operation_inputs(state)
    asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="format_validation",
        reason="A required mesh must be supplied before its format can be validated.",
    ))
    state.hook_state["blockers"] = [{"id": "blocker_data_mesh", "suggested_owner": "data"}]
    completion = _complete_operation(
        state, outcome="blocked", next_step="ask data to provide the declared mesh artifact",
        checks=[{"name": "mesh_available", "passed": False, "evidence": {"path": "channel.msh"}}],
    )
    audit = audit_operation_log_contract(state)

    assert completion["status"] == "success", completion
    assert audit["passed"] is True, audit
    assert audit["blocked_closure"] is True
    assert audit["has_verification"] is True


def test_blocked_operation_without_registered_blocker_closes_as_declaration(tmp_path: Path):
    """判决拆除（oc:178 删，2026-08-31）：blocked 收据本身就是 blocker 声明。"""
    from nodes.experiment.tools import run_contract

    state = State.new("experiment", tmp_path)
    _bind_operation_inputs(state)
    asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="format_validation",
        reason="Validate an input format without scientific analysis.",
    ))
    from nodes.experiment.tools.operation_completion import _record_operation_completion
    evidence = Path(state.root) / "missing_input.stdout"
    evidence.write_text("missing declared input\n", encoding="utf-8")
    result = asyncio.run(_record_operation_completion(
        state, task_kind="generic", objective="verify missing-input closure rejection", outcome="blocked",
        artifact_paths=[str(evidence)],
        checks=[{"name": "input_available", "passed": False, "evidence": {"missing": "input"}}],
        next_step="wait for a declared input package",
    ))

    assert result["status"] == "success", result
    audit = audit_operation_log_contract(state)
    assert audit["passed"] is True, audit
    assert audit["blocked_closure"] is True
    assert audit["n_registered_blockers"] == 0


def test_clean_results_freeze_is_scoped_to_result_artifacts(tmp_path: Path):
    state = _non_scientific_state(tmp_path)
    result_id = state.save_artifact(
        "clean_results", "empty_non_scientific",
        "status: completed_build\nreason: toolchain build completed; no scientific measurements were requested",
    )["id"]
    frozen = _freeze(state, result_id, "no scientific rows")
    assert frozen["status"] == "success"
    assert state.read_artifact(result_id)["metadata"]["frozen"] is True

    # "scoped tool 不能冻别的类型"这个概念随特化工具一起死了：现在只有一个
    # freeze，门按 record 的**类型**自动选 —— 逃逸口在结构上不存在，无需断言。


def test_frozen_analysis_result_protects_its_raw_evidence_from_cleanup(tmp_path: Path):
    from nodes.experiment.tools.safe_bash import _contained_cleanup

    state = _non_scientific_state(tmp_path)
    run_root = tmp_path / "run"
    run_root.mkdir()
    raw = run_root / "large-output.nc"
    raw.write_text("evidence", encoding="utf-8")
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    raw_id = state.save_artifact(
        "raw_results", "formal_raw", json.dumps({"files": [{
            "path": str(raw), "sha256": digest, "bytes": raw.stat().st_size,
            "role": "trajectory", "retention": "protected",
        }]}),
    )["id"]
    assert _freeze(state, raw_id)["status"] == "success"
    artifact_id = state.save_artifact(
        "clean_results", "formal", json.dumps({
            "status": "completed",
            "replay_manifest": {
                "raw_results_artifact_id": raw_id,
                "source_hashes": {str(raw): digest},
            },
        }),
        metadata={},
    )["id"]
    assert _freeze(state, artifact_id)["status"] == "success"
    state.hook_state["path_roles"] = {"run_root": str(run_root)}

    # No clean_results source_path is supplied: the raw manifest itself must protect it.
    assert _contained_cleanup(state, f"rm -rf {run_root}", None) is None
    assert "frozen_result_evidence_deletion_blocked" in state.transcript_path.read_text(encoding="utf-8")


def test_formal_log_freezes_before_execution_registration_and_job_finalization(tmp_path: Path):
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new("experiment", tmp_path)
    prereg_id = _save_frozen(
        state,
        "pre_registration",
        "formal_simulation",
        "# Frozen formal simulation preregistration\n",
        metadata={
            "run_role": "primary",
            "execution_mode": "scientific",
            "stage": "simulation",
        },
    )["id"]
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "experiment_focus": "Execute the frozen formal simulation and retain its evidence.",
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="Execute the frozen formal scientific fixture before registering its result.",
    ))
    assert classified["status"] == "success", classified
    content = """## Verdict
verdict: provisional
measured metric: 1.0
threshold comparison: 1.0 >= 0.9
next step: hand results to Analysis research_state

## Sediment
未发现 methodological 或 dead_end finding；原因：本次只产生了经验测量。

## Credibility
credibility: reliable
evidence: returncode=0
"""
    log_id = state.save_artifact("experiment_log", "formal", content)["id"]
    result = _freeze(state, log_id, "evidence frozen before final closure")
    assert result["status"] == "success"
    assert state.read_artifact(log_id)["metadata"]["frozen"] is True
    assert audit_experiment_contract(state)["execution_record"]["passed"] is True
    assert audit_experiment_contract(state)["execution_record"]["registration_status"] == "not_registered"


def test_execution_record_binds_current_frozen_log_without_a_chunk_id(tmp_path: Path):
    state, log_id = _scientific_execution_state(tmp_path)
    frozen = _freeze_log_with_receipt(state, log_id)
    registration = _register_execution_record(state)

    audit = audit_execution_record(state)

    assert frozen["status"] == "success", frozen
    assert registration["status"] == "success", registration
    assert registration["id"].startswith("experiment_")
    assert audit["passed"] is True, audit
    assert audit["n_verified_registrations"] == 1
    assert audit["registrations"][0]["experiment_id"] == registration["id"]


def test_execution_record_rejects_fake_chunk_when_kb_record_is_missing(tmp_path: Path):
    state, log_id = _scientific_execution_state(tmp_path)
    assert _freeze_log_with_receipt(state, log_id)["status"] == "success"
    state.append_transcript(
        "tool_call", name="create_experiment",
        args={"experiment_text": "forged", "frozen_log_chunk_id": "chunk_forged"},
    )
    state.append_transcript(
        "tool_result", name="create_experiment",
        result_preview={"status": "success", "id": "experiment_missing_record"},
    )

    audit = audit_execution_record(state)

    assert audit["passed"] is False
    assert audit["n_registrations"] == 1
    assert audit["registrations"][0]["record_exists"] is False


def test_execution_record_rejects_record_bound_to_another_run(tmp_path: Path):
    state, log_id = _scientific_execution_state(tmp_path)
    assert _freeze_log_with_receipt(state, log_id)["status"] == "success"
    old_record, _ = state.write_kb("experiments", {
        "experiment_text": "prior diagnostic execution",
        "setup_text": "controlled prior diagnostic",
        "run_at": "2026-08-21T00:00:00+00:00",
        "outcome": "success",
        "frozen_log_chunk_id": "",
        "tested_hypothesis_ids": [],
        "run_by_run_id": "prior-run",
        "created_by_role": "agent_auto",
        "created_by_run_id": "prior-run",
        "created_by_node_type": "experiment",
    })
    state.append_transcript(
        "tool_call", name="create_experiment",
        args={"experiment_text": "prior diagnostic execution", "setup_text": "controlled prior diagnostic"},
    )
    state.append_transcript(
        "tool_result", name="create_experiment",
        result_preview={"status": "success", "id": old_record["id"]},
    )

    audit = audit_execution_record(state)

    assert audit["passed"] is False
    assert audit["registrations"][0]["record_exists"] is True
    assert audit["registrations"][0]["bound_to_current_run"] is False


def test_execution_record_requires_freeze_before_registration(tmp_path: Path):
    state, log_id = _scientific_execution_state(tmp_path)
    registration = _register_execution_record(state)
    assert registration["status"] == "success"
    assert _freeze_log_with_receipt(state, log_id)["status"] == "success"

    audit = audit_execution_record(state)

    assert audit["passed"] is False
    assert audit["registrations"][0]["frozen_log_precedes_registration"] is False


def test_execution_record_rejects_duplicate_current_run_logs(tmp_path: Path):
    state, log_id = _scientific_execution_state(tmp_path)
    assert _freeze_log_with_receipt(state, log_id)["status"] == "success"
    _save_frozen(
        state, "experiment_log", "duplicate_execution_log",
        "## Execution Status\nstatus: duplicate\n## Credibility\ncredibility: questionable\n",
    )
    assert _register_execution_record(state)["status"] == "success"

    audit = audit_execution_record(state)

    assert audit["passed"] is False
    assert audit["canonical_log_id"] == log_id


def test_duplicate_experiment_log_cannot_be_frozen_as_a_replacement(tmp_path: Path):
    state = _non_scientific_state(tmp_path)
    original = _non_scientific_log(state, "original")
    _non_scientific_log(state, "replacement_v2")
    integrity = audit_experiment_log_integrity(state)
    assert integrity["passed"] is False
    result = _freeze(state, original, "must reject duplicate")
    assert result["status"] == "error"
    assert "experiment_log_integrity" in result["failed_checks"]


def test_prior_run_log_does_not_block_current_run_log_freeze(tmp_path: Path):
    state = _non_scientific_state(tmp_path)
    current_log_id = _non_scientific_log(state, "live_submission")
    historical_id = "experiment_log__prior_dry_run"
    historical = {
        "type": "experiment_log", "name": "prior_dry_run",
        "content": "status: dry_run",
        "metadata": {"frozen": True},
        "produced_by_run_id": "prior-run",
    }
    original_list, original_read = state.list_artifacts, state.read_artifact

    def list_artifacts(artifact_type=None, own_only=False):
        records = original_list(artifact_type, own_only=own_only)
        if artifact_type == "experiment_log":
            return [{"id": historical_id, "type": "experiment_log"}] + records
        return records

    def read_artifact(artifact_id):
        return historical if artifact_id == historical_id else original_read(artifact_id)

    state.list_artifacts, state.read_artifact = list_artifacts, read_artifact
    integrity = audit_experiment_log_integrity(state)
    assert integrity["passed"] is True
    assert integrity["canonical_log_id"] == current_log_id

    result = _freeze(state, current_log_id)
    assert result["status"] == "success"
    assert state.read_artifact(current_log_id)["metadata"]["frozen"] is True


def _supersede(state, artifact_id, reason):
    from nodes.experiment.tools.contract_audit import _supersede_closure_draft
    return asyncio.run(_supersede_closure_draft(
        state=state, artifact_id=artifact_id, reason=reason))


def test_duplicate_log_failure_names_supersede_tool_as_the_way_out(tmp_path: Path):
    state = _non_scientific_state(tmp_path)
    _non_scientific_log(state, "original")
    _non_scientific_log(state, "mistake")
    integrity = audit_experiment_log_integrity(state)
    assert integrity["passed"] is False
    assert "supersede_closure_draft" in integrity["reason"]


def test_superseding_the_mistaken_draft_restores_one_canonical_log(tmp_path: Path):
    state = _non_scientific_state(tmp_path)
    original = _non_scientific_log(state, "original")
    mistake = _non_scientific_log(state, "mistake")
    result = _supersede(state, mistake, "误建的第二份草稿，与 canonical log 重复")
    assert result["status"] == "success"
    integrity = audit_experiment_log_integrity(state)
    assert integrity["passed"] is True
    assert integrity["canonical_log_id"] == original
    assert integrity["superseded_ids"] == [mistake]
    # 否定记录本身立即冻结为不可变事实
    supersession = state.read_artifact(result["supersession_id"])
    assert supersession["metadata"]["frozen"] is True
    # 被否定的草稿原样保留，未被删除或改写
    assert state.read_artifact(mistake) is not None


def test_frozen_log_cannot_be_superseded(tmp_path: Path):
    state = _non_scientific_state(tmp_path)
    log_id = _non_scientific_log(state, "original")
    assert _freeze(state, log_id, "verified evidence")["status"] == "success"
    result = _supersede(state, log_id, "尝试否定已冻结的证据，必须被拒")
    assert result["status"] == "error"
    assert "冻结" in result["error"]
    assert audit_experiment_log_integrity(state)["passed"] is True


def test_supersession_requires_reason_and_is_not_repeatable(tmp_path: Path):
    state = _non_scientific_state(tmp_path)
    _non_scientific_log(state, "original")
    mistake = _non_scientific_log(state, "mistake")
    assert _supersede(state, mistake, "")["status"] == "error"
    first = _supersede(state, mistake, "误建的第二份草稿，与 canonical log 重复")
    assert first["status"] == "success"
    repeated = _supersede(state, mistake, "重复否定必须被拒")
    assert repeated["status"] == "error"
    assert repeated["supersession_id"] == first["supersession_id"]
    # 否定记录本身不可再否定（它不是 experiment_log）
    negate_negation = _supersede(state, first["supersession_id"], "尝试否定否定记录")
    assert negate_negation["status"] == "error"


def test_freeze_gate_admits_canonical_log_after_superseding_the_duplicate(tmp_path: Path):
    state = _non_scientific_state(tmp_path)
    original = _non_scientific_log(state, "original")
    mistake = _non_scientific_log(state, "mistake")
    blocked = _freeze(state, original, "must reject while duplicate exists")
    assert blocked["status"] == "error"
    assert "experiment_log_integrity" in blocked["failed_checks"]
    supersede = _supersede(state, mistake, "误建的重复草稿，否定后放行 canonical")
    assert supersede["status"] == "success"
    result = _freeze(state, original, "canonical after supersession")
    assert result["status"] == "success"
    assert state.read_artifact(original)["metadata"]["frozen"] is True


def test_json_encoded_tool_result_keeps_late_inconclusive_declaration_auditable(tmp_path: Path):
    from nodes.experiment.tools import sediment
    from nodes.experiment.tools.contract_audit import audit_verdict

    state = State.new("experiment", tmp_path)
    state.hook_state["run_contract"] = {"run_role": "primary", "stage": "simulation"}
    content = """## Hypothesis Verdict
verdict: inconclusive
## Credibility
credibility: questionable
"""
    saved = _save_frozen(state, "experiment_log", "late", content)
    args = {
        "reason": "没有冻结 hypothesis claim，工程运行成功不能构成科学证据。",
        "next_step": "由 hypothesis 节点建立正式 claim 后再运行。",
    }
    result = asyncio.run(sediment._declare_inconclusive_verdict(state, **args))
    state.append_transcript("tool_call", name="declare_inconclusive_verdict", args=args)
    state.append_transcript(
        "tool_result", name="declare_inconclusive_verdict",
        result_preview=json.dumps(result, ensure_ascii=False),
    )
    audit = audit_verdict(state)
    assert saved["id"] == result["experiment_log_id"]
    assert audit["passed"] is True
    assert audit["has_explicit_declaration"] is True


def test_clean_results_freeze_rejects_unstructured_or_reasonless_empty_result(tmp_path: Path):
    state = _non_scientific_state(tmp_path)
    invalid_id = state.save_artifact("clean_results", "invalid", "arbitrary text")["id"]
    invalid = _freeze(state, invalid_id)
    assert invalid["status"] == "error"
    assert any("structured object" in error for error in invalid["errors"])

    empty_id = state.save_artifact(
        "clean_results", "reasonless", "not_replayable: true\nstatus: failed",
    )["id"]
    empty = _freeze(state, empty_id)
    assert empty["status"] == "error"
    assert "reason" in empty["errors"][0]

def test_clean_results_freeze_rejects_nonfinite_numbers_and_prereg_bounds(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    prereg_id = _save_frozen(state, "pre_registration", "bounded", "# prereg", metadata={
        "run_role": "primary",
        "result_invariants": {"results.*.R_max": {"min": 0.0, "max": 1.0}},
    })["id"]
    state.hook_state["node_inputs"] = {"prereg_artifact_id": prereg_id}
    base = {"status": "completed",
            "replay_manifest": {"source_hashes": {"spectrum.txt": "abc"}}}
    nonfinite = dict(base, results=[{"R_max": float("nan")}])
    nonfinite_id = state.save_artifact("clean_results", "nonfinite", json.dumps(nonfinite))["id"]
    rejected_nonfinite = _freeze(state, nonfinite_id)
    assert rejected_nonfinite["status"] == "error"
    assert any("non-finite" in error for error in rejected_nonfinite["errors"])

    out_of_bounds = dict(base, results=[{"R_max": 1.2}])
    bounds_id = state.save_artifact("clean_results", "out_of_bounds", json.dumps(out_of_bounds))["id"]
    rejected_bounds = _freeze(state, bounds_id)
    assert rejected_bounds["status"] == "error"
    assert any("above max" in error for error in rejected_bounds["errors"])


def test_completed_analysis_result_requires_source_hashes(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    artifact_id = state.save_artifact("clean_results", "unhashed", json.dumps({
        "status": "completed", "results": []}))["id"]
    rejected = _freeze(state, artifact_id)
    assert rejected["status"] == "error"
    assert "source_hashes" in rejected["errors"][0]


def _frozen_raw(state: State, name: str, files: list[Path]) -> tuple[str, dict[str, str]]:
    """冻结一份覆盖给定文件的 raw manifest，返回 (artifact_id, path->sha256)。"""
    digests = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    raw_id = state.save_artifact("raw_results", name, json.dumps({"files": [{
        "path": str(path), "sha256": digests[str(path)], "bytes": path.stat().st_size,
        "role": "result", "retention": "protected",
    } for path in files]}))["id"]
    assert _freeze(state, raw_id)["status"] == "success"
    return raw_id, digests


def _clean_with_source_hashes(state: State, name: str, raw_id: str, source_hashes) -> str:
    return state.save_artifact("clean_results", name, json.dumps({
        "status": "completed",
        "replay_manifest": {"raw_results_artifact_id": raw_id, "source_hashes": source_hashes},
    }))["id"]


def _adopted_payload(result: dict) -> dict:
    """把拒绝信息里给的 payload 原样取出来 —— 模型能抄，测试就能解析。"""
    joined = "; ".join(result["errors"])
    assert "adopt: " in joined, joined
    return json.loads(joined.split("adopt: ", 1)[1].split("; omits", 1)[0])


def test_source_hashes_rejection_reports_the_observed_type_not_a_bare_requires(tmp_path: Path):
    """传错形状与完全没传必须给出不同的诊断：模型明明传了，不能再答 "requires"。"""
    state = _non_scientific_state(tmp_path)
    raw = tmp_path / "verlet_result.json"
    raw.write_bytes(b'{"max_rel_energy_drift": 2.4e-05}')
    raw_id, digests = _frozen_raw(state, "primary", [raw])
    digest = digests[str(raw)]

    as_list = _freeze(state, _clean_with_source_hashes(state, "as_list", raw_id, [digest]))
    assert as_list["status"] == "error"
    assert any("got a list" in error for error in as_list["errors"])
    assert _adopted_payload(as_list) == {"source_hashes": {"verlet_result.json": digest}}

    as_empty = _freeze(state, _clean_with_source_hashes(state, "as_empty", raw_id, {}))
    assert as_empty["status"] == "error"
    assert any("got an empty object" in error for error in as_empty["errors"])
    assert _adopted_payload(as_empty) == {"source_hashes": {"verlet_result.json": digest}}

    missing = state.save_artifact("clean_results", "no_replay", json.dumps({
        "status": "completed"}))["id"]
    rejected_missing = _freeze(state, missing)
    assert rejected_missing["status"] == "error"
    assert any("got nothing" in error for error in rejected_missing["errors"])


def test_derived_source_hashes_payload_is_accepted_by_the_gate_that_produced_it(tmp_path: Path):
    """派生的 payload 必须能过门自己的覆盖检查 —— 重名文件下 basename 会撞车。"""
    state = _non_scientific_state(tmp_path)
    first, second = tmp_path / "rank0", tmp_path / "rank1"
    first.mkdir(), second.mkdir()
    logs = [first / "output.log", second / "output.log"]
    logs[0].write_bytes(b"rank 0 finished")
    logs[1].write_bytes(b"rank 1 finished")
    raw_id, digests = _frozen_raw(state, "sharded", logs)

    rejected = _freeze(state, _clean_with_source_hashes(state, "shaped_wrong", raw_id, []))
    assert rejected["status"] == "error"
    adopted = _adopted_payload(rejected)
    # basename 相同 → 整组退回绝对路径，两个 hash 都留在表上
    assert set(adopted["source_hashes"]) == {str(logs[0]), str(logs[1])}
    assert set(adopted["source_hashes"].values()) == set(digests.values())

    accepted = _freeze(state, _clean_with_source_hashes(
        state, "adopted", raw_id, adopted["source_hashes"]))
    assert accepted["status"] == "success", accepted


def test_operational_clean_results_is_not_pushed_toward_a_replay_manifest(tmp_path: Path):
    """operation 的收尾产物不是科学测量：哪怕 status=completed 也不该被教补回放链。

    原先这里还有一条"operation 必须 analysis_eligible=false"的门，与"完成即要回放链"
    合成一个把模型往反方向推的陷阱。资格位拆掉后陷阱的一半自动消失，另一半
    （不教 operation 补回放链）由 execution_mode 守卫担保——本用例钉的就是它。
    """
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {"experiment_focus": "Install the toolchain."}
    assert asyncio.run(_classify_experiment_scope(
        state, scope="operation", operation_category="package_install",
        reason="Install a package before any scientific execution.",
    ))["status"] == "success"

    wrong_id = state.save_artifact("clean_results", "op_wrong", json.dumps({
        "record_kind": "scientific", "status": "completed"}))["id"]
    rejected = _freeze(state, wrong_id)
    assert rejected["status"] == "error"
    assert any("record_kind must be operation" in error for error in rejected["errors"])
    assert not any("replay_manifest" in error for error in rejected["errors"])


def test_raw_results_freeze_verifies_files_and_clean_results_binds_frozen_manifest(tmp_path: Path):
    state = _non_scientific_state(tmp_path)
    raw = tmp_path / "trajectory.bin"
    digest = hashlib.sha256(b"actual raw bytes").hexdigest()
    missing_id = state.save_artifact("raw_results", "missing", json.dumps({"files": [{
        "path": str(raw), "sha256": digest, "bytes": len(b"actual raw bytes"),
        "role": "trajectory", "retention": "protected",
    }]}))["id"]
    missing = _freeze(state, missing_id)
    assert missing["status"] == "error"
    assert any("not a readable regular file" in error for error in missing["errors"])

    raw.write_bytes(b"actual raw bytes")
    mismatch_id = state.save_artifact("raw_results", "mismatch", json.dumps({"files": [{
        "path": str(raw), "sha256": "0" * 64, "bytes": raw.stat().st_size + 1,
        "role": "trajectory", "retention": "protected",
    }]}))["id"]
    mismatch = _freeze(state, mismatch_id)
    assert mismatch["status"] == "error"
    assert any("bytes mismatch" in error for error in mismatch["errors"])
    assert any("sha256 mismatch" in error for error in mismatch["errors"])
    # 真实 E2E（2026-09-02）：shell 被磁盘 reserve 挡死后节点算不出 sha256，
    # 而 gate 当场算过实际摘要却只说 "mismatch"。节点为了拿到这一个值，
    # 往科学路线 DAG 里插了个 hash_outputs 步骤。实际值必须报出来。
    assert any(digest in error for error in mismatch["errors"]), mismatch["errors"]
    assert any("0" * 64 in error for error in mismatch["errors"]), mismatch["errors"]

    valid_id = state.save_artifact("raw_results", "valid", json.dumps({"files": [{
        "path": str(raw), "sha256": digest, "bytes": raw.stat().st_size,
        "role": "trajectory", "retention": "protected",
    }]}))["id"]
    assert _freeze(state, valid_id)["status"] == "success"
    assert audit_result_evidence(state)["passed"] is False  # clean_results is still missing

    clean_id = state.save_artifact("clean_results", "unbound", json.dumps({
        "status": "completed",
        "replay_manifest": {"source_hashes": {str(raw): digest}},
    }))["id"]
    unbound = _freeze(state, clean_id)
    assert unbound["status"] == "error"
    assert any("raw_results_artifact_id" in error for error in unbound["errors"])

    bound_id = state.save_artifact("clean_results", "bound", json.dumps({
        "status": "completed",
        "replay_manifest": {
            "raw_results_artifact_id": valid_id,
            "source_hashes": {str(raw): digest},
        },
    }))["id"]
    assert _freeze(state, bound_id)["status"] == "success"
    assert audit_result_evidence(state)["passed"] is True

    # A later historical artifact must not replace this run's evidence merely
    # because it appears last in State.list_artifacts().
    original_list, original_read = state.list_artifacts, state.read_artifact
    historical_id = "raw_results__historical"
    historical = {
        "id": historical_id, "type": "raw_results", "content": "{}",
        "metadata": {"frozen": True}, "produced_by_run_id": "prior-run",
    }

    def _with_historical_last(artifact_type=None):
        items = original_list(artifact_type)
        if artifact_type == "raw_results":
            return [*items, {"id": historical_id, "type": "raw_results"}]
        return items

    def _read_with_historical(artifact_id):
        return historical if artifact_id == historical_id else original_read(artifact_id)

    state.list_artifacts = _with_historical_last
    state.read_artifact = _read_with_historical
    current_only = audit_result_evidence(state)
    assert current_only["passed"] is True, current_only
    assert current_only["artifacts"]["raw_results"]["artifact_id"] == valid_id

    raw.unlink()
    vanished = audit_result_evidence(state)
    assert vanished["passed"] is False
    assert any("not a readable regular file" in error for error in vanished["errors"])


def test_formal_data_service_delivery_requires_managed_dispatch_receipt(
    tmp_path: Path, monkeypatch,
):
    """A visible dataset is not formal input until the managed Data child returned it."""
    from nodes.experiment.tests.test_blocked_operation_closure import _write_child_dataset

    state = _non_scientific_state(tmp_path)
    prereg_id = state.hook_state["node_inputs"]["prereg_artifact_id"]
    visible_package = tmp_path / "visible-data-package"
    visible_package.mkdir()
    (visible_package / "channel.msh").write_text("mesh bytes", encoding="utf-8")
    manifest = visible_package / "manifest.json"
    manifest.write_text(json.dumps({"lineage": [{"op": "generate_and_review_mesh"}]}), encoding="utf-8")
    request = json.dumps({
        "request_kind": "formal_input_preparation",
        "source_prereg_artifact_id": prereg_id,
        "scientific_parameters": "frozen_prereg_only",
        "required_assets": [{"name": "channel.msh", "format": "Gmsh", "purpose": "solver mesh"}],
        "acceptance": {"file_exists": True, "schema": True, "units": True, "manifest_lineage": True},
    })
    validated = asyncio.run(_validate_data_request_spec(state, request))
    assert validated["status"] == "success"
    visible_dataset_id = state.save_artifact("dataset", "visible_mesh_package", json.dumps({
        "package_dir": str(visible_package), "manifest_path": str(manifest),
        "lineage": [{"op": "generate_and_review_mesh"}],
        "downstream_contract": {"files": ["channel.msh"]},
    }))["id"]

    refused = asyncio.run(_verify_dataset_consumption(
        state, visible_dataset_id, validated["spec_id"],
    ))

    assert refused["status"] == "error", refused
    assert refused["error_code"] == "formal_input_requires_dispatch_receipt"
    assert refused["spec_id"] == validated["spec_id"]
    delivery = state.hook_state["input_delivery_state"][validated["spec_id"]]
    assert delivery.get("verified") is not True

    # Treat next_step as the tool call it claims to be: parse its exact kwargs,
    # then invoke the existing managed-dispatch path with no hand-made receipt.
    next_call = ast.parse(refused["next_step"], mode="eval").body
    assert isinstance(next_call, ast.Call)
    assert isinstance(next_call.func, ast.Name)
    assert next_call.func.id == "dispatch_data_request"
    assert next_call.args == []
    next_kwargs = {
        keyword.arg: ast.literal_eval(keyword.value)
        for keyword in next_call.keywords
        if keyword.arg is not None
    }
    assert next_kwargs["spec_id"] == validated["spec_id"]

    child_run_id = "data-formal-input-child"
    dataset_id = "dataset__managed_formal_mesh"
    managed_package = tmp_path / "managed-data-package"
    managed_package.mkdir()
    (managed_package / "channel.msh").write_text("managed mesh bytes", encoding="utf-8")

    async def fake_run_node(**kwargs):
        assert kwargs["node_type"] == "data"
        assert kwargs["node_inputs"] == validated["dispatch_node_inputs"]
        assert kwargs["user_note"] == next_kwargs["user_note"]
        child_run_dir = state.root.parent / child_run_id
        child_run_dir.mkdir(parents=True, exist_ok=True)
        (child_run_dir / "transcript.jsonl").write_text(json.dumps({
            "event": "run_start",
            "node_type": "data",
            "parent_run_id": state.run_id,
        }) + "\n", encoding="utf-8")
        _write_child_dataset(state, child_run_id, dataset_id, managed_package)
        return {
            "status": "completed",
            "child_run_id": child_run_id,
            "child_node_type": "data",
            "child_status": "completed",
            "all_child_artifacts": [{"id": dataset_id}],
        }

    monkeypatch.setattr("shared.tools.run_node._run_node_tool", fake_run_node)
    dispatched = asyncio.run(_dispatch_data_request(state, **next_kwargs))
    assert dispatched["status"] == "completed", dispatched

    consumed = asyncio.run(_verify_dataset_consumption(
        state, dataset_id, validated["spec_id"],
    ))
    assert consumed["status"] == "success", consumed
    assert consumed["passed"] is True

def test_raw_data_service_delivery_contract_rejects_missing_package_asset(tmp_path: Path):
    """The non-formal Raw Data compatibility path still performs shape validation."""
    state = State.new("experiment", tmp_path)
    package = tmp_path / "data-package"
    package.mkdir()
    manifest = package / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    request = json.dumps({
        "request_kind": "preprocessing_service_request",
        "requesting_stage": "toolchain_build",
        "purpose": "validate a solver input package",
        "target_software": "OpenFOAM 11",
        "scientific_parameters": "not_applicable",
        "required_assets": [{"name": "missing.msh", "format": "Gmsh", "purpose": "solver mesh"}],
        "acceptance": {"file_exists": True, "schema": True, "units": True, "manifest_lineage": True},
    })
    validated = asyncio.run(_validate_data_request_spec(state, request))
    dataset_id = state.save_artifact("dataset", "incomplete_mesh_package", json.dumps({
        "package_dir": str(package), "manifest_path": str(manifest),
        "lineage": [{"op": "generate_and_review_mesh"}],
        "downstream_contract": {"files": ["missing.msh"]},
    }))["id"]
    consumed = asyncio.run(_verify_dataset_consumption(state, dataset_id, validated["spec_id"]))
    assert consumed["status"] == "error"
    assert consumed["missing_requested_assets"] == ["missing.msh"]


def _fallback_spec(prereg_id: str) -> str:
    return json.dumps({
        "request_kind": "formal_input_preparation",
        "source_prereg_artifact_id": prereg_id,
        "scientific_parameters": "frozen_prereg_only",
        "required_assets": [{
            "name": "case/input.dat",
            "format": "text",
            "purpose": "solver input",
        }],
        "acceptance": {
            "file_exists": True, "schema": True, "units": True,
            "manifest_lineage": True,
        },
    })


def _bound_fallback_state(
    tmp_path: Path, *, role: str, policy: dict[str, object],
) -> tuple[State, str]:
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new("experiment", tmp_path)
    prereg_id = _save_frozen(
        state,
        "pre_registration",
        "fallback_contract",
        "# frozen fallback preregistration",
        metadata={
            "run_role": role,
            "execution_mode": "scientific",
            "stage": "simulation",
            "expected_params": {"grid": [12, 12]},
            "input_delivery_policy": policy,
        },
    )["id"]
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "experiment_focus": "consume only the frozen formal input for this scientific fixture",
    }
    classified = asyncio.run(_classify_experiment_scope(
        state, scope="scientific",
        reason="Bind the fixture to its frozen preregistration before any data effect.",
    ))
    assert classified["status"] == "success", classified
    return state, prereg_id


def _write_data_child_blocker(state: State, child_run_id: str, report_id: str) -> None:
    """Write the minimal persisted shape left behind by a completed Data child.

    The producer facts live in the child's run-local ledger
    (``<run>/records.jsonl``); the report body is the native file next to it.
    """
    from core.artifact_provenance import produced
    from core.ledger import RecordStore

    child_root = state.root.parent / child_run_id
    child_root.mkdir(parents=True, exist_ok=True)
    (child_root / "transcript.jsonl").write_text(json.dumps({
        "event": "run_start",
        "node_type": "data",
        "parent_run_id": state.run_id,
    }) + "\n", encoding="utf-8")
    RecordStore(child_root / "artifacts", child_root / "records.jsonl").save(
        artifact_id=report_id,
        artifact_type="preprocessing_blocked_report",
        name=report_id.split("__", 1)[-1],
        content=json.dumps({
            "status": "fatal",
            "reason": "the declared source rejected every fetch attempt",
            "acquisition_attempts": [{
                "url": "https://data.example.org/declared-asset",
                "failure_type": "http_403",
                "evidence_path": "logs/fetch.stderr",
            }],
        }),
        metadata={},
        directory=child_root / "artifacts",
        created_at="2026-09-12T00:00:00+00:00",
        provenance=produced("data", child_run_id),
        produced_by_node_type="data",
        produced_by_run_id=child_run_id,
        by_node="data",
        by_run=child_run_id,
    )


def _data_terminal_blocker(state: State, spec_id: str) -> tuple[str, str]:
    """Install a durable managed-dispatch receipt around a real child artifact shape."""
    from nodes.experiment.tools.contract_audit import _data_dispatch_material
    from nodes.experiment.tools.input_delivery import (
        load_input_delivery_ledger, save_input_delivery_ledger,
    )

    ledger = load_input_delivery_ledger(state)
    entry = ledger["specs"][spec_id]
    child_run_id = "data-child-" + spec_id.rsplit("__", 1)[-1]
    report_id = "preprocessing_blocked_report__planning_contract_failure"
    _write_data_child_blocker(state, child_run_id, report_id)
    material = _data_dispatch_material(spec_id, entry["request"])
    entry["delivery"]["data_dispatch_receipt"] = {
        "schema_version": material["schema_version"],
        "spec_id": spec_id,
        "request_sha256": material["request_sha256"],
        "payload_sha256": material["payload_sha256"],
        "child_run_id": child_run_id,
        "child_node_type": "data",
        "child_status": "incomplete",
        "child_artifact_ids": [report_id],
    }
    save_input_delivery_ledger(state, ledger)
    return child_run_id, report_id


def _verified_fallback_input(state: State, prereg_id: str, tmp_path: Path) -> tuple[str, Path, str]:
    request = asyncio.run(_validate_data_request_spec(state, _fallback_spec(prereg_id)))
    assert request["status"] == "success", request
    from nodes.experiment.tools.run_contract import load_run_contract
    contract = load_run_contract(state)
    data_run_id, report_id = _data_terminal_blocker(state, request["spec_id"])
    recorded = asyncio.run(_record_data_delivery_outcome(
        state, request["spec_id"], "blocked", data_run_id, report_id,
    ))
    assert recorded["status"] == "success", recorded
    authorized = asyncio.run(_authorize_experiment_fallback(state, request["spec_id"]))
    assert authorized["status"] == "success", authorized
    package = tmp_path / "fallback-package" / "case"
    package.mkdir(parents=True)
    input_path = package / "input.dat"
    input_path.write_text("real generated input", encoding="utf-8")
    manifest = package.parent / "manifest.json"
    manifest.write_text(json.dumps({
        "generator": "replay.py",
        "files": [{
            "path": "case/input.dat",
            "sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
        }],
    }), encoding="utf-8")
    fallback_id = state.save_artifact(
        "experiment_fallback_inputs",
        "inputs",
        json.dumps({
            "generation_mode": "experiment_fallback_after_data_failure",
            "source_prereg_artifact_id": prereg_id,
            "source_prereg_version": contract["prereg_version"],
            "source_prereg_content_hash": contract["prereg_content_hash"],
            "data_blocked_report_ids": [report_id],
            "scientific_params": {"grid": [12, 12]},
            "package_dir": str(package.parent),
            "manifest_path": str(manifest),
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "replay_source": "replay.py",
        }),
    )["id"]
    verified = asyncio.run(_verify_experiment_fallback_inputs(
        state, fallback_id, request["spec_id"],
    ))
    assert verified["status"] == "success", verified
    return request["spec_id"], input_path, fallback_id


def test_formal_fallback_uses_managed_data_dispatch_receipt_end_to_end(
    tmp_path: Path, monkeypatch,
):
    """Fallback authority starts with the actual managed child, not a hand-made receipt."""
    state, prereg_id = _bound_fallback_state(
        tmp_path,
        role="primary",
        policy={
            "mode": "experiment_data_fallback",
            "experiment_fallback_permitted": True,
        },
    )
    request = asyncio.run(_validate_data_request_spec(state, _fallback_spec(prereg_id)))
    assert request["status"] == "success", request
    child_run_id = "data-formal-managed-child"
    report_id = "preprocessing_blocked_report__formal_input_unavailable"

    async def fake_run_node(**kwargs):
        assert kwargs["node_type"] == "data"
        assert kwargs["node_inputs"] == request["dispatch_node_inputs"]
        assert kwargs["resume_run_id"] == "fresh"
        _write_data_child_blocker(state, child_run_id, report_id)
        return {
            "status": "incomplete",
            "child_run_id": child_run_id,
            "child_node_type": "data",
            "child_status": "incomplete",
            "all_child_artifacts": [{"id": report_id}],
        }

    monkeypatch.setattr("shared.tools.run_node._run_node_tool", fake_run_node)
    dispatched = asyncio.run(_dispatch_data_request(
        state, request["spec_id"],
        user_note="Ask Data to prepare the frozen formal input before any Experiment fallback.",
    ))
    assert dispatched["data_dispatch_receipt"]["child_run_id"] == child_run_id

    recorded = asyncio.run(_record_data_delivery_outcome(
        state, request["spec_id"], "blocked", child_run_id, report_id,
    ))
    assert recorded["status"] == "success", recorded
    authorized = asyncio.run(_authorize_experiment_fallback(state, request["spec_id"]))

    assert authorized["status"] == "success", authorized
    assert state.hook_state["input_delivery_state"][request["spec_id"]][
        "blocked_report_id"
    ] == report_id


def test_formal_dispatch_rejects_late_prereg_after_pending_receipt(
    tmp_path: Path, monkeypatch,
):
    """A late prereg cannot turn a pending entrance assignment into authority."""
    from nodes.experiment.tools import contract_audit as contract_audit_module
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Prepare formal input only if it was accepted at run entrance.",
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="other",
        reason="Accept the run before any preregistration is visible.",
    ))
    assert classified["status"] == "success", classified

    late_prereg = _save_frozen(
        state,
        "pre_registration",
        "late_formal_dispatch",
        "# late frozen preregistration",
        metadata={
            "run_role": "primary",
            "execution_mode": "scientific",
            "expected_params": {"grid": [12, 12]},
        },
    )["id"]
    request = asyncio.run(_validate_data_request_spec(
        state, _fallback_spec(late_prereg),
    ))
    assert request["status"] == "success", request

    # Isolate Gate B: the generic intent audit normally rejects this drift first.
    monkeypatch.setattr(
        contract_audit_module,
        "audit_execution_intent_binding",
        lambda *_args, **_kwargs: {"passed": True, "status": "bound"},
    )
    real_resolver = contract_audit_module.resolve_run_acceptance
    bind_flags: list[bool] = []

    def resolver_spy(state_arg, *, bind_if_absent):
        bind_flags.append(bind_if_absent)
        return real_resolver(state_arg, bind_if_absent=bind_if_absent)

    child_calls: list[dict] = []

    async def fake_run_node(**kwargs):
        child_calls.append(kwargs)
        return {"status": "success", "child_run_id": "must-not-launch"}

    monkeypatch.setattr(contract_audit_module, "resolve_run_acceptance", resolver_spy)
    monkeypatch.setattr("shared.tools.run_node._run_node_tool", fake_run_node)

    dispatched = asyncio.run(_dispatch_data_request(
        state,
        request["spec_id"],
        user_note="Prepare the exact frozen formal input.",
    ))

    assert dispatched["status"] == "error", dispatched
    assert dispatched["error"] == (
        "formal Data dispatch requires the current frozen preregistration receipt"
    )
    assert dispatched["run_acceptance"]["receipt"]["prereg_assignment"] == {
        "kind": "pending",
    }
    assert dispatched["current_prereg_witness"]["authorizing"] is False
    assert bind_flags == [False]
    assert child_calls == []


def test_formal_dispatch_keeps_explicit_binding_when_project_catalog_grows(
    tmp_path: Path, monkeypatch,
):
    """An unrelated later prereg cannot make a bound same-run receipt ambiguous."""
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new("experiment", tmp_path)
    accepted_prereg = _save_frozen(
        state,
        "pre_registration",
        "accepted_unique_prereg",
        "# accepted frozen preregistration",
        metadata={
            "run_role": "primary",
            "execution_mode": "scientific",
            "expected_params": {"grid": [12, 12]},
        },
    )["id"]
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": accepted_prereg,
        "experiment_focus": "Execute the caller-selected preregistration.",
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="Bind the unique frozen preregistration before formal dispatch.",
    ))
    assert classified["status"] == "success", classified
    assert classified["classification"]["binding_source"] == "explicit_node_input"

    _save_frozen(
        state,
        "pre_registration",
        "later_unrelated_prereg",
        "# unrelated frozen preregistration",
        metadata={
            "run_role": "primary",
            "execution_mode": "scientific",
            "expected_params": {"grid": [24, 24]},
        },
    )
    request = asyncio.run(_validate_data_request_spec(
        state, _fallback_spec(accepted_prereg),
    ))
    assert request["status"] == "success", request

    child_run_id = "data-unique-auto-stable-child"
    report_id = "preprocessing_blocked_report__unique_auto_input_unavailable"
    child_calls: list[dict] = []

    async def fake_run_node(**kwargs):
        child_calls.append(kwargs)
        _write_data_child_blocker(state, child_run_id, report_id)
        return {
            "status": "incomplete",
            "child_run_id": child_run_id,
            "child_node_type": "data",
            "child_status": "incomplete",
            "all_child_artifacts": [{"id": report_id}],
        }

    monkeypatch.setattr("shared.tools.run_node._run_node_tool", fake_run_node)
    dispatched = asyncio.run(_dispatch_data_request(
        state,
        request["spec_id"],
        user_note="Prepare input for the preregistration accepted by this run.",
    ))

    assert child_calls, dispatched
    assert dispatched["data_dispatch_receipt"]["child_run_id"] == child_run_id


def test_primary_formal_generic_fallback_is_consumable_and_revalidated(tmp_path: Path):
    state, prereg_id = _bound_fallback_state(
        tmp_path,
        role="primary",
        policy={
            "mode": "experiment_data_fallback",
            "experiment_fallback_permitted": True,
        },
    )
    spec_id, input_path, fallback_id = _verified_fallback_input(state, prereg_id, tmp_path)

    audit = audit_input_delivery_for_execution(state, fallback_id)
    assert audit["passed"] is True, audit
    delivery = state.hook_state["input_delivery_state"][spec_id]
    assert delivery["provider"] == "experiment_fallback"
    # ca:1272（呈裁③定案）：使用 fallback 即如实记一条执行前提见证，
    # run 内不可撤销 —— 即便冻结 prereg 本身允许 primary 分析，用了替代交付这一
    # 事实本身就把资格压下来。效应而非许可。
    from nodes.experiment.tools.run_contract import load_run_contract as _lrc
    assert _lrc(state)["execution_precondition_witnesses"]

    input_path.write_text("tampered after verification", encoding="utf-8")
    tampered = audit_input_delivery_for_execution(state, fallback_id)
    assert tampered["passed"] is False
    assert tampered["blocking_reasons"] == ["experiment_fallback_authorization_invalid"]
    assert any("manifest_hash_mismatch:case/input.dat" in errors
               for errors in tampered["fallback_receipt_errors"].values())

    input_path.write_text("real generated input", encoding="utf-8")
    fallback_record = state.read_artifact(fallback_id)
    fallback_payload = json.loads(fallback_record["content"])
    manifest_path = Path(fallback_payload["manifest_path"])
    manifest_path.write_text(manifest_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    manifest_tampered = audit_input_delivery_for_execution(state, fallback_id)
    assert manifest_tampered["passed"] is False
    assert any("manifest_sha256_mismatch" in errors
               for errors in manifest_tampered["fallback_receipt_errors"].values())


def test_execution_rejects_tampered_managed_data_dispatch_receipt(tmp_path: Path):
    """The child/spec receipt is revalidated immediately before execution."""
    from nodes.experiment.tools.input_delivery import (
        load_input_delivery_ledger, save_input_delivery_ledger,
    )

    state, prereg_id = _bound_fallback_state(
        tmp_path,
        role="primary",
        policy={
            "mode": "experiment_data_fallback",
            "experiment_fallback_permitted": True,
        },
    )
    spec_id, _input_path, fallback_id = _verified_fallback_input(state, prereg_id, tmp_path)
    ledger = load_input_delivery_ledger(state)
    ledger["specs"][spec_id]["delivery"]["data_dispatch_receipt"]["payload_sha256"] = "0" * 64
    save_input_delivery_ledger(state, ledger)

    audit = audit_input_delivery_for_execution(state, fallback_id)

    assert audit["passed"] is False
    assert audit["blocking_reasons"] == ["experiment_fallback_authorization_invalid"]
    assert any("data_dispatch_payload_sha256_mismatch" in errors
               for errors in audit["fallback_receipt_errors"].values())


def test_generic_fallback_policy_requires_a_registered_verified_input(tmp_path: Path):
    state, _ = _bound_fallback_state(
        tmp_path,
        role="secondary",
        policy={
            "mode": "experiment_data_fallback",
            "experiment_fallback_permitted": True,
        },
    )

    audit = audit_input_delivery_for_execution(state)
    assert audit["passed"] is False
    assert audit["blocking_reasons"] == ["experiment_fallback_input_required"]


def test_generic_fallback_records_prereg_that_appears_after_scope_classification(tmp_path: Path):
    """A later prereg cannot retrofit fallback authority onto an older scope."""
    from nodes.experiment.tools.run_contract import _classify_experiment_scope

    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Execute an exploratory scientific task without a governing preregistration.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "No governing preregistration was assigned at run entrance.",
        },
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="Classify the declared scientific work before any frozen prereg is available.",
    ))
    assert classified["status"] == "success", classified
    prereg_id = _save_frozen(
        state,
        "pre_registration",
        "late_fallback_contract",
        "# late frozen preregistration",
        metadata={
            "run_role": "primary",
            "execution_mode": "scientific",
            "expected_params": {"grid": [12, 12]},
            "input_delivery_policy": {
                "mode": "experiment_data_fallback",
                "experiment_fallback_permitted": True,
            },
        },
    )["id"]
    request = asyncio.run(_validate_data_request_spec(state, _fallback_spec(prereg_id)))
    assert request["status"] == "success", request
    data_run_id, report_id = _data_terminal_blocker(state, request["spec_id"])
    recorded = asyncio.run(_record_data_delivery_outcome(
        state, request["spec_id"], "blocked", data_run_id, report_id,
    ))
    assert recorded["status"] == "success", recorded

    rejected = asyncio.run(_authorize_experiment_fallback(state, request["spec_id"]))

    # ca:1272 降格（呈裁③定案）：契约不允许 fallback 不再是拒绝理由 —— 授权照给，
    # 但执行前提见证如实记下（效应而非许可），契约限制如实进账。
    assert rejected["status"] == "success", rejected
    assert rejected["execution_precondition_unmet"] is True
    assert rejected["fallback_permitted_by_contract"] is False
    assert "does not permit Experiment data fallback" in \
        rejected["contract_restriction_recorded"]


def test_hook_contract_cannot_enable_fallback_absent_from_frozen_prereg(tmp_path: Path):
    from nodes.experiment.tools.run_contract import load_run_contract

    state, prereg_id = _bound_fallback_state(
        tmp_path,
        role="primary",
        policy={"mode": "data_only", "experiment_fallback_permitted": False},
    )
    state.hook_state["run_contract"] = {
        "input_delivery_policy": {
            "mode": "experiment_data_fallback",
            "experiment_fallback_permitted": True,
        },
    }
    contract = load_run_contract(state)
    assert contract["input_delivery_policy"] == {
        "mode": "data_only", "experiment_fallback_permitted": False,
    }

    request = asyncio.run(_validate_data_request_spec(state, _fallback_spec(prereg_id)))
    assert request["status"] == "success", request
    data_run_id, report_id = _data_terminal_blocker(state, request["spec_id"])
    assert asyncio.run(_record_data_delivery_outcome(
        state, request["spec_id"], "blocked", data_run_id, report_id,
    ))["status"] == "success"
    rejected = asyncio.run(_authorize_experiment_fallback(state, request["spec_id"]))
    # ca:1272 降格：同上，授权照给 + 契约限制如实进账。
    assert rejected["status"] == "success", rejected
    assert rejected["execution_precondition_unmet"] is True
    assert rejected["fallback_permitted_by_contract"] is False
    assert "does not permit" in rejected["contract_restriction_recorded"]


def test_legacy_secondary_fallback_remains_nonanalysis_compatible(tmp_path: Path):
    state, prereg_id = _bound_fallback_state(
        tmp_path,
        role="secondary",
        policy={
            "mode": "integration_e2e_fallback",
            "experiment_fallback_permitted": True,
        },
    )
    spec_id, _, fallback_id = _verified_fallback_input(state, prereg_id, tmp_path)

    audit = audit_input_delivery_for_execution(state, fallback_id)
    assert audit["passed"] is True, audit
    delivery = state.hook_state["input_delivery_state"][spec_id]
    from nodes.experiment.tools.run_contract import load_run_contract as _lrc
    assert _lrc(state)["execution_precondition_witnesses"]


def test_primary_records_legacy_secondary_only_fallback_policy_restriction(tmp_path: Path):
    state, prereg_id = _bound_fallback_state(
        tmp_path,
        role="primary",
        policy={
            "mode": "integration_e2e_fallback",
            "experiment_fallback_permitted": True,
        },
    )
    request = asyncio.run(_validate_data_request_spec(state, _fallback_spec(prereg_id)))
    data_run_id, report_id = _data_terminal_blocker(state, request["spec_id"])
    recorded = asyncio.run(_record_data_delivery_outcome(
        state, request["spec_id"], "blocked", data_run_id, report_id,
    ))
    assert recorded["status"] == "success", recorded
    rejected = asyncio.run(_authorize_experiment_fallback(state, request["spec_id"]))
    # ca:1272 降格：同上。legacy 策略不再是拒绝理由，但记录里说得清清楚楚。
    assert rejected["status"] == "success", rejected
    assert rejected["execution_precondition_unmet"] is True
    assert rejected["fallback_permitted_by_contract"] is False
    assert "legacy integration_e2e_fallback" in rejected["contract_restriction_recorded"]


def test_formal_fallback_spec_must_reference_the_current_bound_prereg(tmp_path: Path):
    state, prereg_a = _bound_fallback_state(
        tmp_path,
        role="primary",
        policy={
            "mode": "experiment_data_fallback",
            "experiment_fallback_permitted": True,
        },
    )
    prereg_b = _save_frozen(
        state,
        "pre_registration",
        "other_contract",
        "# unrelated frozen preregistration",
        metadata={
            "run_role": "primary",
            "execution_mode": "scientific",
            "expected_params": {"grid": [12, 12]},
        },
    )["id"]
    assert prereg_a != prereg_b

    rejected = asyncio.run(_validate_data_request_spec(state, _fallback_spec(prereg_b)))
    assert rejected["status"] == "error"
    assert "source_prereg_artifact_id must match the run bound frozen pre_registration" in rejected["errors"]

    same_identity_wrong_snapshot = json.loads(_fallback_spec(prereg_a))
    same_identity_wrong_snapshot["source_prereg_version"] = 99
    same_identity_wrong_snapshot["source_prereg_content_hash"] = "0" * 64
    wrong_snapshot = asyncio.run(_validate_data_request_spec(
        state, json.dumps(same_identity_wrong_snapshot),
    ))
    assert wrong_snapshot["status"] == "error"
    assert "source_prereg_version must match the run bound frozen pre_registration" in wrong_snapshot["errors"]
    assert "source_prereg_content_hash must match the run bound frozen pre_registration" in wrong_snapshot["errors"]


def test_python_install_without_package_identity_is_recorded_as_failed_check(tmp_path: Path):
    """判决拆除·第三波（oc:156 降格）：缺 package_name/import_name 不再拒记收据——
    与同函数 O6 先例一致，是一条 passed=False 的 check，outcome 机械降 partial。
    墙加回去（status=error）即转红。"""
    from nodes.experiment.tools import run_contract
    from nodes.experiment.tools.operation_completion import _record_operation_completion

    state = State.new("experiment", tmp_path)
    # 受管生命周期要求可核验的上游意图（execution_intent_binding_required）；
    # 上游那版跑在裸 state 上，这里补上派发方本来就会传的 node_inputs。
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Install a Python package and file the operation receipt.",
    }
    asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="package_install",
        reason="Install receipt filed without naming the distribution.",
    ))
    completion = asyncio.run(_record_operation_completion(
        state, task_kind="python_install", objective="install something unnamed",
    ))

    assert completion["status"] == "success", completion
    assert "package_identity_declared" in completion["failed_checks"]
    assert completion["outcome"] == "partial" and completion["outcome_demoted_from"] == "success"
    names = [check["name"] for check in completion["checks"]]
    assert "package_metadata" not in names and "python_import" not in names


def test_python_install_operation_creates_bound_triplet_and_detects_tampering(tmp_path: Path):
    from nodes.experiment.tools import run_contract
    from nodes.experiment.tools.operation_completion import _record_operation_completion

    state = State.new("experiment", tmp_path)
    _bind_operation_inputs(state)
    asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="package_install",
        reason="Verify an already available Python distribution without scientific analysis.",
    ))
    # pytest 一定在测试进程里可导入且有 dist 元数据；pip 在 uv 管理的 venv 里
    # 可能缺席，会让本测试在「验证成功三件套」的意图之外分岔成 partial。
    completion = asyncio.run(_record_operation_completion(
        state, task_kind="python_install", objective="verify the local pytest distribution",
        package_name="pytest", import_name="pytest",
    ))

    assert completion["status"] == "success", completion
    clean = json.loads(state.read_artifact(completion["clean_results_artifact_id"])["content"])
    assert clean["record_kind"] == "operation"
    assert "analysis_eligible" not in clean
    assert clean["raw_results_artifact_id"] == completion["raw_results_artifact_id"]
    assert clean["task_kind"] == "python_install"
    assert clean["outcome"] == "success"
    assert audit_operation_log_contract(state)["passed"] is True

    raw = json.loads(state.read_artifact(completion["raw_results_artifact_id"])["content"])
    receipt = Path(raw["files"][0]["path"])
    receipt.write_text(receipt.read_text(encoding="utf-8") + "\ntampered", encoding="utf-8")
    audit = audit_operation_log_contract(state)

    assert audit["passed"] is False
    assert any("sha256 mismatch" in error for error in audit["result_evidence"]["errors"])


def test_operation_clean_results_rejects_wrong_raw_binding_before_freeze(tmp_path: Path):
    from nodes.experiment.tools import run_contract

    state = State.new("experiment", tmp_path)
    _bind_operation_inputs(state)
    asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="format_validation",
        reason="Validate an input format without producing scientific results.",
    ))
    source = tmp_path / "operation.stdout"
    source.write_text("returncode=0\n", encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    raw_id = state.save_artifact("raw_results", "operation_raw", json.dumps({"files": [{
        "path": str(source), "sha256": digest, "bytes": source.stat().st_size,
        "role": "stdout", "retention": "protected",
    }]})) ["id"]
    assert _freeze(state, raw_id)["status"] == "success"
    clean_id = state.save_artifact("clean_results", "wrong_binding", json.dumps({
        "record_kind": "operation", "status": "completed",
        "reason": "operation has no scientific conclusion",
        "raw_results_artifact_id": "raw_results__other_run",
        "verification": {"passed": True, "checks": [{"name": "returncode", "passed": True}]},
    })) ["id"]

    result = _freeze(state, clean_id)

    assert result["status"] == "error"
    assert any("bind the unique current-run raw_results" in error for error in result["errors"])


def test_a_stale_eligibility_bit_in_the_payload_is_neither_gate_nor_requirement(tmp_path: Path):
    from nodes.experiment.tools import run_contract

    state = State.new("experiment", tmp_path)
    _bind_operation_inputs(state)
    asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="format_validation",
        reason="Validate an input format without producing scientific results.",
    ))
    source = tmp_path / "operation.stdout"
    source.write_text("returncode=0\n", encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    raw_id = state.save_artifact("raw_results", "operation_raw", json.dumps({"files": [{
        "path": str(source), "sha256": digest, "bytes": source.stat().st_size,
        "role": "stdout", "retention": "protected",
    }]}))["id"]
    assert _freeze(state, raw_id)["status"] == "success"
    # 载荷里带一个陈旧的资格位，既不被要求、也不再构成门：operation 身份由
    # record_kind 判定（AGENTS.md:143 禁止该资格门以任何名字复活）。
    payload = {
        "record_kind": "operation",
        "status": "completed",
        "reason": "a stale eligibility bit must neither gate nor be demanded",
        "raw_results_artifact_id": raw_id,
        "verification": {"passed": True, "checks": [{"name": "returncode", "passed": True}]},
    }
    payload["analysis_eligible"] = True  # 陈旧键，刻意留着
    clean_id = state.save_artifact(
        "clean_results", "operation_stale_eligibility", json.dumps(payload))["id"]

    result = _freeze(state, clean_id)

    assert result["status"] == "success", result
    assert not any("analysis_eligible" in error
                   for error in (result.get("errors") or []))


def test_operation_audit_binds_clean_summary_to_receipt_and_retains_all_evidence(tmp_path: Path):
    from nodes.experiment.tools import run_contract
    from nodes.experiment.tools.operation_completion import _record_operation_completion

    state = State.new("experiment", tmp_path)
    _bind_operation_inputs(state)
    asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="format_validation",
        reason="Validate a local executable and report without scientific simulation.",
    ))
    executable = state.root / "solver"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    report = state.root / "report.json"
    report.write_text("{\"status\": \"ok\"}\n", encoding="utf-8")
    completion = asyncio.run(_record_operation_completion(
        state, task_kind="generic", objective="validate the local solver artifacts",
        executable_paths=[str(executable)], json_paths=[str(report)],
        checks=[{"name": "build_returncode", "passed": True, "evidence": {"returncode": 0}}],
    ))

    assert completion["status"] == "success", completion
    raw = json.loads(state.read_artifact(completion["raw_results_artifact_id"])["content"])
    assert {entry["path"] for entry in raw["files"]} >= {str(executable.resolve()), str(report.resolve())}

    # Simulate a corrupted or wrongly assembled frozen summary.  The receipt
    # still proves the original task; the audit must not accept a different one.
    clean_path = state.find_artifact_path(completion["clean_results_artifact_id"])
    assert clean_path is not None
    clean = json.loads(clean_path.read_text(encoding="utf-8"))
    clean["objective"] = "a different operation"
    clean_path.write_text(json.dumps(clean), encoding="utf-8")

    audit = audit_operation_log_contract(state)

    assert audit["passed"] is False
    assert any("objective must match the verification receipt" in error for error in audit["result_evidence"]["errors"])


def test_operation_audit_rejects_handwritten_success_receipt_with_failed_check(tmp_path: Path):
    from nodes.experiment.tools import run_contract

    state = State.new("experiment", tmp_path)
    _bind_operation_inputs(state)
    asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="format_validation",
        reason="Verify that handwritten artifacts cannot claim a failed check as success.",
    ))
    receipt_path = state.root / "forged_receipt.json"
    checks = [{"name": "format_parse", "passed": False, "evidence": {"returncode": 1}}]
    receipt = {
        "schema_version": 1, "record_kind": "operation", "task_kind": "generic",
        "objective": "validate one input", "outcome": "success", "checks": checks,
        "next_step": None, "summary": None,
    }
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    raw_id = state.save_artifact("raw_results", "forged_operation_raw", json.dumps({"files": [{
        "path": str(receipt_path.resolve()),
        "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        "bytes": receipt_path.stat().st_size,
        "role": "operation_verification_receipt", "retention": "protected",
    }]}), metadata={"record_kind": "operation"})["id"]
    assert _freeze(state, raw_id)["status"] == "success"
    clean_id = state.save_artifact("clean_results", "forged_operation_clean", json.dumps({
        "record_kind": "operation", "status": "completed",
        "reason": "operation has no scientific conclusion", "raw_results_artifact_id": raw_id,
        "task_kind": "generic", "objective": "validate one input", "outcome": "success",
        "verification": {"passed": True, "checks": checks},
    }), metadata={"record_kind": "operation", "status": "completed"})["id"]
    assert _freeze(state, clean_id)["status"] == "success"
    state.save_artifact("experiment_log", "forged_operation_log", "## Execution\ncommand recorded\n\n## Verification\ncheck recorded", metadata={"record_kind": "operation", "status": "completed"})

    audit = audit_operation_log_contract(state, require_frozen=False)

    assert audit["passed"] is False
    assert any("successful operation verification receipt cannot contain failed checks" in error for error in audit["result_evidence"]["errors"])

def test_operation_may_optionally_sediment_a_methodological_claim_after_triplet_freeze(tmp_path: Path):
    """Operation evidence completes first; KB knowledge is an optional second branch."""
    from nodes.experiment.tools import run_contract
    from shared.tools.library.kb import _create_claim

    state = State.new("experiment", tmp_path)
    _bind_operation_inputs(state)
    asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="package_install",
        reason="Capture a reusable package compatibility finding after local verification.",
    ))
    completion = _complete_operation(
        state, summary="The package verification exposed a reusable compatibility constraint.")
    assert completion["status"] == "success", completion

    claim = asyncio.run(_create_claim(
        state,
        claim_text="The verified package requires an explicit compatibility constraint before deployment.",
        claim_type="methodological",
        sources=[completion["experiment_log_artifact_id"]],
        confidence=0.6,
        orphan_reason="This operation produced a reusable method but no concept is registered yet.",
    ))

    assert claim["status"] == "success", claim
    saved = state.get_kb_record("claims", claim["id"])
    assert saved is not None
    assert any(str(source).startswith("chunk_") for source in saved["sources"])
    assert audit_operation_log_contract(state)["passed"] is True


# ── 资格位拆除后的新语义（2026-09-11）────────────────────────────────────────


def test_a_completed_scientific_result_must_carry_a_replay_manifest(tmp_path: Path):
    """原先写 analysis_eligible:false 就能免交回放链；资格位拆掉后这条要求是无条件的。"""
    state = State.new("experiment", tmp_path)
    clean_id = state.save_artifact("clean_results", "no_replay", json.dumps({
        "status": "completed", "results": [{"value": 1.0}],
    }))["id"]

    rejected = _freeze(state, clean_id)

    assert rejected["status"] == "error"
    assert any("replay_manifest" in error for error in rejected["errors"])
    assert not any("analysis_eligible" in error for error in rejected["errors"])


def test_a_genuinely_unreplayable_result_declares_it_and_says_why(tmp_path: Path):
    """诚实逃生口：确实无法重放的，如实申报并给理由即可冻结——不必调低自己的结论资格。"""
    state = State.new("experiment", tmp_path)
    honest_id = state.save_artifact("clean_results", "honest", json.dumps({
        "status": "completed",
        "not_replayable": True,
        "reason": "the upstream instrument stream is not retained; this run cannot be replayed",
        "results": [{"value": 1.0}],
    }))["id"]

    accepted = _freeze(state, honest_id)

    assert accepted["status"] == "success", accepted
