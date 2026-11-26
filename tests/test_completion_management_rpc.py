"""The live worker answers management while its original turn is suspended."""

import asyncio
import json
from dataclasses import replace

import pytest

from core import completion_driver, loop_hooks
from core.completion import CompletionDecision
from core.llm import LLMResponse
from core.pause import clear_all
from platform_runtime import PlatformSession, RequestSource, serve_jsonl
from tests.test_platform_runtime import _FakeLLM


class Requests(RequestSource):
    def __init__(self):
        self.queue = asyncio.Queue()

    async def readline(self, limit):
        return await self.queue.get()

    async def send(self, **request):
        await self.queue.put(json.dumps(request) + "\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["resume_completion", "stop"])
async def test_management_can_resume_or_stop_a_suspended_original_turn(
    tmp_path, monkeypatch, action
):
    clear_all()
    real_start = PlatformSession.start
    sealed = [False]

    async def start(self):
        await real_start(self)
        self.harness = replace(self.harness, completion_provider="rpc_admit", max_turns=1)

    def provider(ctx, candidate):
        return CompletionDecision(
            "allow" if sealed[0] else "reenter", "unsealed", str(sealed[0]), {}
        )

    monkeypatch.setattr(PlatformSession, "start", start)
    monkeypatch.setitem(
        loop_hooks._HOOKS,
        "rpc_admit",
        loop_hooks.LoopHook("rpc_admit", on_completion_admission=provider),
    )
    source = Requests()
    events = []

    async def event(request_id, kind):
        async with asyncio.timeout(5):
            while True:
                found = next(
                    (e for e in events if e.get("request_id") == request_id and e["type"] == kind),
                    None,
                )
                if found:
                    return found
                await asyncio.sleep(0.005)

    model = _FakeLLM(
        [LLMResponse(content="candidate", tool_calls=[], finish_reason="stop", usage={})]
    )
    server = asyncio.create_task(
        serve_jsonl(
            source, lambda kind, **payload: events.append({"type": kind, **payload}), llm=model
        )
    )
    try:
        await source.send(
            op="init",
            request_id="init",
            tenant_id="tenant-test",
            project_id="project-rpc",
            session_id="session-rpc",
            home_dir=str(tmp_path / "home"),
        )
        await event("init", "ready")
        await source.send(op="turn", request_id="turn", message="Finish the existing work")
        async with asyncio.timeout(5):
            while not completion_driver.suspended_runs():
                await asyncio.sleep(0.005)
        await source.send(op="status", request_id="status")
        status = await event("status", "status")
        assert status["working"] and status["completion_resume"]
        run_id = status["suspended_runs"][0]["run_id"]
        assert not any(e.get("request_id") == "turn" and e["type"] == "result" for e in events)
        sealed[0] = True
        await source.send(op=action, request_id="action", run_id=run_id)
        await event("action", "accepted")
        result = await event("turn", "result")
        assert result["data"]["status"] == ("cancelled" if action == "stop" else "completed")
        assert len(model.seen) == 1
    finally:
        await source.queue.put("")
        await asyncio.wait_for(server, 5)
        clear_all()
