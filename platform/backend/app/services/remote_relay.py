"""Stream personal-project requests over SSH; transport lifetime is not task lifetime."""

from __future__ import annotations

import asyncio
import hmac
import json
from urllib.parse import parse_qs

from app.config import settings
from app.services import project_homes, remote_servers
from app.services.other_homes import ThereIsNoSuchHomeError
from app.services.remote_transport import tunnels

_HOP_HEADERS = {
    "host",
    "authorization",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "upgrade",
    "te",
    "trailer",
    "proxy-authorization",
    "x-remote-server",
    "x-project",
    "x-organisation",
}
_RESPONSE_HOP_HEADERS = {"transfer-encoding", "connection", "keep-alive", "upgrade", "trailer"}
RESPONSE_HEADERS_TIMEOUT_SECONDS = 10.0
RESPONSE_BODY_TIMEOUT_SECONDS = 15.0


def headers_of(scope) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


async def reply(send, status, detail):
    raw = json.dumps({"detail": detail}, ensure_ascii=False).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": raw})


class RuntimeAuthentication:
    """An installed headless runtime requires its device token, even over loopback."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        token = settings.remote_runtime_token
        if token and scope["type"] == "http":
            supplied = headers_of(scope).get("authorization", "")
            if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
                await reply(send, 401, "A valid runtime device token is required.")
                return
        await self.app(scope, receive, send)


def destination(scope) -> str:
    path = scope.get("path", "")
    if not path.startswith("/api/v1/"):
        return ""
    rest = path[len("/api/v1") :]
    # Local settings and connection management must stay local, even with stale context.
    if rest.startswith(("/remote-servers", "/update", "/connections")):
        return ""
    headers = headers_of(scope)
    # Explicit server context is used before a project exists (folder browser / models).
    if explicit := headers.get("x-remote-server"):
        return explicit
    if rest.rstrip("/") in {"/projects", "/projects/folders"}:
        return ""
    project = ""
    for prefix in ("/api/v1/projects/", "/api/v1/chat/projects/"):
        if path.startswith(prefix):
            project = path[len(prefix) :].split("/", 1)[0]
            break
    query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    project = project or (query.get("project_id") or query.get("projectId") or [""])[0]
    project = project or headers.get("x-project", "")
    home = project_homes.where_this_project_lives(project) if project else ""
    return home if home.startswith(remote_servers.PREFIX) else ""


class PersonalProjectsRelay:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not (server_id := destination(scope)):
            await self.app(scope, receive, send)
            return
        import httpx

        started = False
        try:
            record = remote_servers.get(server_id)
            if record is None:
                raise ThereIsNoSuchHomeError(
                    "项目所在的服务器连接已丢失，请恢复连接。",
                    en="The project's server connection is missing. Restore it to continue.",
                    status=409,
                )
            base = await tunnels.endpoint(record)
            token = remote_servers.credentials_for(server_id)["api_token"]
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if not message.get("more_body"):
                    break
            raw_path = scope.get("raw_path", scope["path"].encode()).split(b"?", 1)[0]
            url = base + raw_path.decode("latin-1")
            if query := scope.get("query_string"):
                url += "?" + query.decode("latin-1")
            headers = [
                (key.decode("latin-1"), value.decode("latin-1"))
                for key, value in scope.get("headers", [])
                if key.decode("latin-1").lower() not in _HOP_HEADERS
            ]
            headers.append(("Authorization", f"Bearer {token}"))
            # Bound ordinary responses. SSE bodies can remain open while a worker computes.
            timeout = httpx.Timeout(None, connect=10)
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                request = client.build_request(
                    scope["method"], url, headers=headers, content=bytes(body)
                )
                async with asyncio.timeout(RESPONSE_HEADERS_TIMEOUT_SECONDS):
                    response = await client.send(request, stream=True)
                try:
                    event_stream = (
                        response.headers.get("content-type", "")
                        .lower()
                        .startswith("text/event-stream")
                    )
                    await send(
                        {
                            "type": "http.response.start",
                            "status": response.status_code,
                            "headers": [
                                (key.encode("latin-1"), value.encode("latin-1"))
                                for key, value in response.headers.multi_items()
                                if key.lower() not in _RESPONSE_HOP_HEADERS
                                and not key.lower().startswith("access-control-")
                            ],
                        }
                    )
                    started = True

                    async def forward():
                        chunks = response.aiter_raw().__aiter__()
                        while True:
                            try:
                                async with asyncio.timeout(
                                    None if event_stream else RESPONSE_BODY_TIMEOUT_SECONDS
                                ):
                                    chunk = await anext(chunks)
                            except StopAsyncIteration:
                                break
                            await send(
                                {"type": "http.response.body", "body": chunk, "more_body": True}
                            )
                        await send({"type": "http.response.body", "body": b"", "more_body": False})

                    async def disconnected():
                        while (await receive())["type"] != "http.disconnect":
                            pass

                    readers = {asyncio.create_task(forward()), asyncio.create_task(disconnected())}
                    try:
                        finished, _ = await asyncio.wait(
                            readers, return_when=asyncio.FIRST_COMPLETED
                        )
                        for task in finished:
                            task.result()
                    finally:
                        for task in readers:
                            task.cancel()
                        await asyncio.gather(*readers, return_exceptions=True)
                finally:
                    await response.aclose()
        except ThereIsNoSuchHomeError as exc:
            if not started:
                lang = "zh" if headers_of(scope).get("x-interface-language") == "zh" else "en"
                await reply(send, exc.status, exc.said_in(lang))
        except (httpx.HTTPError, TimeoutError):
            if not started:
                await reply(
                    send,
                    503,
                    "无法连接服务器；远程任务状态待确认。 / "
                    "Connection lost; remote task status is unconfirmed.",
                )
            else:
                # End the stream without inventing a task-failed event. Poll/replay can recover.
                await send({"type": "http.response.body", "body": b"", "more_body": False})
