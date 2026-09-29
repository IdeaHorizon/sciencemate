"""按离线期刊映射定期采集资讯所需的 Index 与 Figure。

这是“期刊路线”：结果进入共享文献库，再投影到本领域动态和个性资讯。
学术搜索与 Project 画像检索仍可独立访问多源数据库，但三条业务链最终复用
同一份按论文组织的归档与可重建目录。
"""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import settings
from app.services.feed.literature_projection import publication_datetime

logger = logging.getLogger(__name__)

MAX_JOURNALS_PER_DOMAIN = 10
MAX_DOMAINS_PER_JOURNAL = 5


def _normal_journal(value: object) -> str:
    text = str(value or "").strip().lower()
    # 出版源常在正式刊名里保留冠词，而分区/ISSN 表省略它，例如
    # “The British Accounting Review” 与 “British Accounting Review”。
    # 必须在去标点前按完整单词移除，不能把 Theory 等以 the 开头的词截坏。
    text = re.sub(r"^the\b[\s\W_]*", "", text)
    return re.sub(r"[^a-z0-9]", "", text)


def _mapping_evidence_rank(raw: object) -> int:
    """稳定映射优先于期刊分区，防止宽泛词命中的 Q1 挤掉明确期刊。

    分区回答“期刊质量如何”，不回答“它属于哪个二级学科”。两者顺序反过来
    时，`port` 对 `sport/reports`、`space` 对社会空间期刊的子串误命中会凭借
    Q1 身份占满每学科十个名额，而 Coastal Engineering 这类明确映射反而落选。
    """
    basis = str(raw or "").strip().lower()
    if basis in {"manual_source_verified", "manual_expert_review"}:
        return 0
    if basis.startswith("user_supplied_"):
        return 1
    if basis.startswith(("journal_title_water_relaxed", "journal_title_agri_relaxed")):
        return 2
    if basis.startswith("journal_title_gap_fill"):
        return 3
    if basis.startswith("journal_title_anchor:") and "legacy_candidate_scan" not in basis:
        return 4
    if basis.startswith("journal_title_anchor_relaxed:"):
        return 5
    return 6


def _mapping_confidence_rank(raw: object) -> int:
    """高置信映射优先；未知置信度放在已标注等级之后。"""
    value = str(raw or "").strip().lower()
    return {
        "high": 0,
        "high-medium": 1,
        "medium": 2,
        "low-medium": 3,
        "low": 4,
    }.get(value, 5)


def _recent_publication(
    raw: object,
    year: object,
    *,
    now: datetime,
    cutoff: datetime,
) -> tuple[datetime | None, bool, str]:
    """Apply recency without pretending coarse source dates are exact days."""
    text = " ".join(str(raw or "").strip().split())
    date_part = text.split("T", 1)[0].replace("/", "-").replace(".", "-")
    numeric = [part for part in date_part.split("-") if part.isdigit()]
    if len(numeric) >= 3:
        published = publication_datetime(raw, year)
        return published, bool(published and cutoff <= published <= now), "day"
    if len(numeric) == 2:
        published = publication_datetime(raw, year)
        accepted = bool(
            published
            and published.year == now.year
            and published.month == now.month
        )
        return published, accepted, "month"
    if len(numeric) == 1 or (not text and year not in (None, "")):
        return publication_datetime(raw, year), False, "year"
    return publication_datetime(raw, year), False, "unknown"


