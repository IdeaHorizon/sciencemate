"""The browser reads the nested Offer, so review evidence must survive there."""
import asyncio
import json

from core.artifact_provenance import forwarded, produced
from core.decision_offer import Offer
from core.state import State
from shared.tools.library.decision_package import _present_decision_package


def _present(tmp_path, critique=None, *, failed_reason=None):
    state = State.new(node_type="_orchestrator", base_dir=tmp_path, project_id="p1")
    cid = None
    if critique is not None:
        cid = state.save_artifact(
            "review_critique", "current", json.dumps(critique, ensure_ascii=False),
            metadata={"produced_by_node_type": "_reviewer"},
            provenance=forwarded(produced("_reviewer", "review-run"),
                                 via_node_type=state.node_type, via_run_id=state.run_id),
        )["id"]
    state.hook_state["pending_post_node_flow"] = [{
        "producing_node": "hypothesis", "producing_run_id": "producer-run",
        "artifact_ids": ["research_state__plan"],
        "review_state": "done" if critique is not None else "failed_awaiting_human",
        "review_critique_artifact_id": cid,
        "curator_state": "done", "decision_state": "pending",
    }]
    return asyncio.run(_present_decision_package(
        state, source_node_type="hypothesis", producing_run_id="producer-run",
        producing_summary="已交付研究计划，等待独立审查后的裁决。",
        artifact_ids_produced=["research_state__plan"],
        review_critique_artifact_id=cid, review_failed_reason=failed_reason,
        human_decision_reason="需要课题负责人确认本次研究取舍。",
    ))["pause_event"]


def test_review_evidence_and_full_report_belong_to_the_transported_offer(tmp_path):
    critique = {
        "verdict": "major_concerns", "summary": "研究计划尚有一处可比性问题。",
        "review_subject": {"artifact_id": "research_state__plan", "version": 2},
        "concerns": [{"severity": "major", "title": "对照条件未固定",
                      "description": "两组使用不同的评估预算。",
                      "suggestion": "固定两组的评估预算。",
                      "evidence_citations": ["plan §2"]}],
        "recommended_action": {"action": "revise", "feedback_to_next_run": "固定预算后重交计划。"},
    }
    event = _present(tmp_path, critique)
    offer = Offer.from_pause_payload(json.loads(json.dumps(event["offer"])))
    assert offer.context == event["context"]
    assert "两组使用不同的评估预算" in offer.context
    assert "固定预算后重交计划。" in offer.context
    assert offer.recommended_id == "revise"


def test_framework_veto_explains_why_recommendation_differs_from_reviewer(tmp_path):
    event = _present(tmp_path, {
        "verdict": "approve", "concerns": [{"severity": "critical", "description": "关键证据缺失。"}],
        "recommended_action": {"action": "proceed"},
    })
    offer = Offer.from_pause_payload(event["offer"])
    assert offer.recommended_id == "revise"
    assert "关键证据缺失" in offer.context
    assert "框架否决" in offer.context


def test_review_failure_reaches_the_offer_without_fabricating_findings(tmp_path):
    event = _present(tmp_path, failed_reason="审查执行超时，未产生报告。")
    offer = Offer.from_pause_payload(event["offer"])
    assert offer.facts["reviewFailedReason"] == "审查执行超时，未产生报告。"
    assert offer.recommended_id == "retry_reviewer"
    assert "proceed" not in offer.choice_ids()
    assert "审查执行超时" in offer.context
