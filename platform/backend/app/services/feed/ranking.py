"""排序与每日选摘。

## 判据：会不会改变你的下一步，不是你会不会点

小红书/抖音那套推荐是为**停留时长**优化的。把它原样搬到科研工具上，和"帮
科学家做科研"这个目标直接冲突：最能留住人的内容是最耸动的，不是最有用的。

所以这里的排序不追求"你会点"，只回答一个问题：**这条东西会不会影响你手头
研究的下一步。** 由此三条：

  - 命中你正在做的课题 > 命中你声明的域 > 泛领域动态；
  - 时效是硬折扣（上周的"新论文"不是新闻）；
  - 每日选摘**有界**（Top N），刷得完是特性不是缺陷。

## 为什么理由是机械拼的，不是模型写的

理由要能被用户当场验证。"这篇提到了 finite-size scaling，和你的 Ising 课题
对得上" —— 用户扫一眼标题就知道真假。模型写的理由读起来更顺，但它可能在
复述一个并不存在的关联，而这种错误恰恰**看不出来**。

M2 可以加一层模型 re-rank，但它排的是候选顺序，理由里点名的重叠词仍然由这
一层机械给出。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feed import (
    FeedDailyPick,
    FeedEngagement,
    FeedEngagementAction,
    FeedItem,
    FeedItemKind,
)
from app.services.feed import domains as domain_service
from app.services.feed.profile import Profile, author_matches, extract_terms, journal_key

#: 每天的选摘条数。三条是"读得完"和"有得挑"之间的位置。
DAILY_PICK_COUNT = 3

#: 参与排序的候选窗口。再往前的东西不叫资讯。
CANDIDATE_WINDOW_DAYS = 14

#: 时效半衰期（天）：一条内容每过这么久，时效分打对折。
RECENCY_HALF_LIFE_DAYS = 3.0

#: 各信号的权重。命中课题远高于命中域 —— 前者是"你正在做的事"，
#: 后者只是"你说你关心的大方向"。
WEIGHT_PROJECT_MATCH = 6.0
WEIGHT_DOMAIN_MATCH = 2.5
WEIGHT_RECENCY = 2.0
WEIGHT_ATTENTION = 1.0

#: 一条内容和一个课题至少要有这么多个重叠术语才算"命中"。
MIN_PROJECT_TERM_HITS = 2

#: 命中强度下限（归一化罕见度之和）。低于它不算命中 —— 两个到处都是的词
#: 凑在一起不构成关联。0.8 大致等于"两个各出现在一成内容里的术语"。
MIN_PROJECT_MATCH_STRENGTH = 0.8

# 方法/过程词不能单独证明领域相关性；必须和至少一个非泛化领域锚点共同出现。
GENERIC_PROJECT_TERMS = frozenset({
    "model", "models", "modeling", "modelling", "dynamics", "dynamic",
    "framework", "frameworks", "kinetic", "kinetics", "growth", "time",
    "event", "events", "force", "forces", "field", "fields", "system",
    "systems", "process", "processes", "analysis", "simulation",
    "simulations", "investigation", "investigations", "method", "methods",
    "design", "designs",
})

#: 命中饱和点：强度到这里就算满分。1.5 大致等于"两个各只出现在 1% 内容里
#: 的术语"，那已经是相当确凿的关联了。
PROJECT_MATCH_SATURATION = 1.5


def _aware(value: datetime | None) -> datetime | None:
    """落库取回来的时间戳统一成 aware。

    列是 `DateTime(timezone=True)`，但取回来是不是 aware **取决于驱动**：
    Postgres 给 aware，SQLite 给 naive。拿 naive 去和 `datetime.now(UTC)`
    相减是 TypeError —— 一个只在某些部署上炸的 500。
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(slots=True)
class ScoredItem:
    item: FeedItem
    score: float
    #: 命中的课题（命中多个时取最强的那个）。
    project_id: str | None = None
    project_name: str = ""
    matched_terms: tuple[str, ...] = ()
    matched_domains: tuple[str, ...] = ()
    #: 命中的**订阅**：期刊名 / 学者姓名。订阅是用户亲自点名的依据，和课题、学科
    #: 一样要能说出口 —— 不然这类条目就只能拿到一句泛泛的兜底话（实测过：
    #: 订阅的水刊论文全部显示"本领域近期动态"，用户完全看不出为什么会推它）。
    matched_journal: str = ""
    matched_scholar: str = ""

    def reason(self) -> str:
        """一句能被当场验证的理由。

        每一句都要能指回**用户自己做过的某件事**（开的课题、选的学科、订阅的
        刊/人）。说不出依据时不编泛话，而是退回"这条是从哪儿来的"这种同样可
        核对的说法 —— 一条只有"本领域近期动态"的卡片对用户零信息，等于让他
        自己猜为什么被推。
        """
        if self.project_name and self.matched_terms:
            shared = "、".join(self.matched_terms[:3])
            return f"和你的课题「{self.project_name}」对得上：{shared}"
        if self.matched_domains:
            labels = "、".join(
                domain_service.label(slug) for slug in self.matched_domains[:2]
            )
            return f"属于你关注的 {labels}"
        if self.matched_journal:
            return f"你订阅的期刊 {self.matched_journal} 有新文章"
        if self.matched_scholar:
            return f"你订阅的学者 {self.matched_scholar} 有新论文"
        routes = acquisition_routes(self.item)
        if "web" in routes:
            return "根据你的课题画像从公开网页发现"
        if "project_profile" in routes:
            return "根据你的课题画像检索到"
        # 兜底也不说泛话：说出这条的来源（期刊名 / 站点），用户能自己判断要不要看。
        venue = (self.item.venue or "").strip()
        if "bignews" in routes:
            return f"科学媒体报道：{venue}" if venue else "科学媒体报道"
        if venue:
            return f"来自 {venue}"
        source = str((self.item.extra or {}).get("source") or "").strip()
        return f"来自 {source}" if source else "平台新收录"


