"""把抓到的原料落库 —— 三道机械闸：有出处、有身份、没见过。

## 为什么闸在这里而不是在采集器里

采集器有四个（往后还会多），闸写在采集器里就是四份各自演化的判据，加第五个
源默认漏过。落库只有一条路，闸放在这条路上就扫得到盘。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feed import (
    FeedItem,
    FeedItemKind,
    FeedSource,
    FeedSourceKind,
    FeedVisibility,
)

from app.services.feed import collectors
from app.services.feed.collectors import CollectedItem, spine_domains

logger = logging.getLogger(__name__)

#: 一轮里最多给几条补图。上限存在是为了让「某天多出很多新内容」
#: 不会变成同样多次的外部请求。
MAX_IMAGE_ENRICHMENTS_PER_ROUND = 12

#: 补图请求之间的间隔 —— 连着打同一个出版商不礼貌。
IMAGE_ENRICH_DELAY_SECONDS = 1.0


class IngestReport:
    """一次落库的结果。`skipped_no_url` / `skipped_no_key` 单独计数 ——
    合成一个"丢了 N 条"就分不出是源换了格式还是本来就重复。"""

    __slots__ = ("stored", "duplicates", "skipped_no_url", "skipped_no_key")

    def __init__(self) -> None:
        self.stored = 0
        self.duplicates = 0
        self.skipped_no_url = 0
        self.skipped_no_key = 0

    @property
    def total_seen(self) -> int:
        return self.stored + self.duplicates + self.skipped_no_url + self.skipped_no_key

    def as_dict(self) -> dict[str, int]:
        return {
            "stored": self.stored,
            "duplicates": self.duplicates,
            "skipped_no_url": self.skipped_no_url,
            "skipped_no_key": self.skipped_no_key,
        }


async def enrich_missing_images(
    client, *, source: FeedSource, items: list[CollectedItem], unknown_keys: set[str]
) -> int:
    """给**新**条目补落地页配图 —— 只在源上声明了 `enrich_image` 时。

    见 `collectors.fetch_og_image`：Nature 系的 og:image 是真实配图，而 arXiv
    的是它自己的 logo，所以这件事只能按源开，不能一概而论。

    只补新条目：已经存过的条目图也存过了，重抓一遍纯粹是对出版商的无谓请求。
    """
    if not (source.config or {}).get("enrich_image"):
        return 0
    enriched = 0
    for item in items:
        if item.image_url or item.canonical_key not in unknown_keys or not item.url:
            continue
        if enriched >= MAX_IMAGE_ENRICHMENTS_PER_ROUND:
            # 有上限：一个源某天突然多出 200 条新内容时，不该变成 200 次外部请求。
            break
        image = await collectors.fetch_og_image(client, item.url)
        if image:
            item.image_url = image
        enriched += 1
        await asyncio.sleep(IMAGE_ENRICH_DELAY_SECONDS)
    return enriched


async def ingest_items(
    db: AsyncSession,
    *,
    source: FeedSource,
    items: list[CollectedItem],
    client=None,
    archive_rss: bool = True,
) -> IngestReport:
    """落一批原料。返回这一批的构成，由调度器决定怎么记账。"""
    report = IngestReport()
    if not items:
        return report

    # 先把这一批里自带的重复去掉：同一个 feed 里同一篇出现两次是常事
    # （更正、重发），没必要为它们各撞一次唯一键。
    deduped: dict[str, CollectedItem] = {}
    for item in items:
        if not item.canonical_key:
            report.skipped_no_key += 1
            continue
        if not (item.url or "").strip():
            # provenance-first：没有出处的外部条目一律不落库。见
            # `app/models/feed.py` 的模块 docstring。
            report.skipped_no_url += 1
            continue
        deduped.setdefault(item.canonical_key, item)

    if not deduped:
        return report

    existing_items = list(
        (
            await db.execute(
                select(FeedItem).where(
                    FeedItem.canonical_key.in_(list(deduped))
                )
            )
        ).scalars().all()
    )
    existing_by_key = {item.canonical_key: item for item in existing_items}
    known = set(existing_by_key)

    # 补图放在**知道哪些是新条目之后**：已经存过的条目图也存过了，重抓一遍
    # 纯粹是对出版商的无谓请求。没有 client（测试、或调用方没给）就跳过 ——
    # 补图是锦上添花，缺了不该让落库失败。
    if client is not None:
        unknown = {key for key in deduped if key not in known}
        if unknown:
            enriched = await enrich_missing_images(
                client, source=source, items=list(deduped.values()), unknown_keys=unknown
            )
            if enriched:
                logger.info("Feed source %r: enriched %d image(s)", source.name, enriched)

    if archive_rss and source.kind == FeedSourceKind.RSS:
        from app.services.feed.rss_archive import archive_rss_item

        # RSS collection is a background job, so every item gets the same
        # fallback attempt. Per-request timeouts and structured failure records
        # prevent a broken publisher from hanging the whole process; there is
        # deliberately no article-count cap here. Failed attempts are retried
        # on the next scheduled poll.
        archive_semaphore = asyncio.Semaphore(8)

        async def archive_one(item: CollectedItem) -> None:
            async with archive_semaphore:
                try:
                    await archive_rss_item(
                        item, source.name, client=client, allow_fallback=True,
                        domains=list(source.domains or []),
                    )
                except Exception:
                    logger.exception("RSS file archive failed for %r", item.title)

        # All articles are attempted; the semaphore only bounds simultaneous
        # network work so one publisher cannot create hundreds of open sockets.
        await asyncio.gather(
            *(archive_one(item) for item in deduped.values() if item.kind == FeedItemKind.PAPER)
        )

    source_domains = list(source.domains or [])
    for key, item in deduped.items():
        if key in known:
            # 旧版本把编辑型科研资讯当成 paper。再次抓到时原地纠正类型与
            # 路线，不制造重复记录，也不要求一次数据库迁移。
            existing = existing_by_key[key]
            incoming_routes = list((item.extra or {}).get("feed_acquisition_routes") or [])
            if item.kind == FeedItemKind.NEWS and "bignews" in incoming_routes:
                existing.kind = FeedItemKind.NEWS
                old_extra = dict(existing.extra or {})
                old_routes = list(old_extra.get("feed_acquisition_routes") or [])
                existing.extra = {
                    **old_extra, **dict(item.extra or {}),
                    "feed_acquisition_routes": sorted(set(old_routes + incoming_routes)),
                }
                if not existing.summary and item.summary:
                    existing.summary = item.summary
                if not existing.image_url and item.image_url:
                    existing.image_url = item.image_url
            report.duplicates += 1
            continue
        # 条目自带的分类优先于源的默认域：arXiv 的条目知道自己是哪个分类，
        # 而一份综合刊的 RSS 只能靠源上配的那组宽域。
        domains = spine_domains(item.domains) or spine_domains(source_domains)
        extra = dict(item.extra or {})
        if source.kind == FeedSourceKind.RSS and item.kind == FeedItemKind.PAPER:
            from nodes.literature.tools.journal_policy import decide

            decision = decide(
                venue=item.venue,
                source=f"rss:{source.name}",
                pub_type="journal-article",
                title=item.title,
            )
            extra.update(
                {
                    "literature_catalog": True,
                    "literature_eligible": decision.accepted,
                    "classification_mode": decision.reason,
                }
            )
            if decision.accepted:
                domains = list(decision.domains)
        record = FeedItem(
            canonical_key=key,
            kind=item.kind,
            title=item.title[:2000],
            url=item.url,
            summary=item.summary or None,
            authors=item.authors[:50],
            venue=(item.venue or None) and item.venue[:200],
            published_at=item.published_at,
            image_url=item.image_url or None,
            domains=domains,
            source_id=source.id,
            visibility=FeedVisibility.PLATFORM,
            extra=extra,
        )
        try:
            # 每条一个 savepoint：并发的另一轮采集可能刚好插了同一个键，
            # 那是重复不是失败。不隔离的话一条撞键会把整批回滚掉。
            #
            # `db.add` 必须在 savepoint **里面**：放外面的话，撞键后 savepoint
            # 回滚，而这个待插入对象仍留在 session 里，收尾 commit 会把它再插
            # 一遍并抛 PendingRollbackError —— 一整轮采集因为一条重复而全废。
            async with db.begin_nested():
                db.add(record)
                await db.flush()
        except IntegrityError:
            report.duplicates += 1
            continue
        report.stored += 1
    return report


def is_stale(
    source: FeedSource,
    *,
    now: datetime | None = None,
    interval_seconds: int | None = None,
) -> bool:
    """这个源该拉了吗 —— 按**它自己的**周期。

    `interval_seconds` 覆盖源上的周期，给空轮退避用（调度器把周期临时拉长）。
    退避是调速，不是事实，所以它只存在于调用点，不写进源那一行。

    `last_polled_at` 从库里取回来在 Postgres 上是 aware、在 SQLite 上是
    naive（驱动差异，不是我们的选择）。拿 naive 去和 aware 相减是 TypeError，
    也就是一个只在某些部署上炸的 500。所以在这里统一，别让每个调用方各记
    一次。
    """
    moment = now or datetime.now(UTC)
    if not source.is_active:
        return False
    last = source.last_polled_at
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    interval = source.poll_interval_seconds if interval_seconds is None else interval_seconds
    return (moment - last).total_seconds() >= interval
