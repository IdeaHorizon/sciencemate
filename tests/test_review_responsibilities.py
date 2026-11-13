"Review scope and recovery survive real context, persistence and handoff boundaries."

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.context_engine import build_messages
from core.loader import load_harness
from core.review_policy import REVIEW_POLICY_SHA256, REVIEW_PRINCIPLES
from core.state import State


@pytest.mark.asyncio
async def test_specialist_findings_keep_their_scope_evidence_and_rubric_version(tmp_path):
    bootstrap()
    from nodes._reviewer.tools.critique_builder import _compose_review_critique as compose

    state = State.new(node_type="_reviewer", base_dir=tmp_path / "runs")
    subject = state.save_artifact(
        "observation_log", "negative_result", "No support found in the specified sample."
    )
    messages = build_messages(
        load_harness("_reviewer"),
        state,
        node_inputs={"source_node_type": "observation", "artifact_id": subject["id"]},
    )
    assert REVIEW_PRINCIPLES in messages[0].content
    assert "sampling_and_counterevidence" in "\n".join(m.content or "" for m in messages)
    await compose(
        state,
        "set_verdict",
        verdict="approve_with_revisions",
        review_scope="Exploratory report; no population-wide causal claim",
        unverified_checks=["Coverage outside the supplied archive"],
    )
    await compose(
        state,
        "add_concern",
        severity="major",
        title="Limited sample",
        description="The archive covers one region only.",
        basis="scientific_risk",
        evidence="observation_log: sources_consulted",
        impact="Does not establish population prevalence",
        affected_use="Population-wide inference",
        suggestion="Restrict the claim to the archive.",
    )
    await compose(
        state,
        "set_recommended_action",
        recommended_action="proceed",
        feedback_to_next_run="Retain the limited scope.",
    )
    result = await compose(
        state,
        "finalize",
        name="observation_scoped_review",
        artifact_under_review=subject["id"],
        source_node_type="observation",
    )
    assert result["status"] == "success", result
    record = state.read_artifact(result["artifact_id"])
    content = json.loads(record["content"])
    assert content["concerns"][0]["affected_use"] == "Population-wide inference"
    assert content["concerns"][0]["evidence"] == "observation_log: sources_consulted"
    assert record["metadata"]["review_scope"] == content["review_scope"]
    assert record["metadata"]["unverified_checks"] == ["Coverage outside the supplied archive"]
    rubric = record["metadata"]["rubric_source"]
    assert (
        rubric["owner_spec_sha256"]
        == hashlib.sha256(Path(rubric["owner_spec_path"]).read_bytes()).hexdigest()
    )
    assert rubric["review_policy_sha256"] == REVIEW_POLICY_SHA256
    assert not rubric["owner_spec_truncated"]
    from shared.tools.library.decision_package import _render_decision_package, build_decision_offer

    decision = _render_decision_package(
        source_node_type="observation",
        producing_run_id=state.run_id,
        producing_summary="",
        artifact_ids_produced=[subject["id"]],
        curator_summary="",
        review_critique_json=content,
        review_failed_reason=None,
        recommended_feedback="Retain the limited scope.",
        offer=build_decision_offer(
            decision_id="scoped-review",
            source_node_type="observation",
            action_ids=["proceed", "revise", "abort"],
            recommended_action="proceed",
        ),
    )
    for disclosed in (
        content["review_scope"],
        "Coverage outside the supplied archive",
        "observation_log: sources_consulted",
        "Population-wide inference",
    ):
        assert disclosed in decision


def test_outcome_recovery_and_review_limits_survive_the_caller_and_compression(tmp_path):
    bootstrap()
    from shared.tools.run_node import _compact_run_node_result, _finish_child

    parent = State.new(node_type="_orchestrator", base_dir=tmp_path / "runs")
    child = State.new(node_type="data", base_dir=tmp_path / "runs")
    outcome = {
        "outcome": "blocked",
        "dispatch_action": "resolve_review_blocker",
        "redispatch_required": False,
        "retry_target": "authority_review",
        "resume_contract": {"kind": "delivery_review", "artifact_id": "dataset__old"},
    }
    result = _finish_child(
        parent,
        "data",
        {},
        {
            "run_id": "data-child",
            "status": "blocked",
            "turns": 0,
            "artifacts": [],
            "state_dir": str(child.root),
            "node_task_outcome": outcome,
            "final_text_preview": "long explanation " * 100,
        },
        load_harness("data"),
        None,
    )
    assert result["redispatch_required"] is False
    result["post_node_review"] = {
        "review_state": "done",
        "review_critique_artifact_id": "review_critique__r",
        "critique": {
            "metadata": {
                "verdict": "approve",
                "review_scope": "Integrity of the failed-run report",
                "unverified_checks": ["Scientific hypothesis"],
                "review_subject": {"version": 1},
            }
        },
    }
    compacted = _compact_run_node_result(json.dumps(result))
    recovered = json.loads(compacted[compacted.index("{") :])
    assert recovered["node_task_outcome"]["outcome"] == "blocked"
    assert recovered["redispatch_required"] is False
    assert recovered["resume_contract"] == outcome["resume_contract"]
    assert recovered["post_node_review"]["review_metadata"]["unverified_checks"] == [
        "Scientific hypothesis"
    ]
    from core.llm import LLMMessage, is_framework_notice
    from core.loop_hooks import HookContext
    from core.loop_hooks_builtin import _dispatch_receipt_on_turn_start

    messages = [
        LLMMessage(role="tool", name="run_node", tool_call_id="actual-child", content=compacted)
    ]
    context = HookContext(
        harness=load_harness("_orchestrator"), state=parent, messages=messages, turn=2
    )
    notice = _dispatch_receipt_on_turn_start(context)
    assert notice and is_framework_notice(notice[0])
    assert "审核服务不可用不证明产物有缺陷" in notice[0].content
    assert _dispatch_receipt_on_turn_start(context) is None  # one receipt, one reminder
    messages.append(LLMMessage(role="user", content="New authorized task with changed inputs"))
    parent.hook_state.pop("_dispatch_receipt_notices")
    assert _dispatch_receipt_on_turn_start(context) is None


