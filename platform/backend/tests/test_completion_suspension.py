"""A suspended child stays nonterminal and exposes a real management action."""

import pytest
from sqlalchemy import select

from app.models.execution import Run, RunAttempt, RunStatus
from app.services.execution_ingest import ExecutionIngestService
from app.services.execution_view import build, build_session_view
from tests.test_execution_foundation import _context, _ingest as _ingest_original, execution_db  # noqa: F401


async def _ingest(service, db, context, raw, **kwargs):
    return await _ingest_original(
        service, db, context, {"at": "2026-11-18T12:00:00Z", **raw}, **kwargs
    )


@pytest.mark.asyncio
async def test_suspend_resume_and_terminal_use_the_same_attempt_and_only_admitted_end(execution_db):
    service = ExecutionIngestService()
    context = _context()
    state = {}
    raws = [
        {"event": "run_start", "run_id": "original", "node_type": "experiment"},
        {
            "event": "run_suspended",
            "run_id": "original",
            "code": "closure_manifest_missing",
            "disposition": "suspend",
            "fact_revision": "actual-facts",
            "facts": {},
            "notice": "交付尚未封存。",
            "recoverable": True,
            "resumable": True,
        },
    ]
    for index, raw in enumerate(raws):
        await _ingest(service, execution_db, context, raw, offset=index * 100, state=state)
    run = await execution_db.scalar(select(Run))
    assert run.status == RunStatus.SUSPENDED
    assert run.ended_at is None
    attempt = await execution_db.scalar(select(RunAttempt))
    assert attempt.ended_at is None
    attempt_id = attempt.id
    view = build(run, observed_status=RunStatus.SUSPENDED, live_runtime=True)
    assert view["phase"] == "alive"
    assert view["waitingOn"] == {"kind": "completion"}
    assert view["canStop"]
    session = build_session_view(
        run,
        pause=None,
        completion_runs=[run],
        observed_status=RunStatus.SUSPENDED,
        live_runtime=True,
    )
    assert session["completions"] == [
        {"runId": run.id, "notice": "交付尚未封存。", "canResume": True, "recoverable": True}
    ]
    readonly = build_session_view(
        run,
        pause=None,
        completion_runs=[run],
        observed_status=RunStatus.SUSPENDED,
        live_runtime=True,
        may_drive=False,
    )
    assert not readonly["completions"][0]["canResume"]
    await _ingest(
        service,
        execution_db,
        context,
        {"event": "completion_resumed", "run_id": "original"},
        offset=200,
        state=state,
    )
    assert run.status == RunStatus.RUNNING
    assert run.summary["completion"] is None
    await _ingest(
        service,
        execution_db,
        context,
        {"event": "run_end", "status": "completed"},
        offset=300,
        state=state,
    )
    assert run.status == RunStatus.COMPLETED
    assert run.ended_at is not None
    assert (await execution_db.scalar(select(RunAttempt))).id == attempt_id


@pytest.mark.asyncio
async def test_loop_observation_never_completes_executor_owned_run(execution_db):
    service = ExecutionIngestService()
    context = _context()
    state = {}
    await _ingest(service, execution_db, context, {"event": "run_start"}, offset=0, state=state)
    await _ingest(
        service,
        execution_db,
        context,
        {"event": "loop_finished", "status": "completed", "managed_finalization": True},
        offset=100,
        state=state,
    )
    run = await execution_db.scalar(select(Run))
    assert run.status == RunStatus.RUNNING
    assert run.ended_at is None