def _recency_score(published_at: datetime | None, *, now: datetime) -> float:
    moment = _aware(published_at)
    if moment is None:
        return 0.0
    age_days = max((now - moment).total_seconds() / 86400.0, 0.0)
    return math.pow(0.5, age_days / RECENCY_HALF_LIFE_DAYS)


def _attention_score(item: FeedItem) -> float:
    """来自源的注意力证据（目前只有 HF 的 upvotes）。

    取 log 而不是原值：投票数是长尾的，线性用会让一条爆款把整个列表淹掉，
    而"被很多人看到"和"对你有用"并不是一回事。
    """
    extra = item.extra if isinstance(item.extra, dict) else {}
    upvotes = extra.get("upvotes")
    if not isinstance(upvotes, int) or upvotes <= 0:
        return 0.0
    return min(math.log1p(upvotes) / math.log(100.0), 1.0)


def _domain_hits(item: FeedItem, profile: Profile) -> tuple[str, ...]:
    """条目的域（含其上行链）与用户订阅的交集。

    走上行链，所以订了 `cond-mat` 的人收得到 `cond-mat.mtrl-sci` 的内容，
    反过来不成立 —— 订阅是"这个范围我都要"，不是"只要这一格"。
    """
    if not profile.domains:
        return ()
    wanted = set(profile.domains)
    hits: list[str] = []
    for slug in item.domains or ():
        for ancestor in domain_service.ancestors(str(slug)):
            if ancestor in wanted and ancestor not in hits:
                hits.append(ancestor)
    return tuple(hits)


class TermDiscrimination:
    """一个术语在**当前候选池里**有多大区分度（IDF）。

    ## 为什么必须现算，不能写一张泛词表

    实测（2026-08-22，真跑一轮 527 条）：给"MLIP 鲁棒性"这个课题推出来的理由
    是"和你的课题对得上：learning、machine、network" —— 那是候选池里**每一篇**
    机器学习论文都有的词。用户扫一眼就知道这不算命中，而排序也确实被它带歪
    了（泛词命中数多，反而排得比真正相关的高）。

    修法有两条：往停用词表里加 learning/machine/network…，或者现算。选后者：

      - 名单是错的方向。"learning" 对一个做机器学习势的课题是泛词，对一个做
        教育心理学的课题是核心词 —— 区分度是**语料的性质**，不是词的性质。
      - 名单会过时。明年的泛词今年还没出现，而漏掉它不会有任何症状。

    所以：一个术语出现在候选池里越多条内容中，它越不能说明什么。这是能机械
    算出来的事实，就别交给一张要人维护的表。
    """

    __slots__ = ("_document_count", "_frequency", "_scale")

    def __init__(self, documents: list[frozenset[str]]) -> None:
        self._document_count = max(len(documents), 1)
        frequency: dict[str, int] = {}
        for terms in documents:
            for term in terms:
                frequency[term] = frequency.get(term, 0) + 1
        self._frequency = frequency
        # 按 log(N) 归一，让"罕见度"落在 0..1 而不随语料大小漂移。
        #
        # 不归一的话（第一版就是），阈值就得跟着候选池的条数走：同一批权重
        # 在 400 条的池子里刚好，在 40 条的池子里全都不达标。而池子的大小
        # 是随采集节奏变的 —— 那种阈值今天对明天错，且不会报错。
        self._scale = math.log(self._document_count) if self._document_count > 1 else 1.0

    def weight(self, term: str) -> float:
        """罕见度，0..1。到处都是的词趋近 0，只出现在少数几条里的词趋近 1。"""
        seen = self._frequency.get(term, 0)
        return math.log(self._document_count / (1 + seen)) / self._scale

    def most_distinctive(self, terms: frozenset[str], *, limit: int = 3) -> tuple[str, ...]:
        """挑最能说明问题的几个词 —— 理由里点名的就是它们。"""
        return tuple(sorted(terms, key=self.weight, reverse=True)[:limit])


