"""Admission-window acceptance with real durable commands and concurrent consumers.

Only final worker dispatch is replaced by an observer. This test proves command
admission/ownership; normal-entry SSH fault experiments prove actual execution.
"""
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.execution import Command, Run, RunStatus, SessionProjection
from app.services import restart_resume
from tests.test_continuous_run_resumes_after_restart import _seed


@pytest.mark.parametrize("run_row_exists", [False, True], ids=["before-run", "before-heartbeat"])
@pytest.mark.parametrize("consumers", [1, 2], ids=["one-consumer", "concurrent-consumers"])
async def test_pending_continuation_is_dispatched_once_with_its_original_command(
    db_session, db_engine, monkeypatch, run_row_exists, consumers,
):
    predecessor_id = await _seed(db_session)
    predecessor = await db_session.get(Run, predecessor_id)
    session = await db_session.get(SessionProjection, predecessor.session_id)
    key = f"continuation:{predecessor_id}"
    successor_id = "run_" + uuid5(NAMESPACE_URL, key).hex
    abandoned_at = datetime.now(UTC) - timedelta(minutes=10)
    if await db_session.scalar(select(Command).where(Command.run_id == predecessor_id)) is None:
        db_session.add(Command(
            tenant_id=predecessor.tenant_id,
            workspace_id=predecessor.workspace_id,
            project_id=predecessor.project_id,
            session_id=predecessor.session_id,
            actor_user_id=session.created_by_user_id,
            kind="run.start",
            idempotency_key=f"start:{predecessor_id}",
            run_id=predecessor_id,
            payload={"message": "one authorized initial task"},
        ))
    await db_session.execute(
        update(Run).where(Run.id == predecessor_id).values(
            created_at=abandoned_at - timedelta(minutes=10),
        )
    )
    command = Command(
        tenant_id=predecessor.tenant_id,
        workspace_id=predecessor.workspace_id,
        project_id=predecessor.project_id,
        session_id=predecessor.session_id,
        actor_user_id=session.created_by_user_id,
        kind="run.continue",
        idempotency_key=key,
        run_id=successor_id,
        payload={"predecessorRunId": predecessor_id},
        created_at=abandoned_at,
        updated_at=abandoned_at,
    )
    db_session.add(command)
    if run_row_exists:
        db_session.add(Run(
            id=successor_id,
            tenant_id=predecessor.tenant_id,
            workspace_id=predecessor.workspace_id,
            project_id=predecessor.project_id,
            session_id=predecessor.session_id,
            status=RunStatus.DISPATCHING.value,
            summary={"continuationOf": predecessor_id},
            created_at=abandoned_at,
            updated_at=abandoned_at,
        ))
    await db_session.commit()
    original_command_id = command.id
    seen = []

    async def observe_dispatch(db, **kwargs):
        admitted = kwargs.get("continuation")
        seen.append((admitted.id, admitted.run_id) if admitted else (None, None))

    monkeypatch.setattr("app.services.local_execution.execute_local_turn", observe_dispatch)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    restart_resume._resuming.clear()
    try:
        await asyncio.gather(*(
            restart_resume.resume_interrupted_continuous_runs(factory) for _ in range(consumers)
        ))
        for _ in range(100):
            if not restart_resume._resuming:
                break
            await asyncio.sleep(.01)
        # A following reconciliation pass must neither abandon nor redispatch it.
        await restart_resume.resume_interrupted_continuous_runs(factory)
        for _ in range(100):
            if not restart_resume._resuming:
                break
            await asyncio.sleep(.01)
        assert seen == [(original_command_id, successor_id)], (
            "Restart after command admission must dispatch that same pending command once; "
            f"run_exists={run_row_exists}, consumers={consumers}, observed={seen}"
        )
    finally:
        restart_resume._resuming.clear()
