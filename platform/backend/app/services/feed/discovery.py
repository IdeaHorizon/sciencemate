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
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.user import User
from app.services.feed import bridge, collectors, curation, digest

logger = logging.getLogger(__name__)

#: 同一个用户多久挖一次。课题没变时，一天一次已经足够 —— arXiv 本来也是
#: 一天一更，更密只是重复付钱。
CURATION_INTERVAL_HOURS = 24

#: 一轮最多伺候几个用户。开关是逐个用户开的，用户多起来之后一轮全跑完会
#: 把一次 tick 拖很长；分批意味着每个人最迟下一轮轮到。
MAX_USERS_PER_ROUND = 5

#: 每个用户每轮最多跑五组检索；每组由确定性的多源检索器执行。
MAX_QUERIES_PER_USER = 5

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
    raw = curation.stored(user).get("curated_at")
    if not isinstance(raw, str) or not raw:
        return True
    try:
        last = _aware(datetime.fromisoformat(raw))
    except ValueError:
        return True
    if last is None:
        return True
    moment = now or datetime.now(UTC)
    return (moment - last) >= timedelta(hours=CURATION_INTERVAL_HOURS)


async def due_users(db: AsyncSession) -> list[User]:
    """开着开关、且到点了的用户。

    过滤在 Python 里做而不是 SQL：开关和上次时间都在 `preferences` 这个
    JSON 袋子里，各方言的 JSON 查询写法不通用，而用户数在这个量级上无所谓。
    """
    rows = await db.execute(select(User).where(User.is_active.is_(True)))
    return [u for u in rows.scalars().all() if is_due(u)][:MAX_USERS_PER_ROUND]


async def _targeted_search(
    _client: httpx.AsyncClient,
    db: AsyncSession,
    *,
    user: User,
    queries: list[str],
    news_queries: list[str] | None = None,
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
        publication_datetime,
    )
    from nodes.literature.tools.archive_papers import archive_index_and_figure
    from nodes.literature.tools.filter import compute_relevance_score
    from nodes.literature.tools.scihub_fetcher import paper_cache_dir
    from nodes.literature.tools.search_engines import Paper, SearchManager
    from shared.tools.web import _web_search

    manager = SearchManager()
    cutoff = datetime.now(UTC) - timedelta(days=7)
    sources = {
        "arxiv", "biorxiv", "medrxiv", "semantic_scholar",
        "pubmed", "crossref", "openalex",
    }
    output_dir = str(paper_cache_dir())
    archived = 0
    for query in queries[:MAX_QUERIES_PER_USER]:
        try:
            papers = await manager.search_all(
                query,
                max_per_source=SEARCH_RESULTS_PER_QUERY,
                enabled_sources=sources,
                date_from=cutoff.date().isoformat(),
                include_local_catalog=False,
                use_query_cache=False,
            )
        except Exception as exc:  # 一个词组失败不带走其余词组
            logger.info("Project-profile search %r failed for %s: %s", query, user.id, exc)
            continue
        for paper in papers:
            published = publication_datetime(
                getattr(paper, "pub_date", ""), getattr(paper, "year", None)
            )
            if published is None or published < cutoff:
                continue
            relevance = compute_relevance_score(paper, query)
            if relevance < MIN_QUERY_RELEVANCE:
                continue
            record = paper.to_dict() if hasattr(paper, "to_dict") else dict(paper)
            record["feed_acquisition_routes"] = ["project_profile"]
            record["discovery_query"] = query
            record["profile_query_relevance"] = relevance
            record["profile_user_ids"] = [str(user.id)]
            record["xiaohongshu_eligible"] = True
            record["classification_mode"] = "project_profile"
            # project_profile 路线的论文**不标订阅学科**：订阅是 journal 路线
            # （本领域动态）的边界，而 project 检索靠课题匹配（profile_user_ids
            # + matched_terms）进推荐，不依赖 domains。把它标成订阅学科会把
            # 「车辆动力学」关键词捞到的流体力学论文错挂到「专门史」下面。
            record["second_level_domains"] = []
            record["harvest_batch"] = datetime.now(UTC).date().isoformat()
            try:
                await asyncio.to_thread(archive_index_and_figure, record, output_dir)
                archived += 1
            except Exception:
                logger.warning(
                    "Project-profile asset archive failed for %r",
                    getattr(paper, "title", ""),
                    exc_info=True,
                )
        await asyncio.sleep(collectors_politeness())

    # 第三条路线：课题相关的「资讯/新闻」检索 —— 用资讯词（news_queries），
    # 与论文检索词（queries）分开。资讯词面向新闻/政策/产业动态（中文优先），
    # 不是论文。没给资讯词就退回论文词，至少还能捞到技术博客这类非论文网页。
    # 模型不直接持有联网工具；平台保存URL和搜索后端，保证结果可核验。
    web_queries = [q for q in (news_queries or []) if q] or [q for q in queries if q]
    web_archived = 0
    for query in web_queries[:MAX_QUERIES_PER_USER]:
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
            candidate = Paper(
                title=title,
                abstract=snippet,
                url=url,
                doi=_doi_from_url(url),
                source="web",
                venue=urlparse(url).hostname or "",
                pub_type="web-resource",
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
                "second_level_domains": [],
                "harvest_batch": datetime.now(UTC).date().isoformat(),
                "discovered_at": datetime.now(UTC).isoformat(),
                "discovery_query": query,
                "profile_query_relevance": relevance,
                "web_search_backend": str(result.get("backend") or ""),
                "web_authority": _web_authority(url),
                "web_traceability": 1.0 if snippet else 0.5,
            })
            try:
                await asyncio.to_thread(archive_index_and_figure, record, output_dir)
                web_archived += 1
            except Exception:
                logger.warning("Web result archive failed for %r", title, exc_info=True)
        await asyncio.sleep(collectors_politeness())

    archived += web_archived
    logger.info(
        "Project-profile discovery for %s archived %d item(s), including %d web item(s)",
        user.id, archived, web_archived,
    )
    # FeedItem只是统一文献库的可重建投影；采集事实只存一份。
    if archived:
        await project_literature_catalog(db, limit=max(400, archived * 3))
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


