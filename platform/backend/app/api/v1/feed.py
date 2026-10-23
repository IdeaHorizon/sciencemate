"""科研资讯流 —— 知道你在研究什么的领域日报。"""

from __future__ import annotations

from app.services.feed.profile import journal_display_name

import logging
import asyncio
import mimetypes
import re
import sqlite3
import httpx
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import String, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import assembly
from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models.feed import (
    FeedEngagement,
    FeedEngagementAction,
    FeedItem,
    FeedItemKind,
    FeedSource,
    FeedVisibility,
)
from app.models.project import Project
from app.models.user import User

from app.schemas.feed import (
    CurationOut,
    CurationWrite,
    DomainCatalogOut,
    DomainGroup,
    DomainOption,
    EngagementWrite,
    FeedCard,
    FeedItemOut,
    FeedStatusOut,
    FeedTodayOut,
    FeedProgressOut,
    InterestsOut,
    InterestsWrite,
    JournalSubscription,
    ScholarSubscription,
    SubscriptionsOut,
    SubscriptionsWrite,
    SubscriptionSearchOut,
    SharedLinkWrite,
    SourceHealthOut,
)
from app.services.feed import (
    canonical,
    curation,
    digest,
    discovery,
    domains as domain_service,
    ranking,
)
from app.services.feed import bridge as feed_bridge
from app.services.feed.copy import t
from app.services.feed import profile as profile_service
from app.services.feed import thumbnail
from app.services.feed.image_proxy import ImageUnavailable, fetch as fetch_image
from app.services.feed.literature_projection import literature_papers_root
from app.services.feed.link_preview import LinkRejected, fetch_preview
from app.services.harness_contract import HarnessContractUnavailable
from app.services import harness_kb
from app.services.user_interface import Language, language_for, language_on_screen, say

logger = logging.getLogger(__name__)

router = APIRouter()

#: 每批推荐条数；候选池仍由 ranking.candidate_items 的窗口限制。
FEED_PAGE_SIZE = 21