def score_item(
    item: FeedItem,
    profile: Profile,
    *,
    now: datetime,
    discrimination: TermDiscrimination,
    item_terms: frozenset[str] | None = None,
) -> ScoredItem:
    """给一条内容打分，并记下**为什么**。

    理由和分数必须一起算出来：分开算就会出现"排在第一但说不出理由"的条目，
    而那种条目正是排序出错时的样子。
    """
    terms = item_terms if item_terms is not None else extract_terms(item.title, item.summary)

    best: tuple[float, str, str, tuple[str, ...]] | None = None
    for signal in profile.projects:
        if not signal.terms:
            continue
        overlap = terms & signal.terms
        if len(overlap) < MIN_PROJECT_TERM_HITS:
            continue
        # 泛词可以提高排序，但不能单独制造“和你的课题对得上”。
        specific_overlap = overlap - GENERIC_PROJECT_TERMS
        if not specific_overlap:
            continue
        # 强度是重叠词**罕见度之和**，不是重叠词个数：三个泛词不如一个
        # 只在这个方向出现的术语。
        strength = sum(discrimination.weight(term) for term in specific_overlap)
        if strength < MIN_PROJECT_MATCH_STRENGTH:
            # 够不上下限 = 只是碰巧共用了几个常见词。说成"和你的课题对得上"
            # 是在编一个用户一眼就能证伪的理由。
            continue
        if best is None or strength > best[0]:
            best = (
                strength,
                signal.project_id,
                signal.name,
                discrimination.most_distinctive(specific_overlap),
            )

    domain_hits = _domain_hits(item, profile)

    score = 0.0
    if best is not None:
        score += WEIGHT_PROJECT_MATCH * min(best[0] / PROJECT_MATCH_SATURATION, 1.0)
    if domain_hits:
        score += WEIGHT_DOMAIN_MATCH
    score += WEIGHT_RECENCY * _recency_score(item.published_at, now=now)
    score += WEIGHT_ATTENTION * _attention_score(item)

    # 订阅命中也要进理由：它是用户亲自点名的依据，跟课题、学科同级。
    matched_journal = ""
    matched_scholar = ""
    if profile.journals and matches_subscribed_journal(item, profile):
        matched_journal = (item.venue or "").strip()
    if profile.scholars:
        for author in item.authors or []:
            if any(author_matches(wanted, author) for wanted in profile.scholars):
                matched_scholar = str(author).strip()
                break

    return ScoredItem(
        item=item,
        score=score,
        project_id=best[1] if best else None,
        project_name=best[2] if best else "",
        matched_terms=best[3] if best else (),
        matched_domains=domain_hits,
        matched_journal=matched_journal,
        matched_scholar=matched_scholar,
    )


def recent_order():
    """候选池的时效排序：有发表时间用发表时间，没有就用**入库时间**。

    不能写成 `published_at DESC NULLS LAST`：那会让所有没写日期的内容永远排在
    最后，而候选池是有界的（`limit*3`）—— 库里"有日期的条目"一旦超过这个界，
    无日期条目就被整批饿出窗口，连被排序的机会都没有。实测症状：project 检索
    命中的中文网页条目（源没给日期）在推荐流里一条都不剩，"和你的课题对得上"
    整段消失，而它们明明在库里、也在 14 天窗口内。

    用 `COALESCE(published_at, created_at)` 让它们按入库时间参与竞争 —— 这正是
    `_windowed()` 里"没写日期不等于不存在"那句注释的本意。
    """
    return func.coalesce(FeedItem.published_at, FeedItem.created_at).desc()