async def curate_one(
    client: httpx.AsyncClient,
    db: AsyncSession,
    *,
    user: User,
    force: bool = False,
    allow_disabled: bool = False,
    selected_domains: list[str] | None = None,
) -> dict[str, int]:
    """替一个用户跑完整轮挖掘。失败只落在他自己的偏好里。"""
    result = await curation.curate(
        db,
        user=user,
        force=force,
        allow_disabled=allow_disabled,
        selected_domains=selected_domains,
    )
    if result is None:
        # 可能是不需要（课题没变）、也可能是不可用（没配模型）。两者
        # `curation` 已经分别记过账，这里不再二次归因。
        return {"domains": 0, "found": 0, "digests": 0}

    found = await _targeted_search(
        client, db, user=user, queries=result["queries"],
        news_queries=result.get("news_queries"),
    )
    digests = await _prebuild_digests(db, user=user)
    return {"domains": len(result["domains"]), "found": found, "digests": digests}


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
SUBSCRIPTION_WINDOW_DAYS = 30

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


async def _papers_by_author(name: str, *, since: str) -> list:
    """按**作者字段**检索一个学者的论文（Crossref / OpenAlex / PubMed）。

    ## 为什么不复用 search_all

    `search_all` 是关键词检索："Shaoda Liu" 会被当成两个词去匹配标题和摘要，
    于是"标题里有 Shaoda、作者里有另一个 Liu"的文章也全回来了。实测那批结果
    里**没有一条**的作者是订阅的那位 —— 它们在送达时被作者比对拒掉，用户看到
    的就是"订阅了学者，永远没内容"，而采集日志还显示 archived N 条。

    三家的作者检索都是各自的一等能力：Crossref 的 `query.author`、OpenAlex 的
    `raw_author_name.search`、PubMed 的 `[Author]` 限定符。这里直接用它们，
    并且**只保留作者确实命中的结果** —— 采集宽、准入严，两边同一套比对。
    """
    from app.services.feed.profile import author_matches
    from nodes.literature.tools.search_engines import Paper

    def _keep(paper: object) -> bool:
        return any(
            author_matches(name, author)
            for author in (getattr(paper, "authors", None) or [])
        )

    found: list = []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        # ── Crossref ──
        try:
            resp = await client.get(
                "https://api.crossref.org/works",
                params={
                    "query.author": name,
                    "rows": AUTHOR_RESULTS_PER_SOURCE,
                    "sort": "published",
                    "order": "desc",
                    "filter": f"type:journal-article,from-pub-date:{since}",
                },
                headers={"User-Agent": "SurveyHarness/1.0"},
                timeout=30,
            )
            resp.raise_for_status()
            for item in (resp.json().get("message") or {}).get("items", []):
                authors = [
                    " ".join(filter(None, [a.get("given", ""), a.get("family", "")])).strip()
                    for a in item.get("author", [])
                ]
                title = " ".join((item.get("title") or [""])[0].split())
                if not title:
                    continue
                issued = (item.get("issued") or {}).get("date-parts") or [[]]
                year = issued[0][0] if issued and issued[0] else None
                paper = Paper(
                    title=title,
                    abstract=_strip_markup(item.get("abstract")),
                    authors=[a for a in authors if a],
                    year=year,
                    venue=" ".join((item.get("container-title") or [""])[:1]).strip(),
                    doi=str(item.get("DOI") or ""),
                    source="crossref",
                    url=str(item.get("URL") or ""),
                    pub_type=str(item.get("type") or "journal-article"),
                    pub_date="-".join(str(v) for v in (issued[0] if issued else []) if v),
                )
                if _keep(paper):
                    found.append(paper)
        except Exception as exc:
            logger.info("Crossref author search failed for %r: %s", name, exc)

        # ── OpenAlex ──
        try:
            params = {
                "filter": f"raw_author_name.search:{name},from_publication_date:{since}",
                "per-page": AUTHOR_RESULTS_PER_SOURCE,
                "sort": "publication_date:desc",
            }
            # OpenAlex 的 api_key 从环境变量取（与检索引擎同一处，不新开配置面）。
            if os.environ.get("OPENALEX_API_KEY"):
                params["api_key"] = os.environ["OPENALEX_API_KEY"]
            resp = await client.get(
                "https://api.openalex.org/works",
                params=params,
                headers={"User-Agent": "SurveyHarness/1.0", "Accept": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            for item in resp.json().get("results", []):
                authors = [
                    ((a.get("author") or {}).get("display_name") or "").strip()
                    for a in item.get("authorships", [])
                ]
                title = " ".join(str(item.get("title") or "").split())
                if not title:
                    continue
                paper = Paper(
                    title=title,
                    abstract=_openalex_abstract(item.get("abstract_inverted_index")),
                    authors=[a for a in authors if a],
                    year=item.get("publication_year"),
                    venue=str(((item.get("primary_location") or {}).get("source") or {}).get("display_name") or ""),
                    doi=str(item.get("doi") or "").removeprefix("https://doi.org/"),
                    source="openalex",
                    url=str(item.get("doi") or ""),
                    citations=int(item.get("cited_by_count") or 0),
                    pub_date=str(item.get("publication_date") or ""),
                )
                if _keep(paper):
                    found.append(paper)
        except Exception as exc:
            logger.info("OpenAlex author search failed for %r: %s", name, exc)

        # ── arXiv（au: 作者字段，摘要总是有）──
        try:
            import xml.etree.ElementTree as ET

            resp = await client.get(
                "http://export.arxiv.org/api/query",
                params={
                    "search_query": f'au:"{name}"',
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                    "max_results": AUTHOR_RESULTS_PER_SOURCE,
                },
                timeout=30,
            )
            resp.raise_for_status()
            ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
            for entry in ET.fromstring(resp.text).findall("a:entry", ns):
                title = " ".join((entry.findtext("a:title", "", ns) or "").split())
                if not title:
                    continue
                published = (entry.findtext("a:published", "", ns) or "")[:10]
                if published and published < since:
                    continue
                authors = [
                    (node.findtext("a:name", "", ns) or "").strip()
                    for node in entry.findall("a:author", ns)
                ]
                paper = Paper(
                    title=title,
                    abstract=" ".join((entry.findtext("a:summary", "", ns) or "").split()),
                    authors=[a for a in authors if a],
                    year=int(published[:4]) if published else None,
                    venue="arXiv",
                    doi=(entry.findtext("arxiv:doi", "", ns) or "").strip(),
                    source="arxiv",
                    url=(entry.findtext("a:id", "", ns) or "").strip(),
                    pub_type="preprint",
                    pub_date=published,
                )
                if _keep(paper):
                    found.append(paper)
        except Exception as exc:
            logger.info("arXiv author search failed for %r: %s", name, exc)

        # ── PubMed（[Author] 限定符；用 efetch 拿摘要，esummary 不带摘要）──
        try:
            import xml.etree.ElementTree as ET

            resp = await client.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                params={
                    "db": "pubmed",
                    "term": f"{name}[Author] AND {since.replace('-', '/')}:3000[Date - Publication]",
                    "retmode": "json",
                    "retmax": AUTHOR_RESULTS_PER_SOURCE,
                    "sort": "date",
                },
                timeout=30,
            )
            resp.raise_for_status()
            ids = (resp.json().get("esearchresult") or {}).get("idlist", [])
            if ids:
                fetch = await client.get(
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                    params={"db": "pubmed", "id": ",".join(ids), "retmode": "xml"},
                    timeout=30,
                )
                fetch.raise_for_status()
                root = ET.fromstring(fetch.text)
                for article in root.findall(".//PubmedArticle"):
                    title_node = article.find(".//ArticleTitle")
                    title = " ".join("".join(title_node.itertext()).split()) if title_node is not None else ""
                    if not title:
                        continue
                    authors = []
                    for node in article.findall(".//AuthorList/Author"):
                        collective = node.findtext("CollectiveName")
                        if collective:
                            authors.append(collective.strip())
                            continue
                        parts = [
                            (node.findtext("ForeName") or "").strip(),
                            (node.findtext("LastName") or "").strip(),
                        ]
                        joined = " ".join(p for p in parts if p).strip()
                        if joined:
                            authors.append(joined)
                    abstract = " ".join(
                        " ".join("".join(chunk.itertext()).split())
                        for chunk in article.findall(".//Abstract/AbstractText")
                    ).strip()
                    pmid = article.findtext(".//PMID") or ""
                    year_text = article.findtext(".//JournalIssue/PubDate/Year") or ""
                    month_text = article.findtext(".//JournalIssue/PubDate/Month") or "1"
                    day_text = article.findtext(".//JournalIssue/PubDate/Day") or "1"
                    months = {
                        "Jan": "1", "Feb": "2", "Mar": "3", "Apr": "4", "May": "5", "Jun": "6",
                        "Jul": "7", "Aug": "8", "Sep": "9", "Oct": "10", "Nov": "11", "Dec": "12",
                    }
                    month = months.get(month_text[:3].title(), month_text) if not month_text.isdigit() else month_text
                    pub_date = f"{year_text}-{month}-{day_text}" if year_text else ""
                    paper = Paper(
                        title=title.rstrip("."),
                        abstract=abstract,
                        authors=authors,
                        year=int(year_text) if year_text.isdigit() else None,
                        venue=(article.findtext(".//Journal/Title") or "").strip(),
                        doi=next(
                            (str(node.text).strip() for node in article.findall(".//ArticleId")
                             if node.get("IdType") == "doi" and node.text),
                            "",
                        ),
                        source="pubmed",
                        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                        pub_type="journal-article",
                        pub_date=pub_date,
                    )
                    if _keep(paper):
                        found.append(paper)
        except Exception as exc:
            logger.info("PubMed author search failed for %r: %s", name, exc)

    return found


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
        publication_datetime,
    )
    from nodes.literature.tools.archive_papers import archive_index_and_figure
    from nodes.literature.tools.filter import compute_relevance_score
    from nodes.literature.tools.scihub_fetcher import paper_cache_dir
    from nodes.literature.tools.search_engines import Paper, SearchManager
    from shared.tools.web import _web_search

    targets: list[tuple[str, str, str]] = []  # (route, query, display)
    for entry in (journals or [])[:MAX_SUBSCRIPTION_TARGETS]:
        name = " ".join(str(entry.get("name") or "").split())
        if name:
            targets.append(("journal", name, name))
    for entry in (scholars or [])[:MAX_SUBSCRIPTION_TARGETS]:
        name = " ".join(str(entry.get("name") or "").split())
        if name:
            targets.append(("scholar", name, name))
    if not targets:
        return 0

    harness_root = settings.harness_root or str(Path(__file__).resolve().parents[5])
    if harness_root not in sys.path:
        sys.path.insert(0, harness_root)

    manager = SearchManager()
    cutoff = datetime.now(UTC) - timedelta(days=SUBSCRIPTION_WINDOW_DAYS)
    sources = {
        "arxiv", "biorxiv", "medrxiv", "semantic_scholar",
        "pubmed", "crossref", "openalex",
    }
    output_dir = str(paper_cache_dir())
    archived = 0
    for route, query, display in targets:
        # ── 学术数据库 ──
        # 学者走**作者字段**检索（Crossref query.author / OpenAlex raw_author_name
        # / PubMed [Author]），期刊走关键词/ISSN 检索。把姓名当关键词搜会捞回
        # 大量同名噪声，而且全在送达时被拒 —— 表现为"订阅了学者永远没内容"。
        if route == "scholar":
            papers = await _papers_by_author(display, since=cutoff.date().isoformat())
        else:
            try:
                papers = await manager.search_all(
                    query,
                    max_per_source=SEARCH_RESULTS_PER_QUERY,
                    enabled_sources=sources,
                    date_from=cutoff.date().isoformat(),
                    include_local_catalog=False,
                    use_query_cache=False,
                )
            except Exception as exc:  # 一个订阅目标失败不带走其余
                logger.info("Subscription search %r failed for %s: %s", query, user.id, exc)
                papers = []
        for paper in papers:
            published = publication_datetime(
                getattr(paper, "pub_date", ""), getattr(paper, "year", None)
            )
            if published is None or published < cutoff:
                continue
            # 学者路线不复核"姓名和标题的词面重叠"：作者命中就是最强的相关性
            # 证据，而一篇论文的标题本来就不该出现作者的名字（会被词面闸门全杀）。
            if route == "scholar":
                relevance = MIN_QUERY_RELEVANCE
            else:
                relevance = compute_relevance_score(paper, query)
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
            record["profile_user_ids"] = [str(user.id)]
            record["xiaohongshu_eligible"] = True
            record["classification_mode"] = f"subscription_{route}"
            # 与 project_profile 同样的理由：订阅路线的论文不标订阅学科，
            # 避免把跨学科期刊/学者的成果错挂到某个二级学科下面。
            record["second_level_domains"] = []
            record["harvest_batch"] = datetime.now(UTC).date().isoformat()
            try:
                await asyncio.to_thread(archive_index_and_figure, record, output_dir)
                archived += 1
            except Exception:
                logger.warning(
                    "Subscription asset archive failed for %r",
                    getattr(paper, "title", ""),
                    exc_info=True,
                )

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
                candidate = Paper(
                    title=title,
                    abstract=snippet,
                    url=url,
                    doi=_doi_from_url(url),
                    source="web",
                    venue=urlparse(url).hostname or "",
                    pub_type="web-resource",
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
                })
                try:
                    await asyncio.to_thread(archive_index_and_figure, record, output_dir)
                    archived += 1
                except Exception:
                    logger.warning("Subscription web archive failed for %r", title, exc_info=True)
        await asyncio.sleep(collectors_politeness())

    logger.info(
        "Subscription discovery for %s archived %d item(s) across %d target(s)",
        user.id, archived, len(targets),
    )
    if archived:
        await project_literature_catalog(db, limit=max(400, archived * 3))
    return archived


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

    key: object = user_id
    try:
        key = _uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError):
        pass
    factory = get_session_factory()
    async with factory() as db:
        user = await db.get(User, key)
        if user is None:
            return 0
        try:
            return await discover_for_subscriptions(
                db, user=user, journals=journals, scholars=scholars
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
