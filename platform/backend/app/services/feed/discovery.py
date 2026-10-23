"""自动挖掘的执行面：推断 → 定向检索 → 主动周报。

## 为什么和全局采集分开一个模块

`scheduler.py` 那一轮问的是"哪个**源**到点了"，与用户无关、全平台共享一份
结果。这里问的是"哪个**用户**该被挖一次了"，每个人的答案都不同，而且要花
他自己的模型预算。两件事的节奏、失败语义、成本归属都不一样，混在一个函数里
只会让"这次失败该算谁的"说不清。

## 三道闸，缺一不做

1. 用户把开关打开了（他自己的预算，默认关）；
2. `feed_curation` 角色配了模型（没配 = 这项能力不在，不是调了会失败）；
3. 距上次挖掘够久，或者他的课题有实质变化。

## 失败绝不外溢

挖掘失败只写进这个用户的偏好（界面上能看见原因），不影响全局采集、不影响
别的用户、更不影响任何研究。一轮里某个用户炸了，下一个照跑。
"""

from __future__ import annotations

import asyncio
import logging
import hashlib
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.user import User
from app.services.feed import bridge, collectors, curation, digest
from app.services.feed import windows

logger = logging.getLogger(__name__)
project_asset_wakeup = asyncio.Event()

#: 一轮最多伺候几个用户。开关是逐个用户开的，用户多起来之后一轮全跑完会
#: 把一次 tick 拖很长；分批意味着每个人最迟下一轮轮到。
MAX_USERS_PER_ROUND = 5

#: 每个用户每轮最多跑八组检索；每组由确定性的多源检索器执行。
MAX_QUERIES_PER_USER = 8

#: 定向检索每次取几条。
SEARCH_RESULTS_PER_QUERY = 15

#: 网页路线每个Project画像词组最多保留几条；五组最多25条，后台有界。
WEB_RESULTS_PER_QUERY = 5

#: 低于该值的搜索命中多半只碰巧共享一个泛词，不进入推荐池。
MIN_QUERY_RELEVANCE = 0.20

#: 一个用户一轮最多预生成几份周报 —— 每份都是一次模型调用。
MAX_DIGESTS_PER_USER = 2




