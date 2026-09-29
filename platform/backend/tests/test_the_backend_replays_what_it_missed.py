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
