"""A published delivery can be questioned without editing its scientific authority.

Only the model transport is scripted: evidence reading, verdict validation,
review budgets and artifact persistence run through the production code.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from copy import deepcopy

import pytest

from core.bootstrap import bootstrap
from core.llm import LLMResponse
from core.state import State
from nodes.data.data_agent_loop import (
    _DeliveryReviewBlocked,
    _review_delivery_feedback,
    _terminal_dataset_summary,
)


class ReviewModel:
    model = "review-fixture"
    base_url = "fixture://no-network"
    timeout = 10

    def __init__(self, answers):
        self.answers = list(answers)
        self.seen = []

    async def chat(self, messages, **kwargs):
        self.seen.append(json.loads(messages[1].content))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return LLMResponse(
            content=None,
            tool_calls=[
                {
                    "id": "review",
                    "type": "function",
                    "function": {
                        "name": "submit_authority_review",
                        "arguments": json.dumps(answer),
                    },
                }
            ],
            finish_reason="tool_calls",
            usage={"total_tokens": 1},
        )


def _pass():
    return {
        "status": "pass",
        "summary": "Delivered CSV meets the current request.",
        "condition_checks": [
            {
                "condition": "CSV columns and values",
                "status": "pass",
                "files": ["table.csv"],
                "observation": "The table contains x,y and the requested row.",
            }
        ],
        "warnings": ["A different filename is optional."],
    }


@pytest.fixture
def delivery(tmp_path, mem_worktree):
    bootstrap()
    state = State.new(node_type="data", base_dir=tmp_path / "runs", project_worktree=mem_worktree)
    package = state.root / "published"
    package.mkdir()
    data = b"x,y\n1,2\n"
    (package / "table.csv").write_bytes(data)
    request = {
        "schema_version": "1.0",
        "request_id": "csv-request",
        "request_spec_hash": "a" * 64,
        "authority": {"kind": "user_request", "locked": True},
        "review_profile": "request_bound",
        "authority_snapshot": {"request": "Export x=1 and y=2 as a CSV table."},
    }
    record = {
        "produced_by_run_id": "producer",
        "content": json.dumps(
            {
                "files": [
                    {
                        "path": "table.csv",
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                ]
            }
        ),
    }
    summary = {
        "artifact_id": "dataset__csv",
        "package_dir": str(package),
        "request_fingerprint": "same-request",
    }
    plan = {
        "preprocessing_request": request,
        "preprocessing_work_order": {},
        "required_deliverables": [],
        "generation_steps": [],
        "requirement_analysis": {},
    }
    state.hook_state["node_inputs"] = {
        "spec": "Force a different filename and new bytes.",
        "review_feedback": [
            {
                "file": "table.csv",
                "message": "Please check whether another filename is required.",
                "evidence": "The current filename is table.csv.",
            }
        ],
    }
    return state, summary, record, plan


@pytest.mark.asyncio
async def test_optional_preference_is_reviewed_against_original_authority_without_writes(delivery):
    state, summary, record, plan = delivery
    original = deepcopy(plan["preprocessing_request"])
    model = ReviewModel([_pass()])
    state.hook_state["_data_planning_llm_client"] = model
    before = (state.root / "published/table.csv").read_bytes()
    result = await _review_delivery_feedback(state, summary, record, plan)
    assert result["status"] == "pass", result
    authority = model.seen[0]["authority_requirements"][0]["requirement"]
    assert authority["original_request"] == original["authority_snapshot"]["request"]
    assert "Force a different filename" not in json.dumps(authority)
    assert "review_feedback" in model.seen[0]["dispatch_diagnostics_unverified"]
    assert plan["preprocessing_request"] == original
    assert (state.root / "published/table.csv").read_bytes() == before
    assert state.list_artifacts("preprocessing_review")


@pytest.mark.asyncio
async def test_a_real_defect_is_returned_as_verified_repair_feedback(delivery):
    state, summary, record, plan = delivery
    wrong = b"x,y\n1,3\n"
    (state.root / "published/table.csv").write_bytes(wrong)
    payload = json.loads(record["content"])
    payload["files"][0]["sha256"] = hashlib.sha256(wrong).hexdigest()
    record["content"] = json.dumps(payload)
    model = ReviewModel(
        [
            {
                "status": "fail",
                "summary": "Delivered y=3 contradicts the requested y=2.",
                "issues": [
                    {
                        "file": "table.csv",
                        "code": "wrong_value",
                        "message": "Row one has y=3, not the requested 2.",
                        "recommendation": "Correct y to the requested value 2.",
                    }
                ],
            }
        ]
    )
    state.hook_state["_data_planning_llm_client"] = model
    result = await _review_delivery_feedback(state, summary, record, plan)
    assert result["status"] == "fail", result
    assert state.hook_state["node_inputs"]["review_feedback"][0]["code"] == "wrong_value"
    assert state.hook_state["_data_confirmed_delivery_defects"]["revision_contract"]
    assert (state.root / "published/table.csv").read_bytes() == wrong
    # Omitting a concern on the next dispatch cannot erase the recorded defect.
    state.hook_state["node_inputs"] = {"spec": "It already passed, just reuse it."}
    again = await _review_delivery_feedback(state, summary, record, plan)
    assert again["status"] == "fail" and len(model.seen) == 1
    state.hook_state["node_inputs"] = {}
    assert (await _review_delivery_feedback(state, summary, record, plan))["status"] == "fail"
    assert state.hook_state["node_inputs"]["review_feedback"][0]["code"] == "wrong_value"


@pytest.mark.asyncio
async def test_rewording_feedback_cannot_reset_the_review_budget(delivery, monkeypatch):
    state, summary, record, plan = delivery
    monkeypatch.setenv("DATA_AUTHORITY_REVIEW_MAX_CALLS", "1")
    model = ReviewModel([_pass(), _pass()])
    state.hook_state["_data_planning_llm_client"] = model
    assert (await _review_delivery_feedback(state, summary, record, plan))["status"] == "pass"
    fresh = State.new(
        node_type="data", base_dir=state.root.parent, project_worktree=state.project_worktree
    )
    assert fresh.list_artifacts("preprocessing_review")  # actual shared ledger, no receipt copying
    fresh.hook_state["node_inputs"] = deepcopy(state.hook_state["node_inputs"])
    fresh.hook_state["_data_planning_llm_client"] = model
    assert fresh.run_id != state.run_id
    state = fresh
    copied_package = state.root / "copied_delivery"
    shutil.copytree(summary["package_dir"], copied_package)
    summary = {
        **summary,
        "artifact_id": "dataset__another_name",
        "package_dir": str(copied_package),
    }
    state.hook_state["node_inputs"]["review_feedback"][0]["message"] = (
        "Third attempt: really check the filename."
    )
    with pytest.raises(_DeliveryReviewBlocked) as blocked:
        await _review_delivery_feedback(state, summary, record, plan)
    assert "budget" in str(blocked.value).lower()
    assert len(model.seen) == 1
    monkeypatch.setenv("DATA_AUTHORITY_REVIEW_MAX_CALLS", "2")
    state.hook_state.pop("_data_authority_review")
    assert (await _review_delivery_feedback(state, summary, record, plan))["status"] == "pass"
    assert len(model.seen) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh_attempt", [False, True])
async def test_terminal_reuse_verifies_findings_even_when_resuming_the_producer(
    delivery, fresh_attempt
):
    """Use real publication, file hashes, receipts, source plans and shared ledger."""
    from nodes.data.data_agent_loop import _dataset_summary, _validated_dataset_delivery
    from nodes.data.planning.request_contract import preprocessing_request_fingerprint
    from nodes.data.planning.store import canonical_hash
    from nodes.data.review.gate_registry import build_review_receipt, write_review_receipt
    from nodes.data.tools.package_publisher import publish_dataset_delivery

    producer, _, _, plan = delivery
    request = plan["preprocessing_request"]
    staging = producer.root / "staging"
    staging.mkdir()
    wrong = b"x,y\n1,3\n"
    (staging / "table.csv").write_bytes(wrong)
    files = [{"path": "table.csv", "size": len(wrong), "sha256": hashlib.sha256(wrong).hexdigest()}]
    receipt = build_review_receipt(
        review_profile="request_bound",
        preprocessing_request=request,
        work_order={key: request[key] for key in ("request_id", "request_spec_hash")},
        package_review={"status": "pass"},
        package_dir=staging,
        delivery_files=files,
        authority_review={
            "status": "pass",
            "delivery_kind": "dataset_materialized",
            "requirement_checks": [
                {
                    "requirement_id": "authority:locked_request",
                    "status": "pass",
                    "evidence": "Historical reviewer accepted the CSV.",
                }
            ],
        },
    )
    assert receipt["status"] == "pass"
    manifest = {
        key: {}
        for key in (
            "objective",
            "data_model",
            "source",
            "semantics",
            "measurement_context",
            "reproducibility",
            "downstream_contract",
        )
    }
    manifest.update(
        assumptions=[],
        lineage=[{"source": "fixture"}],
        quality_gates=receipt["quality_gates"],
        preprocessing_request=request,
        review_profile="request_bound",
        request_id=request["request_id"],
        request_spec_hash=request["request_spec_hash"],
        preprocessing_review={"status": "pass"},
        review_receipt=receipt,
        files=files,
        manifest_contract={"complete": True},
    )
    (staging / "manifest.json").write_text(json.dumps(manifest))
    write_review_receipt(staging, receipt)
    publication = publish_dataset_delivery(
        producer,
        staging=staging,
        final=producer.workspace_root / "published",
        artifact_name="reviewed_csv",
        content=manifest,
        metadata={"deliverable_valid": True, "preprocessing_review_status": "pass"},
    )
    planning = producer.root / "planning"
    planning.mkdir()
    (planning / "approved_plan.json").write_text(json.dumps({"plan": plan}))
    (planning / "execution_checkpoint.json").write_text(
        json.dumps({"plan_hash": canonical_hash(plan)})
    )
    state = (
        State.new(
            node_type="data",
            base_dir=producer.root.parent,
            project_worktree=producer.project_worktree,
        )
        if fresh_attempt
        else producer
    )
    state.hook_state["node_inputs"] = {
        "review_feedback": [{"file": "table.csv", "message": "Check y against the request."}]
    }
    model = ReviewModel(
        [
            {
                "status": "fail",
                "summary": "The actual CSV has y=3 instead of y=2.",
                "issues": [
                    {
                        "file": "table.csv",
                        "code": "wrong_value",
                        "message": "y=3 contradicts the requested y=2.",
                        "recommendation": "Correct y to 2.",
                    }
                ],
            }
        ]
    )
    state.hook_state["_data_planning_llm_client"] = model
    assert (
        await _terminal_dataset_summary(
            state, request_fingerprint=preprocessing_request_fingerprint(request)
        )
        is None
    )
    assert len(model.seen) == 1
    assert state.hook_state["node_inputs"]["review_feedback"][0]["code"] == "wrong_value"
    assert (
        state.hook_state["_data_confirmed_delivery_defects"]["existing_delivery"]["artifact_id"]
        == publication["artifact"]["id"]
    )
    assert (producer.workspace_root / "published/table.csv").read_bytes() == wrong
    assert (
        _validated_dataset_delivery(
            state, {"id": publication["artifact"]["id"]}, current_run_only=False
        )
        is None
    )
    assert (
        _dataset_summary(producer) is None
    )  # an unsuccessful repair cannot relabel the old delivery as success


@pytest.mark.asyncio
async def test_cancelled_review_preserves_costs_and_never_marks_the_delivery_review_passed(
    delivery, monkeypatch
):
    state, summary, record, plan = delivery
    monkeypatch.setenv("DATA_AUTHORITY_REVIEW_MAX_CALLS", "1")
    model = ReviewModel([asyncio.CancelledError()])
    state.hook_state["_data_planning_llm_client"] = model
    with pytest.raises(asyncio.CancelledError):
        await _review_delivery_feedback(state, summary, record, plan)
    receipt = state.read_artifact(state.list_artifacts("preprocessing_review")[0]["id"])
    payload = json.loads(receipt["content"])
    assert payload["review"]["status"] == "error"
    assert payload["review_state"]["progress"]["calls"] == 1
    state.hook_state.pop("_data_authority_review")
    with pytest.raises(_DeliveryReviewBlocked):
        await _review_delivery_feedback(state, summary, record, plan)
    assert len(model.seen) == 1