async def candidate_items(
    db: AsyncSession, *, profile: Profile, limit: int = 400
) -> list[FeedItem]:
    """取候选池：窗口内、这个用户可见、还没被他处理掉的。

    可见性在**查询里**收口，不在打分之后过滤：过滤写在后面就意味着一条
    org 内容会先被算分、再被丢掉，而任何一处忘了过滤都是越权泄露。
    """
    from app.models.feed import FeedVisibility

    now = datetime.now(UTC)
    cutoff = now - timedelta(days=CANDIDATE_WINDOW_DAYS)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def _windowed(statement):
        """窗口 + 可见性 —— 主候选池与订阅补充扫描共用同一套条件。

        两处各写一份的话，一边收紧一边放宽会静默分叉（比如订阅扫描放进来一条
        已经过期的条目），而症状只表现为"偶尔多一条奇怪的"。
        """
        from app.models.feed import FeedVisibility

        return statement.where(
            FeedItem.visibility == FeedVisibility.PLATFORM,
            # published_at 缺失的条目（源没给日期）用入库时间兜底，否则它们会因为
            # 时间比较为 NULL 被整条排除 —— 一条没写日期的内容不该等于不存在。
            (FeedItem.published_at.is_(None) & (FeedItem.created_at >= cutoff))
            # 出版商会提前登记未来卷期。它们不是“今天的动态”，也不能按倒序
            # 占满有界候选池，把已经发表的当前内容挤到查询上限之外。
            | ((FeedItem.published_at >= cutoff) & (FeedItem.published_at <= now))
            # 只有 YYYY-MM 的来源记录会以当月 1 日落到 DateTime，但这个“1日”
            # 只是存储占位，不是真实发表日。本月记录应在整个月内可被资讯候选召回。
            | (
                (FeedItem.extra["publication_date_precision"].as_string() == "month")
                & (FeedItem.published_at >= month_start)
                & (FeedItem.published_at <= now)
            ),
        )

    query = _windowed(select(FeedItem)).order_by(
        recent_order()
    # Rejected catalog projections are filtered below.  Fetch extra rows
    # so they cannot crowd valid news out of a bounded candidate pool.
    ).limit(limit * 3)
    # 六位代码是明确的二级学科选择。必须在 LIMIT 之前过滤，否则全平台其他
    # 热门学科的最新条目会先占满候选池，目标学科明明已入库却永远到不了
    # Python 侧的精细排序。一级/骨架域仍留给后面的层级匹配处理。
    leaf_domains = {
        str(code) for code in profile.domains
        if str(code).isdigit() and len(str(code)) == 6
    }
    if leaf_domains:
        leaf_condition = or_(*(
            # domains 在 PostgreSQL 是 JSONB、测试 SQLite 是 JSON；泛型
            # JSON.contains 会在这个 variant 上错误生成 `LIKE ... JSONB`。
            # 匹配带双引号的完整代码，避免 08250/0825011 之类子串误命中。
            cast(FeedItem.domains, String).like(f'%"{code}"%')
            for code in sorted(leaf_domains)
        ))
        # 课题/网络/综合资讯/订阅路线的论文不标学科 domains（见 discovery
        # 的 second_level_domains=[]），它们靠 extra.profile_user_ids 指向这个
        # 用户进候选池，不能因「domains 不含订阅学科」被上面的预过滤整批滤掉。
        owned_by_this_user = and_(
            cast(FeedItem.extra["profile_user_ids"], String).like(
                f'%"{profile.user_id}"%'
            ),
            or_(*(
                cast(FeedItem.extra["feed_acquisition_routes"], String).like(
                    f"%{route}%"
                )
                for route in ("project_profile", "web", "bignews", "journal", "scholar")
            )),
        )
        query = query.where(or_(leaf_condition, owned_by_this_user))
    items = list((await db.execute(query)).scalars().all())[:limit]
    # ── 订阅补充扫描 ────────────────────────────────────────────────────
    #
    # 订阅的刊/人可以是跨学科的：用户订了某个刊，却把它归在一个他没选的
    # 二级学科下（或者这本刊压根不在离线「期刊→学科」映射表里）。上面的学科
    # 预过滤只看 domains，会把这类条目整批挡在候选池外 —— 症状就是"订阅了
    # 但推荐里从来没有它"。
    #
    # 这里不能再靠"有订阅就不做预过滤"来兜（2026-09-21 实测踩过）：那等于
    # 把学科边界整个撤掉，候选池变成全平台最新条目，公共期刊动态会把课题命中
    # 挤出推荐流，用户看到的是"我的课题怎么不见了"。
    #
    # 所以改成单独扫一遍同窗口的最新条目，用**送达时同一套**匹配函数认领订阅
    # 命中项，再并进候选池。学科边界不动，订阅内容照样进得来。
    from app.services.feed.profile import journal_key as _journal_key

    if profile.journals or profile.scholars:
        # 顺序有讲究：**先看他自己的订阅采集产物**（extra.profile_user_ids 指向他），
        # 再退到通用窗口。通用窗口是按 published_at 截断的，订阅内容会被更新的
        # 无关条目挤出窗口（实测 23 条订阅命中里 14 条落在窗口外）—— 先按归属取
        # 才不会漏，通用的那次用于兜住期刊映射路线（那批内容不属于任何用户）。
        owned_probe = _windowed(select(FeedItem)).where(
            cast(FeedItem.extra["profile_user_ids"], String).like(
                f'%"{profile.user_id}"%'
            )
        ).order_by(recent_order()).limit(limit)
        generic_probe = _windowed(select(FeedItem)).order_by(
            recent_order()
        ).limit(limit)
        probe_rows = list((await db.execute(owned_probe)).scalars().all())
        probe_rows += list((await db.execute(generic_probe)).scalars().all())

        known = {str(item.id) for item in items}
        wanted_journals = set(profile.journals)
        wanted_scholars = {str(value).casefold().strip() for value in profile.scholars}
        for item in probe_rows:
            if str(item.id) in known:
                continue
            venue_key = _journal_key(item.venue)
            by_journal = bool(venue_key) and any(
                venue_key == _journal_key(value) for value in wanted_journals
            )
            by_scholar = any(
                str(author).casefold().strip() in wanted_scholars
                for author in (item.authors or [])
            )
            if by_journal or by_scholar:
                items.append(item)
                known.add(str(item.id))
    # A catalog projection is fail-closed: rows produced under an older or
    # subsequently invalidated journal policy disappear until refreshed as
    # explicitly eligible.  Ordinary feed rows are unaffected.
    items = [
        item
        for item in items
        if not (item.extra or {}).get("literature_catalog")
        or (item.extra or {}).get("literature_eligible") is True
    ]
    if profile.domains or profile.journals:
        # 期刊映射会被纠错；旧记录里复制的 domains 不能永远当真。每次送达前
        # 以当前可信映射复核期刊名，让曾由 port/sport、space 等误命中的历史
        # 条目立即退出，而不要求删除共享论文 Index。
        from app.services.literature_harvester import _journal_targets, _normal_journal

        current_targets = _journal_targets(set(profile.domains))
        allowed_journals = {
            _normal_journal(target.get("journal"))
            for target in current_targets.values()
        }
        # 用户**显式订阅**的刊同样放行：他点名要的刊不该被"订阅学科映射表"
        # 挡在外面（那是我方推断，subscription 是他自己说的）。
        allowed_journals |= set(profile.journals)
        items = [
            item for item in items
            if not (
                (item.extra or {}).get("literature_catalog")
                and "journal" in acquisition_routes(item)
            )
            or _normal_journal(item.venue) in allowed_journals
        ]
    if profile.suppressed_item_ids:
        suppressed = {str(value) for value in profile.suppressed_item_ids}
        items = [i for i in items if str(i.id) not in suppressed]
    # 出版商/数据库（论文形态）无摘要的不进推荐池：见 show_in_feed。
    items = [i for i in items if show_in_feed(i)]
    return items


