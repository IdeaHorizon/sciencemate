"""SQLite contention delays recording the same research, never reruns it."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.models.execution import Command, ExecutionEvent, Run, SessionMessage, SessionProjection
from app.services import local_execution
from app.services.harness_sessions import harness_session_manager
from tests import test_local_runtime_api as runtime
from tests import test_the_backend_replays_what_it_missed as replay_fixtures

runtime_client = runtime.runtime_client
busy_engine = replay_fixtures.busy_engine
worker_output = replay_fixtures.worker_output


@pytest.mark.asyncio
async def test_worker_can_finish_before_the_recording_writer_becomes_available(
    runtime_client, monkeypatch, tmp_path,
):
    client, factory = runtime_client
    monkeypatch.setattr(settings, "harness_bridge_enabled", True)
    monkeypatch.setattr(settings, "harness_state_root", str(tmp_path))
    token = await runtime._token(client, "researcher@atrium.local")
    headers = runtime._headers(token)
    backend = await client.post("/api/v1/settings/model-backends", headers=headers, json={
        "provider": "openai_compatible", "display_name": "Recording test",
        "model": "test-model", "base_url": "http://provider.invalid/v1", "api_key": "test-key",
    })
    assert backend.status_code == 201
    await client.post(f"/api/v1/settings/model-backends/{backend.json()['id']}/default",
                      headers=headers)
    project = (await client.get("/api/v1/projects/", headers=headers)).json()[0]["id"]
    session = (await client.post(f"/api/v1/projects/{project}/sessions", headers=headers,
                                json={"title": "Recording waits for SQLite"})).json()["id"]
    transcript = tmp_path / "recording-worker" / "transcript.jsonl"
    transcript.parent.mkdir()
    transcript.write_bytes(b"")
    finished = asyncio.Event()
    busy_statements: list[str] = []
    calls = 0
    original = local_execution.commit_transcript_wrapper

    def short_native_wait_on_checkout(connection, *_):
        cursor = connection.cursor()
        cursor.execute("PRAGMA busy_timeout=5")
        cursor.close()

    engine = factory.kw["bind"].sync_engine
    sql_event.listen(engine, "checkout", short_native_wait_on_checkout)

    async def short_native_wait(db, **kwargs):
        # Real file contention, with the SQLite driver's wait shortened so the
        # test deterministically exhausts the normal per-record retry budget.
        try:
            return await original(db, **kwargs)
        except OperationalError as exc:
            busy_statements.append(str(exc.statement))
            raise

    monkeypatch.setattr(local_execution, "commit_transcript_wrapper", short_native_wait)
    monkeypatch.setattr(harness_session_manager, "paused_binding", lambda *_: None)
    holder = factory()

    async def worker(**kwargs):
        nonlocal calls
        calls += 1
        await holder.execute(update(SessionProjection).where(
            SessionProjection.session_id == session).values(
                next_sequence=SessionProjection.next_sequence))
        for raw in [
            {"event": "llm_response", "usage": {"total_tokens": 11, "coverage": "partial"}},
            {"event": "run_end", "status": "completed"},
        ]:
            raw["at"] = datetime.now(UTC).isoformat()
            start = transcript.stat().st_size
            with transcript.open("ab") as output:
                output.write((json.dumps(raw) + "\n").encode())
            await kwargs["on_protocol_event"]({
                "type": "transcript", "transcript_path": str(transcript),
                "byte_start": start, "byte_end": transcript.stat().st_size, "event": raw,
            })
        finished.set()
        return {"status": "completed", "run_id": "recording-worker",
                "final_text": "The original calculation finished.", "tokens_used_delta": 11,
                "transcript_path": str(transcript), "artifact_paths": [], "pause_event": None,
                "pause_pending_path": None}

    monkeypatch.setattr(harness_session_manager, "turn", worker)
    request = asyncio.create_task(client.post(f"/api/v1/chat/projects/{project}/stream",
        headers=headers, json={"answer": {"kind": "text", "text": "Do the calculation"},
                               "conversation_id": session}))
    reached_terminal = asyncio.create_task(finished.wait())
    try:
        await asyncio.wait({request, reached_terminal}, timeout=8,
                           return_when=asyncio.FIRST_COMPLETED)
        assert finished.is_set(), "A database lock rejected the original worker's result"
        assert not request.done(), "The request settled before its source records were committed"
        await holder.rollback()
        response = await asyncio.wait_for(request, 15)
        assert '"type": "error"' not in response.text
        assert "The original calculation finished." in response.text
        assert calls == 1
        assert busy_statements and all("UPDATE sessions" in sql for sql in busy_statements)
        async with factory() as db:
            run = await db.scalar(select(Run).where(Run.session_id == session))
            assert run.status == "completed" and run.ended_at is not None
            assert await db.scalar(select(func.count()).select_from(Command).where(
                Command.run_id == run.id)) == 1
            assert await db.scalar(select(func.count()).select_from(ExecutionEvent).where(
                ExecutionEvent.run_id == run.id, ExecutionEvent.kind == "usage.updated")) == 1
            assert await db.scalar(select(func.count()).select_from(SessionMessage).where(
                SessionMessage.session_id == session, SessionMessage.role == "assistant")) == 1
    finally:
        await holder.rollback()
        await holder.close()
        for task in (request, reached_terminal):
            if not task.done():
                task.cancel()
        await asyncio.gather(request, reached_terminal, return_exceptions=True)
        sql_event.remove(engine, "checkout", short_native_wait_on_checkout)


@pytest.mark.asyncio
async def test_shutdown_during_sqlite_contention_resumes_the_same_durable_sources(
    busy_engine, worker_output, monkeypatch,
):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services import session_event_replay
    from app.services.execution_ingest import ExecutionIngestService, IngestContext
    from app.services.harness_transcript_ingest import TranscriptWritePendingError

    project, session, events, expected = worker_output
    context = IngestContext(tenant_id="t", workspace_id="w", project_id=project,
                            session_id=session, run_id="same-research")
    factory = async_sessionmaker(busy_engine, expire_on_commit=False)
    service = ExecutionIngestService()
    async with factory() as seed:
        await service._ensure_scope(seed, context)
        await seed.commit()
    blocked = asyncio.Event()
    original = session_event_replay.commit_transcript_wrapper

    async def observed(db, **kwargs):
        # Observe a complete exhausted finite budget before requesting shutdown.
        # The actual production replay then waits for this same source, rather
        # than treating it as an execution failure or skipping to a later fact.
        try:
            return await original(db, **{**kwargs, "wait_for_writer": False})
        except TranscriptWritePendingError:
            blocked.set()
            return await original(db, **kwargs)

    monkeypatch.setattr(session_event_replay, "commit_transcript_wrapper", observed)
    source_bytes = events.read_bytes()
    async with factory() as holder, factory() as reader:
        await holder.execute(update(SessionProjection).values(
            next_sequence=SessionProjection.next_sequence))
        task = asyncio.create_task(session_event_replay.replay_missed_events(
            reader, service=service, context=context, events_path=events))
        try:
            await asyncio.wait_for(blocked.wait(), 5)
            assert not task.done()
            task.cancel()  # Backend shutdown, not a request to rerun the worker.
            with pytest.raises(asyncio.CancelledError):
                await task
            assert reader.is_active
            assert await reader.scalar(select(func.count()).select_from(ExecutionEvent)) == 0
            assert events.read_bytes() == source_bytes
        finally:
            await reader.rollback()
            await holder.rollback()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async with factory() as recovered:
        report = await session_event_replay.replay_missed_events(
            recovered, service=service, context=context, events_path=events)
        assert report.ingested == expected and report.failed == 0
        run = await recovered.get(Run, context.run_id)
        assert run.status == "completed"
        rows = (await recovered.scalars(select(ExecutionEvent).where(
            ExecutionEvent.file_identity.is_not(None)).order_by(ExecutionEvent.sequence))).all()
        positions = [(row.id, row.file_identity, row.byte_offset, row.sequence) for row in rows]
        assert len({(identity, offset) for _, identity, offset, _ in positions}) == expected
        sequence = await recovered.scalar(select(SessionProjection.next_sequence))
        again = await session_event_replay.replay_missed_events(
            recovered, service=service, context=context, events_path=events)
        assert again.ingested == 0 and again.already_known == expected
        rows_again = (await recovered.scalars(select(ExecutionEvent).where(
            ExecutionEvent.file_identity.is_not(None)).order_by(ExecutionEvent.sequence))).all()
        assert [(row.id, row.file_identity, row.byte_offset, row.sequence)
                for row in rows_again] == positions
        assert await recovered.scalar(select(SessionProjection.next_sequence)) == sequence
