"""Process-owned bridge from Harness tools to authenticated Project Resources."""

from __future__ import annotations

import json
import os

_handler = None
_context = None
DELIVERY_ENV = "HARNESS_RESOURCE_RUNTIME"


def install(handler):
    global _handler
    _handler = handler


def bind_worker(*, user_id, project_id):
    global _context
    delivery = os.environ.pop(DELIVERY_ENV, "")
    if delivery:
        _context = json.loads(delivery)
    if _context is None:
        return
    # App settings validate at import time, including org deployments. Only the
    # bootstrap import sees these values in the environment.
    names = dict(
        database_url="DATABASE_URL",
        platform_data_root="PLATFORM_DATA_ROOT",
        runtime_tenant_id="RUNTIME_TENANT_ID",
        profile="PLATFORM_PROFILE",
        secret_key="SECRET_KEY",
    )
    previous = {name: os.environ.get(name) for name in names.values()}
    # The worker uses this name for its socket *path*. App Settings uses the
    # same legacy name for a boolean. It is transport state, not App config.
    previous["HARNESS_CONTROL_SOCKET"] = os.environ.get("HARNESS_CONTROL_SOCKET")
    try:
        os.environ.pop("HARNESS_CONTROL_SOCKET", None)
        os.environ.update({name: str(_context[key]) for key, name in names.items()})
        from app.services.ssh_resources import install_worker_bridge
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    install_worker_bridge(_context, user_id=user_id, project_id=str(project_id))


async def call(state, action, **params):
    if _handler is None:
        return dict(
            status="error",
            error="Project Resources execution bridge is not installed in this runtime",
        )
    try:
        result = await _handler(state, action, params)
    except Exception as exc:
        return dict(
            status="error",
            error=getattr(exc, "detail", str(exc)),
            retryable=getattr(exc, "status_code", 500) in (500, 502, 503, 504),
        )
    state.append_transcript(
        "remote_resource_operation",
        action=action,
        resource_id=result.get("resource_id") or (result.get("resource") or {}).get("id"),
        job_id=result.get("job_id"),
        remote_state=result.get("state"),
    )
    return result


async def wait(
    state,
    resource_id: str,
    job_id: str,
    *,
    poll_interval_s=None,
    max_wait_s=None,
    resume_tool="remote_job",
    resume_arguments=None,
) -> dict:
    """Yield to the harness for one exact SSH job, not a command/pgrep loop.

    Every observation still goes through the authenticated bridge. The remote
    record remains authoritative; a failed connection never becomes job failure.
    """
    from core import job_wait

    arguments = resume_arguments or {
        key: value
        for key, value in dict(
            action="wait",
            resource_id=resource_id,
            job_id=job_id,
            poll_interval_s=poll_interval_s,
            max_wait_s=max_wait_s,
        ).items()
        if value is not None
    }
    previous = job_wait.load(state)
    same_wait = previous and previous["tool"] == resume_tool and previous["arguments"] == arguments
    expected = (
        previous["identity"] if previous and (same_wait or not previous.get("finished")) else None
    )
    registered = False
    failures = 0

    async def observe():
        nonlocal expected, failures, registered
        result = await call(state, "status", resource_id=resource_id, job_id=job_id)
        if result.get("status") != "success":
            failures += 1
            return {
                **result,
                "wait_outcome": "connection_unconfirmed",
                "job_id": job_id,
                "resource_id": resource_id,
                "do_not_resubmit": True,
            }
        identity = {
            key: result.get(key)
            for key in ("project_id", "resource_id", "job_id", "request_digest")
        }
        if (
            identity["project_id"] != str(state.project_id)
            or identity["resource_id"] != resource_id
            or identity["job_id"] != job_id
            or not identity["request_digest"]
            or (expected is not None and identity != expected)
        ):
            return dict(
                status="error",
                error="Job wait identity does not match the selected job",
                wait_outcome="identity_mismatch",
                do_not_resubmit=True,
            )
        failures = 0
        if not registered:
            expected = identity
            job_wait.register(state, resume_tool, arguments, identity)
            registered = True
        return result

    def done(result):
        if result.get("status") != "success":
            return not result.get("retryable") or failures >= 3
        return result.get("state") not in ("launching", "running")

    try:
        result = await job_wait.poll(
            state,
            observe,
            done,
            interval=poll_interval_s,
            steady_interval=30.0,
            max_wait_s=max_wait_s,
        )
    except job_wait.RunCancelled:
        state.append_transcript(
            "job_wait_cancelled",
            identity=expected,
            note="Wait stopped; use managed cancel to terminate the remote job",
        )
        job_wait.acknowledge(state)
        raise
    state.append_transcript(
        "job_wait_observed",
        identity=expected,
        remote_state=result.get("state"),
        wait_outcome=result.get("wait_outcome"),
    )
    job_wait.finish(state)
    return result