def acquisition_routes(item: FeedItem) -> frozenset[str]:
    extra = item.extra if isinstance(item.extra, dict) else {}
    raw = extra.get("feed_acquisition_routes")
    return frozenset(str(route) for route in raw) if isinstance(raw, list) else frozenset()


def _has_summary(item: FeedItem) -> bool:
    return bool(item.summary and item.summary.strip())


def _requires_summary(item: FeedItem) -> bool:
    """论文形态且非网页发现时必须有摘要；截稿/发布/分享/周报与网页发现不要求。"""
    return item.kind == FeedItemKind.PAPER and "web" not in acquisition_routes(item)


def show_in_feed(item: FeedItem) -> bool:
    """是否有资格进入推荐池。

    候选池（candidate_items）的过滤收口在这里，日报缓存复用等其它入口也复用，
    避免任何一条拼装路径绕过滤出「无摘要的出版商/数据库论文」。
    """
    return not _requires_summary(item) or _has_summary(item)


def rank_field_dynamics(
    items: list[FeedItem], profile: Profile, *, now: datetime | None = None
) -> list[ScoredItem]:
    """期刊路线的领域动态：分区45%＋引用45%＋时效10%。"""
    moment = now or datetime.now(UTC)
    ranked: list[ScoredItem] = []
    for item in items:
        if "journal" not in acquisition_routes(item):
            continue
        domain_hits = _domain_hits(item, profile)
        # 订阅的刊独立于订阅学科成立：用户点名的刊，即使不属于他选的二级学科
        # （跨学科期刊），它的新文章也该出现在"本领域动态"里。
        subscribed_journal = matches_subscribed_journal(item, profile)
        if profile.domains and not domain_hits and not subscribed_journal:
            continue
        extra = item.extra if isinstance(item.extra, dict) else {}
        try:
            quartile = int(extra.get("cas_quartile"))
        except (TypeError, ValueError):
            quartile = 0
        partition = {1: 1.0, 2: 0.8, 3: 0.5, 4: 0.3}.get(quartile, 0.0)
        try:
            citations = max(0, int(extra.get("citations") or 0))
        except (TypeError, ValueError):
            citations = 0
        citation = min(1.0, math.log2(1 + citations) / math.log2(501))
        score = 0.45 * partition + 0.45 * citation + 0.10 * _recency_score(
            item.published_at, now=moment
        )
        ranked.append(
            ScoredItem(
                item=item,
                score=score,
                matched_domains=domain_hits,
                matched_journal=(item.venue or "").strip() if subscribed_journal else "",
            )
        )
    ranked.sort(
        key=lambda value: (
            -value.score,
            -(_aware(value.item.published_at) or moment).timestamp(),
        )
    )
    return ranked


def rank(
    items: list[FeedItem], profile: Profile, *, now: datetime | None = None
) -> list[ScoredItem]:
    """给整个候选池排序。

    术语只抽一遍、区分度按这一池现算 —— 两者都必须在**同一个池**上做，否则
    "这个词罕不罕见"回答的就不是"在用户今天看得到的东西里罕不罕见"。
    """
    moment = now or datetime.now(UTC)
    tokenized = [extract_terms(item.title, item.summary) for item in items]
    discrimination = TermDiscrimination(tokenized)
    scored = [
        score_item(item, profile, now=moment, discrimination=discrimination, item_terms=terms)
        for item, terms in zip(items, tokenized, strict=True)
    ]
    scored.sort(key=lambda s: (-s.score, -(_aware(s.item.published_at) or moment).timestamp()))
    return scored


