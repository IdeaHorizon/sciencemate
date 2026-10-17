"""The model sees a committed receipt, while the frozen delivery keeps full evidence."""

from __future__ import annotations

import asyncio
import json

from core.state import State
from core.tool_registry import execute as execute_tool
from nodes.experiment.tools import delivery_transaction, finish, ledger, run_bootstrap


def _state(tmp_path):
    state = State.new(
        node_type="experiment", base_dir=tmp_path / "runs",
        project_id="finish-receipt", session_id="owner",
    )
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Run a local self-test and report its result.",
        "prereg_assignment": {"kind": "none", "reason": "fixture"},
        "scope": "operation",
        "operation_category": "environment_probe",
    }
    asyncio.run(run_bootstrap.establish(state))
    led = ledger.open_ledger(state, write=True)
    led.append(
        "action_started", action_id="self-test", tool="safe_run_bash",
        effect_class="experiment", command="true", work_root="run",
    )
    led.append("action_settled", action_id="self-test", state="settled", exit_code=0)
    return state


def test_registered_finish_returns_small_receipt_and_keeps_full_delivery(tmp_path):
    state = _state(tmp_path)
    kwargs = {"outcome": "partial", "summary": "Self-test ran; remaining work is disclosed."}

    receipt = asyncio.run(execute_tool("finish", state, **kwargs))

    assert receipt["status"] == "success", receipt
    assert receipt["outcome"] == "partial"
    assert receipt["revision"] == 1
    assert receipt["reused_manifest"] is False
    assert "delivery" not in receipt and "gaps" not in receipt
    assert "结束" in receipt["next_action"]
    assert len(json.dumps(receipt, ensure_ascii=False)) < 2_000
    assert set(receipt["artifact_ids"]) == set(delivery_transaction.TRIO)
    for artifact_id in receipt["artifact_ids"].values():
        assert state.artifact_head(artifact_id).frozen is True

    replay = asyncio.run(execute_tool("finish", state, **kwargs))
    assert replay["status"] == "success" and replay["reused_manifest"] is True
    assert replay["closure_id"] == receipt["closure_id"]
    assert replay["revision"] == 1
    assert "required_satisfied" not in replay
    full = asyncio.run(finish._finish(state, **kwargs))
    assert full["delivery"]["outcome"] == "partial"
    assert full["artifact_ids"] == receipt["artifact_ids"]


def test_large_evidence_does_not_hide_terminal_signal_or_rewrite_refusals():
    huge = {
        "status": "success", "closure_id": "closure", "revision": 2,
        "reused_manifest": False, "artifact_ids": {kind: kind for kind in delivery_transaction.TRIO},
        "delivery": {
            "outcome": "failed", "gaps": [{"evidence": "x" * 400_000}],
            "contradictions": [], "limitations": ["real failure"],
        },
    }
    receipt = finish._model_finish_receipt(huge)
    assert receipt["outcome"] == "failed" and receipt["gap_count"] == 1
    assert len(json.dumps(receipt, ensure_ascii=False)) < 2_000

    refusal = {"status": "success_unsupported", "gaps": [{"gap": "missing"}]}
    assert finish._model_finish_receipt(refusal) is refusal


def test_registered_receipt_uses_sealed_claim_not_recovery_call_arguments(
    tmp_path, monkeypatch,
):
    state = State.new(
        node_type="experiment", base_dir=tmp_path / "runs",
        project_id="finish-recovery", session_id="owner",
    )

    async def recovered(_state, outcome, summary, **_kwargs):
        assert outcome == "success" and summary == "new request"
        return {
            "status": "success", "closure_id": "old-closure",
            "revision": 1, "reused_manifest": True,
            "artifact_ids": {kind: kind for kind in delivery_transaction.TRIO},
            "delivery": {
                "outcome": "blocked", "summary": "sealed older claim",
                "gaps": [{"gap": "missing input"}], "contradictions": [],
                "limitations": ["input was unavailable"],
                "next_step": "request the input",
                "blocker": {"summary": "data handoff pending"},
            },
        }

    monkeypatch.setattr(finish, "_finish", recovered)
    # The full suite may re-register tools from a reloaded module. Exercise the
    # wrapper owned by this module for this mocked recovery branch; the first
    # test above separately verifies the actual registered dispatch path.
    receipt = asyncio.run(finish._finish_for_model(
        state, outcome="success", summary="new request",
    ))
    assert receipt["status"] == "success"
    assert receipt["outcome"] == "blocked"
    assert receipt["sealed_summary"] == "sealed older claim"
    assert receipt["sealed_limitations"] == ["input was unavailable"]
    assert receipt["sealed_next_step"] == "request the input"
    assert receipt["sealed_blocker_summary"] == "data handoff pending"
    assert receipt["revision"] == 1 and receipt["reused_manifest"] is True
