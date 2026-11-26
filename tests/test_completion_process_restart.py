"""Losing a real worker cannot replay an accepted effect at completion recovery."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

WORKER = """
import asyncio, json, sys
from pathlib import Path
from core import cancellation, completion_driver, loop_hooks
from core.agent_loop import LoopResult, load_messages_checkpoint
from core.completion import CompletionDecision
from core.harness import NodeHarness
from core.state import State

async def main():
    base, mode = Path(sys.argv[1]), sys.argv[2]
    harness = NodeHarness(node_type="restart_completion", max_turns=10,
                          completion_provider="restart_provider")
    if mode == "start":
        state = State.new("restart_completion", base / "runs")
        (base / "identity.json").write_text(json.dumps({"run_id": state.run_id}))
        messages = []
    else:
        identity = json.loads((base / "identity.json").read_text())
        state = State.reopen("restart_completion", base / "runs", identity["run_id"])
        messages, _ = load_messages_checkpoint(state)
    def provider(ctx, result):
        sealed = (base / "sealed").exists()
        return CompletionDecision("allow" if sealed else "reenter", "delivery", str(sealed), {},
                                  task_outcome="success" if sealed else None)
    loop_hooks.register_loop_hook(loop_hooks.LoopHook(
        "restart_provider", on_completion_admission=provider))
    async def runner():
        assert mode == "start", "recovering a sealed candidate must not replay effects"
        effect = base / "accepted_effect"
        if not effect.exists():
            effect.write_text(state.run_id)
        return LoopResult("candidate", completion_driver.turn_offset(state) + 1,
                          messages=messages, terminal_cause="model_finished")
    with cancellation.bind_run(state):
        result = await completion_driver.drive(harness, state, messages, object(), runner)
    print(json.dumps({"run_id": state.run_id, "status": result.status,
                      "counts": state.hook_state["_completion_admission"]["reentries"]}))
asyncio.run(main())
"""


@pytest.mark.parametrize("source", ["checkout", "payload"])
def test_new_worker_admits_the_same_saved_candidate_without_replaying_effects(
    tmp_path, monkeypatch, source
):
    root = Path(__file__).resolve().parents[1]
    if source == "payload":
        from scripts.package import build_server_bundle

        interface = tmp_path / "interface"
        interface.mkdir()
        (interface / "index.html").write_text("completion recovery fixture")
        monkeypatch.setattr(build_server_bundle, "DIST", tmp_path / "distribution")
        root = build_server_bundle.stage("0.5.15", interface) / "harness"
    script = tmp_path / "worker.py"
    script.write_text(WORKER)
    env = {**os.environ, "PYTHONPATH": str(root)}
    process = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path), "start"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        snapshot = None
        while time.monotonic() < deadline and process.poll() is None:
            candidates = list((tmp_path / "runs").glob("*/messages_checkpoint.json"))
            if candidates:
                snapshot = json.loads(candidates[0].read_text())
                if snapshot["hook_state"].get("_completion_admission", {}).get("suspended"):
                    break
            time.sleep(0.01)
        assert snapshot and snapshot["hook_state"]["_completion_admission"]["suspended"]
        process.kill()
        process.communicate(timeout=5)
        identity = json.loads((tmp_path / "identity.json").read_text())["run_id"]
        assert (tmp_path / "accepted_effect").read_text() == identity
        (tmp_path / "sealed").touch()
        recovered = subprocess.run(
            [sys.executable, str(script), str(tmp_path), "recover"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert recovered.returncode == 0, recovered.stderr
        result = json.loads(recovered.stdout)
        assert result["status"] == "completed"
        assert result["run_id"] == identity
        assert result["counts"] == snapshot["hook_state"]["_completion_admission"]["reentries"]
        assert (tmp_path / "accepted_effect").read_text() == identity
        assert len(list((tmp_path / "runs").iterdir())) == 1
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
