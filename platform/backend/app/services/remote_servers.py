"""Personal runtime connections. Metadata, credentials and cached projects stay separate.

The server owns the project; this desktop only owns its SSH connection. Closing a
tunnel never sends a stop/cancel command to the remote runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from shared.lib.private_files import mkdir_private

from app.config import settings
from app.services.other_homes import ThereIsNoSuchHomeError

PREFIX = "remote:"
PROTOCOL = 1
_session_credentials: dict[str, dict] = {}


def _directory() -> Path:
    return Path(settings.platform_data_root).expanduser() / "remote-servers"


def _path(server_id: str, suffix: str = ".json") -> Path:
    try:
        key = str(UUID(server_id.removeprefix(PREFIX)))
    except ValueError as exc:
        raise ThereIsNoSuchHomeError("找不到这台服务器。", en="Server not found.") from exc
    return _directory() / (key + suffix)


def _write(path: Path, content: bytes) -> None:
    mkdir_private(path.parent)
    temporary = path.with_name(path.name + "." + uuid4().hex)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _json(path: Path, default):
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise ThereIsNoSuchHomeError(
            "无法读取服务器连接记录，请检查应用数据目录。",
            en="Cannot read the server connection record. Check the application data directory.",
            status=409,
        ) from exc


def get(server_id: str) -> dict | None:
    return _json(_path(server_id), None)


def set_enabled(server_id: str, enabled: bool) -> None:
    record = get(server_id)
    if record is None:
        raise ThereIsNoSuchHomeError("找不到这台服务器。", en="Server not found.")
    _write(_path(server_id), json.dumps({**record, "enabled": enabled}).encode())


def all_servers() -> list[dict]:
    root = _directory()
    if not root.exists():
        return []
    return sorted(
        (
            record
            for path in root.glob("*.json")
            if not path.name.endswith(".projects.json")
            and (record := _json(path, None))
            and "id" in record
        ),
        key=lambda record: record["name"].casefold(),
    )


def save(record: dict, credentials: dict, *, remember_secret: bool) -> dict:
    from app.services.credential_key import master_key

    # The API token is always durable; login passwords/passphrases are opt-in.
    durable = {
        key: value
        for key, value in credentials.items()
        if key not in {"password", "passphrase"} or remember_secret
    }
    _write(_path(record["id"], ".enc"), Fernet(master_key()).encrypt(json.dumps(durable).encode()))
    _write(_path(record["id"]), json.dumps(record, ensure_ascii=False).encode())
    _session_credentials[record["id"]] = credentials
    return record


def credentials_for(server_id: str) -> dict:
    if server_id in _session_credentials:
        return _session_credentials[server_id]
    from app.services.credential_key import master_key

    try:
        credentials = json.loads(
            Fernet(master_key()).decrypt(_path(server_id, ".enc").read_bytes())
        )
        _session_credentials[server_id] = credentials
        return credentials
    except (OSError, InvalidToken, ValueError) as exc:
        raise ThereIsNoSuchHomeError(
            "无法读取服务器凭据，请重新连接。",
            en="Cannot read server credentials. Reconnect the server.",
            status=401,
        ) from exc


def cached_projects(server_id: str) -> list[dict]:
    return _json(_path(server_id, ".projects.json"), [])


def home(record: dict, *, reachable: bool) -> dict:
    return {
        "kind": "remote",
        "connection_id": record["id"],
        "name": record["name"],
        "url": record["host"],
        "reachable": reachable,
        "needs_sign_in": False,
    }


async def ask(server_id: str, method: str, path: str, **kwargs):
    record = get(server_id)
    if record is None:
        raise ThereIsNoSuchHomeError("找不到这台服务器。", en="Server not found.")
    return await ask_connected(record, credentials_for(server_id), method, path, **kwargs)


async def ask_connected(record: dict, credentials: dict, method: str, path: str, **kwargs):
    import httpx

    from app.services.remote_transport import tunnels

    base = await tunnels.endpoint(record, credentials)
    token = credentials["api_token"]
    headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.request(method, base + path, headers=headers, **kwargs)
    except httpx.HTTPError as exc:
        raise ThereIsNoSuchHomeError(
            "无法连接服务器；远程任务状态待确认。",
            en="Cannot reach the server; remote task status is unconfirmed.",
            status=503,
        ) from exc
    if response.is_error:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        message = detail if isinstance(detail, str) else "Remote server rejected the request."
        raise ThereIsNoSuchHomeError(message, en=message, status=response.status_code)
    return response.json()


async def list_projects() -> list[dict]:
    from app.services import project_homes

    async def one(record):
        try:
            async with asyncio.timeout(6):
                projects = await ask(record["id"], "GET", "/api/v1/projects/")
            _write(_path(record["id"], ".projects.json"), json.dumps(projects).encode())
            project_homes.these_live_at(record["id"], [item["id"] for item in projects])
            reachable = True
        except (ThereIsNoSuchHomeError, TimeoutError):
            projects = cached_projects(record["id"])
            reachable = False
        return [{**item, "home": home(record, reachable=reachable)} for item in projects]

    results = await asyncio.gather(*(one(record) for record in all_servers()))
    return [project for group in results for project in group]


async def create_project(server_id: str, payload: dict, lang: str) -> dict:
    from app.services import project_homes

    created = await ask(
        server_id,
        "POST",
        "/api/v1/projects/",
        json={**payload, "home": ""},
        headers={"X-Interface-Language": lang},
    )
    project_homes.this_one_lives_at(created["id"], server_id)
    projects = [item for item in cached_projects(server_id) if item["id"] != created["id"]]
    _write(_path(server_id, ".projects.json"), json.dumps([*projects, created]).encode())
    return {**created, "home": home(get(server_id), reachable=True)}


def register_provider() -> None:
    from app.services import other_homes

    if any(provider.kind == "remote" for provider in other_homes.the_providers()):
        return
    other_homes.offer(
        other_homes.HomeProvider(
            kind="remote",
            knows=lambda identifier: identifier.startswith(PREFIX),
            list_projects=list_projects,
            create_project=create_project,
        )
    )


async def forget(server_id: str) -> None:
    from app.services.remote_transport import tunnels

    if cached_projects(server_id):
        raise ThereIsNoSuchHomeError(
            "这台服务器仍有项目，请保留连接以便重新访问。断开连接不会停止任务。",
            en="This server still has projects. Keep its connection to access them again. "
            "Disconnecting does not stop tasks.",
            status=409,
        )
    await tunnels.disconnect(server_id)
    for suffix in (".json", ".enc", ".projects.json"):
        _path(server_id, suffix).unlink(missing_ok=True)
    _session_credentials.pop(server_id, None)
