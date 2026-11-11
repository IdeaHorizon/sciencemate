"""Reconcile continuous work using existing commands, workers and checkpoints.

One durable run.continue command binds a successor to its predecessor. Command
updated_at is the bounded dispatch claim until a worker/attempt takes ownership;
there is no recovery table, job copy, or second scheduler.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select, update

from app.config import settings
from app.models.execution import ASSERTS_ACTIVE_WORK, Command, Run, RunStatus
from app.services.run_liveness import ACTIVITY_LEASE_SECONDS

logger = logging.getLogger(__name__)
MAX_CONCURRENT_RESUMES = 5
MAX_RECOVERY_ATTEMPTS = 3
_resuming: set[str] = set()


@dataclass(frozen=True)
class ResumeTarget:
    run_id: str
    project_id: str
    session_id: str


def worker_presence(project_id: str, session_id: str) -> str:
    """Unconnectable is not dead. A live PID never authorizes replacement."""
    from app.services.harness_imports import harness_module
    from app.services.harness_sessions import _session_lock_path
    from app.services.session_event_log import registry_row

    path = _session_lock_path(project_id, session_id)
    row = registry_row(path) if path is not None else {}
    pid = row.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return "unknown"
    return "live" if harness_module("shared.lib.process_control").alive(pid) else "dead"


async def _latest_runs(db) -> list[Run]:
    rows = (
        await db.scalars(
            select(Run)
            .where(
                Run.tenant_id == settings.runtime_tenant_id,
                Run.parent_run_id.is_(None),
            )
            .order_by(Run.created_at.desc(), Run.id.desc())
        )
    ).all()
    latest = {}
    for run in rows:
        latest.setdefault((run.project_id, run.session_id), run)
    return list(latest.values())


def _retryable_provider_failure(run: Run) -> bool:
    failure = (run.summary or {}).get("failure") or {}
    # This is the worker's existing typed classification from
    # core.llm.is_transient_provider_error, not another HTTP/provider list.
    return failure.get("code") == "upstream_unavailable" and failure.get("retryable") is True


async def find_resumable_continuous_runs(
    db,
    *,
    now: datetime | None = None,
    own_reservation: str | None = None,
) -> list[ResumeTarget]:
    from app.models.execution import RunAttempt, SessionProjection
    from app.models.project import Project, ProjectStatus
    from app.services import run_liveness
    from app.services.harness_sessions import harness_session_manager
    from app.services.local_execution import project_autonomy_policy

    at = now or datetime.now(UTC)
    active = {s.value for s in ASSERTS_ACTIVE_WORK}
    targets = []
    # Pick latest across ALL statuses before eligibility: a later stop or human
    # pause supersedes interrupted work, even when the old command is unfinished.
    for run in await _latest_runs(db):
        summary = run.summary or {}
        interrupted = bool((summary.get("failure") or {}).get("runtimeInterrupted"))
        provider_retryable = _retryable_provider_failure(run)
        recoverable = interrupted or provider_retryable
        if run.status not in active and not (
            run.status == RunStatus.STALE_UNKNOWN.value
            or (run.status == RunStatus.FAILED.value and recoverable)
        ):
            continue
        session = await db.get(SessionProjection, run.session_id)
        project = await db.get(Project, run.project_id)
        if (
            session is None
            or session.archived_at is not None
            or session.lifecycle_status != "active"
            or project is None
            or project.status == ProjectStatus.ARCHIVED
        ):
            continue
        policy = await project_autonomy_policy(db, project)
        if not (policy.unattended and "*" in policy.authorized_risk_classes):
            continue
        live = harness_session_manager.has_live_runtime(run.project_id, run.session_id)
        idle_provider = False
        if provider_retryable and live:
            from app.services.harness_sessions import read_worker_activity

            activity = read_worker_activity(run.project_id, run.session_id)
            idle_provider = bool(
                activity is not None
                and activity.declared == "idle"
                and not harness_session_manager.is_occupied(run.project_id, run.session_id)
                and harness_session_manager.paused_binding(run.project_id, run.session_id) is None
            )
        if live and not idle_provider:
            continue
        presence = worker_presence(run.project_id, run.session_id)
        if presence == "live" and not idle_provider:
            continue
        attempt = await db.scalar(
            select(RunAttempt)
            .where(
                RunAttempt.run_id == run.id,
            )
            .order_by(RunAttempt.attempt_no.desc())
            .limit(1)
        )
        command = await db.scalar(
            select(Command)
            .where(
                Command.run_id == run.id,
                Command.tenant_id == run.tenant_id,
                Command.kind.like("run.%"),
            )
            .order_by(Command.created_at.desc())
            .limit(1)
        )
        if command is None:
            continue  # no durable execution intent; an old status alone is not work
        pending = command.result is None and command.error is None
        if not pending and not recoverable:
            continue
        # No arbitrary task expiry: a silent model heartbeat cannot revoke a
        # continuous task. Unknown liveness still uses the existing activity lease.
        if presence != "dead" and not recoverable and attempt is not None:
            if (
                run_liveness.activity(run, attempt=attempt, now=at, has_live_binding=False)
                != "unknown"
            ):
                continue
        target_id = run.id
        if (
            pending
            and command.kind == "run.continue"
            and (attempt is None or not attempt.sandbox_manifest)
        ):
            # The same durable dispatch can outlive the process which reserved
            # it, including Run-created/attempt-not-created. No worker could have
            # received an execution without the frozen attempt manifest.
            target_id = command.payload["predecessorRunId"]
        if target_id in _resuming and target_id != own_reservation:
            continue
        targets.append(ResumeTarget(target_id, run.project_id, run.session_id))
    return targets


_CONTINUATION_MESSAGE = (
    "平台执行进程中断；从本会话已保存的断点继续。先核对现有作业与结果，"
    "复用仍在运行或已完成的物理作业，不要重新提交。未知副作用必须先对账。"
)


async def _claim_open_command(db, command, *, now: datetime) -> bool:
    claimed = await db.execute(
        update(Command)
        .where(
            Command.id == command.id,
            Command.updated_at == command.updated_at,
            Command.updated_at < now - timedelta(seconds=ACTIVITY_LEASE_SECONDS),
            Command.result.is_(None),
            Command.error.is_(None),
        )
        .values(updated_at=now)
    )
    await db.commit()
    return claimed.rowcount == 1


async def _recovery_depth(db, run_id: str) -> int:
    depth, seen = 0, set()
    while run_id not in seen and depth < MAX_RECOVERY_ATTEMPTS:
        seen.add(run_id)
        command = await db.scalar(
            select(Command)
            .where(
                Command.run_id == run_id,
                Command.kind == "run.continue",
                Command.tenant_id == settings.runtime_tenant_id,
            )
            .order_by(Command.created_at.desc())
            .limit(1)
        )
        if command is None:
            break
        depth += 1
        run_id = command.payload.get("predecessorRunId")
        if not run_id:
            break
    return depth


async def _claim_continuation(db, target, session, user):
    key = f"continuation:{target.run_id}"
    command = await db.scalar(
        select(Command).where(
            Command.tenant_id == session.tenant_id,
            Command.actor_user_id == user.id,
            Command.idempotency_key == key,
        )
    )
    now = datetime.now(UTC)
    if command is not None:
        if command.result is not None or command.error is not None:
            return None
        # Compare-and-swap the existing command's dispatch claim. Concurrent
        # consumers cannot both acquire it, including when no Run was created yet.
        return command if await _claim_open_command(db, command, now=now) else None
    depth = await _recovery_depth(db, target.run_id)
    if depth >= MAX_RECOVERY_ATTEMPTS:
        from app.services.app_events import record_app_event

        await record_app_event(
            db,
            session_id=target.session_id,
            kind="run.recovery_attention",
            run_id=target.run_id,
            payload={"reason": "recovery_attempt_budget", "attempts": depth},
            dedupe_key=f"recovery-budget:{target.run_id}",
        )
        await db.commit()
        return None
    command = Command(
        tenant_id=session.tenant_id,
        workspace_id=session.workspace_id,
        project_id=target.project_id,
        session_id=target.session_id,
        actor_user_id=user.id,
        kind="run.continue",
        idempotency_key=key,
        run_id="run_" + uuid5(NAMESPACE_URL, key).hex,
        payload={"predecessorRunId": target.run_id},
        updated_at=now,
    )
    db.add(command)
    await db.commit()  # unique command key arbitrates two first-time consumers
    return command


async def _resume_one(factory, target: ResumeTarget) -> None:
    from app.models.execution import SessionProjection
    from app.models.project import Project
    from app.models.user import User
    from app.services.local_execution import execute_local_turn

    async def _noop_progress(_event):
        return None

    try:
        async with factory() as db:
            # Recheck current facts without temporarily dropping our reservation.
            eligible = await find_resumable_continuous_runs(db, own_reservation=target.run_id)
            if target not in eligible:
                return
            session = await db.get(SessionProjection, target.session_id, with_for_update=True)
            driver = session.initiating_user_id or session.created_by_user_id
            user = await db.get(User, driver) if driver else None
            project = await db.get(Project, target.project_id)
            if user is None or project is None:
                return
            run = await db.get(Run, target.run_id)
            if run is not None and run.status in {s.value for s in ASSERTS_ACTIVE_WORK}:
                from app.services.harness_sessions import (
                    _replay_worker_events,
                    terminal_result_on_disk,
                )
                from app.services.local_execution import RejoinTarget, _open_command_for_run

                result, request_id = terminal_result_on_disk(run)
                if result is not None:
                    command = await _open_command_for_run(db, run_id=run.id)
                    if command is None or not await _claim_open_command(
                        db,
                        command,
                        now=datetime.now(UTC),
                    ):
                        return
                    await _replay_worker_events(db, run=run, owner_user_id=user.id)
                    await db.commit()
                    await execute_local_turn(
                        db,
                        user=user,
                        conversation=session,
                        message="",
                        project=project,
                        on_progress=_noop_progress,
                        rejoin=RejoinTarget(
                            run_id=run.id, request_id=request_id, disk_result=result
                        ),
                    )
                    return
            command = await _claim_continuation(db, target, session, user)
            if command is None:
                return
            await db.commit()  # no writer may be held while waiting on the worker
            await execute_local_turn(
                db,
                user=user,
                conversation=session,
                message=_CONTINUATION_MESSAGE,
                project=project,
                on_progress=_noop_progress,
                system_continuation=True,
                continuation=command,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("automatic continuation failed for run %s", target.run_id)
    finally:
        _resuming.discard(target.run_id)


async def resume_interrupted_continuous_runs(factory) -> list[str]:
    from app.models.execution import SessionProjection
    from app.services.harness_sessions import harness_session_manager
    from app.services.local_execution import REJOIN_KIND, retain_detached_execution

    async with factory() as db:
        for run in await _latest_runs(db):
            if run.status not in {
                s.value for s in ASSERTS_ACTIVE_WORK
            } and not _retryable_provider_failure(run):
                continue
            session = await db.get(SessionProjection, run.session_id)
            owner = (session.initiating_user_id or session.created_by_user_id) if session else None
            if owner:
                await db.commit()
                await harness_session_manager.adopt_live_worker(
                    run.project_id,
                    run.session_id,
                    owner_user_id=owner,
                )
        targets = await find_resumable_continuous_runs(db)
    started = []
    for target in targets:
        if len(_resuming) >= MAX_CONCURRENT_RESUMES:
            break
        if target.run_id in _resuming:
            continue
        _resuming.add(target.run_id)
        task = asyncio.create_task(
            _resume_one(factory, target), name=f"resume-{target.run_id[:12]}"
        )
        retain_detached_execution(task, kind=REJOIN_KIND)
        started.append(target.run_id)
    return started
