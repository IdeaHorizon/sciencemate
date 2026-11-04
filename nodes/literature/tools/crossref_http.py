"""Serialize Crossref list requests across searches and collector processes.

The public list endpoint permits one concurrent request and one request/second.
Keep the lock until the response arrives; spacing starts alone is insufficient.
Other providers do not acquire this lock.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from email.utils import parsedate_to_datetime

import httpx

from core.paths import literature_cache_dir
from shared.lib.filelock import try_hold


def retry_seconds(value: str | None, default: float) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return default
    return max(0.0, seconds) if math.isfinite(seconds) else default


async def crossref_list_get(client, url: str, **kwargs):
    directory = literature_cache_dir() / "rate_limits"
    state_file = directory / "crossref-list.json"
    deadline = time.monotonic() + 30
    handle = None
    while handle is None:
        handle = try_hold(directory / "crossref-list.lock")
        if handle is None:
            if time.monotonic() >= deadline:
                raise httpx.PoolTimeout("Crossref 请求排队超过 30 秒")
            await asyncio.sleep(.05)
    try:
        try:
            state = json.loads(state_file.read_text())
            next_start = float(state.get("next_start", 0))
            cooldown = float(state.get("cooldown", 0))
            if not all(math.isfinite(v) for v in (next_start, cooldown)):
                next_start = cooldown = 0
        except (OSError, ValueError, TypeError, AttributeError):
            next_start = cooldown = 0

        def save():
            state_file.write_text(json.dumps({"next_start": next_start, "cooldown": cooldown}))

        # Do not turn a lengthy upstream cooldown into minutes of stalled UI,
        # or send each remaining query group into the same rate limit.
        if cooldown - time.time() > 5:
            remaining = math.ceil(cooldown - time.time())
            response = httpx.Response(429, headers={"Retry-After": str(remaining)},
                request=httpx.Request("GET", url), text="Crossref 限流冷却中，本次未发送请求")
            raise httpx.HTTPStatusError("Crossref 限流冷却中", request=response.request, response=response)

        for attempt in range(2):
            await asyncio.sleep(max(0, next_start - time.time(), cooldown - time.time()))
            next_start = time.time() + 1.05
            save()
            response = await client.get(url, **kwargs)
            if response.status_code != 429:
                response.raise_for_status()
                return response
            delay = retry_seconds(response.headers.get("Retry-After"), 2 if attempt == 0 else 30)
            cooldown = time.time() + max(1.05, delay)
            save()
            if attempt == 1 or delay > 5:
                response.raise_for_status()
    finally:
        handle.close()