REDNOTE_ROUTE_PATTERN = (
    "project_profile", "journal", "scholar", "project_profile", "web",
    "project_profile", "bignews", "journal", "scholar", "project_profile",
    "web", "project_profile", "journal", "bignews",
)


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _partition_score(extra: dict) -> float:
    raw = extra.get("jcr_quartile") or extra.get("cas_quartile") or ""
    match = str(raw).upper().strip().removeprefix("Q")
    try:
        quartile = int(match)
    except ValueError:
        return 0.0
    return {1: 1.0, 2: 0.8, 3: 0.5, 4: 0.3}.get(quartile, 0.0)


def _citation_score(extra: dict) -> float:
    citations = max(0.0, _number(extra.get("citations")))
    return min(1.0, math.log2(1.0 + citations) / math.log2(501.0))


def _source_quality(extra: dict) -> float:
    source = str(extra.get("source") or "").lower()
    if source in {"crossref", "openalex", "pubmed"}:
        return 1.0
    if source in {"semantic_scholar", "arxiv", "biorxiv", "medrxiv"}:
        return 0.85
    return 0.6


def matches_subscribed_journal(item: FeedItem, profile: Profile) -> bool:
    """这条是不是用户订阅的那本刊发的。

    每次送达时**现算**，不靠采集时写进 extra 的标记：订阅是可以被取消的，
    而 extra 是历史事实 —— 取消订阅后旧标记还在，用户会继续看到那本刊。

    两边都过同一个归一化函数：`build_profile` 存进画像的已经是键，但画像被
    别处直接构造时可能塞进来原始刊名（`Nature Machine Intelligence` 与
    `naturemachineintelligence` 是同一本刊）。只归一化一边，"订阅了却推不
    出来"会静默发生 —— 没有报错、没有日志，只有一个空列表。
    """
    if not profile.journals:
        return False
    key = journal_key(item.venue)
    return bool(key) and any(key == journal_key(value) for value in profile.journals)


def matches_subscribed_scholar(item: FeedItem, profile: Profile) -> bool:
    """这条的作者里有没有用户订阅的学者（同样现算，理由同上）。

    比对走 `profile.author_matches` 而不是字符串相等：同一个人的姓名在
    Crossref / OpenAlex / PubMed 里可能是 `Shaoda Liu`、`Liu, Shaoda` 或
    `S. Liu`，精确相等会把它们全判为"不是他"。
    """
    if not profile.scholars:
        return False
    return any(
        author_matches(wanted, author)
        for author in (item.authors or [])
        for wanted in profile.scholars
    )


def _route_score(
    base: ScoredItem, route: str, *, profile: Profile, now: datetime
) -> float | None:
    extra = base.item.extra if isinstance(base.item.extra, dict) else {}
    freshness = _recency_score(base.item.published_at or base.item.created_at, now=now)
    citation = _citation_score(extra)

    if route == "project_profile":
        # Historical acquisition scores cannot authorize current recommendations.
        relevance = 1.0 if base.project_id else 0.0
        if relevance <= 0.0:
            return None
        return 0.70 * relevance + 0.10 * _source_quality(extra) + 0.10 * freshness + 0.10 * citation
    if route == "journal":
        # 订阅期刊（用户自己点名的刊）与订阅学科同级：命中任一即可进推荐。
        subscribed = matches_subscribed_journal(base.item, profile)
        relevance = 1.0 if base.project_id else (
            0.85 if subscribed else (0.55 if base.matched_domains else 0.0)
        )
        # 用户已有画像时，期刊文章必须至少命中课题、订阅期刊或所选学科；没有
        # 画像时仍允许按质量展示公共期刊动态，避免新用户看到空白页。
        if relevance <= 0.0 and (base.matched_domains or base.project_id or subscribed):
            return None
        return 0.55 * relevance + 0.20 * _partition_score(extra) + 0.15 * freshness + 0.10 * citation
    if route == "scholar":
        # 订阅学者：他发了什么就推什么。这是最无歧义的信号（用户亲自点名的人），
        # 所以不要求命中学科 —— 否则跨学科的学者订阅等于没订。
        if not matches_subscribed_scholar(base.item, profile):
            return None
        relevance = 1.0 if base.project_id else 0.9
        return 0.60 * relevance + 0.20 * _source_quality(extra) + 0.20 * freshness
    if route == "bignews":
        # 可信编辑源只占探索位：与课题命中时优先，否则按新鲜度和来源权威性。
        relevance = 1.0 if base.project_id else (0.55 if base.matched_domains else 0.0)
        authority = min(1.0, max(0.0, _number(extra.get("editorial_authority"), 0.85)))
        return 0.50 * relevance + 0.30 * freshness + 0.20 * authority
    if route == "web":
        relevance = 1.0 if base.project_id else 0.0
        if relevance <= 0.0:
            return None
        authority = min(1.0, max(0.0, _number(extra.get("web_authority"))))
        traceability = min(1.0, max(0.0, _number(extra.get("web_traceability"))))
        return 0.60 * relevance + 0.20 * authority + 0.15 * freshness + 0.05 * traceability
    return None