def _local_literature_figure(item: FeedItem) -> Path | None:
    """Return an already archived literature figure for a feed item's DOI.

    The feed request must never trigger a publisher/PDF download.  The harvester
    prepares these assets in the background; this helper only performs a catalog
    lookup and serves a file that is already on the shared literature volume.
    """
    extra = item.extra if isinstance(item.extra, dict) else {}
    catalog_key = str(extra.get("catalog_key") or extra.get("doi") or "").strip().lower()
    if not catalog_key:
        match = re.search(r"10\.\d{4,9}/[^\s?#]+", unquote(str(item.url or "")), re.I)
        if not match:
            return None
        catalog_key = match.group(0).rstrip(".,;)").lower()
    root = literature_papers_root()
    catalog = root / "literature_catalog.sqlite3"
    if not catalog.is_file():
        return None
    try:
        with sqlite3.connect(catalog) as conn:
            row = conn.execute(
                "SELECT figure_path FROM papers WHERE lower(doi)=?", (catalog_key,)
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    try:
        figure = Path(row[0]).resolve()
        # 「标题卡」(title_card) 是 harvester 拿不到真图时生成的自绘占位图：
        # 纯色底 + 描边框 + 标题，和论文插图样式割裂。feed 自己在三级供给里
        # 画「生成封面」(generated_cover)——渐变底、加粗标题、来源，全站统一。
        # 两个占位方案并存会让卡片图一半描边框、一半渐变封面。所以这里只认
        # 真实插图，title_card 回落到 generated_cover。判据取文件名后缀
        # `_title_card`：这是 generate_title_card 唯一且固定的输出命名，不像
        # metadata 里的 fallback/method 字段在历史数据里并不同时存在（查
        # `$.image.fallback` 会漏掉旧数据，且 NULL 会整行误判）。
        if "_title_card" in figure.name:
            return None
        if not figure.is_file() or not figure.is_relative_to(root):
            return None
        if not _valid_figure(figure):
            return None
        return figure
    except (OSError, ValueError):
        return None


def _image_media_type(path: Path) -> str:
    """Return a browser-safe MIME type for an archived image file."""
    guessed, _encoding = mimetypes.guess_type(path.name)
    return guessed if guessed and guessed.startswith("image/") else "application/octet-stream"


#: 本地 literature figure 至少要这个尺寸才配当卡片配图（排除 favicon / logo）。
_MIN_FIGURE_WIDTH = 100
_MIN_FIGURE_HEIGHT = 60


def _valid_figure(path: Path) -> bool:
    """本地 literature figure 必须是真实位图、且够大，才配当卡片配图。

    harvester 从着陆页抓图时可能把一个 SVG（期刊 logo / 图标，常是 80×80）
    按 ``.png`` 落盘（``method=html_meta_or_img`` 且无尺寸校验），Pillow 打不开
    这种「SVG 文本冒充 PNG」。同理，过小的图标（favicon 那类）也没有信息量。
    这里在 feed 侧兜底：打不开或太小的，一律不算有效插图，让条目回退到
    生成封面（generated_cover）。
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - 依赖缺失是部署问题，放宽不拦
        return True
    try:
        with Image.open(path) as im:
            width, height = im.size
    except Exception:
        return False
    return width >= _MIN_FIGURE_WIDTH and height >= _MIN_FIGURE_HEIGHT


def _eligible_feed_item(item: FeedItem) -> bool:
    """Fail closed for rebuildable literature projections."""
    extra = item.extra if isinstance(item.extra, dict) else {}
    return not extra.get("literature_catalog") or extra.get("literature_eligible") is True


def _labels(slugs: list, lang: str = "zh") -> list[str]:
    """域的人读名。词表读不到时返回 slug 本身，不让整个响应垮掉 ——
    这一层的失败方式应该是"标签不好看"，不是"资讯流打不开"。

    `lang` 来自这个人自己的界面设置（`services/user_interface`）。中英两套名
    都在 `core/domain_registry.py` 里，与 slug 同处一处 —— 平台不存第二份。
    """
    try:
        return [domain_service.label(str(s), lang=lang) for s in slugs or ()]
    except HarnessContractUnavailable:
        return [str(s) for s in slugs or ()]


def _to_out(
    item: FeedItem,
    *,
    source_names: dict[str, str] | None = None,
    author_names: dict[str, str] | None = None,
    saved_ids: frozenset[str] = frozenset(),
    opened_ids: frozenset[str] = frozenset(),
    lang: str = "zh",
) -> FeedItemOut:
    return FeedItemOut(
        id=str(item.id),
        kind=item.kind,
        title=item.title,
        url=item.url,
        summary=item.summary,
        authors=list(item.authors or []),
        venue=journal_display_name(item.venue) if item.venue else item.venue,
        published_at=item.published_at,
        domains=[str(s) for s in (item.domains or [])],
        domain_labels=_labels(item.domains, lang),
        source_name=(source_names or {}).get(item.source_id or ""),
        author_display_name=(author_names or {}).get(item.author_user_id or ""),
        saved=str(item.id) in saved_ids,
        viewed=str(item.id) in opened_ids,
        extra=item.extra if isinstance(item.extra, dict) else {},
    )


async def _attach_cached_translations(items: list[FeedItemOut]) -> list[FeedItemOut]:
    """Read the shared index cache once for this response, without a model call."""
    def load():
        import sys
        root = settings.harness_root or str(Path(__file__).resolve().parents[5])
        if root not in sys.path:
            sys.path.insert(0, root)
        from nodes.literature.tools.local_index import LocalPaperIndex
        from app.services.feed.literature_projection import literature_papers_root
        index = LocalPaperIndex(str(literature_papers_root() / "literature_catalog.sqlite3"))
        records = [{"title": item.title[:1000], "abstract": item.summary or ""} for item in items]
        index.hydrate_translations(records)
        return records
    if not items:
        return items
    records = await asyncio.to_thread(load)
    for item, record in zip(items, records):
        item.title_zh = record.get("title_zh")
        item.abstract_zh = record.get("abstract_zh")
    return items


async def _lookup_names(db: AsyncSession, items: list[FeedItem]) -> tuple[dict, dict]:
    """一次查完源名和作者名 —— 不要在渲染循环里逐条查库。"""
    source_ids = {i.source_id for i in items if i.source_id}
    author_ids = {i.author_user_id for i in items if i.author_user_id}
    sources: dict[str, str] = {}
    authors: dict[str, str] = {}
    if source_ids:
        rows = await db.execute(
            select(FeedSource.id, FeedSource.name).where(FeedSource.id.in_(source_ids))
        )
        sources = {row[0]: row[1] for row in rows}
    if author_ids:
        rows = await db.execute(
            select(User.id, User.display_name).where(User.id.in_(author_ids))
        )
        authors = {row[0]: row[1] for row in rows}
    return sources, authors


async def _saved_ids(db: AsyncSession, *, user_id: str) -> frozenset[str]:
    rows = (
        await db.execute(
            select(FeedEngagement.item_id).where(
                FeedEngagement.user_id == user_id,
                FeedEngagement.action == FeedEngagementAction.SAVE,
            )
        )
    ).scalars().all()
    return frozenset(rows)


async def _opened_ids(db: AsyncSession, *, user_id: str) -> frozenset[str]:
    rows = (
        await db.execute(
            select(FeedEngagement.item_id).where(
                FeedEngagement.user_id == user_id,
                FeedEngagement.action == FeedEngagementAction.OPEN,
            )
        )
    ).scalars().all()
    return frozenset(str(item_id) for item_id in rows)


def _audience_id(user: User) -> str:
    """org 可见内容的受众键 —— **不用** `governance_scope_for`。

    那个函数回答的是"这个人管得着谁"（权限），按角色分叉：一个普通研究员的
    scope 是 `individual`，id 就是他自己的 user_id。拿它当受众键，后果是
    研究员发的"组织可见"内容只有他自己看得到 —— 一个看起来在工作、实际上
    谁也送不到的分享按钮。

    受众要问的是"这个人和谁在一起"，那是他的隶属：先组后机构。
    """
    return user.group_id or user.institution_id


def _visible_clause(user: User):
    """能看见什么：全平台内容 + 自己组织的内容。

    收口在**查询**里，不在取回来之后过滤 —— 过滤写在后面意味着任何一处忘了
    加就是越权泄露，而那不会有任何症状。
    """
    return (FeedItem.visibility == FeedVisibility.PLATFORM) | (
        (FeedItem.visibility == FeedVisibility.ORGANIZATION)
        & (FeedItem.organization_id == _audience_id(user))
    ) | (
        (FeedItem.visibility == FeedVisibility.INSTITUTION)
        & (FeedItem.organization_id == user.institution_id)
    )


def _mix_recommendation_sources(feed: list[FeedCard]) -> list[FeedCard]:
    """Give domain, journal, scholar and project equal turns on every page."""
    def source(card: FeedCard) -> str:
        if card.project_id or "课题" in card.reason or "项目" in card.reason:
            return "project"
        if "学者" in card.reason:
            return "scholar"
        if "期刊" in card.reason:
            return "journal"
        return "domain"

    # Preserve within-source ranking, rotating equally among all available
    # sources. Today's pick badge remains, without reserving a source's turns.
    order = ("domain", "journal", "scholar", "project")
    buckets = {key: [] for key in order}
    for card in feed:
        memberships = getattr(card, "recommendation_sources", None) or [source(card)]
        for key in memberships:
            if key in buckets:
                buckets[key].append(card)
    positions = {key: 0 for key in order}
    result: list[FeedCard] = []
    seen: set[int] = set()
    while len(result) < len(feed):
        for key in order:
            while positions[key] < len(buckets[key]) and id(buckets[key][positions[key]]) in seen:
                positions[key] += 1
            if positions[key] < len(buckets[key]):
                card = buckets[key][positions[key]]
                result.append(card)
                seen.add(id(card))
                positions[key] += 1
    return result


async def _recommendation_progress(db, user, profile, *, recommendable: int) -> FeedProgressOut:
    """Count this user's relevant indexes before the preparation/recommendation gates."""
    from app.services.literature_harvester import literature_index_harvester

    rows = (await db.execute(select(FeedItem).where(
        _visible_clause(user),
        FeedItem.kind.in_([FeedItemKind.PAPER, FeedItemKind.NEWS, FeedItemKind.DIGEST]),
        ranking.publication_window_clause(datetime.now(UTC)),
    ))).scalars().all()
    relevant = []
    source_counts = {source: {"source": source, "collected": 0, "pending": 0} for source in ("domain", "journal", "scholar", "project")}
    for item in rows:
        if ranking.is_journal_notice(item):
            continue
        extra = item.extra or {}
        routes = ranking.acquisition_routes(item)
        owned_project = (str(user.id) in (extra.get("profile_user_ids") or [])
                         and bool(routes & {"project_profile", "web"}) and bool(profile.projects)
                         and (profile.active_project_queries is None
                              or str(extra.get("discovery_query") or "") in profile.active_project_queries))
        membership = {
            "domain": bool(ranking._domain_hits(item, profile)),
            "journal": ranking.matches_subscribed_journal(item, profile),
            "scholar": ranking.matches_subscribed_scholar(item, profile),
            "project": owned_project,
        }
        if any(membership.values()):
            relevant.append(item)
            waiting = bool(extra.get("literature_catalog")) and extra.get("literature_preparation") != "ready"
            for source, hit in membership.items():
                if hit:
                    source_counts[source]["collected"] += 1
                    source_counts[source]["pending"] += int(waiting)
    domain_counts = {code: {"domain": code, "label": domain_service.label(code, lang=language_for(user)),
                            "collected": 0, "prepared": 0, "pending": 0,
                            "recommendable": 0, "translated": 0, "translation_pending": 0}
                     for code in profile_service.stored_domains(user)}
    from types import SimpleNamespace
    selected_domains = SimpleNamespace(domains=tuple(domain_counts))
    for item in relevant:
        for code in ranking._domain_hits(item, selected_domains):
            if code in domain_counts:
                row = domain_counts[code]
                row["collected"] += 1
                waiting = bool((item.extra or {}).get("literature_catalog")) and (item.extra or {}).get("literature_preparation") != "ready"
                row["pending" if waiting else "prepared"] += 1
    pending = sum(bool((item.extra or {}).get("literature_catalog"))
                  and (item.extra or {}).get("literature_preparation") != "ready"
                  for item in relevant)
    task_state = discovery.subscription_task_state(user.id)
    searching = literature_index_harvester.is_online_refreshing(user.id) or task_state == "searching"
    project_error = bool(curation.stored(user).get("curated_error")) and bool(profile.projects)
    phase = ("searching" if searching else "preparing" if task_state == "preparing"
             else "queued" if pending else "failed" if project_error and not recommendable else "settled")
    return FeedProgressOut(
        phase=phase, collected=len(relevant), prepared=len(relevant) - pending,
        pending=pending, recommendable=recommendable, project_error=project_error,
        domain_progress=list(domain_counts.values()), source_progress=list(source_counts.values()),
    )


async def _build_feed_pool(user: User, db: AsyncSession) -> FeedTodayOut:
    """打开平台第一眼 —— Top 3 + 几个分区。"""
    lang = language_for(user)
    from app.services.literature_harvester import literature_index_harvester
    suppressed = await ranking.suppressed_ids(db, user_id=user.id)
    profile = await profile_service.build_profile(
        db, user=user, suppressed_item_ids=suppressed
    )
    candidates = await ranking.candidate_items(db, profile=profile)
    scored = ranking.rank(candidates, profile)
    # 小红书候选由期刊、Project画像和网页搜索三路汇合：各路先用自己的
    # 指标评分，再按5:3:2动态混排；不足的一路由其余路线自然递补。
    rednote_scored = ranking.rank_rednote(
        candidates, profile, user_id=str(user.id), base_scored=scored
    )
    rednote_scored = await ranking.apply_behavior_preferences(
        db, user_id=str(user.id), scored=rednote_scored
    )
    field_dynamics = await ranking.apply_behavior_preferences(
        db, user_id=str(user.id), scored=ranking.rank_field_dynamics(candidates, profile)
    )

    # 关注方向可能由旧客户端、管理员或其他入口修改；不能只依赖设置接口
    # 是否成功清理缓存。若今日选摘中的条目全部与当前画像无命中，丢弃旧选摘
    # 并立即重算，避免用户换方向后仍看到昨天的三条。
    from app.models.feed import FeedDailyPick
    refresh_picks = False
    cached_picks = await db.get(FeedDailyPick, (user.id, datetime.now(UTC).date()))
    if cached_picks is not None and cached_picks.picks and (
        profile.domains or profile.projects or profile.journals or profile.scholars
    ):
        scored_by_id = {str(item.item.id): item for item in rednote_scored}
        cached_scored = [
            scored_by_id.get(str(payload.get("item_id")))
            for payload in cached_picks.picks
            if isinstance(payload, dict)
        ]
        if not cached_scored or any(item is None for item in cached_scored):
            refresh_picks = True

    picks_payload = await ranking.daily_picks(db, user_id=user.id, scored=rednote_scored, refresh=refresh_picks)
    pick_ids = [p["item_id"] for p in picks_payload]
    # 日报表中的 item_id 通过 JSON 保存为字符串，而 SQLAlchemy 查询返回的
    # FeedItem.id 是 UUID。统一用字符串作为索引，否则同一条记录会在
    # “可能与你相关”中出现，却因 `str != UUID` 在“今日精选”组装时被丢掉。
    by_id = {str(item.id): item for item in candidates}
    # 今日选摘可能因时间窗口或设置变化退出当前候选，按 id 补齐引用。
    missing = [i for i in pick_ids if str(i) not in by_id]
    if missing:
        for item in (
            await db.execute(select(FeedItem).where(FeedItem.id.in_(missing)))
        ).scalars():
            by_id[str(item.id)] = item

    project_names = {p.project_id: p.name for p in profile.projects}
    saved = await _saved_ids(db, user_id=user.id)
    opened = await _opened_ids(db, user_id=user.id)
    pick_items = [by_id[str(i)] for i in pick_ids if str(i) in by_id]
    source_names, author_names = await _lookup_names(db, [*candidates, *pick_items])

    feed: list[FeedCard] = []

    def _card(
        item: FeedItem,
        *,
        reason: str = "",
        project_id: str | None = None,
        project_name: str | None = None,
        is_today_pick: bool = False,
    ) -> FeedCard:
        return FeedCard(
            item=_to_out(
                item, source_names=source_names, author_names=author_names,
                saved_ids=saved, opened_ids=opened, lang=lang,
            ),
            reason=reason,
            project_id=project_id,
            project_name=project_name,
            is_today_pick=is_today_pick,
            recommendation_sources=[key for key, matches in (
                ("domain", bool(ranking._domain_hits(item, profile))),
                ("journal", ranking.matches_subscribed_journal(item, profile)),
                ("scholar", ranking.matches_subscribed_scholar(item, profile)),
                ("project", bool(project_id) or "课题" in reason or "项目" in reason),
            ) if matches],
        )

    # 今日必读：每天替你挑的少数几条，挂角标、排在流最前。
    seen: set[str] = set()
    for payload in picks_payload:
        item = by_id.get(payload["item_id"])
        if item is None or ranking.is_journal_notice(item) or not ranking.show_in_feed(item):
            continue
        feed.append(_card(
            item,
            reason=payload.get("reason") or "",
            project_id=payload.get("project_id"),
            project_name=project_names.get(payload.get("project_id") or ""),
            is_today_pick=True,
        ))
        seen.add(str(item.id))

    matched = [
        value
        for value in rednote_scored
        if str(value.item.id) not in seen
        and value.item.kind != FeedItemKind.DEADLINE
    ]
    # “可能与你相关”的准入：课题（project_profile）与网络搜索（web）维持原样，
    # 另把用户**订阅的期刊与学者**并进来 —— 订阅是用户自己点名的信号，订了就该
    # 在推荐里看得见。手选学科命中也保留混排位置，避免统一追加到课题内容之后。
    for_you = [
        value for value in matched
        if value.project_id
        or value.matched_domains
        or ranking.acquisition_routes(value.item)
        & {"project_profile", "web", "bignews", "scholar"}
        or ranking.matches_subscribed_journal(value.item, profile)
        or ranking.matches_subscribed_scholar(value.item, profile)
    ]
    # “本领域动态”是期刊采集路线的稳定视图，独立分区/引用/时效排序。
    general = [
        value for value in field_dynamics
        if str(value.item.id) not in seen
    ]
    deadlines = [
        s
        for s in scored
        if s.item.kind == FeedItemKind.DEADLINE and str(s.item.id) not in seen
    ]

    # 一条流：今日必读 → 与你相关 → 本领域动态 → 临近截稿。不再分板块，
    # 每一条都带可验证的理由；同一份工作只出现一次（课题优先于学科）。
    for value in for_you:
        feed.append(_card(
            value.item,
            reason=value.reason(),
            project_id=value.project_id,
            project_name=value.project_name,
        ))
        seen.add(str(value.item.id))
    for value in general:
        if str(value.item.id) in seen:
            continue
        feed.append(_card(
            value.item,
            reason=value.reason(),
            project_id=value.project_id,
            project_name=value.project_name,
        ))
        seen.add(str(value.item.id))
    for value in deadlines:
        if str(value.item.id) in seen:
            continue
        feed.append(_card(value.item, reason=""))

    empty_reason = ""
    if not candidates:
        # 顺序是有讲究的：先问**这台机器上到底会不会有东西进来**，再问那两个
        # 开关。它们默认都是 True 且个人档从不覆盖，所以只看开关的话，一台
        # 永不采集的机器会走到"第一轮几分钟内完成"那一句 —— 一句永远兑现不了
        # 的承诺，而用户只会以为再等等就好。
        if not assembly.background_collection_enabled():
            empty_reason = t("empty.no_collector_here", lang)
        elif not settings.feed_external_fetch_enabled:
            empty_reason = t("empty.collection_disabled", lang)
        elif not settings.feed_collector_enabled:
            empty_reason = t("empty.scheduler_off", lang)
        else:
            empty_reason = ""

    feed = _mix_recommendation_sources(feed)
    # A DOI may exist in both the public index and this user's school library.
    # Annotate only this user's response; never persist licensed holdings on a
    # globally visible FeedItem.
    school_by_doi = {
        str((item.extra or {}).get("doi") or "").casefold(): item.extra
        for item in candidates if (item.extra or {}).get("school_library")
        and (item.extra or {}).get("doi")
    }
    for card in feed:
        holding = school_by_doi.get(str(card.item.extra.get("doi") or "").casefold())
        if holding:
            card.item.extra = {
                **card.item.extra,
                "school_library": holding["school_library"],
                "school_fulltext_url": holding.get("school_fulltext_url"),
            }
    # Full progress statistics scan the literature catalog. The dedicated
    # /progress endpoint computes them; card delivery must not wait for it.
    progress = FeedProgressOut(recommendable=len(feed))
    return FeedTodayOut(
        progress=progress,
        feed=feed,
        has_more=False,
        onboarded=profile.onboarded,
        personalized=profile.has_signal,
        empty_reason=empty_reason,
        refreshing=(
            literature_index_harvester.is_online_refreshing(user.id)
            or discovery.is_subscription_refreshing(user.id)
        ),
    )


async def preparing_translation_pool(user: User, db: AsyncSession) -> FeedTodayOut:
    """Private prewarming pool; incomplete articles never enter the public view."""
    from app.services.feed.pool_cache import get_pool
    async def build():
        suppressed = await ranking.suppressed_ids(db, user_id=user.id)
        profile = await profile_service.build_profile(db, user=user, suppressed_item_ids=suppressed)
        candidates = await ranking.candidate_items(db, profile=profile, include_preparing=True)
        matched = ranking.rank_rednote(candidates, profile, user_id=str(user.id))
        dynamics = ranking.rank_field_dynamics(candidates, profile)
        seen, cards = set(), []
        for value in [*matched, *dynamics]:
            item = value.item
            if str(item.id) in seen or (item.extra or {}).get("literature_preparation") == "ready":
                continue
            if not (item.extra or {}).get("literature_catalog") or (item.extra or {}).get("literature_eligible") is not True:
                continue
            if not (value.project_id or value.matched_domains or ranking.matches_subscribed_journal(item, profile)
                    or ranking.matches_subscribed_scholar(item, profile)):
                continue
            cards.append(FeedCard(item=_to_out(item), project_id=value.project_id, reason=value.reason(),
                recommendation_sources=[key for key, matches in (
                    ("domain", bool(ranking._domain_hits(item, profile))),
                    ("journal", ranking.matches_subscribed_journal(item, profile)),
                    ("scholar", ranking.matches_subscribed_scholar(item, profile)),
                    ("project", bool(value.project_id))) if matches]))
            seen.add(str(item.id))
        return FeedTodayOut(feed=_mix_recommendation_sources(cards))
    result = await get_pool(db, user, build, namespace="translation-preparation")
    await _attach_cached_translations([card.item for card in result.feed])
    return result


async def recommendation_pool(user: User, db: AsyncSession, *, allow_stale=True) -> FeedTodayOut:
    from app.services.feed.pool_cache import get_pool
    from app.services.feed.translation import translation_ready
    from app.services.literature_harvester import literature_index_harvester
    result = await get_pool(db, user, lambda: _build_feed_pool(user, db), allow_stale=allow_stale)
    await _attach_cached_translations([card.item for card in result.feed])
    result.progress.translated = sum(translation_ready(card.item) for card in result.feed)
    result.progress.translation_pending = len(result.feed) - result.progress.translated
    groups = {}
    for card in result.feed:
        sources = tuple(sorted(card.recommendation_sources or ["domain"]))
        group = groups.setdefault(sources, {"sources": list(sources), "total": 0, "translated": 0})
        group["total"] += 1
        group["translated"] += int(translation_ready(card.item))
    result.progress.source_groups = list(groups.values())
    result.refreshing = (literature_index_harvester.is_online_refreshing(user.id)
                         or discovery.is_subscription_refreshing(user.id))
    return result


@router.get("/today", response_model=FeedTodayOut)
async def feed_today(limit: int = Query(default=FEED_PAGE_SIZE, ge=1, le=100),
                     offset: int = Query(default=0, ge=0),
                     ready_only: bool = Query(default=True), sources: str | None = None,
                     user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)) -> FeedTodayOut:
    result = await recommendation_pool(user, db, allow_stale=bool(ready_only))
    # An older snapshot may survive in another worker. Apply current dismissals
    # before pagination so ignored cards cannot return on refresh or scrolling.
    dismissed = await ranking.suppressed_ids(db, user_id=user.id)
    if dismissed:
        result.feed = [card for card in result.feed if str(card.item.id) not in dismissed]
    from app.services.feed.translation import translation_ready
    if ready_only:
        result.feed = [card for card in result.feed if translation_ready(card.item)]
    if sources is not None:
        selected = set(sources.split(",")) & {"domain", "journal", "scholar", "project"}
        result.feed = [card for card in result.feed if selected.intersection(card.recommendation_sources or ["domain"])]
    total = len(result.feed)
    result.feed = result.feed[offset:offset + limit]
    result.has_more = total > offset + limit
    return result


