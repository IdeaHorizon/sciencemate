"""Task inputs stay diagnostic data across workers, history, and event APIs."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.v1.execution import get_runtime_tenant_id, router
from app.auth import get_current_user
from app.database import get_db
from app.services.execution_ingest import ExecutionIngestService, TranscriptAdapter
from app.services.execution_presentation import presentation_event

from .test_execution_foundation import AT, _context, _ingest, execution_db  # noqa: F401


@pytest.mark.parametrize("raw_event", ["node_dispatched", "node_dispatch_announced"])
@pytest.mark.parametrize("node", ["experiment", "_reviewer", "_curator", "literature"])
def test_new_and_old_workers_have_one_text_free_dispatch_contract(raw_event, node):
    draft = TranscriptAdapter().adapt({
        "event": raw_event, "at": AT, "node_type": node,
        "dispatch_id": "disp-1", "background": False,
        "user_note": "PRIVATE_TASK\n" * 500,
        "node_inputs": {"experiment_spec": "PRIVATE"},
    }, context=_context(), adapter_state={}, event_id="event-1")
    assert draft.kind == "node.dispatched"
    assert draft.payload == {"nodeType": node, "dispatchId": "disp-1", "background": False}
    assert draft.visibility.value == "summary"


@pytest.mark.parametrize("source", [{}, {"rawEvent": "node_dispatch_announced"}])
@pytest.mark.asyncio
async def test_history_api_upgrades_persisted_dispatch_without_mutating_evidence(
    execution_db, source,  # noqa: F811
):
    service, state = ExecutionIngestService(), {}
    await _ingest(service, execution_db, _context(), {
        "event": "run_start", "at": AT, "node_type": "_orchestrator",
    }, offset=0, state=state)
    outcome = await _ingest(service, execution_db, _context(), {
        "event": "node_dispatched", "at": AT, "node_type": "experiment",
    }, offset=10, state=state)
    stored = outcome.event
    # Recreate the persisted 0.5.16 shape, rather than re-ingesting with the new
    # adapter. This is the user's existing conversation after an app upgrade.
    stored.kind = "orchestrator.said"
    stored.source = source
    stored.payload = {"aboutNodeType": "experiment", "text": "PRIVATE_TASK" * 200,
                      "background": False, "submissionId": "old-submit"}
    await execution_db.commit()
    original_payload = dict(stored.payload)
    await _ingest(service, execution_db, _context(), {
        "event": "run_end", "at": AT, "status": "completed",
    }, offset=20, state=state)
    await execution_db.commit()

    api = FastAPI()
    api.include_router(router, prefix="/api/v1")

    async def db():
        yield execution_db

    api.dependency_overrides[get_db] = db
    api.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="reader")
    api.dependency_overrides[get_runtime_tenant_id] = lambda: "tenant-a"
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get("/api/v1/sessions/session-a/events")
        async with asyncio.timeout(3):
            stream = await client.get("/api/v1/sessions/session-a/events/stream",
                                      params={"runId": _context().run_id})
    assert response.status_code == 200
    item = next(e for e in response.json()["items"] if e["id"] == stored.id)
    assert item["kind"] == "node.dispatched"
    assert item["payload"] == {"nodeType": "experiment", "background": False,
                               "submissionId": "old-submit"}
    assert "PRIVATE" not in json.dumps(item)
    assert stream.status_code == 200
    assert "PRIVATE" not in stream.text
    streamed = [json.loads(line.removeprefix("data: ")) for line in stream.text.splitlines()
                if line.startswith("data: ")]
    streamed_item = next(e for e in streamed if e.get("id") == stored.id)
    assert streamed_item["payload"] == item["payload"]
    assert streamed_item["kind"] == item["kind"]
    assert stored.payload == original_payload
    assert stored.kind == "orchestrator.said"


def test_normal_replies_are_not_classified_by_their_words_or_json():
    for payload in [
        {"text": '字段 experiment_spec 接受 {"value": 1}'},
        {"text": "节点当前在跑", "aboutNodeType": "experiment"},
        {"text": "已把建议送给实验节点", "aboutNodeType": "experiment",
         "repliesToMessageId": "user-message"},
    ]:
        assert presentation_event("orchestrator.said", {"rawEvent": "interrupt_reply"}, payload) == (
            "orchestrator.said", payload,
        )
