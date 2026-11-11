"""Desktop-owned personal runtime connections, available in both editions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, SecretStr, field_validator

from app.config import settings
from app.services import remote_servers as store
from app.services.other_homes import ThereIsNoSuchHomeError
from app.services.remote_transport import HostKeyRequiredError, provision, ssh_aliases, tunnels
from app.services.user_interface import Language, language_on_screen


def desktop_only() -> None:
    from app.assembly import can_choose_project_folders

    if not can_choose_project_folders() or settings.remote_runtime_token:
        raise HTTPException(403, "Remote connections are managed on your personal desktop.")


router = APIRouter(prefix="/remote-servers", dependencies=[Depends(desktop_only)])
runtime_router = APIRouter()
AVAILABILITY_TIMEOUT_SECONDS = 5.0
_operations: dict[str, dict] = {}
_tasks: set[asyncio.Task] = set()


class ConnectionInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    host: str = Field(min_length=1, max_length=253)
    username: str = Field(default="", max_length=100)
    port: int | None = Field(default=None, ge=1, le=65535)
    auth_method: Literal["config", "key", "password"] = "config"
    identity_file: str = Field(default="", max_length=4096)
    password: SecretStr | None = None
    passphrase: SecretStr | None = None
    fingerprint: str | None = Field(default=None, max_length=200)
    remember_secret: bool = False
    server_id: str | None = None

    @field_validator("name", "host", "username", "identity_file")
    @classmethod
    def clean(cls, value: str) -> str:
        value = value.strip()
        if any(ord(char) < 32 for char in value):
            raise ValueError("Control characters are not allowed")
        return value

    @field_validator("host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        if (
            not value
            or any(char.isspace() or char in "/\\@" for char in value)
            or value.startswith("-")
        ):
            raise ValueError("Enter an IP address, hostname or SSH alias")
        return value

    @field_validator("server_id")
    @classmethod
    def valid_server_id(cls, value: str | None) -> str | None:
        if value:
            from uuid import UUID

            if not value.startswith(store.PREFIX):
                raise ValueError("Invalid server connection ID")
            UUID(value.removeprefix(store.PREFIX))
        return value


@runtime_router.get("/remote-runtime")
async def describe_runtime() -> dict:
    from app.version import installed_version

    return {
        "protocol": store.PROTOCOL,
        "personal": bool(settings.remote_runtime_token),
        "version": installed_version(),
    }


@router.get("")
async def list_connections() -> dict:
    return {
        "items": [
            {**item, "connected": tunnels.connected(item["id"])} for item in store.all_servers()
        ]
    }


@router.get("/ssh-options")
async def ssh_options() -> dict:
    def read():
        root = Path.home() / ".ssh"
        keys = sorted(
            str(path)
            for path in root.glob("*")
            if path.is_file()
            and not path.name.endswith(".pub")
            and path.name not in {"config", "known_hosts", "known_hosts.old", "authorized_keys"}
        )
        return {"aliases": ssh_aliases(), "keys": keys}

    return await asyncio.to_thread(read)


@router.post("/connect", status_code=202)
async def connect_server(
    data: ConnectionInput, lang: Language = Depends(language_on_screen)
) -> dict:
    existing = store.get(data.server_id) if data.server_id else None
    if data.server_id and not existing:
        raise HTTPException(404, "Server not found")
    if not existing:
        existing = next(
            (
                item
                for item in store.all_servers()
                if all(item[key] == getattr(data, key) for key in ("host", "username", "port"))
            ),
            None,
        )
    # Existing connections keep their pinned identity; editing a fingerprint cannot replace it.
    record = {
        **data.model_dump(exclude={"password", "passphrase", "remember_secret", "server_id"}),
        "id": existing["id"] if existing else store.PREFIX + str(uuid4()),
    }
    if existing:
        if any(record[key] != existing[key] for key in ("host", "username", "port")):
            raise HTTPException(409, "Create a separate connection for a different server.")
        record["fingerprint"] = existing["fingerprint"]
    credentials = {
        key: value.get_secret_value()
        for key in ("password", "passphrase")
        if (value := getattr(data, key)) is not None
    }
    operation_id = str(uuid4())
    operation = {"id": operation_id, "phase": "connecting", "done": False}
    # Bound finished-operation history; active installs remain visible.
    for key in list(_operations):
        if len(_operations) < 100:
            break
        if _operations[key]["done"]:
            _operations.pop(key)
    _operations[operation_id] = operation

    async def install():
        try:
            operation["phase"] = "installing"
            installed, secrets = await provision(record, credentials)
            await tunnels.disconnect(installed["id"])
            operation["phase"] = "checking"
            info = await store.ask_connected(installed, secrets, "GET", "/api/v1/remote-runtime")
            if info.get("protocol") != store.PROTOCOL or not info.get("personal"):
                raise ThereIsNoSuchHomeError(
                    "远程运行时版本不兼容，请更新后重试。",
                    en="The remote runtime is incompatible. Update it and retry.",
                    status=409,
                )
            await asyncio.to_thread(
                store.save, installed, secrets, remember_secret=data.remember_secret
            )
            operation.update(phase="ready", server={**installed, "connected": True})
        except HostKeyRequiredError as exc:
            operation.update(phase="host_key", fingerprint=exc.fingerprint, error=exc.said_in(lang))
        except ThereIsNoSuchHomeError as exc:
            operation.update(phase="failed", error=exc.said_in(lang))
        except Exception:
            # Do not expose connection-library errors or remote output containing secrets.
            operation.update(
                phase="failed",
                error=(
                    "连接配置失败，请重试。"
                    if lang == "zh"
                    else "Connection setup failed. Please retry."
                ),
            )
        finally:
            if operation["phase"] != "ready":
                await tunnels.disconnect(record["id"])
            operation["done"] = True

    task = asyncio.create_task(install(), name=f"remote-install-{operation_id}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"operation_id": operation_id}


@router.get("/operations/{operation_id}")
async def installation_status(operation_id: str) -> dict:
    if operation_id not in _operations:
        raise HTTPException(404, "Installation status unavailable. Check the server and retry.")
    return _operations[operation_id]


@router.post("/{server_id}/check")
async def check_server(server_id: str, lang: Language = Depends(language_on_screen)) -> dict:
    try:
        store.set_enabled(server_id, True)
        info = await store.ask(server_id, "GET", "/api/v1/remote-runtime")
        if info.get("protocol") != store.PROTOCOL or not info.get("personal"):
            raise HTTPException(409, "Update the remote runtime to connect.")
        return info
    except ThereIsNoSuchHomeError as exc:
        raise HTTPException(exc.status, exc.said_in(lang)) from exc


@router.get("/{server_id}/availability")
async def server_availability(
    server_id: str,
    lang: Language = Depends(language_on_screen),
) -> dict:
    """Observe API reachability; never infer or change a research task's lifecycle."""
    record = store.get(server_id)
    if record is None:
        raise HTTPException(404, "Server not found")
    state = "online"
    try:
        if record.get("enabled") is False:
            state = "disconnected"
        else:
            async with asyncio.timeout(AVAILABILITY_TIMEOUT_SECONDS):
                info = await store.ask(server_id, "GET", "/api/v1/remote-runtime")
            if info.get("protocol") != store.PROTOCOL or not info.get("personal"):
                state = "incompatible"
    except TimeoutError:
        state = "unavailable"
    except ThereIsNoSuchHomeError as exc:
        state = {401: "authentication_required", 409: "verification_required"}.get(
            exc.status, "unavailable"
        )
    return {"state": state, "checked_at": datetime.now(UTC).isoformat()}


@router.post("/{server_id}/disconnect", status_code=204)
async def disconnect_server(server_id: str, lang: Language = Depends(language_on_screen)):
    try:
        store.set_enabled(server_id, False)
        await tunnels.disconnect(server_id)
    except ThereIsNoSuchHomeError as exc:
        raise HTTPException(exc.status, exc.said_in(lang)) from exc


@router.delete("/{server_id}", status_code=204)
async def remove_server(server_id: str, lang: Language = Depends(language_on_screen)):
    try:
        await store.forget(server_id)
    except ThereIsNoSuchHomeError as exc:
        raise HTTPException(exc.status, exc.said_in(lang)) from exc