def _attach_domain_progress(progress, pool, user):
    from types import SimpleNamespace
    from app.services.feed import domain_progress
    from app.services.feed.translation import translation_ready
    from app.services.literature_harvester import literature_index_harvester
    states = domain_progress.read(user.id)
    active_codes = set(literature_index_harvester._online_domains.get(str(user.id), ())) if literature_index_harvester.is_online_refreshing(user.id) else set()
    counts = {row["domain"]: row for row in progress.domain_progress}
    selected = SimpleNamespace(domains=tuple(counts))
    for card in pool.feed:
        if "domain" not in card.recommendation_sources:
            continue
        for code in ranking._domain_hits(card.item, selected):
            if code in counts:
                counts[code]["recommendable"] += 1
                counts[code]["translated"] += int(translation_ready(card.item))
    for code, row in counts.items():
        row["translation_pending"] = row["recommendable"] - row["translated"]
        saved = states.get(code, {})
        row["updated_at"] = saved.get("updated_at")
        row["phase"] = domain_progress.phase(active=code in active_codes,
            acquisition=saved.get("state"), collected=row["collected"], pending=row["pending"],
            recommendable=row["recommendable"], translation_pending=row["translation_pending"])


@router.get("/progress", response_model=FeedProgressOut)
async def feed_progress(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    pool = await recommendation_pool(user, db, allow_stale=False)
    profile = await profile_service.build_profile(db, user=user)
    progress = await _recommendation_progress(db, user, profile, recommendable=len(pool.feed))
    progress.translated = pool.progress.translated
    progress.translation_pending = pool.progress.translation_pending
    progress.source_groups = pool.progress.source_groups
    _attach_domain_progress(progress, pool, user)
    from app.services.feed.source_progress import attach
    attach(progress, user, profile)
    return progress


@router.post("/refresh", status_code=204)
async def refresh_recommendations(user: User = Depends(get_current_user)):
    from app.services.feed.pool_cache import clear
    clear()


@router.get("/items", response_model=list[FeedItemOut])
async def list_items(
    domain: str | None = None,
    kind: str | None = None,
    saved: bool = False,
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[FeedItemOut]:
    """点进去看更多。"""
    query = select(FeedItem).where(_visible_clause(user))
    if kind:
        query = query.where(FeedItem.kind == kind)
    if saved:
        saved_ids = await _saved_ids(db, user_id=user.id)
        if not saved_ids:
            return []
        query = query.where(FeedItem.id.in_(saved_ids))
    query = query.order_by(ranking.recent_order()).limit(limit).offset(offset)
    items = list((await db.execute(query)).scalars().all())
    items = [
        item for item in items
        if _eligible_feed_item(item) and (saved or not ranking.is_journal_notice(item))
    ]

    if domain:
        # 域过滤走上行链，在**取回来之后**做：`domains` 是 JSON 列，
        # 各方言的 JSON 包含查询写法不通用，而候选量已被 limit 收住。
        try:
            items = [
                item
                for item in items
                if any(domain in domain_service.ancestors(str(s)) for s in item.domains or ())
            ]
        except HarnessContractUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail=t("error.vocabulary_filter", language_for(user)),
            ) from exc

    source_names, author_names = await _lookup_names(db, items)
    saved_ids = await _saved_ids(db, user_id=user.id)
    opened_ids = await _opened_ids(db, user_id=user.id)
    return await _attach_cached_translations([
        _to_out(i, source_names=source_names, author_names=author_names,
                saved_ids=saved_ids, opened_ids=opened_ids, lang=language_for(user))
        for i in items
    ])


@router.get("/items/{item_id}", response_model=FeedItemOut)
async def get_item(
    item_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FeedItemOut:
    item = await db.scalar(
        select(FeedItem).where(FeedItem.id == item_id, _visible_clause(user))
    )
    if item is None or not _eligible_feed_item(item):
        raise HTTPException(status_code=404, detail="Feed item not found")
    source_names, author_names = await _lookup_names(db, [item])
    saved_ids = await _saved_ids(db, user_id=user.id)
    opened_ids = await _opened_ids(db, user_id=user.id)
    result = _to_out(
        item, source_names=source_names, author_names=author_names, saved_ids=saved_ids,
        opened_ids=opened_ids, lang=language_for(user),
    )
    await _attach_cached_translations([result])
    return result


@router.post("/items/{item_id}/engagement", status_code=204)
async def record_engagement(
    item_id: str,
    data: EngagementWrite,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """记一次互动。幂等：同一个人对同一条做同一个动作只记一次。"""
    item = await db.scalar(
        select(FeedItem).where(FeedItem.id == item_id, _visible_clause(user))
    )
    if item is None or not _eligible_feed_item(item):
        raise HTTPException(status_code=404, detail="Feed item not found")
    already = await db.scalar(
        select(FeedEngagement.id).where(
            FeedEngagement.user_id == user.id,
            FeedEngagement.item_id == item_id,
            FeedEngagement.action == data.action,
        )
    )
    if already is not None:
        return
    try:
        # `db.add` 必须在 savepoint **里面**。放外面的话，撞唯一键之后
        # savepoint 回滚，但这个待插入对象还留在 session 里 —— 请求收尾
        # 那次 commit 会把它再插一遍，于是抛 PendingRollbackError，
        # 一个"幂等重放"变成 500。（真跑一次才现形：新库里没有重复。）
        async with db.begin_nested():
            db.add(FeedEngagement(user_id=user.id, item_id=item_id, action=data.action))
            await db.flush()
    except IntegrityError:
        # 上面查过了还撞上 = 两个请求同时来。这是幂等，不是错误。
        pass


@router.delete("/items/{item_id}/engagement/{action}", status_code=204)
async def undo_engagement(
    item_id: str,
    action: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """撤销一次互动（取消收藏、恢复被忽略的）。

    忽略必须能撤销：一个点错了就永久消失的按钮，用户下次就不敢点了。
    """
    record = await db.scalar(
        select(FeedEngagement).where(
            FeedEngagement.user_id == user.id,
            FeedEngagement.item_id == item_id,
            FeedEngagement.action == action,
        )
    )
    if record is not None:
        await db.delete(record)
        await db.flush()


async def _interests_state(db: AsyncSession, user: User) -> InterestsOut:
    """兴趣视图的**唯一**构造处。

    三个端点（读、写、删推断项）都要返回同一个形状。各拼各的话，某天加一个
    字段就会有一处忘了加 —— 而那一处不会报错，只是少返回一样东西。
    """
    lang = language_for(user)
    stored = profile_service.stored_domains(user)
    suggested: list[DomainOption] = []
    inferred: list[DomainOption] = []
    return InterestsOut(
        domains=list(stored),
        domain_labels=_labels(list(stored), lang),
        inferred=inferred,
        suggested=suggested,
        onboarded=profile_service.is_onboarded(user),
    )


@router.get("/interests", response_model=InterestsOut)
async def get_interests(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InterestsOut:
    """当前兴趣（手选 + 推断）+ 预填建议。"""
    return await _interests_state(db, user)


@router.put("/interests", response_model=InterestsOut)
async def put_interests(
    data: InterestsWrite,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InterestsOut:
    """存兴趣。**存过就算 onboarded**，哪怕存的是空列表。

    "我不想订任何方向，只看全站" 和 "我从没被问过" 不是同一件事。把前者当
    后者，每次进来都会再弹一次问卷。
    """
    try:
        registry = domain_service.domain_registry()
    except HarnessContractUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail=t("error.vocabulary_interests", language_for(user)),
        ) from exc

    # 兴趣可以订到 archive 层（`cond-mat` = 整个凝聚态都要），而条目的域必须
    # 是具体分类 —— 两者的合法集合本来就不一样，所以这里不复用
    # `is_known_domain`（那是给条目用的）。
    valid = (
        set(registry.spine_categories())
        | set(registry.ARXIV_SPINE)
        | set(domain_service.cas_domains())
        | set(domain_service.cas_groups())
        | {domain_service.FEED_ARXIV_DOMAIN}
    )
    unknown = [d for d in data.domains if d not in valid]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=t("error.unknown_domains", language_for(user),
                     domains=", ".join(unknown[:5])),
        )

    previous_domains = set(profile_service.stored_domains(user))
    preferences = dict(user.preferences) if isinstance(user.preferences, dict) else {}
    feed_prefs = dict(preferences.get("feed") or {})
    feed_prefs["domains"] = list(dict.fromkeys(data.domains))
    feed_prefs["onboarded_at"] = datetime.now(UTC).isoformat()
    preferences["feed"] = feed_prefs
    user.preferences = preferences
    await db.flush()

    # 兴趣一变，今天的选摘就该重算 —— 否则用户刚选完方向，日报还是老三条，
    # 看起来像是这个设置没生效。
    from app.models.feed import FeedDailyPick

    today = await db.get(FeedDailyPick, (user.id, datetime.now(UTC).date()))
    if today is not None:
        await db.delete(today)
        await db.flush()

    # 资讯改为在线即时获取：只刷新用户刚选中的学科，不再等待后台遍历全部
    # 二级学科。任务由生命周期内的单例托管，重复保存会取消旧方向的在途任务。
    from app.services.literature_harvester import literature_index_harvester

    newly_selected = [
        code for code in feed_prefs["domains"] if code not in previous_domains
    ]
    if newly_selected:
        literature_index_harvester.request_online_refresh(
            user_id=user.id,
            domains=newly_selected,
            force=True,
        )

    return await _interests_state(db, user)



def _stored_subscriptions(user: User) -> SubscriptionsOut:
    preferences = user.preferences if isinstance(user.preferences, dict) else {}
    feed = preferences.get("feed") if isinstance(preferences.get("feed"), dict) else {}
    raw = feed.get("subscriptions") if isinstance(feed.get("subscriptions"), dict) else {}
    journals, scholars = [], []
    for value in raw.get("journals", []) if isinstance(raw.get("journals"), list) else []:
        try:
            journals.append(JournalSubscription.model_validate(value))
        except Exception:
            continue
    for value in raw.get("scholars", []) if isinstance(raw.get("scholars"), list) else []:
        try:
            scholars.append(ScholarSubscription.model_validate(value))
        except Exception:
            continue
    return SubscriptionsOut(journals=journals, scholars=scholars)


@router.get("/subscriptions", response_model=SubscriptionsOut)
async def get_subscriptions(user: User = Depends(get_current_user)) -> SubscriptionsOut:
    result = _stored_subscriptions(user)
    if any(value.identity_status in {"unresolved", "unavailable"} for value in result.scholars):
        from app.services.feed import scholar_migration
        scholar_migration.request(user.id)
    return result


@router.put("/subscriptions", response_model=SubscriptionsOut)
async def put_subscriptions(
    data: SubscriptionsWrite,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SubscriptionsOut:
    previous = _stored_subscriptions(user)
    previous_journal_keys = {value.key for value in previous.journals}
    previous_scholar_keys = {value.key for value in previous.scholars}
    preferences = dict(user.preferences) if isinstance(user.preferences, dict) else {}
    feed = dict(preferences.get("feed") or {})
    feed["subscriptions"] = {
        "journals": [value.model_dump() for value in data.journals],
        "scholars": [value.model_dump() for value in data.scholars],
    }
    preferences["feed"] = feed
    user.preferences = preferences
    await db.flush()
    newly_followed = [value for value in data.journals if value.key not in previous_journal_keys]
    newly_followed_scholars = [value for value in data.scholars if value.key not in previous_scholar_keys]
    if newly_followed:
        from app.services.literature_harvester import literature_index_harvester
        literature_index_harvester.request_subscription_refresh(
            user_id=user.id,
            journals=[{
                "name": value.name, "issn": value.issn, "eissn": value.eissn,
                "quartile": value.jcr_quartile or "",
            } for value in newly_followed],
        )
    # 订阅不能只是订阅页上的筛子：新关注的期刊/学者要主动去捞一次，学术数据库
    # 与网络检索都跑，推荐页才有东西可推（见 discovery.discover_for_subscriptions）。
    if newly_followed or newly_followed_scholars:
        from app.services.feed import discovery
        discovery.request_subscription_discovery(
            user_id=user.id,
            journals=[{
                "key": value.key, "name": value.name, "issn": value.issn, "eissn": value.eissn,
                "quartile": value.jcr_quartile or "",
            } for value in newly_followed],
            scholars=[value.model_dump() for value in newly_followed_scholars],
        )
    return _stored_subscriptions(user)


@router.get("/subscriptions/search", response_model=SubscriptionSearchOut)
async def search_subscriptions(
    q: str = Query(min_length=1, max_length=200),
    kind: str = Query(default="journal", pattern="^(journal|scholar)$"),
    limit: int = Query(default=20, ge=1, le=50),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    lang: Language = Depends(language_on_screen),
) -> SubscriptionSearchOut:
    needle = " ".join(q.split()).casefold()
    compact_needle = needle.replace("-", "")
    if kind == "journal":
        from nodes.literature.tools.journal_metrics import database_path
        path = database_path()
        if not path.is_file():
            return SubscriptionSearchOut()
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT journal,issn,eissn,impact_factor,jcr_quartile
                     FROM journals
                    WHERE lower(journal) LIKE ? OR lower(coalesce(journal_abbr, '')) LIKE ?
                       OR replace(lower(coalesce(issn, '')), '-', '') LIKE ?
                       OR replace(lower(coalesce(eissn, '')), '-', '') LIKE ?
                    ORDER BY impact_factor DESC NULLS LAST, journal LIMIT ?""",
                (f"%{needle}%", f"%{needle}%", f"%{compact_needle}%", f"%{compact_needle}%", limit),
            ).fetchall()
        return SubscriptionSearchOut(journals=[
            JournalSubscription(
                key=str(row["issn"] or row["eissn"] or row["journal"]).casefold(),
                name=journal_display_name(row["journal"]), issn=str(row["issn"] or ""),
                eissn=str(row["eissn"] or ""), impact_factor=row["impact_factor"],
                jcr_quartile=row["jcr_quartile"],
            ) for row in rows
        ])

    from app.services.feed import author_identity
    try:
        candidates = await author_identity.search(q, limit=limit)
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail=say({"zh": "学者身份检索暂不可用，请稍后重试；不会按同名合并结果。", "en": "Scholar identity lookup is unavailable right now; please retry later. Results are not merged by name."}, lang))
    return SubscriptionSearchOut(scholars=[ScholarSubscription.model_validate(row) for row in candidates])


@router.get("/subscriptions/items", response_model=list[FeedCard])
async def subscription_items(
    kind: str = "all",
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[FeedCard]:
    if kind not in {"all", "domain", "journal", "scholar"}:
        raise HTTPException(status_code=422, detail="Unknown subscription kind")
    subscriptions = _stored_subscriptions(user)
    scholar_display = {value.name.casefold(): value.name for value in subscriptions.scholars}
    if not subscriptions.journals and not scholar_display and not profile_service.stored_domains(user):
        return []
    publication_window = ranking.publication_window_clause(datetime.now(UTC))

    # 先扫**用户自己的订阅采集产物**（extra.profile_user_ids 指向他）。
    #
    # 这一步是必须的：下面那条"最新 1200 条"的通用窗口是按时序截断的，用户订阅
    # 的期刊/学者论文会被更新的无关内容挤出窗口 —— 实测 23 条作者命中里有 14 条
    # 落在窗口外，用户看到的是"订阅了还是不显示"。订阅页要看的本来就是"我的订阅
    # 带来了什么"，所以先按归属取，再拿通用窗口补齐期刊映射路线的内容。
    my_rows = list((await db.execute(
        select(FeedItem).where(
            _visible_clause(user),
            FeedItem.kind == FeedItemKind.PAPER,
            cast(FeedItem.extra["profile_user_ids"], String).like(f'%"{user.id}"%'),
            publication_window,
        ).order_by(ranking.recent_order()).limit(5000)
    )).scalars().all())
    seen_ids = {str(item.id) for item in my_rows}
    generic_rows = list((await db.execute(
        select(FeedItem).where(
            _visible_clause(user),
            FeedItem.kind == FeedItemKind.PAPER,
            publication_window,
        ).order_by(ranking.recent_order()).limit(5000)
    )).scalars().all())
    candidates = my_rows + [item for item in generic_rows if str(item.id) not in seen_ids]
    # 比对用画像里那套**共享**归一化（profile.journal_key / ranking.matches_*）：
    # 订阅页与推荐页各写一份的话，同一本刊会出现"这边认得出、那边认不出"的
    # 分歧，而这正是"订阅了却没内容"的成因。
    profile = await profile_service.build_profile(db, user=user)
    matched: list[tuple[FeedItem, str]] = []
    for item in candidates:
        routes = ranking.acquisition_routes(item)
        journal_origin = not routes or "journal" in routes
        if kind in {"all", "journal"} and journal_origin and ranking.matches_subscribed_journal(item, profile):
            matched.append((item, f"订阅期刊：{item.venue or ''}"))
        elif kind in {"all", "scholar"} and ranking.matches_subscribed_scholar(item, profile):
            hit = next(
                (scholar_display.get(str(author).casefold().strip(), "")
                 for author in (item.authors or [])
                 if str(author).casefold().strip() in scholar_display),
                "",
            )
            matched.append((item, f"订阅学者：{hit}"))
        elif kind in {"all", "domain"} and journal_origin and ranking._domain_hits(item, profile):
            matched.append((item, "订阅学科"))
    # 用户明确订阅的期刊即使摘要暂未补齐也要显示；订阅动态本身就是
    # “这本刊最近出了什么”的索引视图，不能被普通推荐的摘要准入挡掉。
    matched = [
        (item, reason) for item, reason in matched
        if not ranking.is_journal_notice(item)
        and (ranking.show_in_feed(item) or ranking.matches_subscribed_journal(item, profile))
    ]
    ranked = ranking.rank([item for item, _ in matched], profile)
    reasons = {str(item.id): reason for item, reason in matched}
    chosen = [value.item for value in ranked[offset:offset + limit]]
    source_names, author_names = await _lookup_names(db, chosen)
    saved = await _saved_ids(db, user_id=user.id)
    opened = await _opened_ids(db, user_id=user.id)
    lang = language_for(user)
    cards = [FeedCard(
        item=_to_out(item, source_names=source_names, author_names=author_names,
                     saved_ids=saved, opened_ids=opened, lang=lang),
        reason=reasons.get(str(item.id), ""),
    ) for item in chosen]
    await _attach_cached_translations([card.item for card in cards])
    return cards


@router.get("/domains", response_model=DomainCatalogOut)
async def domain_catalog(user: User = Depends(get_current_user)) -> DomainCatalogOut:
    """可选方向的分组目录 —— 词表来自 harness，平台不存第二份。"""
    try:
        groups = domain_service.catalog(lang=language_for(user))
    except HarnessContractUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail=t("error.vocabulary_catalog", language_for(user)),
        ) from exc
    return DomainCatalogOut(
        groups=[
            DomainGroup(
                archive=group["archive"],
                label=group["label"],
                categories=[
                    DomainOption(
                        domain=option["domain"],
                        label=option["label"],
                        kind=option.get("kind", "spine"),
                    )
                    for option in group["categories"]
                ],
            )
            for group in groups
        ]
    )


@router.get("/digests/{domain:path}", response_model=FeedItemOut | None)
async def get_domain_digest(
    domain: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FeedItemOut | None:
    """这个方向本周的动向简报；没有就现生成一份（生成一次，全平台共享）。

    返回 `null` 表示"这周这个方向的内容还不够缩成一段" —— 那不是错误。
    """
    item = await digest.ensure_digest(db, domain=domain, user=user)
    if item is None:
        return None
    return _to_out(item)


@router.post("/shares", response_model=FeedItemOut, status_code=201)
async def share_link(
    data: SharedLinkWrite,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FeedItemOut:
    """转一条链接进来，平台抓标题和摘要做成卡片。

    这是冷启动期摩擦最低的 UGC：发原创贴很难，转一条链接加一句话人人肯干。
    也是 X / 公众号 / 知乎那类封闭生态唯一体面的入口。
    """
    try:
        preview = await fetch_preview(data.url)
    except LinkRejected as exc:
        # 拒收原因原样给用户 —— 他要能知道是链接的问题还是平台的问题。
        raise HTTPException(status_code=422, detail=exc.said_in(language_for(user))) from exc

    key = canonical.canonical_key(url=preview.url)
    if not key:
        raise HTTPException(status_code=422,
                            detail=t("error.link_has_no_identity", language_for(user)))

    existing = await db.scalar(select(FeedItem).where(FeedItem.canonical_key == key))
    if existing is not None:
        # 已经有人分享过（或采集器已经收过）。不新建一条，把已有那条给他 ——
        # 同一份东西在流里出现两次，比"你分享的这条已经在了"更难受。
        source_names, author_names = await _lookup_names(db, [existing])
        return _to_out(existing, source_names=source_names, author_names=author_names)

    try:
        clean_domains = [d for d in data.domains if domain_service.is_known_domain(d)]
    except HarnessContractUnavailable:
        clean_domains = []

    record = FeedItem(
        canonical_key=key,
        kind=FeedItemKind.POST,
        title=preview.title,
        url=preview.url,
        # 用户的一句话在前，抓到的页面摘要在后 —— 他为什么转这条，比页面
        # 自己怎么介绍自己更重要。
        summary="\n\n".join(p for p in (data.comment.strip(), preview.summary) if p) or None,
        authors=[],
        venue=preview.venue or None,
        published_at=datetime.now(UTC),
        domains=clean_domains,
        author_user_id=user.id,
        organization_id=_audience_id(user),
        visibility=data.visibility,
        extra={"shared": True},
    )
    db.add(record)
    await db.flush()
    return _to_out(record, author_names={user.id: user.display_name})


@router.get("/status", response_model=FeedStatusOut)
async def feed_status(db: AsyncSession = Depends(get_db)) -> FeedStatusOut:
    """源健康与采集状态。

    有这个端点是因为：一个静默不工作的源，和一个"这周确实没新内容"的源，
    从资讯流本身看长得一模一样。
    """
    sources = list(
        (await db.execute(select(FeedSource).order_by(FeedSource.name))).scalars().all()
    )
    total = await db.scalar(select(func.count()).select_from(FeedItem)) or 0
    return FeedStatusOut(
        sources=[
            SourceHealthOut(
                id=s.id,
                kind=s.kind,
                name=s.name,
                is_active=s.is_active,
                domains=[str(d) for d in (s.domains or [])],
                poll_interval_seconds=s.poll_interval_seconds,
                last_polled_at=s.last_polled_at,
                last_success_at=s.last_success_at,
                last_item_count=s.last_item_count,
                consecutive_failures=s.consecutive_failures,
                last_error=s.last_error,
            )
            for s in sources
        ],
        total_items=int(total),
        collector_enabled=settings.feed_collector_enabled,
        external_fetch_enabled=settings.feed_external_fetch_enabled,
        domain_registry_available=domain_service.registry_available(),
    )


# ── 自动挖掘 ────────────────────────────────────────────────────────────────


async def _curation_state(db: AsyncSession, user: User) -> CurationOut:
    backend = await feed_bridge.curation_backend(db, user)
    stored = curation.stored(user)
    last_raw = stored.get("curated_at")
    last: datetime | None = None
    if isinstance(last_raw, str) and last_raw:
        try:
            last = datetime.fromisoformat(last_raw)
        except ValueError:
            last = None
    inferred = list(curation.inferred_domains(user))
    return CurationOut(
        enabled=curation.is_enabled(user),
        # 「配了模型吗」和「用户开了吗」分开答 —— 合成一个布尔值，用户就
        # 分不清"我没开"和"开了也没用"。
        available=discovery.role_available(backend is not None),
        model_label=(
            f"{backend.display_name} · {backend.model}" if backend is not None else None
        ),
        last_run_at=last,
        last_error=stored.get("curated_error") or None,
        inferred_domains=inferred,
        inferred_domain_labels=_labels(inferred, language_for(user)),
        inferred_queries=list(curation.inferred_queries(user)),
    )


@router.get("/curation", response_model=CurationOut)
async def get_curation(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CurationOut:
    """自动挖掘的开关状态 + 正在为你服务的模型。"""
    return await _curation_state(db, user)


@router.put("/curation", response_model=CurationOut)
async def put_curation(
    data: CurationWrite,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CurationOut:
    """开/关自动挖掘。

    打开时**立刻跑一次**，不等下一轮采集：用户刚点完开关，界面上什么都没变
    的话，他没法分辨是"在后台排队"还是"这个开关没接线"。

    关掉时保留已推断的结果不删 —— 他可能只是想省钱，而那些方向仍然是对的；
    真要清掉，删具体某一条（`DELETE /feed/interests/inferred/{domain}`）
    才是他表达"这个推错了"的动作。
    """
    if data.enabled:
        backend = await feed_bridge.curation_backend(db, user)
        if backend is None:
            # 打不开就说清为什么，别让开关弹回去而不给理由。
            raise HTTPException(
                status_code=409,
                detail=t("curation.no_model", language_for(user)),
            )

    preferences = dict(user.preferences) if isinstance(user.preferences, dict) else {}
    feed_prefs = dict(preferences.get("feed") or {})
    feed_prefs["auto_curation"] = bool(data.enabled)
    preferences["feed"] = feed_prefs
    user.preferences = preferences
    await db.flush()

    if data.enabled:
        # 立刻跑一次。失败不抛 —— `curate` 会把原因写进偏好，下面照常返回，
        # 界面上就能看到"开着但没工作，因为 X"。
        await curation.curate(db, user=user, force=True)
        from app.models.feed import FeedDailyPick

        today = await db.get(FeedDailyPick, (user.id, datetime.now(UTC).date()))
        if today is not None:
            # 方向变了，今天的选摘就该重算 —— 否则用户刚开完开关，
            # 日报还是老三条，看起来像这个开关没生效。
            await db.delete(today)
        await db.flush()

    return await _curation_state(db, user)


@router.delete("/interests/inferred/{domain:path}", response_model=InterestsOut)
async def reject_inferred_domain(
    domain: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InterestsOut:
    """删掉一条推断出来的方向，并**记住别再推**。

    只从列表里移除是不够的：下次挖掘会把它重新推断出来，用户于是每天删同一个，
    而系统看起来像没听见。
    """
    preferences = dict(user.preferences) if isinstance(user.preferences, dict) else {}
    feed_prefs = dict(preferences.get("feed") or {})
    inferred = [d for d in (feed_prefs.get("inferred_domains") or []) if d != domain]
    rejected = list(feed_prefs.get("rejected_domains") or [])
    if domain not in rejected:
        rejected.append(domain)
    feed_prefs["inferred_domains"] = inferred
    feed_prefs["rejected_domains"] = rejected[:80]
    preferences["feed"] = feed_prefs
    user.preferences = preferences
    await db.flush()
    return await _interests_state(db, user)


@router.get("/items/{item_id}/image")
async def get_item_image(
    item_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """条目的配图 —— **每一条都有**，三级供给（见 `feed/thumbnail.py`）：

      1. feed 自带的图（出版商自己选的配图），由后端代取；
      2. 论文 PDF 里裁出来的插图（arXiv / bioRxiv 的 feed 没有图，但论文有）；
      3. 生成的排版封面（明显是生成的，不冒充任何科学插图）。

    **由后端代取而不是把外链交给浏览器**：直连外链等于每次打开首页就向出版商
    广播一次"这个用户在读这条"（IP + Referer）。接口收 `item_id` 不收 URL ——
    地址来自我们自己的库，所以这条路上没有 SSRF 面。
    """
    headers = {
        "Cache-Control": "private, max-age=86400",
        "X-Content-Type-Options": "nosniff",
    }
    item = await db.scalar(
        select(FeedItem).where(FeedItem.id == item_id, _visible_clause(user))
    )
    if item is None or not _eligible_feed_item(item):
        raise HTTPException(status_code=404, detail="Feed item not found")

    headers = {
        # 图不随登录状态变，但它属于某个条目 —— 交给浏览器私有缓存，不给中间
        # 代理缓存（那会把"谁看过哪条"泄露到另一层）。
        "Cache-Control": "private, max-age=86400",
        # 我们代取的第三方内容，不该被当成脚本或页面执行。
        "X-Content-Type-Options": "nosniff",
    }

    # 0. 共享 literature catalog 中已经由后台采集器准备好的 figure。
    #     这一分支只读本地文件，绝不在用户请求时联网。
    local_figure = _local_literature_figure(item)
    if local_figure is not None:
        return Response(
            content=local_figure.read_bytes(),
            media_type=_image_media_type(local_figure),
            headers=headers,
        )

    # ① feed 自带的图
    if item.image_url:
        try:
            body, content_type = await fetch_image(item.image_url)
            return Response(content=body, media_type=content_type, headers=headers)
        except ImageUnavailable as exc:
            # 取不回来就往下走，不是错误 —— 下面还有两级。
            logger.info("Feed image unavailable for %s: %s", item_id, exc)

    # ② 论文 PDF 里的插图。落盘缓存：同一条只做一次，重启也不重做
    #    （每次都重下一遍 PDF，对 arXiv 不礼貌，对我们也慢）。
    cached = thumbnail.cache_path(item_id)
    if cached.is_file():
        return Response(content=cached.read_bytes(), media_type="image/png", headers=headers)

    pdf_url = thumbnail.pdf_url_for(item.url)
    missed = thumbnail.miss_path(item_id)
    if pdf_url and not missed.exists():
        # 2026-09-01 生产止血：插图补取挪到后台（见 thumbnail.ensure_cached_in_background
        # 的说明——此前在用户请求里排队下载 PDF，实测单请求挂 394 秒，占满浏览器
        # 连接导致整个 UI 点不动）。本次请求立即回③生成封面，下次刷到即真图。
        thumbnail.ensure_cached_in_background(item_id, pdf_url)

    # ③ 生成的封面。**不缓存到盘**：它是纯函数算出来的，几百字节，
    #    重算比读盘还快，而且改了样式立刻全站生效。
    venue = item.venue
    if not venue and item.source_id:
        venue = await db.scalar(select(FeedSource.name).where(FeedSource.id == item.source_id))
    cover = thumbnail.generated_cover(
        title=item.title,
        venue=venue or "",
        domain=next(iter(item.domains or []), ""),
    )
    return Response(content=cover, media_type="image/svg+xml", headers=headers)
