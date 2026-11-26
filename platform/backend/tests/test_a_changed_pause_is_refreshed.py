"""A stale answer refreshes the actual question without answering it."""

import json

import pytest
from sqlalchemy import func, select

from app.models.execution import RunStatus
from app.services.harness_sessions import HarnessSessionError
from app.services.local_execution import terminal_state_for
from tests import test_local_runtime_api as runtime
from tests.test_an_answer_consumes_the_pause_it_dispatches import _binding, _noop, _session

runtime_client = runtime.runtime_client


def _snapshot():
    pause = {"pending_tool_call_id": "pause-current", "question": "Choose the actual new route",
             "options": ["first route", "second route"],
             "offer": {"offer_id": "offer-current", "choices": []}}
    return {"current_pause_id": "pause-current", "current_pause_event": pause}


@pytest.mark.asyncio
async def test_a_conflict_replaces_the_old_pause_but_never_replays_the_old_answer():
    session, sent = _session(rpc_result={"status": "completed"})

    async def changed(payload, **_):
        sent.append(payload)
        raise HarnessSessionError("question changed", code="pause_conflict", details=_snapshot())

    session._rpc_locked = changed
    with pytest.raises(HarnessSessionError):
        await session.answer(binding=_binding(), answer="old answer", choice=None,
                             on_progress=_noop, on_protocol_event=_noop)
    assert session.paused is True
    assert session.pause_id == "pause-current"
    assert session.offer_id == "offer-current"
    assert len(sent) == 1 and sent[0]["pause_id"] == "chatcmpl-tool-1"


@pytest.mark.parametrize("mismatch", [False, True])
@pytest.mark.asyncio
async def test_an_incomplete_or_inconsistent_snapshot_never_guesses_a_pause(mismatch):
    session, _ = _session(rpc_result={"status": "completed"})
    details = _snapshot()
    if mismatch:
        details["current_pause_event"]["pending_tool_call_id"] = "different"
    else:
        details.pop("current_pause_event")

    async def changed(*_, **__):
        raise HarnessSessionError("question changed", code="pause_conflict", details=details)

    session._rpc_locked = changed
    with pytest.raises(HarnessSessionError):
        await session.answer(binding=_binding(), answer="old answer", choice=None,
                             on_progress=_noop, on_protocol_event=_noop)
    assert session.pause_id == "chatcmpl-tool-1" and session.offer_id == "run1:p1:o1"


def test_a_pause_conflict_keeps_the_original_run_waiting():
    assert terminal_state_for(HarnessSessionError("question changed", code="pause_conflict")) == (
        RunStatus.WAITING_HUMAN.value, None)


def test_an_older_worker_conflict_never_recommends_resending_the_same_answer():
    from app.services.run_failures import describe

    failure = describe(HarnessSessionError("question changed", code="pause_conflict"))
    assert failure.code == "pause_conflict" and failure.retryable is False
    assert "again" not in failure.recovery.lower()