def _normalized_web_url(raw: object) -> str:
    """Resolve DDG redirect URLs and reject non-public URL shapes."""
    value = str(raw or "").strip()
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlparse(value)
    if parsed.netloc.endswith("duckduckgo.com"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            value = unquote(target)
            parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _web_authority(url: str) -> float:
    """Conservative, auditable authority prior based only on the source host."""
    host = (urlparse(url).hostname or "").lower().strip(".")
    if not host:
        return 0.0
    if host.endswith((".gov", ".gov.cn", ".edu", ".edu.cn", ".ac.uk", ".ac.cn")):
        return 1.0
    if host.endswith((".int", ".org")):
        return 0.85
    if any(token in host for token in (
        "nature.com", "science.org", "springer.com", "wiley.com",
        "elsevier.com", "acm.org", "ieee.org", "nih.gov", "who.int",
    )):
        return 0.9
    return 0.55


def _doi_from_url(url: str) -> str:
    match = re.search(r"10\.\d{4,9}/[^\s?#]+", unquote(url), re.I)
    return match.group(0).rstrip(".,;:)").lower() if match else ""


def _aware(value: datetime | None) -> datetime | None:
    """SQLite 给 naive、Postgres 给 aware（驱动差异）。统一，别让每个
    调用方各记一次。"""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def is_due(user: User, *, now: datetime | None = None) -> bool:
    """这个用户该挖了吗。"""
    if not curation.is_enabled(user):
        return False
    state = curation.stored(user)
    # 旧版本把模型的空输出记成成功。失败记录仍受每日闸门限速。
    if (state.get("curated_at") and not state.get("curation_result_version") and not state.get("curated_error")
            and not state.get("inferred_queries") and not state.get("inferred_news_queries")):
        return True
    raw = state.get("curated_at") if state.get("curated_error") else (state.get("last_project_search_at") or state.get("curated_at"))
    if not isinstance(raw, str) or not raw:
        return True
    try:
        last = _aware(datetime.fromisoformat(raw))
    except ValueError:
        return True
    if last is None:
        return True
    moment = now or datetime.now(UTC)
    # A failed model call produced no queries. Retry it within an hour instead
    # of letting one timeout silence project discovery for a full day.
    interval = min(settings.feed_refresh_interval_seconds, 3600) if state.get("curated_error") else settings.feed_refresh_interval_seconds
    return (moment - last).total_seconds() >= interval


async def due_users(db: AsyncSession) -> list[User]:
    """开着开关、且到点了的用户。

    过滤在 Python 里做而不是 SQL：开关和上次时间都在 `preferences` 这个
    JSON 袋子里，各方言的 JSON 查询写法不通用，而用户数在这个量级上无所谓。
    """
    rows = await db.execute(select(User).where(User.is_active.is_(True)))
    return [u for u in rows.scalars().all() if is_due(u)][:MAX_USERS_PER_ROUND]


async def _targeted_search(
    client: httpx.AsyncClient,
    db: AsyncSession,
    *, user: User, queries: list[str], news_queries: list[str] | None = None,
) -> int:
    """网络与论文并行，各用独立会话，完成的记录立即发布。"""
    from app.database import get_session_factory

    async def collect(lane: str) -> int:
        async with get_session_factory()() as lane_db:
            try:
                count = await _targeted_search_lane(
                    client, lane_db, user=user, queries=queries,
                    news_queries=news_queries, lane=lane,
                )
                await lane_db.commit()
                return count
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Project discovery %s lane failed for %s", lane, user.id)
                return 0

    return sum(await asyncio.gather(collect("paper"), collect("web")))


async def _targeted_search_lane(
    _client: httpx.AsyncClient,
    db: AsyncSession,
    *,
    user: User,
    queries: list[str],
    news_queries: list[str] | None = None,
    lane: str = "paper",
) -> int:
    """按项目画像做多源检索，并归档到统一文献库。

    结果仍是全平台共享的论文事实；``profile_user_ids``只记录是谁的Project
    触发了发现，推荐时还会重新以该用户当前Project画像核对相关性。
    """
    harness_root = settings.harness_root or str(Path(__file__).resolve().parents[5])
    if harness_root not in sys.path:
        sys.path.insert(0, harness_root)
    from app.services.feed.literature_projection import (
        project_literature_catalog,
    )
    from nodes.literature.tools.archive_papers import archive_index_and_figure, archive_indexes_only
    from nodes.literature.tools.filter import compute_relevance_score
    from nodes.literature.tools.scihub_fetcher import paper_cache_dir
    from nodes.literature.tools.search_engines import Paper, SearchManager
    from shared.tools.web import _web_search

    manager = SearchManager()
    from app.services.literature_harvester import _recent_publication

    now = datetime.now(UTC)
    cutoff = windows.web_cutoff(now)
    remote_from = cutoff.replace(day=1).date().isoformat()
    sources = {
        "arxiv", "biorxiv", "medrxiv", "semantic_scholar",
        "pubmed", "crossref", "openalex",
    }
    output_dir = str(paper_cache_dir())
    archived = 0
    # 第三条路线：课题相关的「资讯/新闻」检索 —— 用资讯词（news_queries），
    # 与论文检索词（queries）分开。资讯词面向新闻/政策/产业动态（中文优先），
    # 不是论文。没给资讯词就退回论文词，至少还能捞到技术博客这类非论文网页。
    # 模型不直接持有联网工具；平台保存URL和搜索后端，保证结果可核验。
    web_queries = [q for q in (news_queries or []) if q] or [q for q in queries if q]
    web_archived = 0
    web_cutoff = windows.web_cutoff(now)
    for query in (web_queries[:MAX_QUERIES_PER_USER] if lane == "web" else []):
        try:
            result = await _web_search(None, query=query, limit=WEB_RESULTS_PER_QUERY)
        except Exception as exc:
            logger.info("Project-profile web search %r failed for %s: %s", query, user.id, exc)
            continue
        if result.get("status") != "success":
            logger.info(
                "Project-profile web search %r unavailable for %s: %s",
                query, user.id, result.get("error") or "unknown",
            )
            continue
        for raw in result.get("results") or []:
            if not isinstance(raw, dict):
                continue
            title = " ".join(str(raw.get("title") or "").split())
            snippet = " ".join(str(raw.get("snippet") or "").split())
            url = _normalized_web_url(raw.get("url"))
            if not title or not url:
                continue
            raw_date = str(raw.get("published_at") or raw.get("publication_date") or raw.get("date") or "").strip()
            if not raw_date:
                title_date = re.search(r"\b(20\d{2})[./-](\d{1,2})[./-](\d{1,2})\b", title)
                if title_date:
                    raw_date = "-".join(title_date.groups())
            precision = ""
            if raw_date:
                _published, is_recent, precision = _recent_publication(
                    raw_date, None, now=now, cutoff=web_cutoff,
                )
                if not is_recent:
                    continue
            candidate = Paper(
                title=title,
                abstract=snippet,
                url=url,
                doi=_doi_from_url(url),
                source="web",
                venue=urlparse(url).hostname or "",
                pub_type="web-resource",
                pub_date=raw_date,
            )
            relevance = compute_relevance_score(candidate, query)
            if relevance < MIN_QUERY_RELEVANCE:
                continue
            record = candidate.to_dict()
            record.update({
                "feed_acquisition_routes": ["web"],
                "profile_user_ids": [str(user.id)],
                "xiaohongshu_eligible": True,
                "classification_mode": "project_profile_web_search",
                "abstract_enrichment_attempted": True,
                "second_level_domains": [],
                "harvest_batch": datetime.now(UTC).date().isoformat(),
                "discovered_at": datetime.now(UTC).isoformat(),
                "discovery_query": query,
                "profile_query_relevance": relevance,
                "web_search_backend": str(result.get("backend") or ""),
                "web_authority": _web_authority(url),
                "web_traceability": 1.0 if snippet else 0.5,
                "publication_date_precision": precision,
                "publication_date_unverified": not bool(raw_date),
            })
            try:
                await asyncio.to_thread(archive_index_and_figure, record, output_dir)
                web_archived += 1
                await project_literature_catalog(db, limit=50, profile_user_id=str(user.id))
                await db.commit()
            except Exception:
                logger.warning("Web result archive failed for %r", title, exc_info=True)
        await asyncio.sleep(collectors_politeness())

    if web_archived:
        await project_literature_catalog(db, limit=max(400, web_archived * 3))
        await db.commit()
    for query in (queries[:MAX_QUERIES_PER_USER] if lane == "paper" else []):
        query_started = time.monotonic()
        window_key = f"project:{user.id}:{query.casefold()}"
        query_from, full_refresh = (manager.local_index.harvest_window(window_key, remote_from, now=now)
                                    if manager.local_index is not None else (remote_from, True))
        logger.info("Project harvest window: query=%s from=%s full=%s", query, query_from, full_refresh)
        archive_ok = True
        search_started = time.monotonic()
        try:
            papers = await manager.search_all(
                query,
                max_per_source=SEARCH_RESULTS_PER_QUERY,
                enabled_sources=sources,
                date_from=query_from,
                include_local_catalog=False,
                use_query_cache=False,
            )
        except Exception as exc:  # 一个词组失败不带走其余词组
            logger.info("Project-profile search %r failed for %s: %s", query, user.id, exc)
            continue
        search_seconds = time.monotonic() - search_started
        archive_seconds = projection_seconds = 0.0
        accepted = 0
        projected = 0
        batch: list[dict] = []

        async def flush_batch() -> None:
            nonlocal archived, archive_ok, archive_seconds, projection_seconds, projected
            if not batch:
                return
            records = list(batch)
            batch.clear()
            try:
                started = time.monotonic()
                saved = await asyncio.to_thread(archive_indexes_only, records, output_dir)
                archive_seconds += time.monotonic() - started
                archived += len(saved)
                keys = []
                for row in saved:
                    doi = str(row.get("doi") or "").strip().lower()
                    url = str(row.get("url") or "").strip()
                    if doi:
                        keys.append(doi)
                    elif url:
                        keys.append("url:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:32])
                started = time.monotonic()
                report = await project_literature_catalog(
                    db, limit=max(1, len(keys)), profile_user_id=str(user.id),
                    catalog_keys=list(dict.fromkeys(keys)),
                )
                await db.commit()
                projection_seconds += time.monotonic() - started
                projected += report.seen
                project_asset_wakeup.set()
            except Exception:
                archive_ok = False
                await db.rollback()
                logger.warning("Project-profile batch archive/projection failed for %r", query, exc_info=True)

        for paper in papers:
            published, is_recent, precision = _recent_publication(
                getattr(paper, "pub_date", ""), getattr(paper, "year", None),
                now=now, cutoff=cutoff,
            )
            if not is_recent:
                continue
            relevance = compute_relevance_score(paper, query)
            if relevance < MIN_QUERY_RELEVANCE:
                continue
            record = paper.to_dict() if hasattr(paper, "to_dict") else dict(paper)
            record["feed_acquisition_routes"] = ["project_profile"]
            record["discovery_query"] = query
            record["profile_query_relevance"] = relevance
            record["publication_date_precision"] = precision
            record["profile_user_ids"] = [str(user.id)]
            record["xiaohongshu_eligible"] = True
            record["classification_mode"] = "project_profile"
            # project_profile 路线的论文**不标订阅学科**：订阅是 journal 路线
            # （本领域动态）的边界，而 project 检索靠课题匹配（profile_user_ids
            # + matched_terms）进推荐，不依赖 domains。把它标成订阅学科会把
            # 「车辆动力学」关键词捞到的流体力学论文错挂到「专门史」下面。
            record["second_level_domains"] = []
            record["harvest_batch"] = datetime.now(UTC).date().isoformat()
            # A source abstract already satisfies the metadata stage.  The
            # figure archive below finishes the preparation gate.
            if str(record.get("abstract") or "").strip():
                record["abstract_enrichment_attempted"] = True
            batch.append(record)
            accepted += 1
            if len(batch) >= 25:
                await flush_batch()
        await flush_batch()
        logger.info(
            "Project query timing: query=%s returned=%d accepted=%d projected=%d "
            "search=%.2fs archive=%.2fs projection=%.2fs total=%.2fs",
            query, len(papers), accepted, projected, search_seconds,
            archive_seconds, projection_seconds, time.monotonic() - query_started,
        )
        source_ok = any(diag.get("status") == "ok" for diag in (papers.audit_dict().get("source_diagnostics") or {}).values()) if hasattr(papers, "audit_dict") else bool(papers)
        if archive_ok and source_ok and manager.local_index is not None:
            manager.local_index.mark_harvest_window(window_key, full=full_refresh, now=now)
        await asyncio.sleep(collectors_politeness())

    archived += web_archived
    logger.info(
        "Project-profile discovery for %s archived %d item(s), including %d web item(s)",
        user.id, archived, web_archived,
    )
    # Each paper batch and the web lane have already projected their own records.
    return archived


def collectors_politeness() -> float:
    from app.services.feed.scheduler import POLITENESS_DELAY_SECONDS

    return POLITENESS_DELAY_SECONDS


async def _prebuild_digests(db: AsyncSession, *, user: User) -> int:
    """替这个用户把他关注方向的周报先生成好。

    这正是"平台没有服务身份"那个坑的解：用户打开开关并指名模型，就补上了
    缺失的那个"人"—— 同意与凭据都齐了，定时生成才站得住。
    """
    built = 0
    for domain in curation.effective_domains(user)[:MAX_DIGESTS_PER_USER]:
        try:
            item = await digest.ensure_digest(db, domain=domain, user=user)
        except Exception:  # noqa: BLE001 - 一个域失败不该带走别的
            logger.warning("Prebuilding digest for %s failed", domain, exc_info=True)
            continue
        if item is not None:
            built += 1
    return built


async def _curate_one_impl(
    client: httpx.AsyncClient,
    db: AsyncSession,
    *,
    user: User,
    force: bool = False,
    allow_disabled: bool = False,
    selected_domains: list[str] | None = None,
) -> dict[str, int]:
    """替一个用户跑完整轮挖掘。失败只落在他自己的偏好里。"""
    curate_started = time.monotonic()
    result = await curation.curate(
        db,
        user=user,
        force=force,
        allow_disabled=allow_disabled,
        selected_domains=selected_domains,
    )
    # 画像写回会锁住用户行；必须在耗时的外部检索之前提交，否则独立的
    # 订阅调度和用户修改偏好都得等整轮检索结束，超时还会丢掉画像进度。
    await db.commit()
    curation_seconds = time.monotonic() - curate_started
    if result is None:
        state = curation.stored(user)
        # Reuse current queries for the daily harvest even when the project
        # text did not change. Curation only reruns the model when its input
        # fingerprint changes; discovery still needs a new search each day.
        if not state.get("curated_error"):
            projects = await curation.user_projects(db, user)
            if projects:
                examples = await curation.saved_project_examples(db, user, projects)
                current = curation.project_fingerprint(projects, examples)
                queries = list(curation.inferred_queries(user))
                news = list(state.get("inferred_news_queries") or [])
                if current == state.get("curated_fingerprint") and (queries or news):
                    found = await _targeted_search(
                        client, db, user=user, queries=queries, news_queries=news,
                    )
                    _mark_project_searched(user)
                    await db.commit()
                    return {"domains": 0, "found": found, "digests": 0}
        # A transient model failure should not cancel today's acquisition when
        # the last successful queries still describe the same active projects.
        # Reuse them at most once per day while the model retries hourly.
        previous_queries = list(curation.inferred_queries(user))
        previous_news = list(state.get("inferred_news_queries") or [])
        previous_at = curation._coerce_datetime(state.get("fallback_search_at"))
        now = datetime.now(UTC)
        can_reuse = bool(state.get("curated_error") and (previous_queries or previous_news))
        can_reuse = can_reuse and (previous_at is None or (now - previous_at).total_seconds() >= 86400)
        if can_reuse:
            projects = await curation.user_projects(db, user)
            can_reuse = bool(projects and state.get("curated_fingerprint") == curation.project_fingerprint(projects))
        if can_reuse:
            preferences = dict(user.preferences or {})
            feed = dict(preferences.get("feed") or {})
            feed["fallback_search_at"] = now.isoformat()
            preferences["feed"] = feed
            user.preferences = preferences
            await db.commit()
            found = await _targeted_search(
                client, db, user=user, queries=previous_queries,
                news_queries=previous_news,
            )
            _mark_project_searched(user)
            await db.commit()
            return {"domains": 0, "found": found, "digests": 0}
        return {"domains": 0, "found": 0, "digests": 0}

    search_started = time.monotonic()
    found = await _targeted_search(
        client, db, user=user, queries=result["queries"],
        news_queries=result.get("news_queries"),
    )
    _mark_project_searched(user)
    await db.commit()
    search_seconds = time.monotonic() - search_started
    digest_started = time.monotonic()
    digests = await _prebuild_digests(db, user=user)
    logger.info(
        "Project round timing: user=%s curation=%.2fs search=%.2fs digests=%.2fs total=%.2fs found=%d",
        user.id, curation_seconds, search_seconds,
        time.monotonic() - digest_started, time.monotonic() - curate_started, found,
    )
    return {"domains": len(result["domains"]), "found": found, "digests": digests}


def _mark_project_searched(user: User) -> None:
    preferences = dict(user.preferences or {})
    feed = dict(preferences.get("feed") or {})
    feed["last_project_search_at"] = datetime.now(UTC).isoformat()
    preferences["feed"] = feed
    user.preferences = preferences


_project_inflight: dict[str, int] = {}


async def curate_one(client, db, *, user, force=False, allow_disabled=False, selected_domains=None):
    key = str(user.id)
    _project_inflight[key] = _project_inflight.get(key, 0) + 1
    try:
        return await _curate_one_impl(client, db, user=user, force=force,
            allow_disabled=allow_disabled, selected_domains=selected_domains)
    finally:
        _project_inflight[key] -= 1
        if not _project_inflight[key]:
            _project_inflight.pop(key, None)


async def run_round(db: AsyncSession) -> dict[str, int]:
    """一轮用户挖掘。返回这轮的账。"""
    users = await due_users(db)
    if not users:
        return {"users": 0, "found": 0, "digests": 0}

    totals = {"users": 0, "found": 0, "digests": 0}
    async with collectors.make_client() as client:
        for user in users:
            try:
                one = await curate_one(
                    client,
                    db,
                    user=user,
                    # ``due_users`` 已经用 curated_at 做过 24 小时闸门。这里若仍
                    # 只按 Project 指纹判断，课题文本不变的用户将永远不再检索
                    # 新发表文献；到期轮次应重新生成并执行查询。
                    force=True,
                    selected_domains=list(curation.effective_domains(user)),
                )
            except Exception:  # noqa: BLE001 - 一个用户炸了，下一个照跑
                logger.warning("Curation round failed for %s", user.id, exc_info=True)
                continue
            totals["users"] += 1
            totals["found"] += one["found"]
            totals["digests"] += one["digests"]
    return totals


def role_available(backend_present: bool) -> bool:
    """开关能不能用 —— 由 `feed_curation` 角色配没配决定。

    单独一个函数是为了让接口层和界面用**同一条判据**说话：一边显示"可用"、
    另一边运行时发现没模型，是这类开关最经典的分歧。
    """
    return backend_present


#: 订阅采集的窗口。比题路线（7 天）宽：订阅的刊往往是月刊/季刊，7 天窗口
#: 里可能一篇新的都没有，看起来就像"订阅没用"。

#: 订阅路线每轮最多处理几个期刊 + 几个学者 —— 订阅列表可以很长，采集是有界的。
MAX_SUBSCRIPTION_TARGETS = 8

#: 作者检索每个来源取几条。
AUTHOR_RESULTS_PER_SOURCE = 20


def _strip_markup(value: object) -> str:
    """去掉 JATS/HTML 标签 —— Crossref 的摘要是结构化 XML，直接进卡片会是乱码。"""
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return " ".join(text.split())


def _openalex_abstract(inverted: object) -> str:
    """把 OpenAlex 的倒排索引摘要还原成正文。

    OpenAlex 的 `abstract_inverted_index` 是 {词: [位置...]} 的映射，不是文本。
    不还原就等于"这条没有摘要"，于是它进不了资讯流（`show_in_feed` 要求论文
    必须有摘要）—— 用户看到的是"订阅了学者却没内容"，而库里其实有全文摘要。
    """
    if not isinstance(inverted, dict):
        return ""
    positions: dict[int, str] = {}
    for word, slots in inverted.items():
        if not isinstance(slots, list):
            continue
        for slot in slots:
            if isinstance(slot, int):
                positions[slot] = str(word)
    if not positions:
        return ""
    return " ".join(positions[index] for index in sorted(positions))


async def _papers_by_author(name: str, *, since: str, identity: dict | None = None) -> list:
    from app.services.feed import author_identity
    if not identity or not author_identity.identity(identity):
        return []
    return await author_identity.papers(identity, since=since)


async def discover_for_subscriptions(
    db: AsyncSession,
    *,
    user: User,
    journals: list[dict[str, object]] | None = None,
    scholars: list[dict[str, object]] | None = None,
) -> int:
    """订阅（期刊 / 学者）驱动的定向采集 —— 网络与学术数据库都跑。

    ## 为什么订阅也要有采集面

    "订阅"如果只是订阅页上的一个过滤器，用户订了一本刊却等不到它的新成果，
    这个功能就是空的。期刊路线原本只有一个入口：全局采集器按离线「期刊→学科」
    映射表遍历，**用户亲自订的刊并不在表里**。学者路线则完全没有入口。

    所以这里补上：用户点名了谁/哪本刊，就主动去捞他们的新产出，落到统一文献
    库里。学术数据库（arXiv / bioRxiv / medRxiv / Semantic Scholar / PubMed /
    Crossref / OpenAlex）与网络检索都跑 —— 学者主页、预印本、实验室新闻这类
    数据库没有入口的产出，靠网络路线兜住。

    ## 采集宽、准入严

    检索用名字做词面匹配，必然带回同名/同刊名的噪声。所以这里**不做最终判断**：
    记录只带 route 标记和 ``profile_user_ids``，真正的准入在送达时现算
    （``ranking.matches_subscribed_journal`` / ``matches_subscribed_scholar``）。
    这样取消订阅后旧记录会立刻退出，而采集面不必回滚。
    """
    from app.services.feed.literature_projection import (
        project_literature_catalog,
    )
    from app.services.literature_harvester import _recent_publication
    from nodes.literature.tools.archive_papers import archive_indexes_only
    from nodes.literature.tools.filter import compute_relevance_score
    from nodes.literature.tools.scihub_fetcher import paper_cache_dir
    from nodes.literature.tools.search_engines import CrossrefSearch, Paper, SearchManager
    from shared.tools.web import _web_search

    journal_issns = {
        " ".join(str(entry.get("name") or "").split()): str(entry.get("issn") or entry.get("eissn") or "").strip()
        for entry in (journals or [])[:MAX_SUBSCRIPTION_TARGETS]
    }
    from app.services.feed import author_identity
    scholar_targets = {author_identity.identity(entry): entry for entry in scholars or [] if author_identity.identity(entry)}
    targets: list[tuple[str, str, str]] = []  # (route, query, display)
    for entry in (journals or [])[:MAX_SUBSCRIPTION_TARGETS]:
        name = " ".join(str(entry.get("name") or "").split())
        if name:
            targets.append(("journal", name, name))
    for entry in (scholars or [])[:MAX_SUBSCRIPTION_TARGETS]:
        name = " ".join(str(entry.get("name") or "").split())
        if name and author_identity.identity(entry):
            targets.append(("scholar", author_identity.identity(entry), name))
    if not targets:
        return 0

    harness_root = settings.harness_root or str(Path(__file__).resolve().parents[5])
    if harness_root not in sys.path:
        sys.path.insert(0, harness_root)

    manager = SearchManager()
    now = datetime.now(UTC)
    cutoff = windows.web_cutoff(now)
    remote_from = cutoff.replace(day=1).date().isoformat()
    sources = {
        "arxiv", "biorxiv", "medrxiv", "semantic_scholar",
        "pubmed", "crossref", "openalex",
    }
    output_dir = str(paper_cache_dir())
    index_records: list[dict[str, object]] = []
    successful_windows = []
    web_cutoff = windows.web_cutoff(now)
    for route, query, display in targets:
        window_key = f"subscription:{user.id}:{route}:{query.casefold()}"
        query_from, full_refresh = (manager.local_index.harvest_window(window_key, remote_from, now=now)
                                    if manager.local_index is not None else (remote_from, True))
        academic_ok = False
        logger.info("Subscription harvest window: target=%s from=%s full=%s", display, query_from, full_refresh)
        # ── 学术数据库 ──
        # 学者走**作者字段**检索（Crossref query.author / OpenAlex raw_author_name
        # / PubMed [Author]），期刊走关键词/ISSN 检索。把姓名当关键词搜会捞回
        # 大量同名噪声，而且全在送达时被拒 —— 表现为"订阅了学者永远没内容"。
        if route == "scholar":
            papers = await _papers_by_author(display, since=query_from, identity=scholar_targets[query])
            academic_ok = bool(papers)
        else:
            try:
                papers = await manager.search_all(
                    query,
                    max_per_source=SEARCH_RESULTS_PER_QUERY,
                    enabled_sources=sources,
                    metadata_lookup_sources={*sources, "publisher_landing"},
                    date_from=query_from,
                    include_local_catalog=False,
                    use_query_cache=False,
                )
                academic_ok = any(diag.get("status") == "ok" for diag in (papers.audit_dict().get("source_diagnostics") or {}).values()) if hasattr(papers, "audit_dict") else bool(papers)
            except Exception as exc:  # 一个订阅目标失败不带走其余
                logger.info("Subscription search %r failed for %s: %s", query, user.id, exc)
                papers = []
        exact_dois: set[str] = set()
        if route == "journal" and journal_issns.get(display):
            # SearchManager's journal-name query is one relevance page.  The
            # first follow-up must cover the complete year for the exact ISSN.
            try:
                async with collectors.make_client() as crossref_client:
                    exact_papers = await CrossrefSearch(crossref_client).search_issn_window(
                        journal_issns[display],
                        date_from=query_from,
                        date_until=now.date().isoformat(),
                    )
                academic_ok = True
                exact_dois = {str(p.doi).casefold() for p in exact_papers if p.doi}
                papers = [*exact_papers, *papers]
                logger.info(
                    "Subscription exact ISSN search: journal=%s crossref=%d",
                    display, len(exact_papers),
                )
            except Exception:
                academic_ok = False  # Do not advance a complete-journal watermark after failed pagination.
                logger.warning("Subscription exact ISSN search failed for %s", display, exc_info=True)
        if academic_ok and manager.local_index is not None:
            successful_windows.append((window_key, full_refresh))
        seen_papers: set[str] = set()
        for paper in papers:
            identity = str(getattr(paper, "doi", "") or "").casefold() or str(getattr(paper, "title", "") or "").casefold()
            if identity in seen_papers:
                continue
            seen_papers.add(identity)
            published, is_recent, precision = _recent_publication(
                getattr(paper, "pub_date", ""), getattr(paper, "year", None),
                now=now, cutoff=cutoff,
            )
            if not is_recent:
                continue
            # 学者路线不复核"姓名和标题的词面重叠"：作者命中就是最强的相关性
            # 证据，而一篇论文的标题本来就不该出现作者的名字（会被词面闸门全杀）。
            if route == "scholar":
                relevance = MIN_QUERY_RELEVANCE
            else:
                relevance = (
                    1.0 if getattr(paper, "doi", None) and str(paper.doi).casefold() in exact_dois
                    else compute_relevance_score(paper, query)
                )
                if relevance < MIN_QUERY_RELEVANCE:
                    continue
            record = paper.to_dict() if hasattr(paper, "to_dict") else dict(paper)
            record["feed_acquisition_routes"] = [route]
            # 明确的「这是用户亲自订阅触发的」标记：投影侧据此免走期刊映射判定
            # （subscription_journal 的产物不是按映射表扫出来的，判不出"属于哪个
            # 学科"，会被 journal_policy 拒掉）。用布尔字段而不是去匹配
            # classification_mode 的字符串 —— 后者一改名就静默失效。
            record["subscription_requested"] = True
            record["discovery_query"] = query
            record["profile_query_relevance"] = relevance
            record["publication_date_precision"] = precision
            record["profile_user_ids"] = [str(user.id)]
            record["xiaohongshu_eligible"] = True
            record["classification_mode"] = f"subscription_{route}"
            # 与 project_profile 同样的理由：订阅路线的论文不标订阅学科，
            # 避免把跨学科期刊/学者的成果错挂到某个二级学科下面。
            record["second_level_domains"] = []
            record["harvest_batch"] = datetime.now(UTC).date().isoformat()
            index_records.append(record)

        if route == "scholar":
            continue
        # ── 网络检索：期刊官网/学者主页/预印本/新闻这类数据库没有入口的产出 ──
        web_query = (
            f"{display} latest papers"
            if route == "scholar"
            else f"{display} new articles"
        )
        try:
            result = await _web_search(None, query=web_query, limit=WEB_RESULTS_PER_QUERY)
        except Exception as exc:
            logger.info("Subscription web search %r failed for %s: %s", web_query, user.id, exc)
            result = {}
        if result.get("status") == "success":
            for raw in result.get("results") or []:
                if not isinstance(raw, dict):
                    continue
                title = " ".join(str(raw.get("title") or "").split())
                snippet = " ".join(str(raw.get("snippet") or "").split())
                url = _normalized_web_url(raw.get("url"))
                if not title or not url:
                    continue
                raw_date = str(raw.get("published_at") or raw.get("publication_date") or raw.get("date") or "").strip()
                precision = ""
                if raw_date:
                    _published, is_recent, precision = _recent_publication(
                        raw_date, None, now=now, cutoff=web_cutoff,
                    )
                    if not is_recent:
                        continue
                candidate = Paper(
                    title=title,
                    abstract=snippet,
                    url=url,
                    doi=_doi_from_url(url),
                    source="web",
                    venue=urlparse(url).hostname or "",
                    pub_type="web-resource",
                    pub_date=raw_date,
                )
                relevance = compute_relevance_score(candidate, display)
                if relevance < MIN_QUERY_RELEVANCE:
                    continue
                record = candidate.to_dict()
                record.update({
                    "feed_acquisition_routes": [route],
                    "subscription_requested": True,
                    "profile_user_ids": [str(user.id)],
                    "xiaohongshu_eligible": True,
                    "classification_mode": f"subscription_{route}_web",
                    "second_level_domains": [],
                    "harvest_batch": datetime.now(UTC).date().isoformat(),
                    "discovered_at": datetime.now(UTC).isoformat(),
                    "discovery_query": web_query,
                    "profile_query_relevance": relevance,
                    "web_search_backend": str(result.get("backend") or ""),
                    "web_authority": _web_authority(url),
                    "web_traceability": 1.0 if snippet else 0.5,
                    "publication_date_precision": precision,
                    "publication_date_unverified": not bool(raw_date),
                })
                index_records.append(record)
        await asyncio.sleep(collectors_politeness())

    # 先把索引写入统一 catalog；图片/全文资产由后台补齐任务异步处理。
    for record in index_records:
        record["abstract_enrichment_attempted"] = True
    # Keep an already completed index ready when the daily year window finds
    # the same DOI again; archive_indexes_only preserves its existing status.
    # 订阅结果不能因为某篇论文的图片请求慢而整体丢失。
    try:
        archived_records = await asyncio.to_thread(
            archive_indexes_only, index_records, output_dir
        )
        if manager.local_index is not None:
            for window_key, full_refresh in successful_windows:
                manager.local_index.mark_harvest_window(window_key, full=full_refresh, now=now)
    except Exception:
        logger.warning("Subscription index archive failed for %s", user.id, exc_info=True)
        archived_records = []
    archived = len(archived_records)
    logger.info(
        "Subscription discovery for %s indexed %d item(s) across %d target(s)",
        user.id, archived, len(targets),
    )
    if archived:
        await project_literature_catalog(db, limit=max(400, archived * 3))
    return archived


SUBSCRIPTION_TARGET_TIMEOUT_SECONDS = 600
_subscription_inflight: set[str] = set()
_subscription_asset_inflight: set[str] = set()
_subscription_active_targets: dict[str, str] = {}


def due_subscription_targets(
    user: User, *, now: datetime | None = None,
) -> tuple[list[dict], list[dict]]:
    """按目标持久化每日进度，先筛到期项再分批，避免第9个订阅永远轮不到。"""
    state = curation.stored(user)
    subscriptions = state.get("subscriptions") or {}
    history = state.get("subscription_discovery") or {}
    moment = now or datetime.now(UTC)
    result: list[list[dict]] = []
    for kind in ("journals", "scholars"):
        due = []
        for entry in subscriptions.get(kind) or []:
            if not isinstance(entry, dict) or not str(entry.get("name") or "").strip():
                continue
            key = _subscription_key(kind, entry)
            last = curation._coerce_datetime((history.get(key) or {}).get("attempted_at"))
            if last is None or (moment - last).total_seconds() >= settings.feed_refresh_interval_seconds:
                due.append(entry)
        result.append(due[:MAX_SUBSCRIPTION_TARGETS])
    return result[0], result[1]


def _subscription_key(kind: str, entry: dict) -> str:
    return f"{kind}:{str(entry.get('key') or entry.get('name') or '').strip().casefold()}"


async def _discover_subscription_batch(
    db: AsyncSession, *, user: User, journals: list[dict], scholars: list[dict],
) -> int:
    user_key = str(user.id)
    if user_key in _subscription_inflight:
        return 0
    _subscription_inflight.add(user_key)
    archived = 0
    try:
        for kind, entries in (("journals", journals), ("scholars", scholars)):
            for entry in entries[:MAX_SUBSCRIPTION_TARGETS]:
                key = _subscription_key(kind, entry)
                _subscription_active_targets[user_key] = kind
                started = datetime.now(UTC).isoformat()
                logger.info("Subscription discovery started: user=%s target=%s", user.id, key)
                found, error = 0, None
                try:
                    found = await asyncio.wait_for(
                        discover_for_subscriptions(
                            db, user=user,
                            journals=[entry] if kind == "journals" else [],
                            scholars=[entry] if kind == "scholars" else [],
                        ), timeout=SUBSCRIPTION_TARGET_TIMEOUT_SECONDS,
                    )
                    # 原来的即时采集缺 commit，投影到feed_items的结果会被回滚。
                    await db.commit()
                except Exception as exc:
                    await db.rollback()
                    error = f"{type(exc).__name__}: {exc}"[:500]
                    logger.warning("Subscription discovery failed: target=%s error=%s", key, error)
                # 重新读取偏好，保留检索期间用户修改的订阅和课题挖掘结果。
                await db.refresh(user)
                preferences = dict(user.preferences or {})
                feed = dict(preferences.get("feed") or {})
                history = dict(feed.get("subscription_discovery") or {})
                previous = dict(history.get(key) or {})
                previous.update(attempted_at=started, archived=found, error=error)
                if error is None:
                    previous["succeeded_at"] = datetime.now(UTC).isoformat()
                history[key] = previous
                feed["subscription_discovery"] = history
                preferences["feed"] = feed
                user.preferences = preferences
                await db.commit()
                archived += found
                logger.info("Subscription discovery finished: target=%s archived=%d error=%s", key, found, error)
    finally:
        _subscription_active_targets.pop(user_key, None)
        _subscription_inflight.discard(user_key)
    if archived:
        await complete_subscription_assets_for_user(
            user_key, batches=10, journal_names=[str(x.get("name") or "") for x in journals]
        )
    return archived


def subscription_task_state(user_id: object) -> str:
    key = str(user_id)
    if key in _subscription_asset_inflight:
        return "preparing"
    if key in _subscription_inflight:
        return "searching"
    return "idle"


def is_subscription_refreshing(user_id: object) -> bool:
    key = str(user_id)
    return key in _subscription_inflight or key in _subscription_asset_inflight


async def complete_subscription_assets_for_user(
    user_id: object, *, batches: int = 3, journal_names: list[str] | None = None, project_only: bool = False, catalog_keys: list[str] | None = None
) -> dict[str, int]:
    """Complete subscribed indexes in small batches and publish each batch."""
    from app.database import get_session_factory
    from app.services.feed.literature_projection import project_literature_catalog
    from app.services.feed.abstract_backfill import backfill_subscription_abstracts
    from nodes.literature.tools.archive_papers import complete_missing_figures
    from nodes.literature.tools.scihub_fetcher import paper_cache_dir

    key = str(user_id)
    if key in _subscription_asset_inflight:
        return {"candidates": 0, "completed": 0, "failed": 0, "abstract_found": 0, "abstract_attempted": 0}
    _subscription_asset_inflight.add(key)
    totals = {"candidates": 0, "completed": 0, "failed": 0, "abstract_found": 0, "abstract_attempted": 0}
    try:
        for batch_number in range(1, max(1, batches) + 1):
            completed_keys: list[str] = []
            batch_started = time.monotonic()
            abstracts = await backfill_subscription_abstracts(
                key,
                journal_names=journal_names,
                limit=settings.literature_asset_completion_max_per_round,
                project_only=project_only, catalog_keys=catalog_keys, updated_keys=completed_keys,
            )
            abstract_finished = time.monotonic()
            totals["abstract_found"] += abstracts["found"]
            totals["abstract_attempted"] += abstracts["candidates"]
            result = await asyncio.to_thread(
                complete_missing_figures,
                str(paper_cache_dir()),
                limit=settings.literature_asset_completion_max_per_round,
                budget_seconds=settings.literature_asset_completion_budget_seconds,
                profile_user_id=key,
                journal_names=journal_names,
                project_only=project_only, catalog_keys=catalog_keys, completed_keys=completed_keys,
                concurrency=settings.literature_asset_completion_concurrency,
            )
            images_finished = time.monotonic()
            for field in totals:
                totals[field] += int(result.get(field) or 0)
            if int(result.get("completed") or 0) or abstracts["candidates"]:
                async with get_session_factory()() as db:
                    await project_literature_catalog(
                        db, limit=max(50, len(completed_keys)), profile_user_id=key,
                        catalog_keys=completed_keys,
                    )
                    await db.commit()
            if result.get("candidates") or abstracts["candidates"]:
                logger.info(
                    "Subscription asset batch: user=%s batch=%d images=%d abstracts=%d abstract_seconds=%.2f image_seconds=%.2f projection_seconds=%.2f total_seconds=%.2f",
                    key, batch_number, int(result.get("completed") or 0),
                    int(abstracts.get("found") or 0),
                    abstract_finished - batch_started, images_finished - abstract_finished,
                    time.monotonic() - images_finished, time.monotonic() - batch_started,
                )
            # No new asset or abstract: respect source cooldown before retrying.
            if not result.get("completed") and not abstracts["found"]:
                break
        if totals["candidates"] or totals["abstract_found"]:
            logger.info("Subscription asset completion: user=%s result=%s", key, totals)
    finally:
        _subscription_asset_inflight.discard(key)
    return totals


async def _subscription_asset_keys(db: AsyncSession, user: User) -> list[str]:
    """Enroll current subscriptions and interleave their pending asset queues."""
    import json
    import sqlite3
    from collections import deque
    from app.models.feed import FeedItem, FeedItemKind, FeedVisibility
    from app.services.feed import profile as profiles, ranking
    from app.services.feed.literature_projection import literature_papers_root, project_literature_catalog
    from nodes.literature.tools.archive_papers import archive_indexes_only

    profile = await profiles.build_profile(db, user=user)
    if not (profile.domains or profile.journals or profile.scholars or profile.projects):
        return []
    rows = (await db.execute(select(FeedItem).where(
        FeedItem.kind == FeedItemKind.PAPER,
        FeedItem.visibility == FeedVisibility.PLATFORM,
        ranking.publication_window_clause(datetime.now(UTC)),
    ).order_by(ranking.recent_order(), FeedItem.id))).scalars().all()
    lanes = [deque(), deque(), deque(), deque()]
    pending = set()
    for item in rows:
        extra = item.extra or {}
        key = str(extra.get("catalog_key") or "").lower()
        if not key or extra.get("literature_eligible") is not True or ranking.is_journal_notice(item):
            continue
        project_member = (bool(profile.projects)
                          and str(user.id) in (extra.get("profile_user_ids") or [])
                          and bool(ranking.acquisition_routes(item) & {"project_profile", "web"})
                          and (profile.active_project_queries is None
                               or str(extra.get("discovery_query") or "") in profile.active_project_queries))
        membership = (bool(ranking._domain_hits(item, profile)),
                      ranking.matches_subscribed_journal(item, profile),
                      ranking.matches_subscribed_scholar(item, profile), project_member)
        for lane, hit in zip(lanes, membership):
            if hit:
                lane.append(key)
        if any(membership) and extra.get("literature_preparation") != "ready":
            pending.add(key)
    keys, seen = [], set()
    while any(lanes):
        for lane in lanes:
            while lane and lane[0] in seen:
                lane.popleft()
            if lane:
                key = lane.popleft()
                keys.append(key)
                seen.add(key)
    root = literature_papers_root()
    def enroll() -> tuple[list[str], list[str]]:
        catalog = root / "literature_catalog.sqlite3"
        if not catalog.is_file():
            return [], []
        records, completed, work = [], [], []
        now = datetime.now(UTC)
        with sqlite3.connect(catalog) as conn:
            for key in keys:
                row = conn.execute("SELECT metadata_json FROM papers WHERE doi=?", (key,)).fetchone()
                if not row:
                    continue
                record = json.loads(row[0])
                if key in pending and record.get("literature_preparation") == "ready":
                    completed.append(key)
                needs_assets = record.get("literature_preparation") != "ready"
                needs_abstract = not str(record.get("abstract") or "").strip() and record.get("abstract_lookup_completed") is not True
                retry_at = record.get("abstract_lookup_retry_after")
                if needs_abstract and retry_at:
                    try:
                        deadline = datetime.fromisoformat(str(retry_at))
                        needs_abstract = deadline <= now if deadline.tzinfo else deadline.replace(tzinfo=UTC) <= now
                    except ValueError:
                        pass
                if needs_assets or needs_abstract:
                    work.append(key)
                owners = list(record.get("profile_user_ids") or [])
                if str(user.id) not in owners:
                    record["profile_user_ids"] = [*owners, str(user.id)]
                    records.append(record)
        if records:
            archive_indexes_only(records, root)
        return completed, work
    completed, work = await asyncio.to_thread(enroll)
    if completed:
        await project_literature_catalog(db, limit=len(completed), catalog_keys=completed)
        await db.commit()
    return work


async def run_subscription_asset_round() -> dict[str, int]:
    """Resume pending subscription assets on scheduler ticks and after restarts."""
    from app.database import get_session_factory
    factory = get_session_factory()
    async with factory() as db:
        user_ids = list((await db.execute(
            select(User.id).where(User.is_active.is_(True))
        )).scalars().all())
    totals = {"users": 0, "completed": 0}
    for user_id in user_ids:
        if str(user_id) in _subscription_inflight or str(user_id) in _subscription_asset_inflight:
            continue
        async with factory() as db:
            user = await db.get(User, user_id)
            catalog_keys = await _subscription_asset_keys(db, user) if user is not None else []
        if not catalog_keys:
            continue
        result = await complete_subscription_assets_for_user(user_id, catalog_keys=catalog_keys)
        if result["candidates"] or result["abstract_attempted"]:
            totals["users"] += 1
            totals["completed"] += result["completed"] + result["abstract_found"]
        if totals["users"] >= MAX_USERS_PER_ROUND:
            break
    return totals


async def run_project_asset_round() -> dict[str, int]:
    """Resume abstract completion for project discoveries without subscriptions."""
    from app.database import get_session_factory
    from app.services.feed.abstract_backfill import backfill_subscription_abstracts
    from app.services.feed.literature_projection import project_literature_catalog

    factory = get_session_factory()
    async with factory() as db:
        user_ids = list((await db.execute(
            select(User.id).where(User.is_active.is_(True))
        )).scalars().all())
    totals = {"users": 0, "attempted": 0, "found": 0}
    for user_id in user_ids:
        result = await complete_subscription_assets_for_user(user_id, batches=1, project_only=True)
        attempts = result.get("candidates", 0) + result.get("abstract_attempted", 0)
        if not attempts:
            continue
        totals["users"] += 1
        totals["attempted"] += attempts
        totals["found"] += result.get("abstract_found", 0)
        logger.info("Project asset completion: user=%s result=%s", user_id, result)
        if totals["users"] >= MAX_USERS_PER_ROUND:
            break
    return totals


async def run_subscription_round() -> dict[str, int]:
    """每天刷新已订阅的期刊和学者，不依赖课题模型或再次点击关注。"""
    from app.database import get_session_factory

    totals = {"users": 0, "found": 0}
    if not settings.feed_external_fetch_enabled:
        return totals
    factory = get_session_factory()
    async with factory() as db:
        user_ids = list((await db.execute(select(User.id).where(User.is_active.is_(True)))).scalars().all())
    for user_id in user_ids:
        async with factory() as db:
            user = await db.get(User, user_id)
            if user is None:
                continue
            journals, scholars = due_subscription_targets(user)
            if (not journals and not scholars) or str(user.id) in _subscription_inflight:
                continue
            totals["found"] += await _discover_subscription_batch(
                db, user=user, journals=journals, scholars=scholars,
            )
            totals["users"] += 1
            if totals["users"] >= MAX_USERS_PER_ROUND:
                break
    if totals["users"]:
        logger.info("Daily subscription discovery: %s", totals)
    return totals


async def discover_subscriptions_for_user(
    user_id: object,
    *,
    journals: list[dict[str, object]] | None = None,
    scholars: list[dict[str, object]] | None = None,
) -> int:
    """后台线程里跑一次订阅采集。

    **必须自己开会话**：调用它的是一次 HTTP 请求，响应发出去之后那个会话就关
    了，拿它去跑一个几十秒的采集只会在半路炸掉。
    """
    import uuid as _uuid

    from app.database import get_session_factory

    key = str(user_id)
    factory = get_session_factory()
    async with factory() as db:
        user = await db.get(User, key)
        if user is None:
            return 0
        try:
            return await _discover_subscription_batch(
                db, user=user, journals=journals or [], scholars=scholars or []
            )
        except Exception:
            logger.warning("Subscription discovery crashed for %s", user_id, exc_info=True)
            return 0


def request_subscription_discovery(
    *,
    user_id: object,
    journals: list[dict[str, object]] | None = None,
    scholars: list[dict[str, object]] | None = None,
) -> bool:
    """从 HTTP 请求里挂一个后台订阅采集，不阻塞这次响应。

    与 `literature_harvester.request_subscription_refresh` 同一个模式（也是
    同一个理由：用户点一下"关注"就让他等几十秒采完，是不可接受的）。
    """
    if not journals and not scholars:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    loop.create_task(
        discover_subscriptions_for_user(user_id, journals=journals, scholars=scholars),
        name=f"feed-subscription-discovery-{user_id}",
    )
    return True


__all__ = [
    "curate_one",
    "discover_for_subscriptions",
    "discover_subscriptions_for_user",
    "due_users",
    "is_due",
    "request_subscription_discovery",
    "role_available",
    "run_round",
    "bridge",
]
