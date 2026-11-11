"""Existing worker transience facts reach the durable, bounded scheduler."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.execution import Command, Run, SessionProjection
from app.services import harness_sessions, local_execution, restart_resume
from app.services.harness_sessions import HarnessSessionError, harness_session_manager
from tests.test_continuous_run_resumes_after_restart import _seed


def classified(status):
    from app.services.harness_imports import harness_module

    class ExhaustedProvider(Exception):
        pass

    exc = ExhaustedProvider("provider exhausted its internal retry budget")
    exc.status = status
    code = harness_module("platform_runtime")._error_code_for(exc)
    return local_execution._sanitized_platform_failure(HarnessSessionError(str(exc), code=code))


async def drain():
    tasks = [
        t
        for t, kind in list(local_execution._DETACHED_EXECUTION_TASKS.items())
        if kind == local_execution.REJOIN_KIND
    ]
    if tasks:
        await asyncio.gather(*tasks)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    restart_resume._resuming.clear()
    monkeypatch.setattr(harness_session_manager, "_sessions", {})
    yield
    restart_resume._resuming.clear()


@pytest.mark.parametrize("status,eligible", [(503, True), (401, False)])
async def test_503_then_available_continues_but_nonretryable_does_not(
    db_session,
    db_engine,
    monkeypatch,
    status,
    eligible,
):
    rid = await _seed(db_session, status="failed")
    run = await db_session.get(Run, rid)
    failure = classified(status)
    run.summary = {"failure": failure}
    command = await db_session.scalar(select(Command).where(Command.run_id == rid))
    command.error = failure
    await db_session.commit()
    calls = []

    async def now_available(db, **kwargs):
        successor = kwargs["continuation"]
        calls.append(successor.run_id)
        assert kwargs["system_continuation"] is True
        db.add(
            Run(
                id=successor.run_id,
                tenant_id=successor.tenant_id,
                workspace_id=successor.workspace_id,
                project_id=successor.project_id,
                session_id=successor.session_id,
                status="completed",
                created_at=datetime.now(UTC),
                summary={"continuationOf": rid},
            )
        )
        successor.result = {"status": "completed"}
        await db.commit()

    monkeypatch.setattr(local_execution, "execute_local_turn", now_available)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    selected = await restart_resume.resume_interrupted_continuous_runs(factory)
    await drain()
    assert selected == ([rid] if eligible else [])
    assert len(calls) == int(eligible)
    assert await restart_resume.resume_interrupted_continuous_runs(factory) == []
    await db_session.refresh(run)
    assert run.status == "failed"


async def test_retry_budget_is_finite_for_the_original_command_lineage(
    db_session, db_engine, monkeypatch
):
    rid = await _seed(db_session, status="failed")
    original = await db_session.get(Run, rid)
    failure = classified(503)
    predecessor = rid
    session = await db_session.get(SessionProjection, original.session_id)
    for attempt in range(restart_resume.MAX_RECOVERY_ATTEMPTS):
        successor = f"provider-recovery-{attempt}"
        db_session.add(
            Command(
                tenant_id=original.tenant_id,
                workspace_id=original.workspace_id,
                project_id=original.project_id,
                session_id=original.session_id,
                actor_user_id=session.created_by_user_id,
                kind="run.continue",
                run_id=successor,
                idempotency_key=f"continuation:{predecessor}",
                payload={"predecessorRunId": predecessor},
                error=failure,
                created_at=datetime.now(UTC) - timedelta(days=3),
            )
        )
        predecessor = successor
    db_session.add(
        Run(
            id=predecessor,
            tenant_id=original.tenant_id,
            workspace_id=original.workspace_id,
            project_id=original.project_id,
            session_id=original.session_id,
            status="failed",
            summary={"failure": failure},
            created_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    assert (
        await restart_resume._recovery_depth(db_session, predecessor)
        == restart_resume.MAX_RECOVERY_ATTEMPTS
    )

    async def forbidden(*args, **kwargs):
        pytest.fail("an exhausted task received an unbounded provider attempt")

    monkeypatch.setattr(local_execution, "execute_local_turn", forbidden)
    await restart_resume.resume_interrupted_continuous_runs(
        async_sessionmaker(db_engine, expire_on_commit=False)
    )
    await drain()
    assert (
        len((await db_session.scalars(select(Command).where(Command.kind == "run.continue"))).all())
        == restart_resume.MAX_RECOVERY_ATTEMPTS
    )


async def test_retryable_provider_does_not_take_busy_or_human_paused_worker(
    db_session, monkeypatch
):
    rid = await _seed(db_session, status="failed")
    run = await db_session.get(Run, rid)
    run.summary = {"failure": classified(503)}
    await db_session.commit()
    monkeypatch.setattr(harness_session_manager, "has_live_runtime", lambda *_: True)
    monkeypatch.setattr(restart_resume, "worker_presence", lambda *_: "live")
    monkeypatch.setattr(
        harness_sessions, "read_worker_activity", lambda *_: SimpleNamespace(declared="idle")
    )
    monkeypatch.setattr(harness_session_manager, "is_occupied", lambda *_: False)
    monkeypatch.setattr(harness_session_manager, "paused_binding", lambda *_: None)
    assert [t.run_id for t in await restart_resume.find_resumable_continuous_runs(db_session)] == [
        rid
    ]
    monkeypatch.setattr(harness_session_manager, "is_occupied", lambda *_: True)
    assert await restart_resume.find_resumable_continuous_runs(db_session) == []
    monkeypatch.setattr(harness_session_manager, "is_occupied", lambda *_: False)
    monkeypatch.setattr(harness_session_manager, "paused_binding", lambda *_: object())
    assert await restart_resume.find_resumable_continuous_runs(db_session) == []
