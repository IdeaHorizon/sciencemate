"""Resolve old name subscriptions, preserving ambiguous choices for confirmation."""
from __future__ import annotations
import asyncio
import logging
import time
from copy import deepcopy
from sqlalchemy import select
from app.database import get_session_factory
from app.models.user import User
from app.services.feed import author_identity

logger = logging.getLogger(__name__)
_tasks = {}
_attempts = {}


def choose(entry, candidates):
    # Only an exhaustive search with exactly one exact-name entity authorizes
    # automatic binding. Never infer identity from popularity or project topics.
    from app.services.feed.profile import author_matches
    exact = [row for row in candidates if author_matches(entry.get('name', ''), row.get('name', ''))]
    if len(exact) == 1 and all(row.get('search_complete', False) for row in candidates):
        return {**exact[0], 'identity_candidates': [], 'identity_message': '旧订阅已自动匹配唯一作者身份'}
    return {**entry, 'identity_status': 'ambiguous' if exact else 'unavailable',
            'identity_candidates': exact,
            'identity_message': ('发现多个同名作者，请选择机构和方向相符的具体学者' if len(exact) > 1 else '已找到候选身份，请核对机构和方向后绑定') if exact else '尚未找到可确认身份，请补充机构或重新搜索'}


async def migrate(user_id):
    factory = get_session_factory()
    async with factory() as db:
        user = await db.get(User, str(user_id))
        if user is None:
            return {'resolved': 0, 'ambiguous': 0}
        feed = (user.preferences or {}).get('feed') or {}
        entries = deepcopy((feed.get('subscriptions') or {}).get('scholars') or [])
    pending = [entry for entry in entries if not author_identity.identity(entry) and not (entry.get('identity_status') == 'ambiguous' and entry.get('identity_candidates'))]
    original_names = {entry['key']: entry.get('name') for entry in pending}
    semaphore = asyncio.Semaphore(3)
    async def resolve(entry):
        async with semaphore:
            try:
                rows = await author_identity.search(entry['name'], limit=50)
                return entry['key'], choose(entry, rows)
            except Exception:
                logger.warning('Legacy scholar identity lookup unavailable for %r', entry.get('name'))
                return entry['key'], {**entry, 'identity_status': 'unavailable', 'identity_message': '身份检索暂不可用，后台会重试', 'identity_candidates': []}
    updates = dict(await asyncio.gather(*(resolve(entry) for entry in pending)))
    resolved = []
    async with factory() as db:
        # Re-read and lock after network access so subscription edits/removals
        # during resolution cannot be overwritten or resurrected.
        user = (await db.execute(select(User).where(User.id == str(user_id)).with_for_update())).scalar_one_or_none()
        if user is None:
            return {'resolved': 0, 'ambiguous': 0}
        prefs = deepcopy(user.preferences or {})
        feed = prefs.setdefault('feed', {})
        subscriptions = feed.setdefault('subscriptions', {})
        new_entries = []
        for entry in subscriptions.get('scholars') or []:
            replacement = updates.get(entry['key'])
            if replacement and not author_identity.identity(entry) and entry.get('name') == original_names.get(entry['key']):
                entry = replacement
                if author_identity.identity(entry):
                    resolved.append(entry)
            if not any(other['key'] == entry['key'] for other in new_entries):
                new_entries.append(entry)
        subscriptions['scholars'] = new_entries
        if updates:
            user.preferences = prefs
            await db.commit()
    if resolved:
        from app.services.feed.discovery import request_subscription_discovery
        request_subscription_discovery(user_id=user_id, scholars=resolved)
    return {'resolved': len(resolved), 'ambiguous': sum(row.get('identity_status') == 'ambiguous' for row in updates.values())}


def request(user_id):
    key = str(user_id)
    running = _tasks.get(key)
    if running and not running.done():
        return
    if time.monotonic() - _attempts.get(key, float('-inf')) < 300:
        return
    _attempts[key] = time.monotonic()
    task = asyncio.create_task(migrate(key), name='scholar-identity-migration-'+key)
    _tasks[key] = task
    def finish(done):
        if _tasks.get(key) is done:
            _tasks.pop(key, None)
        if not done.cancelled() and done.exception():
            logger.error('Scholar migration failed for user=%s', key, exc_info=done.exception())
    task.add_done_callback(finish)


async def stop():
    pending = list(_tasks.values())
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _tasks.clear()
    _attempts.clear()


def start():
    async def scan():
        async with get_session_factory()() as db:
            users = (await db.execute(select(User).where(User.is_active.is_(True)))).scalars().all()
            for user in users:
                scholars = (((user.preferences or {}).get('feed') or {}).get('subscriptions') or {}).get('scholars') or []
                if any(not author_identity.identity(entry) and entry.get('identity_status') != 'ambiguous' for entry in scholars):
                    request(user.id)
    task = asyncio.create_task(scan(), name='scholar-identity-migration-startup')
    _tasks['startup'] = task
