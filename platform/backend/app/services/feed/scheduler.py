"""采集调度 —— 每个源按**它自己的**节奏，不是一个全局 cron。

## 三个节奏

设计上"什么时候生产内容"有三个互相独立的节奏，这个模块实现前两个：

  1. **源的节奏**：arXiv 工作日一天一更、期刊按周、deadline 表几乎不动。
     一个全局 cron 只能取最快的那个，等于把别的源每次都白拉一遍。所以周期
     是 `feed_sources.poll_interval_seconds`，一源一值。
  2. **平台空闲的节奏**：空闲时再去抓一遍 arXiv 没有边际收益 —— 原料的量由
     源决定，不由我们勤快程度决定。空闲时值钱的活是**深加工**（把已采的原料
     做成域周报），见 `digest.py`。
  3. 用户事件的节奏（新开一个 Project → 定向补这个方向的近况）—— M2。

## 熔断的判据是"有没有边际新增"，不是"跑没跑"

一个源连着几轮抓回来的全是重复，说明它这段时间就是没有新东西，**这不是
故障**，但继续按原周期拉它是白烧。所以空轮会让它退避（周期翻倍，有上限），
一旦重新有新内容立刻恢复原周期。

真正的失败（网络错、格式变、被封）另算：连续 `max_consecutive_failures` 次
就把源停用，并把最后一次的错误留在 `last_error` 里 —— 一个静默不工作的源
和一个正常但没新内容的源，从外面看长得一模一样，必须能分开。

## 单写者

只有这一个循环写 `feed_sources` 的健康字段和 `feed_items`。多个写者要处理
"两轮同时拉同一个源"，而它带来的唯一好处是吞吐 —— 十几个源不需要吞吐。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session_factory
from app.models.feed import FeedSource
from app.models.user import User
from app.services.feed import collectors, discovery, ingest
from app.services.feed.collectors import CollectorError
from app.services.harness_contract import HarnessContractUnavailable

logger = logging.getLogger(__name__)

#: 一轮用户挖掘的硬上限。正常一轮（每用户最多 5 组学术检索 + 5 组网页搜索 +
#: 归档）约 3-5 分钟；这个值只负责“任何一处挂死都不无限阻塞全局采集”，
#: 不是正常路径的目标耗时。
DISCOVERY_ROUND_TIMEOUT_SECONDS = 420.0


#: 多久看一次"谁到点了"。它**不是**采集周期 —— 采集周期在每个源自己身上。
TICK_SECONDS = 300

#: 同时最多拉几个源。压到很低是刻意的：这些是别人的免费接口，
#: 我们没有理由并发地打它们。
MAX_CONCURRENT_FETCHES = 3

#: 同一轮里两次请求之间的间隔。arXiv 的使用条款明确要求请求之间留出间隔。
POLITENESS_DELAY_SECONDS = 3.0

#: 空轮退避的上限倍数。超过它就不再翻倍 —— 再稀疏的源也该每天看一眼。
MAX_IDLE_BACKOFF_MULTIPLIER = 4


class FeedCollector:
    """后台采集循环。生命周期挂在 app lifespan 上。"""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        #: 源 id → 连续空轮次数。只在内存里：它是一个调速用的观察，不是事实，
        #: 重启后从零开始最多多拉一轮。
        self._idle_rounds: dict[str, int] = {}
        #: 当前这一轮的 HTTP client（补图那一步要用，见 run_once）。
        self._client: httpx.AsyncClient | None = None

    def start(self) -> None:
        if not settings.feed_collector_enabled:
            logger.info("Feed collector disabled by configuration")
            return
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="feed-collector")
        logger.info("Feed collector started (tick=%ss)", TICK_SECONDS)

    async def stop(self) -> None:
        """停掉循环并等它真的退出。

        只 `cancel()` 不 `await` 的话，进程会在一次正在进行的 HTTP 抓取中间
        退出，而那条 DB 事务的收尾要么没跑要么跑了一半 —— 关机路径上的错误
        最难查，因为它们只在关机时出现。
        """
        self._stop.set()
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 一轮炸了不该带走循环 —— 下一轮还有机会。但必须留下堆栈：
                # 一个静默重试的循环等于没有循环。
                logger.exception("Feed collection round failed")
            try:
                await self.run_discovery_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 挖掘失败**绝不能**影响全局采集：它花的是某个用户的预算，
                # 而采集是全平台共享的内容供给。所以它有自己的 try。
                logger.exception("Feed discovery round failed")
            try:
                await self.run_literature_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Local literature feed round failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)
            except TimeoutError:
                continue

    async def run_literature_once(self, limit: int = 50) -> dict[str, int]:
        """把本地已准备好的 literature figure 发布为资讯卡片。

        这里只读共享 catalog 和写入 FeedItem；不访问远程源、不下载 PDF/图片。
        RSS 仍由 ``run_once`` 独立负责，两类内容在 FeedItem 层汇合。
        """
        from app.services.feed.literature_projection import project_literature_catalog

        factory = get_session_factory()
        async with factory() as db:
            report = await project_literature_catalog(db, limit=limit)
            await db.commit()
        return report.as_dict()

    async def run_once(self) -> dict[str, int]:
        """跑一轮：谁到点拉谁。返回这一轮的账。"""
        from app.services.feed import domains as domain_service
        from app.services.feed.profile import stored_domains

        factory = get_session_factory()
        async with factory() as db:
            sources = list((await db.execute(select(FeedSource))).scalars().all())
            users = list(
                (await db.execute(select(User).where(User.is_active.is_(True))))
                .scalars()
                .all()
            )
            subscribed = {
                code for user in users for code in stored_domains(user) if code
            }

            def wanted(source: FeedSource) -> bool:
                venue = str((source.config or {}).get("venue") or "").strip()
                # 综合科研媒体没有固定学科；它们走有界的 bignews 探索位，
                # 不能因 domains 为空而永远不被调度。只开放明确白名单。
                if venue in collectors.TRUSTED_SCIENCE_NEWS_VENUES:
                    return bool(users)
                item_domains = {str(code) for code in (source.domains or []) if code}
                if not subscribed or not item_domains:
                    return False
                if domain_service.FEED_ARXIV_DOMAIN in subscribed and any(
                    not code.isdigit() for code in item_domains
                ):
                    return True
                return any(
                    selected in domain_service.ancestors(item)
                    for item in item_domains
                    for selected in subscribed
                )

            due = [s for s in sources if wanted(s) and self._is_due(s)]
            if not due:
                return {"polled": 0, "stored": 0}

            if not settings.feed_external_fetch_enabled:
                logger.debug("External fetching disabled; %d source(s) left alone", len(due))
                return {"polled": 0, "stored": 0}

            semaphore = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
            stored_total = 0
            async with collectors.make_client() as client:
                # 补图要在落库那一步发请求（它才知道哪些是新条目），而那一步
                # 拿不到这个 client。挂在实例上传过去 —— 比再开一个连接池好。
                self._client = client
                # 顺序落库、并发抓取：抓取是 IO 等待，可以并行；落库共用一个
                # session，并行写它就是在一个非线程安全的对象上开赛跑。
                fetched = await asyncio.gather(
                    *(self._fetch(client, semaphore, source) for source in due),
                    return_exceptions=False,
                )
                for source, items, error in fetched:
                    stored_total += await self._record(db, source, items, error)
            await db.commit()
            return {"polled": len(due), "stored": stored_total}

    async def run_discovery_once(self) -> dict[str, int]:
        """按用户跑一轮自动挖掘 —— 与全局采集**分开一轮**。

        分开不是洁癖：全局采集问的是"哪个源到点了"（与用户无关、结果全平台
        共享、不花任何人的模型预算）；挖掘问的是"哪个用户该被挖了"（每人答案
        不同、花的是他自己的钱）。混在一起，"这次失败该算谁的"就说不清，而且
        一个用户的模型超时会拖住所有源的采集。
        """
        if not settings.feed_collector_enabled:
            return {"users": 0, "found": 0, "digests": 0}
        factory = get_session_factory()
        async with factory() as db:
            try:
                totals = await asyncio.wait_for(
                    discovery.run_round(db), timeout=DISCOVERY_ROUND_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                # 兜底：某个外网源（网页搜索/学术源）挂死在慢连接上时，不能让
                # 一轮挖掘无限阻塞下去 —— 那样会拖死后面的全局采集和文献投影。
                await db.rollback()
                logger.warning(
                    "Feed discovery round timed out after %ds; abandoning this round",
                    DISCOVERY_ROUND_TIMEOUT_SECONDS,
                )
                return {"users": 0, "found": 0, "digests": 0, "timed_out": True}
            await db.commit()
        if totals["users"]:
            logger.info("Feed discovery round: %s", totals)
        return totals

    def _is_due(self, source: FeedSource) -> bool:
        # 空轮退避：连着空 N 轮就把**这一次判断**用的周期拉长，但不超过上限。
        # 退避是调速不是事实，所以只作用在这个判断上，不写回源那一行 ——
        # 写回去的话，下次真有新内容时那个被改过的周期还留在库里。
        idle = self._idle_rounds.get(source.id, 0)
        multiplier = min(1 + max(idle, 0), MAX_IDLE_BACKOFF_MULTIPLIER)
        return ingest.is_stale(
            source, interval_seconds=source.poll_interval_seconds * multiplier
        )

    async def _fetch(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        source: FeedSource,
    ) -> tuple[FeedSource, list[collectors.CollectedItem], str]:
        async with semaphore:
            try:
                items = await collectors.collect(
                    client, kind=source.kind, config=dict(source.config or {})
                )
                await asyncio.sleep(POLITENESS_DELAY_SECONDS)
                return (source, items, "")
            except CollectorError as exc:
                return (source, [], str(exc))
            except Exception as exc:  # noqa: BLE001 - 一个源的意外不该带走整轮
                logger.warning("Unexpected error polling %s", source.name, exc_info=True)
                return (source, [], f"{type(exc).__name__}: {exc}")

    async def _record(
        self,
        db: AsyncSession,
        source: FeedSource,
        items: list[collectors.CollectedItem],
        error: str,
    ) -> int:
        """把一次抓取的结果记进源的健康字段，并落库新条目。"""
        now = datetime.now(UTC)
        source.last_polled_at = now

        if error:
            source.consecutive_failures += 1
            source.last_error = error[:2000]
            if source.consecutive_failures >= source.max_consecutive_failures:
                source.is_active = False
                logger.error(
                    "Feed source %r disabled after %d consecutive failures: %s",
                    source.name, source.consecutive_failures, error,
                )
            return 0

        try:
            report = await ingest.ingest_items(
                db, source=source, items=items, client=self._client
            )
        except HarnessContractUnavailable as exc:
            # 词表读不到是**平台自己的故障**，不是这个源的问题。记成源失败是
            # 唯一能让它被看见的地方（源健康在界面上有呈现），但错误文本要说
            # 清是谁的锅 —— 否则运维会去查 arXiv 为什么挂了。
            source.consecutive_failures += 1
            source.last_error = f"域词表不可用（平台侧，与本源无关）：{exc}"[:2000]
            logger.error("Domain registry unavailable; feed ingest blocked", exc_info=True)
            return 0

        source.consecutive_failures = 0
        source.last_error = None
        source.last_success_at = now
        source.last_item_count = report.stored

        if report.stored == 0:
            self._idle_rounds[source.id] = self._idle_rounds.get(source.id, 0) + 1
        else:
            self._idle_rounds.pop(source.id, None)

        if report.skipped_no_key or report.skipped_no_url:
            # 这两个数不为零通常意味着**源换了格式**，而不是内容有问题。
            # 静默丢弃的话，症状是这个源慢慢就没内容了，没人知道为什么。
            logger.warning(
                "Feed source %r: %d item(s) without a resolvable identity, "
                "%d without a source URL — check whether the feed format changed",
                source.name, report.skipped_no_key, report.skipped_no_url,
            )
        logger.info("Feed source %r: %s", source.name, report.as_dict())
        return report.stored


#: 进程内唯一的采集器 —— 单写者（见模块 docstring）。
feed_collector = FeedCollector()