def _recommendation_identity(item: FeedItem) -> str:
    """Deduplicate the same work across DOI, repository and web discovery routes."""
    extra = item.extra if isinstance(item.extra, dict) else {}
    doi = str(extra.get("doi") or "").strip().lower()
    if doi:
        return "doi:" + doi
    normalized_title = "".join(ch.lower() for ch in item.title if ch.isalnum())
    return "title:" + normalized_title if normalized_title else "item:" + str(item.id)


async def apply_behavior_preferences(
    db: AsyncSession, *, user_id: str, scored: list[ScoredItem], now: datetime | None = None,
) -> list[ScoredItem]:
    """Use recent explicit behavior as a bounded re-rank signal.

    Opens and saves are positive, dismissals are strongly negative, and all
    evidence decays with time. Impressions are deliberately neutral: merely
    being shown something must not be mistaken for liking it.
    """
    if not scored:
        return scored
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=180)
    rows = (await db.execute(
        select(FeedEngagement, FeedItem).join(
            FeedItem, FeedItem.id == FeedEngagement.item_id
        ).where(
            FeedEngagement.user_id == user_id,
            FeedEngagement.created_at >= cutoff,
            FeedEngagement.action.in_([
                FeedEngagementAction.OPEN, FeedEngagementAction.SAVE,
                FeedEngagementAction.DISMISS,
            ]),
        )
    )).all()
    if not rows:
        return scored
    term_weights: dict[str, float] = {}
    venue_weights: dict[str, float] = {}
    author_weights: dict[str, float] = {}
    action_weights = {
        FeedEngagementAction.OPEN: 1.0,
        FeedEngagementAction.SAVE: 3.0,
        FeedEngagementAction.DISMISS: -4.0,
    }
    for engagement, item in rows:
        created = _aware(engagement.created_at) or moment
        age_days = max(0.0, (moment - created).total_seconds() / 86400.0)
        decay = math.pow(0.5, age_days / 45.0)
        weight = action_weights.get(engagement.action, 0.0) * decay
        for term in extract_terms(item.title, item.summary):
            term_weights[term] = term_weights.get(term, 0.0) + weight
        venue = str(item.venue or "").casefold().strip()
        if venue:
            venue_weights[venue] = venue_weights.get(venue, 0.0) + weight
        for author in item.authors or []:
            name = str(author).casefold().strip()
            if name:
                author_weights[name] = author_weights.get(name, 0.0) + weight

    adjusted: list[ScoredItem] = []
    for value in scored:
        item = value.item
        terms = extract_terms(item.title, item.summary)
        topic = sum(sorted((term_weights.get(term, 0.0) for term in terms), reverse=True)[:5])
        venue = venue_weights.get(str(item.venue or "").casefold().strip(), 0.0)
        authors = max((author_weights.get(str(author).casefold().strip(), 0.0) for author in item.authors or []), default=0.0)
        affinity = max(-1.0, min(1.0, (topic / 10.0) + (venue / 8.0) + (authors / 8.0)))
        adjusted.append(replace(value, score=value.score + 0.20 * affinity))
    adjusted.sort(key=lambda value: (
        -value.score,
        -(_aware(value.item.published_at or value.item.created_at) or moment).timestamp(),
    ))
    return adjusted


def rank_rednote(
    items: list[FeedItem], profile: Profile, *, user_id: str,
    now: datetime | None = None,
) -> list[ScoredItem]:
    """多路独立评分，再按动态配额混排并做基础来源多样性。

    推荐 = project（课题检索，网络 + 学术数据库）+ 学科 + 订阅期刊 + 订阅学者。
    这四路互不覆盖：只订了某本刊或某位学者、却没订对应学科时，那些成果也要
    进推荐流 —— 否则"订阅"只影响订阅页，推荐页看不见，等于半截功能。
    """
    moment = now or datetime.now(UTC)
    base_scored = rank(items, profile, now=moment)
    buckets: dict[str, list[ScoredItem]] = {
        "project_profile": [], "journal": [], "scholar": [], "web": [], "bignews": [],
    }
    for base in base_scored:
        routes = acquisition_routes(base.item)
        extra = base.item.extra if isinstance(base.item.extra, dict) else {}
        owners = {str(value) for value in (extra.get("profile_user_ids") or [])}
        for route in routes & set(buckets):
            # project_profile / web 是「这个用户自己的 project」触发的课题路线。
            if route in {"project_profile", "web"} and str(user_id) not in owners:
                continue
            # 手选学科是 journal 路线（本领域动态）的推荐边界：选了学科后，期刊
            # 必须命中订阅学科，不能让「碰巧与旧 Project 重叠两个词」的论文混进
            # 学科动态。而 project_profile / web 是课题路线（可能与你相关），靠
            # project 检索相关度通过、独立于订阅学科 —— 关键词捞到的不属于任何
            # 订阅学科的内容也合法。没订阅（但有课题）时保留纯 Project 推荐。
            if route == "journal":
                subscribed = matches_subscribed_journal(base.item, profile)
                if not subscribed:
                    if profile.domains and not base.matched_domains:
                        continue
                    if not profile.domains and profile.projects and not (
                        base.project_id or base.matched_domains
                    ):
                        continue
            score = _route_score(base, route, profile=profile, now=moment)
            if score is not None:
                buckets[route].append(replace(base, score=score))
    for values in buckets.values():
        values.sort(key=lambda value: (
            -value.score,
            -(_aware(value.item.published_at or value.item.created_at) or moment).timestamp(),
        ))

    positions = {route: 0 for route in buckets}
    seen: set[str] = set()
    mixed: list[ScoredItem] = []
    total_unique = len({
        _recommendation_identity(value.item)
        for values in buckets.values() for value in values
    })
    while len(mixed) < total_unique:
        advanced = False
        for route in REDNOTE_ROUTE_PATTERN:
            values = buckets[route]
            while positions[route] < len(values):
                candidate = values[positions[route]]
                positions[route] += 1
                identity = _recommendation_identity(candidate.item)
                if identity in seen:
                    continue
                seen.add(identity)
                mixed.append(candidate)
                advanced = True
                break
        if not advanced:
            break

    # 前排同一期刊/网站最多两条；不足项随后回填，不因此丢失候选。
    diverse: list[ScoredItem] = []
    deferred: list[ScoredItem] = []
    source_counts: dict[str, int] = {}
    for candidate in mixed:
        source_key = (candidate.item.venue or url_host(candidate.item.url) or "unknown").lower()
        if source_counts.get(source_key, 0) >= 2:
            deferred.append(candidate)
            continue
        source_counts[source_key] = source_counts.get(source_key, 0) + 1
        diverse.append(candidate)
    return diverse + deferred


