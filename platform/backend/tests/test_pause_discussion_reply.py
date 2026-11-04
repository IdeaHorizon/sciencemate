from types import SimpleNamespace

import pytest

from app.services.harness_sessions import harness_session_manager
from app.services.local_execution import _model_reply


@pytest.mark.asyncio
async def test_pause_discussion_keeps_the_explanation_instead_of_repeating_the_question(
    monkeypatch,
):
    async def answer(**kwargs):
        return {
            "status": "paused",
            "dialogue_only": True,
            "final_text": "建议修改，因为两组计量单位不一致。",
            "pause_event": {"question": "要修改吗？", "pending_tool_call_id": "decision"},
            "pause_id": "decision",
            "tokens_used_delta": 10,
        }

    async def ignore(*args, **kwargs):
        pass

    monkeypatch.setattr(harness_session_manager, "answer", answer)
    reply, _, result = await _model_reply(
        {},
        "为什么建议修改？",
        None,
        SimpleNamespace(id="user"),
        "project",
        "conversation",
        "run",
        "session",
        None,
        True,
        ignore,
        ignore,
    )
    assert reply == "建议修改，因为两组计量单位不一致。"
    assert result["pause_id"] == "decision" and result["dialogue_only"] is True