def _journal_targets(
    domain_codes: set[str] | None = None,
) -> dict[str, dict[str, object]]:
    """读取收敛后的离线期刊映射，而不是直接执行整张候选关系表。"""
    configured = os.environ.get("HARNESS_JOURNAL_DOMAIN_MAP", "").strip()
    path = Path(configured).expanduser() if configured else Path(__file__).resolve().parents[4] / "docs" / "xiaohongshu_journal_map.tsv"
    candidates: list[dict[str, object]] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                name = " ".join(str(row.get("journal_name") or "").split())
                code = str(row.get("二级学科代码") or "").strip()
                key = _normal_journal(name)
                if not name or not code:
                    continue
                if domain_codes is not None and code not in domain_codes:
                    continue
                issns: list[str] = []
                for issn in re.split(r"[;,\s]+", str(row.get("issn") or "")):
                    value = issn.strip().upper()
                    if value and value not in issns:
                        issns.append(value)
                try:
                    quartile = int(str(row.get("cas_quartile") or ""))
                except ValueError:
                    quartile = 9
                basis = str(row.get("分类依据") or "").lower()
                candidates.append({
                    "journal": name,
                    "journal_key": key,
                    "issns": issns,
                    "domain": code,
                    "quartile": quartile,
                    "rank": (
                        _mapping_evidence_rank(basis),
                        _mapping_confidence_rank(row.get("分类置信度")),
                        quartile,
                        0 if str(row.get("top") or "") == "是" else 1,
                        1 if "legacy_candidate_scan" in basis else 0,
                        name.casefold(),
                    ),
                })
    except (OSError, csv.Error):
        logger.exception("Cannot load Xiaohongshu journal map: %s", path)
        return {}

    by_domain: dict[str, list[dict[str, object]]] = {}
    for candidate in candidates:
        by_domain.setdefault(str(candidate["domain"]), []).append(candidate)
    for code, values in list(by_domain.items()):
        values.sort(key=lambda item: item["rank"])
        best_evidence = int(values[0]["rank"][0])
        # 一旦某个学科已有人工补充或学科专用规则（0..2），就不再用泛词
        # 推测项凑满十本。否则 `port`/`space` 一类误映射即使排在后面，仍会
        # 填满剩余名额。没有可信层的学科才退回它现有的最佳证据层，保持覆盖。
        evidence_ceiling = 2 if best_evidence <= 2 else best_evidence
        by_domain[code] = [
            item for item in values
            if int(item["rank"][0]) <= evidence_ceiling
        ]

    # 候选少的学科先选；逐轮每类只取一本，兼顾覆盖和质量。
    selected: list[dict[str, object]] = []
    selected_pairs: set[tuple[str, str]] = set()
    journal_load: dict[str, int] = {}
    domain_load: dict[str, int] = {code: 0 for code in by_domain}
    ordered_domains = sorted(by_domain, key=lambda code: (len(by_domain[code]), code))
    while True:
        changed = False
        for code in ordered_domains:
            if domain_load[code] >= MAX_JOURNALS_PER_DOMAIN:
                continue
            for candidate in by_domain[code]:
                journal_key = str(candidate["journal_key"])
                pair = (journal_key, code)
                if pair in selected_pairs:
                    continue
                if journal_load.get(journal_key, 0) >= MAX_DOMAINS_PER_JOURNAL:
                    continue
                selected.append(candidate)
                selected_pairs.add(pair)
                journal_load[journal_key] = journal_load.get(journal_key, 0) + 1
                domain_load[code] += 1
                changed = True
                break
        if not changed or all(
            count >= MAX_JOURNALS_PER_DOMAIN for count in domain_load.values()
        ):
            break

    grouped: dict[str, dict[str, object]] = {}
    for candidate in selected:
        key = str(candidate["journal_key"])
        target = grouped.setdefault(key, {
            "journal": candidate["journal"],
            "issns": [],
            "domains": [],
            "quartile": None,
        })
        target["issns"] = list(dict.fromkeys([
            *list(target["issns"]), *list(candidate["issns"]),
        ]))
        code = str(candidate["domain"])
        if code not in target["domains"]:
            target["domains"].append(code)
        quartile = int(candidate["quartile"])
        if quartile <= 4 and (
            target["quartile"] is None or quartile < target["quartile"]
        ):
            target["quartile"] = quartile

    targets: dict[str, dict[str, object]] = {}
    for target in grouped.values():
        issns = target["issns"]
        query = "issn:" + ";".join(issns) if issns else "venue:" + str(target["journal"])
        query_key = query.lower()
        existing = targets.get(query_key)
        if existing is None:
            targets[query_key] = target
            continue
        # 同一ISSN偶尔对应更名期刊或映射表中的多个名称。采集请求只发一次，
        # 但学科归属必须取并集，不能由TSV中最后一行静默覆盖前面的学科。
        existing["domains"] = list(dict.fromkeys([
            *list(existing["domains"]), *list(target["domains"]),
        ]))
        existing["issns"] = list(dict.fromkeys([
            *list(existing["issns"]), *list(target["issns"]),
        ]))
        if target["quartile"] and (
            existing["quartile"] is None or target["quartile"] < existing["quartile"]
        ):
            existing["quartile"] = target["quartile"]
    return targets


