"""后端不在的那段时间，worker 说过的话补得回来（RFC 异步运行时 P0-3 接线）。

## 这条路径此前根本不存在

`session_event_log.read_events` 与它的跨进程测试 2026-08-18 就写好了，worker
也一直在往 `events.jsonl` 落盘 —— 但全后端**没有一个生产调用点**。读的那一半
有库、有测试、没有调用方。P0-3 的验收判据（"事件无丢失无重复，byte_offset
对账"）因此在生产路径上从来没有兑现过，而测试全绿。

所以这个文件验的不是 `read_events` 会不会读（那已经验过了），而是**补齐这件
事会不会发生**，以及它的两条性质：不丢、不重。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.execution import EXECUTION_TABLES, ExecutionEvent
from app.services.execution_ingest import ExecutionIngestService, IngestContext
from app.services.session_event_replay import replay_missed_events

AT = "2026-08-23T01:02:03+00:00"


@pytest_asyncio.fixture
async def execution_db() -> AsyncSession:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=EXECUTION_TABLES)
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def worker_output(tmp_path, monkeypatch):
    """一个 worker 留下的现场：真 transcript + 真 events.jsonl。

    包装事件里的 `byte_start/byte_end` 是**真实字节位置** —— 补齐路径会照着
    它回 transcript 里取原文，位置错了取到的就是半条记录。
    """
    from app.config import settings

    project_id, session_id = "proj-replay", "sess-replay"
    worktree_root = tmp_path / "wt"
    runs = (
        worktree_root / project_id / session_id
        / ".research" / "runtime" / "runs"
    )
    runs.mkdir(parents=True)
    monkeypatch.setattr(settings, "project_worktree_root", str(worktree_root))

    transcript = runs / "transcript.jsonl"
    events = runs / "events.jsonl"
    records = [
        {"event": "run_start", "at": AT, "node_type": "analysis"},
        {"event": "tool_call", "at": AT, "turn": 1, "name": "execute", "args": {}},
        {"event": "run_end", "at": AT, "status": "completed"},
    ]
    offset = 0
    with transcript.open("wb") as body, events.open("w", encoding="utf-8") as log:
        for record in records:
            line = (json.dumps(record, ensure_ascii=False) + "\n").encode()
            body.write(line)
            # worker 每写一条 transcript 就发一个包装事件；中间还夹着进度、
            # token 流这类**转瞬即逝**的东西 —— 补齐必须只挑前者。
            log.write(json.dumps({"type": "progress", "detail": "…"}) + "\n")
            log.write(
                json.dumps(
                    {
                        "type": "transcript",
                        "transcript_path": str(transcript),
                        "byte_start": offset,
                        "byte_end": offset + len(line),
                        "event": record,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            offset += len(line)
    return project_id, session_id, events, len(records)


def _context(project_id: str, session_id: str, run_id: str = "run-replay") -> IngestContext:
    return IngestContext(
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        project_id=project_id,
        session_id=session_id,
        run_id=run_id,
    )


async def _durable_count(db: AsyncSession, session_id: str) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(ExecutionEvent)
            .where(
                ExecutionEvent.session_id == session_id,
                ExecutionEvent.file_identity.is_not(None),
            )
        )
        or 0
    )


@pytest.mark.asyncio
async def test_the_gap_is_actually_replayed(execution_db, worker_output):
    """不丢：后端不在时产生的每一条 transcript 记录都进得来。"""
    project_id, session_id, events, expected = worker_output
    report = await replay_missed_events(
        execution_db,
        service=ExecutionIngestService(),
        context=_context(project_id, session_id),
        events_path=events,
    )
    assert report.events_file is True
    assert report.ingested == expected, "补齐没把那段历史补进来"
    assert report.failed == 0 and report.malformed == 0
    assert await _durable_count(execution_db, session_id) == expected


@pytest.mark.asyncio
async def test_replaying_twice_does_not_duplicate_history(execution_db, worker_output):
    """不重：水位是**现算**的，所以补齐是幂等的。

    这条同时验了"断点不存第二份"这个取舍：没有任何一列 offset 需要维护，
    因此也没有任何一列 offset 会和事件表分叉。
    """
    project_id, session_id, events, expected = worker_output
    context = _context(project_id, session_id)
    service = ExecutionIngestService()
    await replay_missed_events(
        execution_db, service=service, context=context, events_path=events
    )
    again = await replay_missed_events(
        execution_db, service=service, context=context, events_path=events
    )
    assert again.ingested == 0
    assert again.already_known == expected
    assert await _durable_count(execution_db, session_id) == expected


@pytest.mark.asyncio
async def test_a_half_replay_is_finished_by_the_next_one(execution_db, worker_output):
    """补一半崩了，下次接着补 —— 而不是"offset 推上去了、记录没落库"。"""
    project_id, session_id, events, expected = worker_output
    service = ExecutionIngestService()
    # 先用**同一个 session、另一条 run** 摄进第一条（模拟"上次补到一半"）。
    first_only = events.parent / "partial.jsonl"
    first_only.write_text(
        "\n".join(events.read_text(encoding="utf-8").splitlines()[:2]) + "\n",
        encoding="utf-8",
    )
    half = await replay_missed_events(
        execution_db, service=service, context=_context(project_id, session_id),
        events_path=first_only,
    )
    assert half.ingested == 1

    rest = await replay_missed_events(
        execution_db, service=service, context=_context(project_id, session_id),
        events_path=events,
    )
    assert rest.ingested == expected - 1
    assert rest.already_known == 1
    assert await _durable_count(execution_db, session_id) == expected


@pytest.mark.asyncio
async def test_history_from_an_earlier_run_is_not_re_ingested_under_a_new_run(
    execution_db, worker_output
):
    """水位必须按 **session** 取，不能按 run 取。

    摄取的幂等键含 run_id：按 run 取水位的话，这个会话历次 turn 的历史在
    换一条 run 之后全都"看起来没摄过"，于是被重新摄一遍、挂在错误的 run
    名下。库里凭空多出一段重复历史，而没有任何一层会报错。
    """
    project_id, session_id, events, expected = worker_output
    service = ExecutionIngestService()
    await replay_missed_events(
        execution_db, service=service,
        context=_context(project_id, session_id, run_id="run-old"), events_path=events,
    )
    later = await replay_missed_events(
        execution_db, service=service,
        context=_context(project_id, session_id, run_id="run-new"), events_path=events,
    )
    assert later.ingested == 0, "同一段历史被第二条 run 又摄了一遍"
    assert await _durable_count(execution_db, session_id) == expected


@pytest.mark.asyncio
async def test_no_events_file_is_not_a_failure(execution_db, tmp_path):
    """老 worker 没落过盘 —— 那段历史确实不存在，如实报告，别假装补得回来。"""
    report = await replay_missed_events(
        execution_db,
        service=ExecutionIngestService(),
        context=_context("p", "s"),
        events_path=Path(tmp_path / "nope.jsonl"),
    )
    assert report.events_file is False
    assert report.ingested == 0


@pytest.mark.asyncio
async def test_a_later_live_fact_does_not_hide_an_earlier_gap(execution_db, worker_output):
    project_id, session_id, events, expected = worker_output
    service = ExecutionIngestService()
    last_only = events.parent / "live-tail.jsonl"
    last_only.write_text("\n".join(events.read_text().splitlines()[-2:]) + "\n")
    first = await replay_missed_events(
        execution_db, service=service, context=_context(project_id, session_id),
        events_path=last_only)
    assert first.ingested == 1
    rest = await replay_missed_events(
        execution_db, service=service, context=_context(project_id, session_id),
        events_path=events)
    assert rest.ingested == expected - 1
    assert rest.already_known == 1
    assert await _durable_count(execution_db, session_id) == expected


@pytest.mark.asyncio
async def test_two_sqlite_writers_share_dedupe_sequence_and_projection(db_engine, monkeypatch):
    """Real file/connection race: replay and live delivery of one usage record."""
    from app.models.execution import Run, SessionProjection

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    service = ExecutionIngestService()
    context = _context("race-project", "race-session", "race-run")
    async with factory() as db:
        await service._ensure_scope(db, context)
        await db.commit()

    first_at_sequence = asyncio.Event()
    second_at_sequence = asyncio.Event()
    release = asyncio.Event()
    allocate = service._next_sequence
    arrivals = 0

    async def pause_after_dedupe(db, session):
        nonlocal arrivals
        arrivals += 1
        (first_at_sequence if arrivals == 1 else second_at_sequence).set()
        await release.wait()
        return await allocate(db, session)

    monkeypatch.setattr(service, "_next_sequence", pause_after_dedupe)
    raw = {"event": "llm_response", "at": AT,
           "usage": {"prompt_tokens": 13, "completion_tokens": 3, "total_tokens": 16}}
    line = json.dumps(raw).encode()

    async def deliver():
        async with factory() as db:
            result = await service.ingest_raw_record(
                db, context=context, file_identity="same-physical-file", byte_offset=388519,
                raw_line=line, raw=raw, adapter_state={},
            )
            await db.commit()
            return result.duplicate

    first = asyncio.create_task(deliver())
    await asyncio.wait_for(first_at_sequence.wait(), 5)
    second = asyncio.create_task(deliver())
    try:
        # The broken SELECT FOR UPDATE path lets both readers pass dedupe.
        # The fixed write boundary keeps the second reader outside it.
        await asyncio.wait_for(second_at_sequence.wait(), 0.2)
    except TimeoutError:
        pass
    finally:
        release.set()
    assert await asyncio.wait_for(asyncio.gather(first, second), 10) == [False, True]
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(ExecutionEvent)) == 1
        run = await db.get(Run, "race-run")
        assert (run.prompt_tokens, run.completion_tokens, run.total_tokens) == (13, 3, 16)
        session = await db.get(SessionProjection, "race-session")
        assert session.next_sequence == 1


@pytest.mark.asyncio
async def test_failed_replay_flush_keeps_the_committed_prefix_and_can_retry(
    execution_db, worker_output, monkeypatch,
):
    from app.services import session_event_replay as replay

    project_id, session_id, events, expected = worker_output
    original = replay.ingest_transcript_wrapper

    async def fail_second_record(db, **kwargs):
        if kwargs["event"]["event"]["event"] == "tool_call":
            values = (await db.execute(select(ExecutionEvent.__table__))).mappings().first()
            db.expunge_all()
            db.add(ExecutionEvent(**dict(values)))
            await db.flush()  # actual IntegrityError leaves the ORM Session inactive
        return await original(db, **kwargs)

    monkeypatch.setattr(replay, "ingest_transcript_wrapper", fail_second_record)
    report = await replay.replay_missed_events(
        execution_db, service=ExecutionIngestService(),
        context=_context(project_id, session_id), events_path=events,
    )
    assert report.failed == 1 and report.ingested == 1
    assert execution_db.is_active
    assert await _durable_count(execution_db, session_id) == 1

    monkeypatch.setattr(replay, "ingest_transcript_wrapper", original)
    retry = await replay.replay_missed_events(
        execution_db, service=ExecutionIngestService(),
        context=_context(project_id, session_id), events_path=events,
    )
    assert retry.failed == 0 and retry.ingested == expected - 1
    assert await _durable_count(execution_db, session_id) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_event", ["interrupt_reply", "user_interrupt_received"])
async def test_narration_only_records_survive_replay_without_duplicate_or_poisoned_transaction(
    execution_db, worker_output, raw_event
):
    """A durable derived event can be the only projection of one raw record."""
    from app.services.harness_transcript_ingest import ingest_transcript_wrapper

    project_id, session_id, events, _ = worker_output
    transcript = events.parent / "narration.jsonl"
    raw = {
        "event": raw_event,
        "at": AT,
        "text": "Keep the existing work.",
        "message_id": "input-once",
        "submission_id": "submission-once",
    }
    transcript.write_text(json.dumps(raw) + "\n")
    wrapper = {
        "type": "transcript",
        "transcript_path": str(transcript),
        "byte_start": 0,
        "byte_end": transcript.stat().st_size,
        "event": raw,
    }
    events.write_text(json.dumps(wrapper) + "\n")
    context = _context(project_id, session_id)
    service = ExecutionIngestService()
    await ingest_transcript_wrapper(
        execution_db, service=service, context=context, event=wrapper, harness_states={}
    )
    await execution_db.commit()
    # Older encoders may have a different event ID for the same immutable source.
    from sqlalchemy import update

    await execution_db.execute(update(ExecutionEvent).values(id="f" * 64))
    await execution_db.commit()
    execution_db.expunge_all()
    historical = list(await execution_db.scalars(select(ExecutionEvent)))
    assert len(historical) == 1
    original = (historical[0].id, dict(historical[0].payload), historical[0].sequence)
    for _ in range(2):
        report = await replay_missed_events(
            execution_db, service=ExecutionIngestService(), context=context, events_path=events
        )
        assert report.failed == 0
        await execution_db.commit()
        rows = list(await execution_db.scalars(select(ExecutionEvent)))
        assert [(e.id, e.payload, e.sequence) for e in rows] == [original]
    # The next fact still commits in the same transaction owner after replay.
    from app.services.app_events import record_app_event

    await record_app_event(
        execution_db,
        session_id=session_id,
        run_id=context.run_id,
        kind="interject.queued",
        payload={"messageId": "another-input"},
        dedupe_key="another-input",
    )
    await execution_db.commit()
    assert await execution_db.scalar(select(func.count()).select_from(ExecutionEvent)) == 2
    newest = await execution_db.scalar(
        select(ExecutionEvent).where(ExecutionEvent.kind == "interject.queued")
    )
    assert newest.sequence == original[2] + 1


@pytest_asyncio.fixture
async def busy_engine(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'busy.sqlite3'}", connect_args={"timeout": 0.01})
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=EXECUTION_TABLES))
    yield engine
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("release_lock", [True, False])
async def test_sqlite_busy_retries_only_the_same_uncommitted_fact(
    busy_engine, worker_output, release_lock,
):
    from sqlalchemy import update
    from sqlalchemy.exc import OperationalError

    from app.models.execution import SessionProjection
    from app.services.harness_transcript_ingest import (
        commit_transcript_wrapper,
        ingest_transcript_wrapper,
    )

    project, session, events, _ = worker_output
    event = next(json.loads(line) for line in events.read_text().splitlines()
                 if json.loads(line).get("type") == "transcript")
    context = _context(project, session)
    factory = async_sessionmaker(busy_engine, expire_on_commit=False)
    service = ExecutionIngestService()
    async with factory() as seed:
        await service._ensure_scope(seed, context)
        await seed.commit()
    observed_busy = asyncio.Event()
    attempts = 0
    states = {}

    async def observe(db, **kwargs):
        nonlocal attempts
        attempts += 1
        try:
            return await ingest_transcript_wrapper(db, **kwargs)
        except OperationalError:
            kwargs["harness_states"]["failed_attempt"] = {"must_not_publish": True}
            observed_busy.set()
            raise

    async with factory() as holder, factory() as reader:
        await holder.execute(update(SessionProjection).values(next_sequence=0))
        task = asyncio.create_task(commit_transcript_wrapper(
            reader, ingest=observe, service=service, context=context,
            event=event, harness_states=states))
        try:
            await asyncio.wait_for(observed_busy.wait(), 5)
            assert states == {}
            if release_lock:
                await holder.rollback()
                native = await asyncio.wait_for(task, 5)
                assert native == event["event"] and attempts >= 2
                assert "failed_attempt" not in states
                count = await _durable_count(reader, session)
                sequence = await reader.scalar(select(SessionProjection.next_sequence))
                await commit_transcript_wrapper(reader, service=service, context=context,
                                                event=event, harness_states=states)
                assert await _durable_count(reader, session) == count
                assert await reader.scalar(select(SessionProjection.next_sequence)) == sequence
            else:
                with pytest.raises(OperationalError):
                    await task
                assert attempts == 4 and states == {} and reader.is_active
                await holder.rollback()
                assert await _durable_count(reader, session) == 0
        finally:
            await holder.rollback()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_non_busy_ingest_failure_is_not_retried_or_published(execution_db):
    import sqlite3

    from sqlalchemy.exc import OperationalError

    from app.services.harness_transcript_ingest import commit_transcript_wrapper

    calls = 0
    states = {"committed": {"identity": "original"}}

    async def broken(db, **kwargs):
        nonlocal calls
        calls += 1
        kwargs["harness_states"]["committed"]["identity"] = "uncommitted"
        raise OperationalError("statement", {}, sqlite3.OperationalError("no such table: broken"))

    with pytest.raises(OperationalError):
        await commit_transcript_wrapper(execution_db, ingest=broken, harness_states=states)
    assert calls == 1 and states == {"committed": {"identity": "original"}}
