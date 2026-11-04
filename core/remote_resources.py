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
        return dict(status="error", error=getattr(exc, "detail", str(exc)))
    state.append_transcript(
        "remote_resource_operation",
        action=action,
        resource_id=result.get("resource_id") or (result.get("resource") or {}).get("id"),
        job_id=result.get("job_id"),
        remote_state=result.get("state"),
    )
    return result
