"""Acceptance: recovery cannot step backwards past a later session decision.

These fixtures use persisted runs and the public recovery selector. No worker,
model, or dispatcher replacement chooses the answer for the selector.
"""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import update

from app.models.execution import Run, RunStatus
from app.services.restart_resume import find_resumable_continuous_runs
from tests.test_continuous_run_resumes_after_restart import _seed


async def interrupted_then(db, status):
    predecessor_id = await _seed(db)
    predecessor = await db.get(Run, predecessor_id)
    await db.execute(
        update(Run).where(Run.id == predecessor_id).values(
            created_at=datetime.now(UTC) - timedelta(minutes=10),
        )
    )
    latest = Run(
        id=f"run_{uuid4().hex}",
        tenant_id=predecessor.tenant_id,
        workspace_id=predecessor.workspace_id,
        project_id=predecessor.project_id,
        session_id=predecessor.session_id,
        status=status,
        created_at=datetime.now(UTC),
    )
    db.add(latest)
    await db.commit()
    return predecessor, latest


@pytest.mark.parametrize(
    "status",
    [
        RunStatus.CANCELLED.value,
        RunStatus.WAITING_HUMAN.value,
        RunStatus.WAITING_PERMISSION.value,
        RunStatus.COMPLETED.value,
        RunStatus.COMPLETED_WITH_WARNING.value,
    ],
)
async def test_a_later_stop_pause_or_completion_prevents_reviving_old_work(db_session, status):
    predecessor, latest = await interrupted_then(db_session, status)

    selected = await find_resumable_continuous_runs(db_session)

    assert selected == [], (
        f"Recovery selected {predecessor.id} before later {latest.id} ({status}); "
        "select the latest top-level run across all statuses before evaluating eligibility"
    )


async def test_an_older_interruption_is_not_selected_under_a_new_running_turn(db_session):
    predecessor, latest = await interrupted_then(db_session, RunStatus.RUNNING.value)

    selected = await find_resumable_continuous_runs(db_session)

    assert predecessor.id not in {target.run_id for target in selected}, (
        f"An older interruption was selected while a newer turn {latest.id} exists"
    )