def test_descriptive_quality_score_does_not_authorize_or_require_a_rerun():
    from shared.tools.library.decision_package import _apply_critical_veto

    assert _apply_critical_veto(
        {"per_dimension_scores": {"novelty": 1, "honesty": 5}}, "proceed", None
    ) == ("proceed", None)
    action, reason = _apply_critical_veto(
        {
            "concerns": [
                {
                    "severity": "critical",
                    "description": "The stated result has no underlying execution evidence.",
                }
            ]
        },
        "proceed",
        None,
    )
    assert action == "revise" and reason


@pytest.mark.parametrize("outcome", ["partial", "blocked"])
def test_reuse_notice_keeps_an_unsuccessful_task_unsuccessful(tmp_path, outcome):
    from core.llm import LLMMessage, is_framework_notice
    from core.loop_hooks import HookContext
    from core.loop_hooks_builtin import _dispatch_receipt_on_turn_start

    parent = State.new("_orchestrator", tmp_path / "runs")
    context = HookContext(
        harness=load_harness("_orchestrator"),
        state=parent,
        turn=2,
        messages=[
            LLMMessage(
                role="tool",
                name="run_node",
                tool_call_id="partial-delivery",
                content=json.dumps(
                    {
                        "node_task_outcome": {
                            "outcome": outcome,
                            "dispatch_action": "reuse_delivery",
                            "redispatch_required": False,
                            "artifact_id": "preprocessing_document__partial",
                        }
                    }
                ),
            )
        ],
    )
    notices = _dispatch_receipt_on_turn_start(context)
    assert notices and is_framework_notice(notices[0])
    assert "原任务未确认成功" in notices[0].content
    assert "该子任务已按回执绑定的原始要求交付" not in notices[0].content


@pytest.mark.asyncio
async def test_current_single_pdf_submission_cannot_keep_its_review_after_pdf_changes(
    tmp_path, mem_worktree
):
    bootstrap()
    from core.obligations import _latest_writing_verdict
    from nodes._reviewer.tools.critique_builder import _compose_review_critique as compose

    writer = State.new("writing", tmp_path / "runs", project_worktree=mem_worktree)
    pdf = writer.workspace_root / "manuscript.pdf"
    pdf.write_bytes(b"%PDF-1.4\nsubmitted version")
    subject = writer.save_artifact(
        "manuscript",
        "single_pdf",
        "Actual submitted source snapshot",
        metadata={"pdf_path": str(pdf)},
    )
    reviewer = State.new("_reviewer", tmp_path / "runs", project_worktree=mem_worktree)
    build_messages(
        load_harness("_reviewer"),
        reviewer,
        node_inputs={"source_node_type": "writing", "artifact_id": subject["id"]},
    )
    await compose(reviewer, "set_verdict", verdict="approve")
    await compose(reviewer, "set_recommended_action", recommended_action="proceed")
    result = await compose(
        reviewer,
        "finalize",
        name="writing_critique_single_pdf",
        artifact_under_review=subject["id"],
        source_node_type="writing",
    )
    assert result["status"] == "success", result
    critique = reviewer.read_artifact(result["artifact_id"])
    hashes = critique["metadata"]["review_subject"]["pdf_variant_sha256"]
    assert hashes == {"primary": hashlib.sha256(pdf.read_bytes()).hexdigest()}
    record = writer.read_artifact(subject["id"])
    assert _latest_writing_verdict(mem_worktree, subject["id"], record) == "proceed"
    pdf.write_bytes(b"%PDF-1.4\nchanged after review")
    assert _latest_writing_verdict(mem_worktree, subject["id"], record) == ""
