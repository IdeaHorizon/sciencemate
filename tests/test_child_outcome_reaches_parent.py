"""Child corrective feedback survives finalize, first return and compaction (#1382)."""

import json

import pytest

from core.agent_loop import LoopResult
from core.executor import finalize_run
from core.harness import NodeHarness
from core.state import State
from shared.tools.run_node import _compact_run_node_result, _finish_child


@pytest.mark.asyncio
async def test_structured_outcome_survives_the_complete_return_chain(tmp_path):
    parent = State.new("_orchestrator", tmp_path)
    child = State.new("data", tmp_path)
    outcome = {
        "outcome": "success",
        "dispatch_action": "reuse_delivery",
        "redispatch_required": False,
        "artifact_id": "mesh-identity",
        "dispatch_feedback": {
            "reason": "x" * 700 + "This was a scheduling preference, not a user requirement",
            "audit_ref": "audit.json",
        },
    }
    child.hook_state["node_task_outcome"] = outcome
    harness = NodeHarness(node_type="data")
    summary = await finalize_run(
        child, harness, LoopResult(final_text="x" * 800, turns=1, messages=[]), None
    )
    assert summary["node_task_outcome"] == outcome
    result = _finish_child(parent, "data", {}, summary, harness, None)
    assert result["node_task_outcome"] == outcome
    compact = json.loads(_compact_run_node_result(json.dumps(result)).split("\n", 1)[1])
    assert compact["node_task_outcome"] == outcome
