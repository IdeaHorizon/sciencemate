"""A worker reply may need the same SQLite writer as its incoming message."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

from app.config import data_root
from app.models.execution import ExecutionEvent, Run, SessionMessage, SessionProjection
from app.services.app_events import record_app_event
from app.services.execution_ingest import ExecutionIngestService, IngestContext
from app.services.harness_sessions import HarnessSessionStaleError, harness_session_manager
from app.services.harness_transcript_ingest import ingest_transcript_wrapper
from app.services.sessions import interject_active_session
from tests import test_local_runtime_api as fixtures

runtime_client = fixtures.runtime_client


async def prepare(client, factory):
    token = await fixtures._token(client, "researcher@atrium.local")
    headers = fixtures._headers(token)
    project_id = (await client.get("/api/v1/projects/", headers=headers)).json()[0]["id"]
    session_id = (
        await client.post(
            f"/api/v1/projects/{project_id}/sessions",
            headers=headers,
            json={"title": "Concurrent interjection"},
        )
    ).json()["id"]
    async with factory() as db:
        session = await db.get(SessionProjection, session_id)
        context = IngestContext(
            tenant_id=session.tenant_id,
            workspace_id=session.workspace_id,
            project_id=project_id,
            session_id=session_id,
            run_id="run-interjection",
            actor_user_id=session.initiating_user_id,
        )
        db.add(
            Run(
                id=context.run_id,
                tenant_id=context.tenant_id,
                workspace_id=context.workspace_id,
                project_id=project_id,
                session_id=session_id,
                status="running",
            )
        )
        await db.commit()
    return headers, context


def worker_notification(factory, context, calls, *, unknown=False):
    async def deliver(**payload):
        calls.append(payload)
        raw = {
            "event": "interrupt_tool",
            "at": datetime.now(UTC).isoformat(),
            "tool_name": "remote_job",
            "arguments": {"action": "status"},
            "result": {"status": "success", "state": "running"},
            "submission_id": payload["message_id"],
        }
        path = data_root("state") / "interjection" / "transcript.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw) + "\n")
        async with factory() as writer:
            # A short failure budget makes the pre-fix dependency cycle explicit.
            # Production SQLite timeouts are unchanged.
            await writer.execute(text("PRAGMA busy_timeout=100"))
            visible = await writer.get(SessionMessage, payload["message_id"])
            service = ExecutionIngestService()
            await ingest_transcript_wrapper(
                writer,
                service=service,
                context=context,
                event={
                    "type": "transcript",
                    "transcript_path": str(path),
                    "byte_start": 0,
                    "byte_end": path.stat().st_size,
                },
                harness_states={},
            )
            await writer.commit()
            assert visible is not None and visible.content == payload["text"]
            # Re-read the same source with fresh adapter state. No second tool action.
            await ingest_transcript_wrapper(
                writer,
                service=service,
                context=context,
                event={
                    "type": "transcript",
                    "transcript_path": str(path),
                    "byte_start": 0,
                    "byte_end": path.stat().st_size,
                },
                harness_states={},
            )
            await writer.commit()
        if unknown:
            raise HarnessSessionStaleError("Acknowledgement lost after the worker recorded input")
        return {"item_id": "in-observation", "occupancy": "working"}

    return deliver


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["interrupt", "chat"])
async def test_public_interjection_releases_write_transaction_before_waiting(
    runtime_client,
    monkeypatch,
    entry,
):
    client, factory = runtime_client
    headers, context = await prepare(client, factory)
    calls = []
    monkeypatch.setattr(harness_session_manager, "is_occupied", lambda *_a, **_k: True)
    monkeypatch.setattr(
        harness_session_manager, "deliver", worker_notification(factory, context, calls)
    )
    if entry == "interrupt":
        response = await client.post(
            f"/api/v1/projects/{context.project_id}/sessions/{context.session_id}/interrupt",
            headers=headers,
            json={"text": "Keep the existing completed cohort; report progress."},
        )
    else:
        response = await client.post(
            f"/api/v1/chat/projects/{context.project_id}/stream",
            headers=headers,
            json={
                "conversation_id": context.session_id,
                "answer": {
                    "kind": "text",
                    "text": "Keep the existing completed cohort; report progress.",
                },
            },
        )
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    async with factory() as db:
        messages = list(
            await db.scalars(
                select(SessionMessage).where(SessionMessage.session_id == context.session_id)
            )
        )
        events = list(
            await db.scalars(
                select(ExecutionEvent).where(ExecutionEvent.session_id == context.session_id)
            )
        )
        assert len(messages) == 1 and messages[0].id == calls[0]["message_id"]
        assert len([e for e in events if e.kind == "tool.completed"]) == 1
        receipt = next(e for e in events if e.kind == "interject.queued")
        assert receipt.payload["messageId"] == messages[0].id
        count = len(events)
        await record_app_event(
            db,
            session_id=context.session_id,
            kind="interject.queued",
            run_id=context.run_id,
            payload=dict(receipt.payload),
            dedupe_key=messages[0].id,
        )
        await db.commit()
        assert (
            len(
                list(
                    await db.scalars(
                        select(ExecutionEvent).where(
                            ExecutionEvent.session_id == context.session_id
                        )
                    )
                )
            )
            == count
        )


@pytest.mark.asyncio
async def test_lost_ack_retains_input_and_observation_without_retry_or_false_receipt(
    runtime_client,
    monkeypatch,
):
    client, factory = runtime_client
    _, context = await prepare(client, factory)
    calls = []
    monkeypatch.setattr(
        harness_session_manager,
        "deliver",
        worker_notification(factory, context, calls, unknown=True),
    )
    async with factory() as db:
        session = await db.get(SessionProjection, context.session_id)
        with pytest.raises(HarnessSessionStaleError, match="Acknowledgement lost"):
            await interject_active_session(
                db,
                session=session,
                text="Observe once.",
                author_user_id=context.actor_user_id,
                run_id=context.run_id,
            )
        await db.rollback()
    async with factory() as db:
        messages = list(
            await db.scalars(
                select(SessionMessage).where(SessionMessage.session_id == context.session_id)
            )
        )
        events = list(
            await db.scalars(
                select(ExecutionEvent).where(ExecutionEvent.session_id == context.session_id)
            )
        )
        assert len(calls) == len(messages) == 1
        assert messages[0].id == calls[0]["message_id"]
        assert len([e for e in events if e.kind == "tool.completed"]) == 1
        assert not any(e.kind == "interject.queued" for e in events)


@pytest.mark.asyncio
async def test_input_commit_failure_never_notifies_worker(runtime_client, monkeypatch):
    client, factory = runtime_client
    _, context = await prepare(client, factory)
    calls = []
    monkeypatch.setattr(
        harness_session_manager, "deliver", worker_notification(factory, context, calls)
    )
    async with factory() as db:
        session = await db.get(SessionProjection, context.session_id)

        async def failed_commit():
            raise OSError("injected input persistence failure")

        monkeypatch.setattr(db, "commit", failed_commit)
        with pytest.raises(OSError, match="input persistence failure"):
            await interject_active_session(
                db,
                session=session,
                text="Do not notify before commit.",
                author_user_id=context.actor_user_id,
                run_id=context.run_id,
            )
        await db.rollback()
    async with factory() as db:
        assert calls == []
        assert (
            list(
                await db.scalars(
                    select(SessionMessage).where(SessionMessage.session_id == context.session_id)
                )
            )
            == []
        )
