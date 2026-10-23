"""推荐采集统一按24小时刷新，来源健康与用户订阅各自独立记账。

每5分钟检查到期项，实际周期统一取 settings.feed_refresh_interval_seconds。
公共源按 last_polled_at、课题发现按 curated_at、订阅发现按各目标执行记录
计时；订阅循环独立运行，避免学者慢请求拖住公共源。学科期刊由独立的
literature_harvester 按同一周期刷新。

空轮不延长周期。来源失败保留 last_error，次日继续尝试；用户手动关闭的源
仍保持关闭。只有公共源循环写 feed_sources 的健康状态。
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
from app.services.say import in_both_languages

logger = logging.getLogger(__name__)

#: 一轮用户挖掘的硬上限。正常一轮（每用户最多 8 组学术检索 + 8 组网页搜索 +
#: 归档）可达数分钟；这个值只负责“任何一处挂死都不无限阻塞全局采集”，
#: 不是正常路径的目标耗时。
DISCOVERY_ROUND_TIMEOUT_SECONDS = 600.0


#: 每5分钟检查到期项，实际采集统一按24小时。
TICK_SECONDS = 300

#: 同时最多拉几个源。压到很低是刻意的：这些是别人的免费接口，
#: 我们没有理由并发地打它们。
MAX_CONCURRENT_FETCHES = 3

#: 同一轮里两次请求之间的间隔。arXiv 的使用条款明确要求请求之间留出间隔。
POLITENESS_DELAY_SECONDS = 3.0

class FeedCollector:
    """后台采集循环。生命周期挂在 app lifespan 上。"""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._subscription_task: asyncio.Task[None] | None = None
        self._school_task: asyncio.Task[None] | None = None
        self._project_asset_task: asyncio.Task[None] | None = None
        self._translation_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        #: 当前这一轮的 HTTP client（补图那一步要用，见 run_once）。
        self._client: httpx.AsyncClient | None = None
        self._school_last_refresh: datetime | None = None

    def start(self) -> None:
        if not settings.feed_collector_enabled:
            logger.info("Feed collector disabled by configuration")
            return
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="feed-collector")
        self._subscription_task = asyncio.create_task(
            self._run_subscriptions(), name="feed-subscription-collector"
        )
        self._school_task = asyncio.create_task(
            self._run_school(), name="feed-school-library-collector"
        )
        self._project_asset_task = asyncio.create_task(
            self._run_project_assets(), name="feed-project-asset-collector"
        )
        self._translation_task = asyncio.create_task(
            self._run_translations(), name="feed-translation-worker"
        )
        logger.info("Feed collector started (tick=%ss)", TICK_SECONDS)

    async def stop(self) -> None:
        """停掉循环并等它真的退出。

        只 `cancel()` 不 `await` 的话，进程会在一次正在进行的 HTTP 抓取中间
        退出，而那条 DB 事务的收尾要么没跑要么跑了一半 —— 关机路径上的错误
        最难查，因为它们只在关机时出现。
        """
        self._stop.set()
        subscriptions, self._subscription_task = self._subscription_task, None
        school, self._school_task = self._school_task, None
        project_assets, self._project_asset_task = self._project_asset_task, None
        translations, self._translation_task = self._translation_task, None
        for worker in (subscriptions, school, project_assets, translations):
            if worker is not None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
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

    async def _run_school(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_school_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("School literature feed round failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)
            except TimeoutError:
                continue

    async def _run_subscriptions(self) -> None:
        # 订阅检索独立循环，学者慢请求不能阻塞公共源和课题发现。
        while not self._stop.is_set():
            completed = 0
            try:
                await discovery.run_subscription_round()
                completion = await discovery.run_subscription_asset_round()
                completed = completion.get("completed", 0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Feed subscription discovery round failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1 if completed else TICK_SECONDS)
            except TimeoutError:
                continue

    async def _run_project_assets(self) -> None:
        while not self._stop.is_set():
            found = 0
            discovery.project_asset_wakeup.clear()
            try:
                result = await discovery.run_project_asset_round()
                found = result.get("found", 0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Project abstract completion round failed")
            try:
                await asyncio.wait_for(discovery.project_asset_wakeup.wait(), timeout=1 if found else TICK_SECONDS)
            except TimeoutError:
                continue

    async def _run_translations(self) -> None:
        from app.services.feed.translation import FeedTranslationWorker
        worker = FeedTranslationWorker()
        while not self._stop.is_set():
            attempted = 0
            try:
                attempted = await worker.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Feed background translation round failed")
            try:
                # Continue through new ready items; failed sources have their own
                # five minute cooldown and cannot starve the rest of the pool.
                await asyncio.wait_for(self._stop.wait(), timeout=1 if attempted else 15)
            except TimeoutError:
                continue

    async def run_school_once(self, *, force: bool = False) -> dict[str, int]:
        """Poll the optional school catalogue on the same daily refresh cadence."""
        from datetime import timedelta
        from app.services import school_library
        from app.services.feed.school_projection import sync_school_feed

        if school_library._configuration() is None:
            return {"seen": 0, "stored": 0, "updated": 0, "withdrawn": 0}
        now = datetime.now(UTC)
        interval = timedelta(seconds=settings.feed_refresh_interval_seconds)
        if not force and self._school_last_refresh and now - self._school_last_refresh < interval:
            return {"seen": 0, "stored": 0, "updated": 0, "withdrawn": 0}
        factory = get_session_factory()
        async with factory() as db:
            report = await sync_school_feed(db, now=now)
            await db.commit()
        self._school_last_refresh = now
        logger.info("School literature feed refresh: %s", report)
        return report

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
        return ingest.is_stale(
            source, interval_seconds=settings.feed_refresh_interval_seconds
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
                logger.error(
                    "Feed source %r failed %d times; retrying next day: %s",
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
            # 存在源上、谁都看得到：写的那一刻不知道是谁、哪种语言来看，两种并排写。
            source.last_error = in_both_languages({
                "zh": "域词表不可用（平台侧，与本源无关）：{why}",
                "en": "The domain vocabulary is unavailable (platform side, nothing to do with this "
                      "source): {why}",
            }, why=exc)[:2000]
            logger.error("Domain registry unavailable; feed ingest blocked", exc_info=True)
            return 0

        source.consecutive_failures = 0
        source.last_error = None
        source.last_success_at = now
        source.last_item_count = report.stored

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
