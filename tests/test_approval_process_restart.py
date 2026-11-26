"""Crash after a real local side effect, then recover its owner's receipt.

The fresh interpreter must never repeat the append even if the model retries
or the tool is declared re-entrant. This covers the approval/result checkpoint gap.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

WORKER = r"""
import asyncio, json, os, sys
from pathlib import Path
from core import approvals, tool_registry
from core.state import State
from shared.lib import dangerous_commands as danger

root, mode = Path(sys.argv[1]), sys.argv[2]
state = State(run_id="original", node_type="experiment", root=root, session_id="s")
state.hook_state["_current_tool_call_id_being_executed"] = "original-call"
effect = root / "effects"
receipt = root / "owner-receipt.json"

async def execute(*, state, **kwargs):
    if not danger.is_confirmed(state, "bounded append"):
        return danger.build_pause_payload(state, tool="durable_append", text="bounded append",
                                         category="audit", preview="append once to test-owned file")
    with effect.open("a") as stream:
        stream.write("effect\n"); stream.flush(); os.fsync(stream.fileno())
    receipt.write_text(json.dumps({"status":"success", "execution":"original-call"}))
    os._exit(17)  # tool receipt exists, dispatcher/message checkpoint did not commit

def recover(arguments, *, state, tool_call_id):
    assert tool_call_id == "original-call"
    return {"result": json.loads(receipt.read_text())} if receipt.exists() else None

tool_registry.register_tool(tool_registry.ToolDefinition(name="durable_append", description="test",
    parameters_schema={"type":"object"}, interrupted_recovery=recover), execute)
danger.BYPASS_ENABLED = False
danger.PREAUTHORIZED_CATEGORIES = set()

async def main():
    if mode == "crash":
        payload = (await tool_registry.execute("durable_append", state))["pause_event"]
        record = approvals.decide(state, payload, "approve")
        (root / "operation-id").write_text(record["operation_id"])
        await approvals.run_approved(state, record["operation_id"])
    else:
        operation_id = (root / "operation-id").read_text()
        result = await approvals.run_approved(state, operation_id)
        assert await approvals.run_approved(state, operation_id) == result
        print(json.dumps(result))
asyncio.run(main())
"""


def test_a_fresh_interpreter_recovers_a_receipt_without_repeating_the_effect(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    script = tmp_path / "approval-worker.py"
    script.write_text(WORKER)
    env = {**os.environ, "PYTHONPATH": str(repo), "HARNESS_FRAMEWORK_HOME": str(tmp_path / "home")}
    crashed = subprocess.run(
        [sys.executable, str(script), str(tmp_path), "crash"],
        env=env,
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert crashed.returncode == 17, crashed.stderr
    restored = subprocess.run(
        [sys.executable, str(script), str(tmp_path), "recover"],
        env=env,
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert restored.returncode == 0, restored.stderr
    assert json.loads(restored.stdout)["execution"] == "original-call"
    assert (tmp_path / "effects").read_text().splitlines() == ["effect"]
