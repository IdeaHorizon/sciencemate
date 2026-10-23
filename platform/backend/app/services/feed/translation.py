"""Translate the whole current recommendation pool independently of browser pagination."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time

from sqlalchemy import select

from app.config import settings
from app.database import get_session_factory
from app.models.user import User
from app.services.feed import bridge
from app.services.model_backends import resolved_api_key, select_effective_backend

logger = logging.getLogger(__name__)


def needs_translation(text: str | None) -> bool:
    return bool(text and re.search(r"[A-Za-z]", text) and not re.search(r"[\u4e00-\u9fff]", text))


def translation_ready(item) -> bool:
    return (not needs_translation(item.title) or bool((item.title_zh or "").strip())) and (
        not needs_translation(item.summary) or bool((item.abstract_zh or "").strip())
    )


def source_key(item) -> str:
    return hashlib.sha256(f"{item.id}\0{item.title}\0{item.summary or ''}".encode()).hexdigest()


class FeedTranslationWorker:
    def __init__(self):
        self.retry_after: dict[tuple[str, str], float] = {}
        self.semaphore = asyncio.Semaphore(max(1, min(settings.feed_translation_concurrency, 8)))

    async def run_once(self) -> int:
        # Import lazily: the HTTP view and worker share exactly the same admission
        # rules. No second, broader candidate pool is translated at the user's cost.
        from app.api.v1.feed import feed_today, preparing_translation_pool
        from app.api.v1.literature import translate_literature_page
        from app.schemas.literature import LiteratureTranslateIn

        factory = get_session_factory()
        async with factory() as db:
            user_ids = list((await db.execute(select(User.id).where(User.is_active.is_(True)))).scalars())
        attempted = 0
        now = time.monotonic()
        self.retry_after = {key: until for key, until in self.retry_after.items() if until > now}
        for user_id in user_ids:
            async with factory() as db:
                user = await db.get(User, user_id)
                backend = await select_effective_backend(db, user, role=bridge.REASONING_ROLE)
                if backend is None or not resolved_api_key(backend):
                    continue
                pool = await feed_today(limit=1000000, offset=0, ready_only=False, user=user, db=db)
                batch = [card.item for card in pool.feed if not translation_ready(card.item)
                         and self.retry_after.get((str(user_id), source_key(card.item)), 0) <= now][:3 * max(1, min(settings.feed_translation_concurrency, 8))]
                # Reserve one batch for articles still fetching images/metadata.
                # Their title and any already available abstract can be cached now.
                preparing = await preparing_translation_pool(user, db)
                early = [card.item for card in preparing.feed if not translation_ready(card.item)
                         and self.retry_after.get((str(user_id), source_key(card.item)), 0) <= now][:3]
                capacity = 3 * max(1, min(settings.feed_translation_concurrency, 8))
                if early and (capacity > 3 or not batch):
                    batch = batch[:max(0, capacity - len(early))] + early
                await db.commit()
            if not batch:
                continue
            for item in batch:
                self.retry_after[(str(user_id), source_key(item))] = time.monotonic() + 300
            async def translate_batch(part):
                payload = LiteratureTranslateIn(papers=[{
                    "key": str(item.id), "title": item.title[:1000],
                    "abstract": (item.summary or "")[:50000] or None,
                    "doi": item.extra.get("doi"), "url": item.url,
                    "authors": item.authors[:10], "venue": (item.venue or "")[:1000] or None,
                } for item in part], generate_summaries=False, combine_translation=True)
                async with self.semaphore:
                    try:
                        started = time.monotonic()
                        async with factory() as db:
                            current_user = await db.get(User, user_id)
                            result = await translate_literature_page(payload, user=current_user, db=db)
                        logger.info("Feed background translation: papers=%s elapsed=%.2fs status=%s diagnostics=%s",
                                    len(part), time.monotonic() - started, result.status, result.diagnostics)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Feed background translation batch failed")
            await asyncio.gather(*(translate_batch(batch[i:i + 3]) for i in range(0, len(batch), 3)))
            attempted += len(batch)
        return attempted