def url_host(raw: str | None) -> str:
    if not raw:
        return ""
    from urllib.parse import urlparse
    return urlparse(raw).hostname or ""


def choose_daily_picks(
    scored: list[ScoredItem], *, count: int = DAILY_PICK_COUNT
) -> list[ScoredItem]:
    """选出今天的 Top N，**尽量覆盖不同课题**。

    用户有几个课题在跑，日报就该替他各看一眼，而不是三条全砸在同一个课题上
    （那样另外两个课题的动态他永远看不到）。所以先每个课题取最强的一条，
    不够再按分数补齐。
    """
    picks: list[ScoredItem] = []
    used_projects: set[str] = set()
    for candidate in scored:
        if len(picks) >= count:
            break
        if candidate.project_id and candidate.project_id not in used_projects:
            picks.append(candidate)
            used_projects.add(candidate.project_id)
    for candidate in scored:
        if len(picks) >= count:
            break
        if candidate not in picks:
            picks.append(candidate)
    return picks[:count]


async def suppressed_ids(db: AsyncSession, *, user_id: str) -> frozenset[str]:
    """这个用户已经处理过、不该再推的条目。

    `dismiss` 是永久的；`impression` 只压当天的选摘（别人一天里刷新几次不该
    换一批），所以它不进这个集合 —— 它由 `feed_daily_picks` 那张表天然实现。
    """
    rows = (
        await db.execute(
            select(FeedEngagement.item_id).where(
                FeedEngagement.user_id == user_id,
                FeedEngagement.action == FeedEngagementAction.DISMISS,
            )
        )
    ).scalars().all()
    return frozenset(rows)


async def daily_picks(
    db: AsyncSession, *, user_id: str, scored: list[ScoredItem], today: date | None = None
) -> list[dict]:
    """今天的选摘：有就读出来，没有就算一次并存下。

    存的是 item_id 的引用，不是内容副本 —— 内容后续会被补全（摘要、热度），
    存副本就是让当天的选摘停在选中那一刻的样子。
    """
    when = today or datetime.now(UTC).date()
    existing = await db.get(FeedDailyPick, (user_id, when))
    if existing is not None and existing.picks:
        return list(existing.picks)

    # 轮换机制：新的一天优先避开用户近 14 天已经选中的论文。
    history_rows = (await db.execute(
        select(FeedDailyPick).where(
            FeedDailyPick.user_id == user_id,
            FeedDailyPick.pick_date >= when - timedelta(days=CANDIDATE_WINDOW_DAYS),
            FeedDailyPick.pick_date < when,
        )
    )).scalars().all()
    seen = {
        str(item_id)
        for row in history_rows
        for payload_item in (row.picks or [])
        if isinstance(payload_item, dict)
        for item_id in [payload_item.get("item_id")]
        if item_id
    }
    fresh = [candidate for candidate in scored if str(candidate.item.id) not in seen]
    # 全部候选都看过时允许循环，避免资讯流变成空白。
    chosen = choose_daily_picks(fresh or scored)
    payload = [
        {
            "item_id": str(pick.item.id),
            "reason": pick.reason(),
            "project_id": pick.project_id,
        }
        for pick in chosen
    ]
    if existing is None:
        db.add(FeedDailyPick(user_id=user_id, pick_date=when, picks=payload))
    else:
        existing.picks = payload
    await db.flush()
    return payload
