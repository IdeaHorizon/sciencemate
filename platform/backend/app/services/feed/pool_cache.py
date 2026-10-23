"""Bounded per-user recommendation snapshots, invalidated on committed changes."""
from __future__ import annotations
import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from datetime import UTC, datetime
from weakref import WeakValueDictionary

from sqlalchemy import event, func, inspect, select
from sqlalchemy.orm import Session
from app.models.feed import FeedEngagement, FeedEngagementAction, FeedItem
from app.models.project import Project
from app.models.user import User
from app import assembly
from app.config import settings

_revision = 0
_cache = OrderedDict()
_recent = OrderedDict()
_locks = WeakValueDictionary()
TTL_SECONDS = 30
MAX_ENTRIES = 128


def clear():
    global _revision
    _revision += 1
    _cache.clear()
    _recent.clear()


@event.listens_for(Session, "after_flush")
def _remember_changes(session, context):
    for obj in (*session.new, *session.dirty, *session.deleted):
        if isinstance(obj, FeedEngagement) and obj.action == FeedEngagementAction.IMPRESSION:
            continue  # Exposure is neutral in ranking; scrolling cannot clear pages.
        if isinstance(obj, (FeedEngagement, User, Project)):
            session.info["feed_pool_changed"] = "hard"
        elif isinstance(obj, FeedItem):
            sensitive = obj in session.deleted or (obj in session.dirty and any(inspect(obj).attrs[name].history.has_changes()
                for name in ("visibility", "organization_id")))
            if sensitive:
                session.info["feed_pool_changed"] = "hard"
            else:
                session.info.setdefault("feed_pool_changed", "feed")


@event.listens_for(Session, "after_commit")
def _invalidate_committed(session):
    global _revision
    change = session.info.pop("feed_pool_changed", None)
    if change == "hard":
        clear()
    elif change:
        _revision += 1
        _cache.clear()


@event.listens_for(Session, "after_rollback")
def _discard_changes(session):
    session.info.pop("feed_pool_changed", None)


async def get_pool(db, user, build, *, allow_stale=False, namespace="recommendations"):
    # Cheap database markers also notice inserts/project edits by another process.
    items = (await db.execute(select(func.count(FeedItem.id), func.max(FeedItem.created_at)))).one()
    projects = (await db.execute(select(func.count(Project.id), func.max(Project.updated_at)))).one()
    identity = json.dumps([namespace, id(db.bind), str(user.id), user.institution_id, user.group_id,
                           user.preferences, list(items), list(projects),
                           datetime.now(UTC).date().isoformat(), _revision,
                           assembly.background_collection_enabled(),
                           settings.feed_external_fetch_enabled, settings.feed_collector_enabled],
                          sort_keys=True, default=str)
    profile_identity = json.dumps([namespace, id(db.bind), str(user.id), user.institution_id, user.group_id,
                                   user.preferences, list(projects), datetime.now(UTC).date().isoformat(),
                                   assembly.background_collection_enabled(), settings.feed_external_fetch_enabled,
                                   settings.feed_collector_enabled], sort_keys=True, default=str)
    profile_key = hashlib.sha256(profile_identity.encode()).hexdigest()
    key = hashlib.sha256(identity.encode()).hexdigest()
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < TTL_SECONDS:
        _cache.move_to_end(key)
        return cached[1].model_copy(deep=True)
    previous = _recent.get(profile_key)
    if allow_stale and previous and previous[1].feed and time.monotonic() - previous[0] < 120:
        # The independent worker refreshes admission/ranking. Browser pagination
        # keeps using the existing prepared snapshot during that rebuild.
        return previous[1].model_copy(deep=True)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < TTL_SECONDS:
            return cached[1].model_copy(deep=True)
        value = await build()
        snapshot = (time.monotonic(), value.model_copy(deep=True))
        _cache[key] = snapshot
        _recent[profile_key] = snapshot
        _recent.move_to_end(profile_key)
        while len(_recent) > MAX_ENTRIES:
            _recent.popitem(last=False)
        while len(_cache) > MAX_ENTRIES:
            _cache.popitem(last=False)
        return value
