"""In-process wakeups for durable execution-event SSE followers.

The database remains authoritative.  This notifier only avoids client polling
and lets a single App Server wake followers immediately after a commit.  A
heartbeat re-checks the database, so missed/external notifications do not lose
events; multi-process deployments can replace this seam with PG LISTEN/NOTIFY.
"""

import asyncio

_condition = asyncio.Condition()
_revision = 0
_TRANSIENT_QUEUE_LIMIT = 256
_transient_subscribers: dict[
    tuple[str, str, str], set[asyncio.Queue[dict]]
] = {}


def execution_event_revision() -> int:
    return _revision


async def notify_execution_observers() -> None:
    global _revision
    async with _condition:
        _revision += 1
        _condition.notify_all()


async def wait_for_execution_events(revision: int, *, timeout: float) -> int:
    async with _condition:
        if _revision != revision:
            return _revision
        await asyncio.wait_for(
            _condition.wait_for(lambda: _revision != revision),
            timeout=timeout,
        )
        return _revision


def subscribe_run_transients(
    *, tenant_id: str, session_id: str, run_id: str
) -> asyncio.Queue[dict]:
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=_TRANSIENT_QUEUE_LIMIT)
    key = (tenant_id, session_id, run_id)
    _transient_subscribers.setdefault(key, set()).add(queue)
    return queue


def unsubscribe_run_transients(
    queue: asyncio.Queue[dict], *, tenant_id: str, session_id: str, run_id: str
) -> None:
    key = (tenant_id, session_id, run_id)
    subscribers = _transient_subscribers.get(key)
    if not subscribers:
        return
    subscribers.discard(queue)
    if not subscribers:
        _transient_subscribers.pop(key, None)


async def publish_run_transient(
    *, tenant_id: str, session_id: str, run_id: str, event: dict
) -> None:
    """Fan out future-only UI data without blocking execution on a slow UI.

    Token chunks are deliberately non-durable.  If a subscriber falls behind,
    its pending live chunks are replaced by a gap marker plus the newest chunk.
    The canonical assistant SessionMessage remains the lossless recovery path.
    """
    key = (tenant_id, session_id, run_id)
    for queue in tuple(_transient_subscribers.get(key, ())):
        try:
            queue.put_nowait(dict(event))
        except asyncio.QueueFull:
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            queue.put_nowait(
                {
                    "type": "token_gap",
                    "recovery": "assistant_message",
                }
            )
            queue.put_nowait(dict(event))
