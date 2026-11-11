"""Durable recovery arbitration, distinct from a process-local scheduling set."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.execution import Command, Run, RunStatus
from app.services import local_execution, restart_resume
from tests.test_continuous_run_resumes_after_restart import _seed


@pytest.fixture(autouse=True)
def clean_reservations():
    restart_resume._resuming.clear()
    yield
    restart_resume._resuming.clear()


async def test_failed_transport_is_eligible_but_provider_failure_is_not(db_session):
    rid = await _seed(db_session, status=RunStatus.FAILED.value)
    run = await db_session.get(Run, rid)
    run.summary = {"failure": {"runtimeInterrupted": True}}
    await db_session.commit()
    assert [t.run_id for t in await restart_resume.find_resumable_continuous_runs(db_session)] == [
        rid
    ]
    run.summary = {"failure": {"retryable": True}}
    await db_session.commit()
    assert await restart_resume.find_resumable_continuous_runs(db_session) == []


async def test_dead_registered_worker_overrides_a_fresh_activity_lease(db_session, monkeypatch):
    rid = await _seed(db_session, stale_reason="", lease_offset_minutes=5)
    monkeypatch.setattr(restart_resume, "worker_presence", lambda *_: "dead")
    assert [t.run_id for t in await restart_resume.find_resumable_continuous_runs(db_session)] == [
        rid
    ]
    monkeypatch.setattr(restart_resume, "worker_presence", lambda *_: "live")
    assert await restart_resume.find_resumable_continuous_runs(db_session) == []


async def test_overlapping_dispatchers_and_restart_reuse_one_claim(
    db_session, db_engine, monkeypatch
):
    rid = await _seed(db_session)
    target = (await restart_resume.find_resumable_continuous_runs(db_session))[0]
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    calls = []

    async def execute(db, **kwargs):
        command = kwargs["continuation"]
        calls.append((command.id, command.run_id, command.payload))
        assert kwargs["system_continuation"] is True
        await asyncio.sleep(0)

    monkeypatch.setattr(local_execution, "execute_local_turn", execute)
    await asyncio.gather(
        restart_resume._resume_one(factory, target),
        restart_resume._resume_one(factory, target),
    )
    assert len(calls) == 1
    async with factory() as db:
        commands = (await db.scalars(select(Command).where(Command.kind == "run.continue"))).all()
        assert len(commands) == 1
        command_id, successor, payload = calls[0]
        assert payload == {"predecessorRunId": rid} and successor != rid
        await db.execute(
            update(Command)
            .where(Command.id == command_id)
            .values(
                updated_at=datetime.now(UTC) - timedelta(seconds=181),
            )
        )
        await db.commit()
    await asyncio.gather(
        restart_resume._resume_one(factory, target),
        restart_resume._resume_one(factory, target),
    )
    assert calls == [calls[0], calls[0]], "recovery replaced the durable dispatch identity"
    async with factory() as db:
        assert (
            len((await db.scalars(select(Command).where(Command.kind == "run.continue"))).all())
            == 1
        )
        assert (await db.get(Run, rid)).status == RunStatus.RUNNING.value


@pytest.mark.parametrize("status", ["cancelled", "waiting_human", "waiting_permission"])
async def test_scheduled_recovery_rechecks_a_later_human_boundary(
    db_session,
    db_engine,
    monkeypatch,
    status,
):
    await _seed(db_session)
    target = (await restart_resume.find_resumable_continuous_runs(db_session))[0]
    old = await db_session.get(Run, target.run_id)
    db_session.add(
        Run(
            id="later",
            tenant_id=old.tenant_id,
            workspace_id=old.workspace_id,
            project_id=old.project_id,
            session_id=old.session_id,
            status=status,
            created_at=datetime.now(UTC) + timedelta(seconds=1),
        )
    )
    await db_session.commit()

    async def forbidden(*_args, **_kwargs):
        pytest.fail("a stale scheduling snapshot overrode the human boundary")

    monkeypatch.setattr(local_execution, "execute_local_turn", forbidden)
    await restart_resume._resume_one(async_sessionmaker(db_engine, expire_on_commit=False), target)
    assert (
        await db_session.scalars(select(Command).where(Command.kind == "run.continue"))
    ).all() == []


async def test_durable_work_does_not_expire_with_its_model_heartbeat(db_session):
    rid = await _seed(db_session, heartbeat_at=datetime.now(UTC) - timedelta(hours=9))
    assert [t.run_id for t in await restart_resume.find_resumable_continuous_runs(db_session)] == [
        rid
    ]
    from sqlalchemy import delete

    await db_session.execute(delete(Command).where(Command.run_id == rid))
    await db_session.commit()
    assert await restart_resume.find_resumable_continuous_runs(db_session) == []


async def test_a_reserved_successor_without_attempt_or_summary_reuses_its_command(
    db_session,
    db_engine,
    monkeypatch,
):
    from app.models.execution import SessionProjection
    from app.models.user import User

    rid = await _seed(db_session)
    target = (await restart_resume.find_resumable_continuous_runs(db_session))[0]
    source = await db_session.get(Run, rid)
    session = await db_session.get(SessionProjection, source.session_id)
    user = await db_session.get(User, session.created_by_user_id)
    command = await restart_resume._claim_continuation(db_session, target, session, user)
    original = (command.id, command.run_id)
    # Crash window 1: only the durable command exists.
    assert (await restart_resume.find_resumable_continuous_runs(db_session))[0] == target
    # Crash window 2: a newer Run exists, but no attempt, summary or worker.
    db_session.add(
        Run(
            id=command.run_id,
            tenant_id=source.tenant_id,
            workspace_id=source.workspace_id,
            project_id=source.project_id,
            session_id=source.session_id,
            status="running",
            summary={},
            created_at=datetime.now(UTC) + timedelta(milliseconds=1),
        )
    )
    await db_session.execute(
        update(Command)
        .where(Command.id == command.id)
        .values(
            updated_at=datetime.now(UTC) - timedelta(seconds=181),
        )
    )
    await db_session.commit()
    assert await restart_resume.find_resumable_continuous_runs(db_session) == [target]
    calls = []

    async def execute(db, **kwargs):
        calls.append((kwargs["continuation"].id, kwargs["continuation"].run_id))

    monkeypatch.setattr(local_execution, "execute_local_turn", execute)
    await restart_resume._resume_one(async_sessionmaker(db_engine, expire_on_commit=False), target)
    assert calls == [original]
    assert (
        len((await db_session.scalars(select(Command).where(Command.kind == "run.continue"))).all())
        == 1
    )


async def test_a_terminal_disk_result_is_settled_without_a_new_execution(
    db_session,
    db_engine,
    monkeypatch,
):
    from app.services import harness_sessions

    rid = await _seed(db_session)
    target = (await restart_resume.find_resumable_continuous_runs(db_session))[0]
    await db_session.execute(
        update(Command)
        .where(Command.run_id == rid)
        .values(
            updated_at=datetime.now(UTC) - timedelta(seconds=181),
        )
    )
    await db_session.commit()
    result = {"status": "completed", "final_text": "original physical result"}
    monkeypatch.setattr(
        harness_sessions, "terminal_result_on_disk", lambda run: (result, "original")
    )

    async def replay(*args, **kwargs):
        return None

    monkeypatch.setattr(harness_sessions, "_replay_worker_events", replay)
    calls = []

    async def execute(db, **kwargs):
        assert kwargs["rejoin"].run_id == rid and kwargs["rejoin"].disk_result is result
        assert "continuation" not in kwargs
        calls.append(kwargs["rejoin"].request_id)

    monkeypatch.setattr(local_execution, "execute_local_turn", execute)
    await asyncio.gather(
        restart_resume._resume_one(async_sessionmaker(db_engine, expire_on_commit=False), target),
        restart_resume._resume_one(async_sessionmaker(db_engine, expire_on_commit=False), target),
    )
    assert calls == ["original"]
    assert (
        await db_session.scalars(select(Command).where(Command.kind == "run.continue"))
    ).all() == []
