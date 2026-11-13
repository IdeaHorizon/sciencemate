"""Kill a real parent worker, then recover its child in another interpreter."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

WORKER = r"""
import asyncio, json, sys
from pathlib import Path
from core.bootstrap import bootstrap
from core.agent_loop import (
    LoopResult,
    _persist_messages_checkpoint,
    load_messages_checkpoint,
    run_loop,
)
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse
from core.state import State
import core.executor as executor
import core.loader as loader
import core.llm as llm_module
import chat


async def main():
    root, mode = Path(sys.argv[1]), sys.argv[2]
    bootstrap()
    child_harness = NodeHarness(node_type="_curator", system_prompt="child", tools=[], max_turns=2)
    loader.load_harness = lambda _: child_harness
    executor.load_harness = lambda _: child_harness
    llm_module.LLMClient = lambda: object()

    async def child_loop(harness, state, messages, llm):
        if any(message.content == "physical effect already accepted" for message in messages):
            return LoopResult(
                final_text="finished original child", turns=2, messages=messages, status="completed"
            )
        effect = root / "physical-effects"
        effect.write_text((effect.read_text() if effect.exists() else "") + state.run_id + "\n")
        _persist_messages_checkpoint(
            state, [LLMMessage(role="user", content="physical effect already accepted")], turn=1
        )
        (root / "ready.json").write_text(
            json.dumps({"parent": state.parent_run_id, "child": state.run_id})
        )
        await asyncio.Event().wait()

    executor.run_loop = child_loop
    harness = NodeHarness(
        node_type="_orchestrator",
        system_prompt="parent",
        tools=["run_node"],
        callable_nodes=["*"],
        max_turns=2,
    )
    if mode == "start":
        parent = State.new("_orchestrator", root / "runs", session_id="s")
        messages = [LLMMessage(role="user", content="do this once")]
    else:
        identity = json.loads((root / "ready.json").read_text())
        parent = State.reopen("_orchestrator", root / "runs", identity["parent"], session_id="s")
        messages, _ = load_messages_checkpoint(parent)
        chat._repair_message_tool_protocol(messages)

    class Model:
        async def chat(self, messages, **kwargs):
            if mode == "start":
                return LLMResponse(
                    content=None,
                    finish_reason="tool_calls",
                    usage={},
                    tool_calls=[
                        {
                            "id": "original",
                            "type": "function",
                            "function": {
                                "name": "run_node",
                                "arguments": json.dumps(
                                    {"node_type": "_curator", "user_note": "original task"}
                                ),
                            },
                        }
                    ],
                )
            result = json.loads(
                next(message.content for message in messages if message.tool_call_id == "original")
            )
            assert (
                result["child_run_id"] == identity["child"]
                and result["child_status"] == "completed"
            )
            return LLMResponse(content="done", tool_calls=[], finish_reason="stop", usage={})

    result = await run_loop(harness, parent, messages, Model())
    print(json.dumps({"status": result.status, "runs": len(list((root / "runs").iterdir()))}))


asyncio.run(main())
"""


@pytest.mark.parametrize("source", ["checkout", "payload"])
def test_a_killed_parent_rejoins_the_original_child_in_a_new_process(tmp_path, monkeypatch, source):
    repository = Path(__file__).resolve().parents[1]
    if source == "payload":
        from scripts.package import build_server_bundle

        interface = tmp_path / "interface"
        interface.mkdir()
        (interface / "index.html").write_text("packaging fixture")
        monkeypatch.setattr(build_server_bundle, "DIST", tmp_path / "distribution")
        repository = build_server_bundle.stage("0.5.15", interface) / "harness"
    script = tmp_path / "worker.py"
    script.write_text(WORKER)
    env = {
        **os.environ,
        "PYTHONPATH": str(repository),
        "HARNESS_FRAMEWORK_HOME": str(tmp_path / "home"),
    }
    worker = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path), "start"],
        cwd=repository,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while (
            not (tmp_path / "ready.json").exists()
            and worker.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert (tmp_path / "ready.json").exists(), worker.communicate(timeout=1)
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.communicate(timeout=5)
    original = json.loads((tmp_path / "ready.json").read_text())
    recovered = subprocess.run(
        [sys.executable, str(script), str(tmp_path), "resume"],
        cwd=repository,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(recovered.stdout.splitlines()[-1]) == {"status": "completed", "runs": 2}
    assert (tmp_path / "physical-effects").read_text().splitlines() == [original["child"]]