class LiteratureIndexHarvester:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._asset_task: asyncio.Task[None] | None = None
        self._openalex_blocked_until: datetime | None = None
        self._online_tasks: dict[str, asyncio.Task[None]] = {}
        self._online_domains: dict[str, tuple[str, ...]] = {}
        self._last_online_refresh: dict[str, tuple[tuple[str, ...], datetime]] = {}

    def start(self) -> None:
        if (
            not settings.literature_harvester_enabled
            or not settings.literature_harvester_offline_enabled
            or self._task is not None
        ):
            if settings.literature_harvester_enabled:
                logger.info("Literature offline harvest is disabled; online refresh is available")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="literature-index-harvester")
        logger.info("Literature index harvester started (interval=%ss)", settings.literature_harvester_interval_seconds)

    async def stop(self) -> None:
        self._stop.set()
        online, self._online_tasks = list(self._online_tasks.values()), {}
        for running in online:
            running.cancel()
        if online:
            await asyncio.gather(*online, return_exceptions=True)
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if self._asset_task is not None:
            self._asset_task.cancel()
            try:
                await self._asset_task
            except asyncio.CancelledError:
                pass
            self._asset_task = None

    async def _project(self, limit: int) -> int:
        from app.database import get_session_factory
        from app.services.feed.literature_projection import project_literature_catalog

        factory = get_session_factory()
        async with factory() as db:
            report = await project_literature_catalog(db, limit=max(50, limit))
            await db.commit()
        return report.stored + report.duplicates

    async def _complete_assets(self, output_dir: str) -> None:
        from nodes.literature.tools.archive_papers import complete_missing_figures

        try:
            completion = await asyncio.to_thread(
                complete_missing_figures,
                output_dir,
                limit=settings.literature_asset_completion_max_per_round,
                budget_seconds=settings.literature_asset_completion_budget_seconds,
            )
            logger.info("Literature background asset completion: %s", completion)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Literature background asset completion failed")

    async def _run(self) -> None:
        while not self._stop.is_set():
            processed = 0
            try:
                result = await self.run_once()
                processed = int(result.get("queries") or 0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Literature index harvest round failed")
            # 一轮达到批量上限，说明期刊队列很可能还没排空；短暂停顿后继续。
            # 只有本轮不足一个批次时才进入七天休眠，从而覆盖全部映射期刊，
            # 而不是每七天只处理前100本。
            delay = (
                settings.literature_harvester_batch_pause_seconds
                if processed >= settings.literature_harvester_max_queries_per_round
                else settings.literature_harvester_check_interval_seconds
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                continue

    async def _subscribed_journals(self) -> list[dict[str, object]]:
        """Return explicit journal follows from all active users."""
        from sqlalchemy import select
        from app.database import get_session_factory
        from app.models.user import User
        factory = get_session_factory()
        async with factory() as db:
            users = list((await db.execute(select(User).where(User.is_active.is_(True)))).scalars().all())
        found: dict[str, dict[str, object]] = {}
        for user in users:
            preferences = user.preferences if isinstance(user.preferences, dict) else {}
            feed = preferences.get("feed") if isinstance(preferences.get("feed"), dict) else {}
            subscriptions = feed.get("subscriptions") if isinstance(feed.get("subscriptions"), dict) else {}
            for raw in subscriptions.get("journals", []) if isinstance(subscriptions.get("journals"), list) else []:
                if not isinstance(raw, dict):
                    continue
                name = " ".join(str(raw.get("name") or "").split())
                if not name:
                    continue
                key = str(raw.get("key") or _normal_journal(name))
                found[key] = {
                    "name": name, "issn": str(raw.get("issn") or ""),
                    "eissn": str(raw.get("eissn") or ""),
                    "quartile": str(raw.get("jcr_quartile") or ""),
                }
        return list(found.values())

    async def _subscribed_domains(self) -> set[str]:
        """Return the union of active users' explicit feed subscriptions."""
        from sqlalchemy import select

        from app.database import get_session_factory
        from app.models.user import User
        from app.services.feed.profile import stored_domains

        factory = get_session_factory()
        async with factory() as db:
            users = list(
                (await db.execute(select(User).where(User.is_active.is_(True))))
                .scalars()
                .all()
            )
        return {
            code
            for user in users
            for code in stored_domains(user)
            if code
        }

    def request_subscription_refresh(
        self, *, user_id: object, journals: list[dict[str, object]]
    ) -> bool:
        """Refresh newly followed journals through the same index pipeline."""
        if not settings.literature_harvester_enabled or not journals:
            return False
        key = "subscription:" + str(user_id)
        running = self._online_tasks.get(key)
        if running is not None and not running.done():
            running.cancel()
        task = asyncio.create_task(
            self.run_once(domain_codes=set(), online=True, explicit_journals=journals),
            name=f"literature-subscription-refresh-{user_id}",
        )
        self._online_tasks[key] = task
        task.add_done_callback(
            lambda completed, k=key: self._subscription_finished(k, completed)
        )
        return True

    def _subscription_finished(self, key: str, task: asyncio.Task) -> None:
        if self._online_tasks.get(key) is task:
            self._online_tasks.pop(key, None)

    def request_online_refresh(
        self,
        *,
        user_id: object,
        domains: list[str],
        force: bool = False,
    ) -> bool:
        """Start one bounded refresh for the user's current selection."""
        if not settings.literature_harvester_enabled:
            return False
        selected = list(dict.fromkeys(str(code) for code in domains if str(code)))
        if not selected:
            return False
        key = str(user_id)
        signature = tuple(selected)
        running = self._online_tasks.get(key)
        if running is not None and not running.done():
            if self._online_domains.get(key) == signature:
                return False
            running.cancel()
        previous = self._last_online_refresh.get(key)
        if (
            not force
            and previous is not None
            and previous[0] == signature
            and datetime.now(UTC) - previous[1] < timedelta(minutes=15)
        ):
            return False
        task = asyncio.create_task(
            self._run_online_refresh(user_id=user_id, domains=selected),
            name=f"literature-online-refresh-{key}",
        )
        self._online_tasks[key] = task
        self._online_domains[key] = signature
        task.add_done_callback(
            lambda completed, k=key, sig=signature: self._online_finished(
                k, sig, completed
            )
        )
        return True

    def _online_finished(
        self, key: str, signature: tuple[str, ...], task: asyncio.Task[None]
    ) -> None:
        if self._online_tasks.get(key) is not task:
            return
        self._online_tasks.pop(key, None)
        self._online_domains.pop(key, None)
        if not task.cancelled() and task.exception() is None:
            self._last_online_refresh[key] = (signature, datetime.now(UTC))

    def is_online_refreshing(self, user_id: object) -> bool:
        task = self._online_tasks.get(str(user_id))
        return bool(task is not None and not task.done())

    async def _run_online_refresh(
        self, *, user_id: object, domains: list[str]
    ) -> None:
        """Fetch selected journals first, then the selected-domain + Project route."""
        started = asyncio.get_running_loop().time()
        try:
            journal = await self.run_once(domain_codes=set(domains), online=True)
            journal_seconds = asyncio.get_running_loop().time() - started

            from app.database import get_session_factory
            from app.models.user import User
            from app.services.feed import collectors, discovery

            project_started = asyncio.get_running_loop().time()
            factory = get_session_factory()
            async with factory() as db:
                user = await db.get(User, user_id)
                project = {"domains": 0, "found": 0, "digests": 0}
                if user is not None:
                    async with collectors.make_client() as client:
                        project = await discovery.curate_one(
                            client,
                            db,
                            user=user,
                            force=True,
                            allow_disabled=False,
                            selected_domains=domains,
                        )
                    await db.commit()
            project_seconds = asyncio.get_running_loop().time() - project_started
            logger.info(
                "Online literature refresh user=%s domains=%s journal=%s "
                "project=%s timing[journal=%.2fs project=%.2fs total=%.2fs]",
                user_id, domains, journal, project, journal_seconds, project_seconds,
                asyncio.get_running_loop().time() - started,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Online literature refresh failed for user=%s", user_id)

    async def run_once(
        self,
        *,
        domain_codes: set[str] | None = None,
        online: bool = False,
        explicit_journals: list[dict[str, object]] | None = None,
    ) -> dict[str, int]:
        harness_root = settings.harness_root or str(Path(__file__).resolve().parents[4])
        if harness_root not in sys.path:
            sys.path.insert(0, harness_root)
        from nodes.literature.tools.search_engines import SearchManager
        from nodes.literature.tools.archive_papers import archive_indexes_only
        from nodes.literature.tools.scihub_fetcher import paper_cache_dir

        if domain_codes is None:
            domain_codes = await self._subscribed_domains()
            if explicit_journals is None:
                explicit_journals = await self._subscribed_journals()

        manager = SearchManager(per_source_limits={
            "crossref": settings.literature_harvester_max_per_source,
            "openalex": settings.literature_harvester_max_per_source,
        })
        if manager.local_index is None:
            return {"queries": 0, "papers": 0, "failed": 0}
        targets = _journal_targets(domain_codes) if domain_codes else {}
        for raw in explicit_journals or []:
            name = " ".join(str(raw.get("name") or "").split())
            issns = [str(value).strip().upper() for value in (raw.get("issn"), raw.get("eissn")) if str(value or "").strip()]
            if not name:
                continue
            query = f"issn:{issns[0]}" if issns else f"venue:{name}"
            raw_quartile = str(raw.get("quartile") or "").upper().removeprefix("Q")
            try:
                quartile = int(raw_quartile)
            except ValueError:
                quartile = 9
            targets[query.lower()] = {
                "journal": name, "journal_key": _normal_journal(name),
                "issns": issns, "domains": [], "quartile": quartile,
            }
        if not targets:
            logger.info("Literature harvest skipped: no subscribed domains or journals")
            return {"queries": 0, "papers": 0, "failed": 0}
        if online:
            queries = list(targets)
        else:
            manager.local_index.register_harvest_queries(list(targets))
            limit = settings.literature_harvester_max_queries_per_round
            queries = manager.local_index.due_harvest_queries_for(
                list(targets),
                settings.literature_harvester_interval_seconds,
                limit,
            )
        totals = {
            "mapped_journals": len(targets),
            "queries": 0,
            "papers": 0,
            "accepted": 0,
            "rejected": 0,
            "archived": 0,
            "failed": 0,
            "asset_candidates": 0,
            "assets_completed": 0,
            "assets_deferred": 0,
            "projected": 0,
        }
        now = datetime.now(UTC)
        cutoff = now - timedelta(
            days=max(1, settings.literature_harvester_recent_days)
        )
        # 远程层要同时容纳“最近7天的精确日期”和“当前月份精度”。本地再按
        # 日期精度严格筛选，避免把 YYYY-MM 强行当成当月1日后误删。
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        remote_from = min(cutoff, month_start).date().isoformat()
        # 期刊采集必须由ISSN/期刊名约束；arXiv和S2不支持这类精确过滤，
        # 把它们混进来只会得到主题结果，而不是指定期刊的近期动态。
        sources = {"crossref"}
        if (
            self._openalex_blocked_until is None
            or datetime.now(UTC) >= self._openalex_blocked_until
        ):
            sources.add("openalex")
        output_dir = str(paper_cache_dir())
        archived_since_projection = 0
        for query in queries:
            target = targets.get(query.lower())
            if target is None:
                manager.local_index.mark_harvest_query(
                    query, "error", error="journal target missing from current mapping"
                )
                totals["failed"] += 1
                continue
            try:
                results = await manager.search_all(
                    query,
                    max_per_source=settings.literature_harvester_max_per_source,
                    enabled_sources=sources,
                    date_from=remote_from,
                    include_local_catalog=False,
                    use_query_cache=False,
                )
                diagnostics = results.audit_dict().get("source_diagnostics") or {}
                openalex_error = str(
                    (diagnostics.get("openalex") or {}).get("error") or ""
                )
                compact_error = openalex_error.replace(" ", "")
                if "openalex" in sources and (
                    "Insufficient budget" in openalex_error
                    or '"dailyRemainingUsd":0' in compact_error
                ):
                    tomorrow = datetime.now(UTC).date() + timedelta(days=1)
                    self._openalex_blocked_until = datetime.combine(
                        tomorrow, datetime.min.time(), tzinfo=UTC
                    )
                    sources.discard("openalex")
                    logger.warning(
                        "OpenAlex daily budget exhausted; disabled until %s",
                        self._openalex_blocked_until.isoformat(),
                    )
                source_ok = any(
                    str(diag.get("status") or "") == "ok"
                    for diag in diagnostics.values()
                )
                if not source_ok:
                    errors = "; ".join(
                        f"{source}: {diag.get('error') or diag.get('status')}"
                        for source, diag in diagnostics.items()
                    ) or "all requested sources failed"
                    manager.local_index.mark_harvest_query(
                        query, "error", error=errors
                    )
                    totals["queries"] += 1
                    totals["failed"] += 1
                    continue

                expected_issns = {
                    re.sub(r"[^0-9X]", "", value.upper())
                    for value in target["issns"]
                }
                expected_venue = _normal_journal(target["journal"])
                records_to_archive: list[dict[str, object]] = []
                for paper in results:
                    published, is_recent, date_precision = _recent_publication(
                        getattr(paper, "pub_date", ""),
                        getattr(paper, "year", None),
                        now=now,
                        cutoff=cutoff,
                    )
                    if not is_recent:
                        totals["rejected"] += 1
                        continue
                    returned_issns = {
                        re.sub(r"[^0-9X]", "", value.upper())
                        for raw in (getattr(paper, "issn", ""), getattr(paper, "eissn", ""))
                        for value in re.split(r"[;,\s]+", str(raw or ""))
                        if value.strip()
                    }
                    # 远程源的ISSN过滤仍做一次本地复核；若源没回ISSN，则要求
                    # 期刊名精确归一后相同，避免错误期刊继承学科分类。
                    if not (expected_issns & returned_issns) and _normal_journal(
                        getattr(paper, "venue", "")
                    ) != expected_venue:
                        totals["rejected"] += 1
                        continue
                    totals["accepted"] += 1
                    try:
                        record = paper.to_dict() if hasattr(paper, "to_dict") else dict(paper)
                        domains = list(target["domains"])
                        record["journal_classification"] = {
                            "level2_codes": domains,
                            "reason": "online_journal_mapping" if online else "offline_journal_mapping",
                        }
                        record["xiaohongshu_eligible"] = True
                        record["classification_mode"] = (
                            "online_journal_mapping" if online else "offline_journal_mapping"
                        )
                        record["second_level_domains"] = domains
                        record["feed_acquisition_routes"] = ["journal"]
                        record["journal_harvest_verified"] = True
                        record["cas_quartile"] = target["quartile"]
                        record["harvest_batch"] = datetime.now(UTC).date().isoformat()
                        record["publication_date_precision"] = date_precision
                        records_to_archive.append(record)
                    except Exception:
                        logger.warning(
                            "Literature asset archive failed for %r",
                            getattr(paper, "title", ""),
                            exc_info=True,
                        )
                if records_to_archive:
                    logger.debug(
                        "Archiving %d literature indexes for %s",
                        len(records_to_archive),
                        query,
                    )
                    archived = await asyncio.to_thread(
                        archive_indexes_only, records_to_archive, output_dir
                    )
                    totals["archived"] += len(archived)
                    archived_since_projection += len(archived)
                manager.local_index.mark_harvest_query(query, "ok", len(results))
                totals["queries"] += 1
                totals["papers"] += len(results)
                if archived_since_projection and totals["queries"] % 10 == 0:
                    totals["projected"] += await self._project(
                        max(100, archived_since_projection * 3)
                    )
                    archived_since_projection = 0
            except Exception as exc:
                manager.local_index.mark_harvest_query(
                    query, "error", error=f"{type(exc).__name__}: {exc}"
                )
                totals["failed"] += 1
                logger.warning("Literature harvest failed for %r", query, exc_info=True)
        if archived_since_projection:
            totals["projected"] += await self._project(
                max(100, archived_since_projection * 3)
            )
        # 图片补齐跟随真实采集轮次。调度器每小时检查到期水位，但无查询到期时
        # 不应每小时重复扫同一批失败图片；成功水位到期或在线即时采集后再补齐。
        if queries and (self._asset_task is None or self._asset_task.done()):
            totals["asset_candidates"] = (
                settings.literature_asset_completion_max_per_round
            )
            self._asset_task = asyncio.create_task(
                self._complete_assets(output_dir),
                name="literature-asset-completion",
            )
        logger.info("Literature index harvest: %s", totals)
        return totals


literature_index_harvester = LiteratureIndexHarvester()