@pytest.mark.parametrize("structured_offer", [False, True])
@pytest.mark.asyncio
async def test_the_current_question_is_projected_before_any_new_answer_is_accepted(
    runtime_client, monkeypatch, tmp_path, structured_offer,
):
    from app.config import settings
    from app.models.execution import Command, ExecutionEvent, Run
    from app.services.execution_ingest import ExecutionIngestService
    from app.services.harness_sessions import AppRunBinding, harness_session_manager

    client, factory = runtime_client
    monkeypatch.setattr(settings, "harness_bridge_enabled", True)
    monkeypatch.setattr(settings, "harness_state_root", str(tmp_path))
    token = await runtime._token(client, "researcher@atrium.local")
    headers = runtime._headers(token)
    backend = await client.post("/api/v1/settings/model-backends", headers=headers, json={
        "provider": "openai_compatible", "display_name": "Current question test",
        "model": "test-model", "base_url": "http://provider.invalid/v1", "api_key": "test-key",
    })
    await client.post(f"/api/v1/settings/model-backends/{backend.json()['id']}/default",
                      headers=headers)
    project = (await client.get("/api/v1/projects/", headers=headers)).json()[0]["id"]
    session_id = (await client.post(f"/api/v1/projects/{project}/sessions", headers=headers,
                                   json={"title": "Current live question"})).json()["id"]
    transcript = tmp_path / "current-question" / "transcript.jsonl"
    transcript.parent.mkdir()
    transcript.write_bytes(b"")
    managed, sent = _session(rpc_result={"status": "completed"})
    managed.paused = False
    managed.binding = None
    old_pause = {"question": "Old research route?", "pending_tool_call_id": "pause-old",
                 "options": ["old first", "old second"]}
    current = _snapshot()
    pause = current["current_pause_event"]
    option_details = [
        {"id": "first", "label": "first route", "description": "Inspect the first route",
         "recommended": False},
        {"id": "second", "label": "second route", "description": "Inspect the second route",
         "recommended": True},
    ]
    if structured_offer:
        from core.decision_offer import Choice, Offer

        pause["offer"] = Offer(
            decision_id="current-route-decision", kind="human_input", question=pause["question"],
            choices=(Choice("first", "first route", "Inspect the first route"),
                     Choice("second", "second route", "Inspect the second route")),
            recommended_id="second",
        ).to_pause_payload()
        pause["offer"]["future_offer_field"] = {
            "evidence": "preserve future structured context",
        }
    else:
        pause.pop("offer")  # Legacy free-text question path.
    pause.update({
        "metadata": {"header": "新路线", "future_metadata_field": {"retained": True}},
        "option_details": option_details, "recommended_option_index": 1,
        "future_pause_field": {"retained": "through the ingress boundary"},
        "event": "run_end", "reason": "completed",  # Not authoritative event routing.
    })
    observed: list[dict] = []
    original_ingest = ExecutionIngestService.ingest_raw_record

    async def capture_ingress(self, db, **kwargs):
        if kwargs["raw"].get("question") == pause["question"]:
            observed.append(dict(kwargs["raw"]))
        return await original_ingest(self, db, **kwargs)

    monkeypatch.setattr(ExecutionIngestService, "ingest_raw_record", capture_ingress)

    async def turn(**kwargs):
        managed.binding = AppRunBinding(kwargs["user"].id, session_id, kwargs["run_id"], session_id)
        await managed._apply_result_pause({"status": "paused", "pause_event": old_pause})
        return {"status": "paused", "run_id": "original-harness",
                "transcript_path": str(transcript), "final_text": "", "tokens_used_delta": 0,
                "pause_event": old_pause, "pause_id": "pause-old", "_session_resumable": True}

    async def answer(**kwargs):
        return await managed.answer(binding=managed.binding, answer=kwargs["answer"],
            choice=kwargs.get("choice"), on_progress=kwargs["on_progress"],
            on_protocol_event=kwargs["on_protocol_event"])

    async def rpc(payload, **_):
        sent.append(payload)
        if payload["pause_id"] == "pause-old":
            raise HarnessSessionError("question changed", code="pause_conflict", details=current)
        assert payload["pause_id"] == "pause-current"
        return {"data": {"status": "completed", "transcript_path": str(transcript),
                         "final_text": "Original research continued once.", "tokens_used_delta": 0}}

    managed._rpc_locked = rpc
    monkeypatch.setattr(harness_session_manager, "turn", turn)
    monkeypatch.setattr(harness_session_manager, "answer", answer)
    monkeypatch.setattr(harness_session_manager, "paused_binding", lambda *_:
                        managed.binding if managed.paused else None)
    async def send(value):
        return await client.post(f"/api/v1/chat/projects/{project}/stream", headers=headers,
            json={"answer": {"kind": "text", "text": value}, "conversation_id": session_id})

    assert (await send("Begin research")).status_code == 200
    await send("Answer intended for the old question")
    assert len(sent) == 1, "The rejected old answer was automatically reapplied"
    async with factory() as db:
        run = await db.scalar(select(Run).where(Run.session_id == session_id))
        original_run = run.id
        assert run.status == "waiting_human" and run.ended_at is None
        latest = await db.scalar(select(ExecutionEvent).where(
            ExecutionEvent.run_id == run.id, ExecutionEvent.kind == "run.paused"
        ).order_by(ExecutionEvent.sequence.desc()))
        assert latest.payload["prompt"] == "Choose the actual new route"
        assert latest.payload["options"] == ["first route", "second route"]
        assert latest.payload["header"] == "新路线"
    assert len(observed) == 1
    assert observed[0]["event"] == "run_paused" and observed[0]["reason"] == "waiting_human"
    assert observed[0]["metadata"] == pause["metadata"]
    assert observed[0]["future_pause_field"] == pause["future_pause_field"]
    assert observed[0]["option_details"] == option_details
    assert observed[0]["recommended_option_index"] == 1
    view = await client.get(f"/api/v1/projects/{project}/sessions/{session_id}", headers=headers)
    assert "Choose the actual new route" in json.dumps(view.json())
    if structured_offer:
        def objects(value):
            if isinstance(value, dict):
                yield value
                for nested in value.values():
                    yield from objects(nested)
            elif isinstance(value, list):
                for nested in value:
                    yield from objects(nested)

        cards = [item for item in objects(view.json()) if item.get("offer") == pause["offer"]]
        assert cards, "The current canonical card lost an offer field"
        assert any(item.get("optionDetails") == option_details
                   and item.get("recommendedOptionIndex") == 1 for item in cards)
    response = await send("Answer to the current question")
    assert "Original research continued once." in response.text
    assert [item["pause_id"] for item in sent] == ["pause-old", "pause-current"]
    async with factory() as db:
        assert (await db.get(Run, original_run)).status == "completed"
        assert await db.scalar(select(func.count()).select_from(Run).where(
            Run.session_id == session_id, Run.parent_run_id.is_(None))) == 1
        commands = (await db.scalars(select(Command).where(Command.run_id == original_run))).all()
        assert [item.kind for item in commands].count("run.start") == 1
        assert [item.kind for item in commands].count("run.answer") == 2

