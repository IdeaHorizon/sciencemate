"""问发布页要东西 —— 桌面自更新和组织服务器自升级共用这一处。

两者问的是**同一个发布页**、用**同一把签名钥匙**验（`self_update.parse_manifest`）、
私有仓库时带**同一个凭据**（`UPDATE_SOURCE_TOKEN`）。从前这几个函数只住在桌面的
`/update` 端点里；组织服务器也要自己升级之后，抄一份就是两份各自演化的答案。
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
ATTEMPTS = 2


class ReleaseNetworkError(RuntimeError):
    """Bounded diagnostics without credentials, paths or signed query strings."""

    def __init__(self, *, stage, url, mode, elapsed, attempts, cause, delivered=0):
        self.diagnostics = {
            "stage": stage,
            "host": urlsplit(url).hostname,
            "connection": mode,
            "elapsed_seconds": round(elapsed, 3),
            "attempts": attempts,
            "cause": type(cause).__name__,
            "bytes_delivered": delivered,
        }
        super().__init__(json.dumps(self.diagnostics, sort_keys=True))


def _connection_path() -> Path:
    from app.config import the_data_root

    return the_data_root() / "update-connection.json"


def connection_mode() -> str:
    from app.config import settings

    if settings.update_connection_mode is not None:
        return settings.update_connection_mode
    try:
        value = json.loads(_connection_path().read_text(encoding="utf-8"))["mode"]
    except FileNotFoundError:
        return "system"
    if value not in {"system", "direct"}:
        raise ValueError("Unknown update connection mode")
    return value


def write_connection_mode(mode: str) -> None:
    from shared.lib.private_files import mkdir_private, write_private_bytes

    from app.config import settings

    if mode not in {"system", "direct"}:
        raise ValueError("Unknown update connection mode")
    if settings.update_connection_mode is not None:
        raise ValueError("The deployment owns its update connection setting")
    path = _connection_path()
    mkdir_private(path.parent)
    temporary = path.with_name(path.name + "." + uuid4().hex)
    try:
        write_private_bytes(temporary, json.dumps({"mode": mode}).encode())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _client(*, headers):
    # Direct mode bypasses proxy discovery, while preserving the configured
    # certificate authorities. trust_env=False alone would also drop those.
    return httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers=headers,
        trust_env=connection_mode() == "system",
        verify=httpx.create_ssl_context(trust_env=True),
    )


def _get(url: str, *, stage: str, headers):
    started = time.monotonic()
    mode = connection_mode()
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with _client(headers=headers) as client:
                response = client.get(url)
                response.raise_for_status()
                return response
        except httpx.HTTPError as exc:
            if attempt < ATTEMPTS and isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
                time.sleep(0.1)
                continue
            raise ReleaseNetworkError(
                stage=stage,
                url=url,
                mode=mode,
                elapsed=time.monotonic() - started,
                attempts=attempt,
                cause=exc,
            ) from exc


def auth_headers() -> dict[str, str]:
    """私有 Forgejo：`user:token` 走 Basic，裸 token 走 `token …`。空 = 匿名。"""
    from app.config import settings

    token = (settings.update_source_token or "").strip()
    if not token:
        return {}
    if ":" in token:
        return {"Authorization": "Basic " + base64.b64encode(token.encode()).decode()}
    return {"Authorization": f"token {token}"}


def get_json(url: str):
    return _get(url, stage="release_list", headers=auth_headers()).json()


def get_bytes(url: str) -> bytes:
    stage = "signature" if urlsplit(url).path.endswith(".sig") else "manifest"
    return _get(url, stage=stage, headers=auth_headers()).content


def stream(url: str, sink) -> None:
    started, delivered, mode = time.monotonic(), 0, connection_mode()
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with _client(headers=auth_headers()) as client:
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes(1024 * 256):
                        sink(chunk)
                        delivered += len(chunk)
            return
        except httpx.HTTPError as exc:
            # The sink owns its digest and staging file. Replaying bytes into
            # a partially filled sink would corrupt both; abort that transfer.
            if (
                not delivered
                and attempt < ATTEMPTS
                and isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))
            ):
                time.sleep(0.1)
                continue
            raise ReleaseNetworkError(
                stage="archive",
                url=url,
                mode=mode,
                elapsed=time.monotonic() - started,
                attempts=attempt,
                cause=exc,
                delivered=delivered,
            ) from exc
