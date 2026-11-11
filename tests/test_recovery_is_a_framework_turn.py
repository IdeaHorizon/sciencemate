"""Runtime continuation is a typed framework notice, never new user intake."""
import pytest

from core.bootstrap import bootstrap
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse
from core.state import State


@pytest.mark.asyncio
async def test_framework_continuation_preserves_user_identity_and_request_replay(tmp_path, monkeypatch):
    import chat
    from tests.test_transcript_reconstructs_requests import _rebuild_and_check

    bootstrap()
    state = State.new("_orchestrator", tmp_path / "runs", project_id="recovery")
    messages = [LLMMessage(role="user", content="the actual human task")]
    harness = NodeHarness(node_type="_orchestrator", system_prompt="test", tools=[], max_turns=2)
    async def no_snapshot(*args, **kwargs):
        return None
    monkeypatch.setattr(chat, "_inject_status_snapshot", no_snapshot)
    class LLM:
        async def chat(self, messages, **kwargs):
            assert [m.content for m in messages if m.role == "user" and m.origin != "framework"] == [
                "the actual human task"]
            return LLMResponse(content="continued from checkpoint", tool_calls=[], finish_reason="stop", usage={})
    result = await chat._run_one_turn_raw(
        state, harness, messages, LLM(), "platform runtime continuation", framework_continuation=True,
    )
    assert result.status == "completed"
    assert [m for m in messages if "platform runtime continuation" in (m.content or "")][0].origin == "framework"
    assert _rebuild_and_check(state) == 1
