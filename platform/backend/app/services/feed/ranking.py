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
import re
import sys
from pathlib import Path
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import String, and_, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feed import (
    FeedDailyPick,
    FeedEngagement,
    FeedEngagementAction,
    FeedItem,
    FeedItemKind,
)
from app.services.feed import domains as domain_service
from app.services.feed import windows
from app.services.feed.profile import Profile, author_matches, extract_terms, journal_key, journal_display_name

#: 每天的选摘条数。三条是"读得完"和"有得挑"之间的位置。
DAILY_PICK_COUNT = 3

#: 参与排序的候选窗口。再往前的东西不叫资讯。
CANDIDATE_WINDOW_DAYS = windows.ACADEMIC_WINDOW_DAYS

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
# 中文课题检索出英文论文时，原检索词提供第二种可核对的关联证据。
MIN_PROJECT_QUERY_RELEVANCE = 0.5
MIN_PROJECT_QUERY_STRENGTH = 0.6

# 方法/过程词不能单独证明领域相关性；必须和至少一个非泛化领域锚点共同出现。
GENERIC_PROJECT_TERMS = frozenset({
    "model", "models", "modeling", "modelling", "dynamics", "dynamic",
    "framework", "frameworks", "kinetic", "kinetics", "growth", "time",
    "event", "events", "force", "forces", "field", "fields", "system",
    "systems", "process", "processes", "analysis", "simulation",
    "simulations", "investigation", "investigations", "method", "methods",
    "design", "designs",
})
GENERIC_QUERY_TERMS = GENERIC_PROJECT_TERMS | frozenset({
    "control", "study", "research", "parameter", "parameters",
    "identification", "trajectory", "tracking", "lateral", "vertical",
    "fault-tolerant",
})
VEHICLE_CONTEXT_TERMS = frozenset({
    "vehicle", "vehicles", "automotive", "automobile", "car", "cars",
    "wheel", "wheels", "robot", "robots", "robotic", "steering",
    "brake", "braking", "suspension", "locomotion", "tire", "tyre",
    "brake-by-wire", "steer-by-wire",
})



# 只约束明确的 MLIP 课题；保留材料/原子模拟交叉主题，不要求检索词全部出现。
_MLIP_TOPIC = re.compile(
    r"\bmlips?\b|interatomic[\s-]+potential|原子间势|机器学习势|(?:AI|机器学习).*势函数", re.I
)
_MLIP_CONTEXT = re.compile(
    r"\b(?:mlips?|mlffs?|interatomic|atomistic|phonons?|crystals?|thermoelectrics?|dft)\b"
    r"|molecular[\s-]+dynamics|potential[\s-]+energy[\s-]+surface"
    r"|density[\s-]+functional|materials?[\s-]+propert"
    r"|machine[\s-]*(?:learning|learned)[\s-]+force[\s-]*fields?"
    r"|neural[\s-]+network[\s-]+potentials?"
    r"|原子间势|机器学习(?:势|力场)|神经网络势|原子模拟|分子动力学|声子|晶体|材料性能|材料性质|热电", re.I
)


def _particle_settling_topic(topic: str) -> bool:
    return bool(re.search(r"\b(?:lbm|boltzmann)\b|格子玻尔兹曼", topic, re.I)
                and re.search(r"sedimentation|settling|沉降", topic, re.I))


def _particle_physics_evidence(topic: str, title: str, summary: str | None) -> tuple[str, ...]:
    """Precise physical concepts supplement token rarity for this project only."""
    if not _particle_settling_topic(topic):
        return ()
    normalize = lambda text: re.sub(r"[‐‑‒–—−]", "-", text).casefold()
    heading = normalize(title)
    text = heading + " " + normalize(summary or "")
    # These meanings share shape/lattice words but do not establish particle-fluid physics.
    if re.search(r"interatomic|phonon|perovskite|atom\s+refinement|quantum|electrostatic|"
                 r"optical|light\s+pressure|acoustic\s+radiation|astigmatism|"
                 r"protoplanet|astrophys|planetary|cosmolog|warfare|robotic\s+manipulator|"
                 r"^\s*(?:reply|comment|correction|erratum|retraction)\b|erratum\s*$", heading):
        return ()
    particle = re.search(r"\b(?:particles?|spheres?|spheroids?|colloids?|microplastics?|beads?)\b|颗粒|粒子|球体", heading)
    settling = re.search(r"\b(?:settling|sediment(?:ation|ing)?|terminal[\s-]+(?:settling[\s-]+)?velocity)\b|沉降|沉速", heading)
    fluid = re.search(r"\b(?:fluids?|flows?|hydrodynamic\w*|liquids?|viscous|newtonian|turbulen\w*|shear|water)\b|流体|流动|流固|黏性|粘性", text)
    lbm = re.search(r"lattice[\s-]+boltzmann|\blbm\b|格子玻尔兹曼", heading)
    ibm = re.search(r"immersed[\s-]+boundary|浸入边界", heading)
    particle_in_text = re.search(r"\b(?:particles?|spheres?|spheroids?|colloids?|microplastics?|beads?)\b|颗粒|粒子|球体", text)
    astrophysics = re.search(r"protoplanet|astrophys|planetary\s+(?:disk|formation)", text)
    explicit_velocity = re.search(r"\bsettling\b|terminal[\s-]+(?:settling[\s-]+)?velocity|沉降速度|沉速", heading)
    if settling and not astrophysics and (particle or (particle_in_text and fluid and explicit_velocity)):
        return ((particle or particle_in_text).group(), settling.group())
    if lbm and ibm:
        return (lbm.group(), ibm.group())
    method_focus = re.search(
        r"\b(?:boundar(?:y|ies)|kernel|interface|solver|scheme|schemes|formulation|"
        r"algorithm|parallelization|parallel|grids?|collision|subgrid|distributions?)\b|"
        r"^\s*(?:a\s+)?(?:new\s+)?lattice[\s-]+boltzmann\s+(?:models?|methods?)\b|"
        r"边界|核函数|界面|求解器|格式|算法|并行|网格", heading,
    )
    if (lbm or ibm) and fluid and method_focus:
        return ((lbm or ibm).group(), fluid.group())
    motion = re.search(r"\b(?:drag|lift|wakes?|torques?|migration|orientation|transport|motion|dispersion)\b|曳力|阻力|升力|尾流|力矩|取向|运动", heading)
    if particle and motion and fluid:
        return (particle.group(), motion.group(), fluid.group())
    return ()


def _project_subject_matches(topic: str, title: str, summary: str | None) -> bool:
    text = re.sub(r"[‐‑‒–—−]", "-", f"{title} {summary or ''}")
    # For a named battery chemistry, a passing mention in the abstract must
    # not turn a different ion system into this project's result. The study's
    # title must identify the target chemistry (including common Na notation).
    if re.search(r"sodium[\s-]*ion|钠离子|钠电池", topic, re.I) and not re.search(
        r"\bsodium\b|\bna[\s-]*ion\b|\bna\+|\bna storage\b|钠", title, re.I
    ):
        return False
    if _MLIP_TOPIC.search(topic):
        return _MLIP_CONTEXT.search(text) is not None
    # Crystal lattices and phonon Boltzmann transport share words with LBM,
    # but do not establish a connection to fluid–particle sedimentation.
    if (re.search(r"\b(?:lbm|boltzmann)\b|格子玻尔兹曼", topic, re.I)
            and re.search(r"sedimentation|settling|沉降", topic, re.I)):
        particle = re.search(r"\b(?:particles?|spheres?|spheroids?|colloids?)\b|颗粒|粒子|球体", text, re.I)
        settling = re.search(r"sediment|settling|terminal[\s-]+velocity|沉降|沉速", text, re.I)
        method = re.search(r"lattice[\s-]+boltzmann|immersed[\s-]+boundary|\b(?:lbm|ibm)\b|格子玻尔兹曼|浸入边界", text, re.I)
        fluid = re.search(r"\b(?:fluids?|flows?|hydrodynamic\w*)\b|fluid[\s-]+(?:solid|particle)|流体|流动|流固", text, re.I)
        particle_motion = re.search(r"\b(?:drag|wakes?|non[\s-]*spherical|irregular)\b|曳力|阻力|尾流|非球形|不规则", text, re.I)
        return bool((particle and settling) or (method and (fluid or particle or settling))
                    or (particle and fluid and particle_motion))
    return True


def _current_query_title_matches(profile: Profile, title: str) -> bool:
    """Credit a project only when the title shares a concrete curated facet.

    Abstracts may mention many neighboring applications. Without a title
    anchor, a lithium-electrolyte or carbon-nanotube paper can be labelled as
    a sodium hard-carbon project merely because its abstract says battery or
    efficiency. The curator's current queries provide a topic-specific guard
    without a field-specific word blacklist.
    """
    queries = profile.active_project_queries
    if not queries:
        return True
    title_terms = extract_terms(title)
    return any(len(title_terms & extract_terms(query)) >= 2 for query in queries)


def _vehicle_dynamics_title_evidence(query: str, title: str) -> tuple[str, ...]:
    """Recover road-vehicle project matches when Chinese project text has no English terms.

    Historical keyword scores often rate generic dynamics papers highly.  This
    route requires specific evidence in the *title*: a by-wire system, or a
    road vehicle together with a mechanical-dynamics topic.  It applies only
    to discovery queries that explicitly ask for vehicle dynamics or by-wire.
    """
    query = query.casefold()
    if not ("vehicle dynamics" in query or "steer-by-wire" in query or "brake-by-wire" in query):
        return ()
    title = title.casefold().replace("‐", "-").replace("–", "-").replace("‑", "-")
    for term in ("steer-by-wire", "brake-by-wire", "x-by-wire"):
        if term in title:
            return (term,)
    if re.search(r"\b(?:aerial|underwater|marine|deep-sea|orbital|interplanetary|"
                 r"flight|routing|charging|batteries|battery|microgrids?|course)\b", title):
        return ()
    if not re.search(r"\b(?:vehicles?|automotive|automobile|cars?|trucks?|buses)\b", title):
        return ()
    for term, pattern in (
        ("vehicle dynamics", r"\bdynamics?\b"),
        ("vehicle chassis", r"\bchassis\b"),
        ("vehicle suspension", r"\bsuspension\b"),
        ("vehicle steering", r"\bsteering\b"),
        ("vehicle braking", r"\bbrak(?:e|ing)\b"),
        ("vehicle torque", r"\btorque\b"),
        ("vehicle handling", r"\bhandling\b"),
        ("vehicle longitudinal control", r"\blongitudinal\b"),
        ("vehicle lateral control", r"\blateral\b"),
    ):
        if re.search(pattern, title):
            return (term,)
    return ()

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
    matched_query_terms: tuple[str, ...] = ()

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
            return f"你订阅的期刊 {journal_display_name(self.matched_journal)} 有新文章"
        if self.matched_scholar:
            return f"你订阅的学者 {self.matched_scholar} 有新论文"
        if self.matched_query_terms:
            return f"按你的课题检索词找到：{'、'.join(self.matched_query_terms[:3])}"
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


def _effective_date(item: FeedItem) -> datetime | None:
    """Month-only source dates use discovery time for ordering, not fake day one."""
    extra = item.extra if isinstance(item.extra, dict) else {}
    if extra.get("publication_date_precision") == "month":
        return item.created_at or item.published_at
    return item.published_at or item.created_at


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
    slugs = item.domains or ()
    extra = item.extra if isinstance(item.extra, dict) else {}
    if item.kind == FeedItemKind.PAPER and (
        extra.get("classification_mode") in {"online_journal_mapping", "offline_journal_mapping", "offline_mapped_journal"}
        or extra.get("journal_harvest_verified") is True
    ):
        from app.config import settings
        harness_root = settings.harness_root or str(Path(__file__).resolve().parents[5])
        if harness_root not in sys.path:
            sys.path.insert(0, harness_root)
        from nodes.literature.tools.journal_scope import current_domains
        slugs = current_domains(item.venue or "", slugs, item.title or "", item.summary or "")
    for slug in slugs:
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
        if not _project_subject_matches(signal.name + " " + " ".join(signal.terms), item.title, item.summary):
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

    if best is not None and not _current_query_title_matches(profile, item.title):
        best = None

    domain_hits = _domain_hits(item, profile)

    matched_query_terms: tuple[str, ...] = ()
    extra = item.extra if isinstance(item.extra, dict) else {}
    routes = acquisition_routes(item)
    query = str(extra.get("discovery_query") or "")
    primary_query_term = query.split()[0] if query.split() else ""
    query_subject_matches = bool(query) and _project_subject_matches(query, item.title, item.summary)
    # Chinese model queries often have a specific subject first, followed by a
    # broad method. A hit on the method alone is not evidence for the subject.
    primary_missed = bool(
        "project_profile" in routes
        and re.fullmatch(r"[\u4e00-\u9fff]{2,}", primary_query_term)
        and primary_query_term not in f"{item.title} {item.summary or ''}"
    )
    if primary_missed:
        best = None
    owners = {str(value) for value in (extra.get("profile_user_ids") or [])}
    query_is_current = profile.active_project_queries is None or query in profile.active_project_queries
    active_project_subject = any(
        (_project_subject_matches(signal.name + " " + " ".join(signal.terms), item.title, item.summary)
         or _particle_physics_evidence(signal.name + " " + " ".join(signal.terms), item.title, item.summary))
        for signal in profile.projects
    )
    if (query_is_current and active_project_subject and profile.projects
            and str(profile.user_id) in owners and routes & {"project_profile", "web"}):
        try:
            query_relevance = float(extra.get("profile_query_relevance") or 0)
        except (TypeError, ValueError):
            query_relevance = 0.0
        if query and not primary_missed and query_subject_matches and query_relevance >= MIN_PROJECT_QUERY_RELEVANCE:
            overlap = terms & extract_terms(query)
            specific = overlap - GENERIC_QUERY_TERMS
            # Chassis is also a biological production host. Vehicle alone can
            # mean a scanning platform or other unrelated transport. Require
            # one additional mobility/vehicle clue before either authorizes a
            # project recommendation.
            if "chassis" in specific and not (terms & VEHICLE_CONTEXT_TERMS):
                specific -= {"chassis"}
            if "vehicle" in specific and not (terms & (VEHICLE_CONTEXT_TERMS - {"vehicle"})):
                specific -= {"vehicle"}
            if len(overlap) >= 2 and specific and sum(
                discrimination.weight(term) for term in specific
            ) >= MIN_PROJECT_QUERY_STRENGTH:
                matched_query_terms = discrimination.most_distinctive(specific)
        # A precise title is stronger evidence than the old broad search
        # score.  Keep this independent of that score so genuinely relevant
        # papers with low retrieval scores can still enter the project lane.
        if query and not primary_missed and query_subject_matches and not matched_query_terms and "project_profile" in routes:
            matched_query_terms = _vehicle_dynamics_title_evidence(query, item.title)
        if query and not primary_missed and not matched_query_terms and "project_profile" in routes:
            for signal in profile.projects:
                evidence = _particle_physics_evidence(
                    signal.name + " " + " ".join(signal.terms), item.title, item.summary,
                )
                if evidence:
                    matched_query_terms = evidence
                    break

    score = 0.0
    if best is not None:
        score += WEIGHT_PROJECT_MATCH * min(best[0] / PROJECT_MATCH_SATURATION, 1.0)
    if domain_hits:
        score += WEIGHT_DOMAIN_MATCH
    score += WEIGHT_RECENCY * _recency_score(_effective_date(item), now=now)
    score += WEIGHT_ATTENTION * _attention_score(item)

    # 订阅命中也要进理由：它是用户亲自点名的依据，跟课题、学科同级。
    matched_journal = ""
    matched_scholar = ""
    if profile.journals and matches_subscribed_journal(item, profile):
        matched_journal = (item.venue or "").strip()
    if profile.scholars and matches_subscribed_scholar(item, profile):
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
        matched_query_terms=matched_query_terms,
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
    return case(
        (FeedItem.extra["publication_date_precision"].as_string() == "month", FeedItem.created_at),
        else_=func.coalesce(FeedItem.published_at, FeedItem.created_at),
    ).desc()


def publication_window_clause(now: datetime):
    """Shared SQL window: network discovery 12 months, passive content 31 days."""
    def date_window(start: datetime):
        return (
            (FeedItem.published_at.is_(None) & (FeedItem.created_at >= start))
            | ((FeedItem.published_at >= start) & (FeedItem.published_at <= now))
            | (
                (FeedItem.extra["publication_date_precision"].as_string() == "month")
                & (FeedItem.published_at >= start.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
                & (FeedItem.published_at <= now)
            )
        )

    # Journal, scholar, project and web routes all call network search APIs.
    # Passive imports and public feed items retain the 31-day default.
    online_routes = or_(*(
        cast(FeedItem.extra["feed_acquisition_routes"], String).like(f"%{route}%")
        for route in ("journal", "scholar", "project_profile", "web")
    ))
    online_item = func.coalesce(
        (FeedItem.extra["source"].as_string() == "web")
        | (FeedItem.extra["web_search_backend"].as_string().is_not(None)
           & (FeedItem.extra["web_search_backend"].as_string() != ""))
        | online_routes,
        False,
    )
    return (
        (online_item & date_window(windows.web_cutoff(now)))
        | (~online_item & date_window(windows.academic_cutoff(now)))
    )


async def candidate_items(
    db: AsyncSession, *, profile: Profile, batch_size: int = 400, include_preparing: bool = False
) -> list[FeedItem]:
    """取候选池：窗口内、这个用户可见、还没被他处理掉的。

    可见性在**查询里**收口，不在打分之后过滤：过滤写在后面就意味着一条
    org 内容会先被算分、再被丢掉，而任何一处忘了过滤都是越权泄露。
    """
    from app.models.feed import FeedVisibility

    # Retained for callers that pass batch_size; the full candidate set is
    # deliberately uncapped and is fetched with one database query.
    now = datetime.now(UTC)
    def _windowed(statement):
        """窗口 + 可见性 —— 主候选池与订阅补充扫描共用同一套条件。

        两处各写一份的话，一边收紧一边放宽会静默分叉（比如订阅扫描放进来一条
        已经过期的条目），而症状只表现为"偶尔多一条奇怪的"。
        """
        return statement.where(
            or_(
                FeedItem.visibility == FeedVisibility.PLATFORM,
                and_(FeedItem.visibility == FeedVisibility.INSTITUTION,
                     FeedItem.organization_id == profile.institution_id),
            ),
            publication_window_clause(now),
        )

    early_order = [case((FeedItem.extra["literature_preparation"].as_string() == "ready", 1), else_=0)] if include_preparing else []
    query = _windowed(select(FeedItem)).order_by(
        *early_order, recent_order(), FeedItem.id
    )
    # 六位代码先缩小主扫描范围；跨学科的订阅和课题由下方完整补充扫描覆盖。
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
    # Fetch all matching rows once. OFFSET pagination made PostgreSQL repeat
    # the same sort and window scan for every 400 rows (27 times on the demo
    # account) while we still retained every row in memory for ranking.
    items = list((await db.execute(query)).scalars().all())
    known = {str(item.id) for item in items}
    ordering = (
        case((FeedItem.extra["literature_preparation"].as_string() == "ready", 1 if include_preparing else 0), else_=0 if include_preparing else 1),
        recent_order(), FeedItem.id,
    )

    async def supplement(statement, matches):
        # Without a leaf-domain prefilter the primary scan already covers all rows.
        if not leaf_domains:
            return
        rows = (await db.execute(statement.order_by(*ordering))).scalars().all()
        for item in rows:
            extra = item.extra or {}
            if (extra.get("literature_catalog") and (
                extra.get("literature_eligible") is not True
                or (not include_preparing and extra.get("literature_preparation") != "ready")
            )) or is_journal_notice(item):
                continue
            if not (show_in_feed(item, include_preparing=include_preparing) or matches_subscribed_journal(item, profile)):
                continue
            if not matches(item):
                continue
            if str(item.id) not in known:
                items.append(item)
                known.add(str(item.id))

    if profile.projects:
        project_probe = _windowed(select(FeedItem)).where(
            cast(FeedItem.extra["profile_user_ids"], String).like(
                f'%"{profile.user_id}"%'
            ),
            or_(*(
                cast(FeedItem.extra["feed_acquisition_routes"], String).like(f"%{route}%")
                for route in ("project_profile", "web")
            )),
        )
        if profile.active_project_queries is not None:
            project_probe = project_probe.where(
                FeedItem.extra["discovery_query"].as_string().in_(profile.active_project_queries)
            )
        await supplement(project_probe, lambda item: True)

    if profile.domains:
        domain_condition = or_(*(
            condition
            for slug in profile.domains
            for condition in (
                cast(FeedItem.domains, String).like(f'%"{slug}"%'),
                cast(FeedItem.domains, String).like(f'%"{slug}.%'),
            )
        ))
        await supplement(
            _windowed(select(FeedItem)).where(domain_condition),
            lambda item: bool(_domain_hits(item, profile)),
        )

    # Journal names and verified scholar identities each receive a complete scan.
    if profile.journals:
        await supplement(_windowed(select(FeedItem)),
                         lambda item: matches_subscribed_journal(item, profile))
    if profile.scholars:
        await supplement(_windowed(select(FeedItem)),
                         lambda item: matches_subscribed_scholar(item, profile))
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
            or matches_subscribed_scholar(item, profile)
        ]
    if profile.suppressed_item_ids:
        suppressed = {str(value) for value in profile.suppressed_item_ids}
        items = [i for i in items if str(i.id) not in suppressed]
    # 普通论文仍要求摘要；用户明确订阅的期刊是例外，先把索引结果展示出来，
    # 摘要由后台资产/元数据补齐后再完善卡片。
    items = [
        i for i in items
        if (
            not (i.extra or {}).get("literature_catalog")
            or (i.extra or {}).get("literature_preparation") == "ready"
            or (include_preparing and (i.extra or {}).get("literature_eligible") is True)
        )
        and not is_journal_notice(i)
        and (show_in_feed(i, include_preparing=include_preparing) or matches_subscribed_journal(i, profile))
    ]
    return items


def is_journal_notice(item: FeedItem) -> bool:
    """Exclude journal corrections, issue guides and publication metadata."""
    from nodes.literature.tools.publication_types import is_journal_notice_title
    return item.kind == FeedItemKind.PAPER and is_journal_notice_title(item.title)


def acquisition_routes(item: FeedItem) -> frozenset[str]:
    extra = item.extra if isinstance(item.extra, dict) else {}
    raw = extra.get("feed_acquisition_routes")
    return frozenset(str(route) for route in raw) if isinstance(raw, list) else frozenset()


def _has_summary(item: FeedItem) -> bool:
    return bool(item.summary and item.summary.strip())


def _requires_summary(item: FeedItem) -> bool:
    """论文形态且非网页发现时必须有摘要；截稿/发布/分享/周报与网页发现不要求。"""
    return item.kind == FeedItemKind.PAPER and "web" not in acquisition_routes(item)


def show_in_feed(item: FeedItem, *, include_preparing: bool = False) -> bool:
    """是否有资格进入推荐池。"""
    extra = item.extra if isinstance(item.extra, dict) else {}
    if not include_preparing and _requires_summary(item) and not _has_summary(item):
        # A project DOI can have no abstract after PubMed, Crossref, OpenAlex
        # and publisher lookups.  The user asked to publish after attempts are
        # complete even when every source lacks one; its title still has to
        # pass the project relevance gate below.
        completed_project = (
            bool(acquisition_routes(item) & {"project_profile", "scholar", "journal"})
            and extra.get("literature_preparation") == "ready"
            and (extra.get("abstract_lookup_completed") is True or extra.get("abstract_enrichment_attempted") is True)
        )
        if not completed_project:
            return False
    if "web" in acquisition_routes(item) or extra.get("web_search_backend"):
        # Search engines also return timeless reference pages and storefronts.
        # They can stay in the catalog, but a dated news/recommendation stream
        # must not present them as newly published discoveries.
        title = item.title.casefold()
        title_date = re.search(r"\b(20\d{2})[./-](\d{1,2})[./-](\d{1,2})\b", title)
        if title_date:
            from app.services.feed.windows import web_cutoff
            try:
                published = datetime(*(int(part) for part in title_date.groups()), tzinfo=UTC)
                now = datetime.now(UTC)
                if published < web_cutoff(now) or published > now:
                    return False
            except ValueError:
                pass
        if any(marker in title for marker in (
            "百度百科", "维基百科", "wikipedia", "在线购买", "立即购买",
            "产品报价", "批发价格",
        )):
            return False
    return True


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
        # An empty domain selection is not permission to show every journal
        # in the shared catalog.  After unfollowing the last journal, the
        # field-dynamics lane has no matching interest and must be empty.
        if not domain_hits and not subscribed_journal:
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
            _effective_date(item), now=moment
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
            -(_aware(_effective_date(value.item)) or moment).timestamp(),
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
    scored.sort(key=lambda s: (-s.score, -(_aware(_effective_date(s.item)) or moment).timestamp()))
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
    """Only verified author entity IDs can supply scholar membership."""
    from app.services.feed.author_identity import matches
    return matches(item, getattr(profile, "scholar_identities", ()))


def _route_score(
    base: ScoredItem, route: str, *, profile: Profile, now: datetime
) -> float | None:
    extra = base.item.extra if isinstance(base.item.extra, dict) else {}
    freshness = _recency_score(_effective_date(base.item), now=now)
    citation = _citation_score(extra)

    if route == "project_profile":
        # Historical acquisition scores cannot authorize current recommendations.
        relevance = 1.0 if base.project_id else (0.55 if base.matched_query_terms else 0.0)
        if relevance <= 0.0:
            return None
        # A daily project harvest must surface newly published relevant work.
        # Relevance is still the admission gate above; within that gate, a
        # citation-heavy paper from months ago should not bury new results.
        return 0.55 * relevance + 0.10 * _source_quality(extra) + 0.25 * freshness + 0.10 * citation
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
        relevance = 1.0 if base.project_id else (0.55 if base.matched_query_terms else 0.0)
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

    Opens and saves are positive signals for related work. A recently opened
    item itself moves down slightly so new work is easier to find. Dismissals
    are strongly negative; impressions remain neutral.
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
    opened_at: dict[str, datetime] = {}
    action_weights = {
        FeedEngagementAction.OPEN: 1.0,
        FeedEngagementAction.SAVE: 3.0,
        FeedEngagementAction.DISMISS: -4.0,
    }
    for engagement, item in rows:
        created = _aware(engagement.created_at) or moment
        age_days = max(0.0, (moment - created).total_seconds() / 86400.0)
        if engagement.action == FeedEngagementAction.OPEN:
            opened_at[str(item.id)] = created
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
        opened = opened_at.get(str(item.id))
        opened_age = max(0.0, (moment - opened).total_seconds() / 86400.0) if opened else 0.0
        # One OPEN record per user/item: repeated clicks cannot stack the
        # penalty. It fades as the paper ages and never removes the item.
        viewed_penalty = 0.25 * math.pow(0.5, opened_age / 14.0) if opened else 0.0
        adjusted.append(replace(value, score=value.score + 0.20 * affinity - viewed_penalty))
    adjusted.sort(key=lambda value: (
        -value.score,
        -(_aware(_effective_date(value.item)) or moment).timestamp(),
    ))
    return adjusted


def rank_rednote(
    items: list[FeedItem], profile: Profile, *, user_id: str,
    now: datetime | None = None, base_scored: list[ScoredItem] | None = None,
) -> list[ScoredItem]:
    """多路独立评分，再按动态配额混排并做基础来源多样性。

    推荐 = project（课题检索，网络 + 学术数据库）+ 学科 + 订阅期刊 + 订阅学者。
    这四路互不覆盖：只订了某本刊或某位学者、却没订对应学科时，那些成果也要
    进推荐流 —— 否则"订阅"只影响订阅页，推荐页看不见，等于半截功能。
    """
    moment = now or datetime.now(UTC)
    if base_scored is None:
        base_scored = rank(items, profile, now=moment)
    buckets: dict[str, list[ScoredItem]] = {
        "project_profile": [], "journal": [], "scholar": [], "web": [], "bignews": [],
    }
    for base in base_scored:
        routes = acquisition_routes(base.item)
        if matches_subscribed_scholar(base.item, profile):
            routes = routes | {"scholar"}
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
            -(_aware(_effective_date(value.item)) or moment).timestamp(),
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
    db: AsyncSession, *, user_id: str, scored: list[ScoredItem], today: date | None = None,
    refresh: bool = False,
) -> list[dict]:
    """今天的选摘：有就读出来，没有就算一次并存下。

    存的是 item_id 的引用，不是内容副本 —— 内容后续会被补全（摘要、热度），
    存副本就是让当天的选摘停在选中那一刻的样子。
    """
    when = today or datetime.now(UTC).date()
    existing = await db.get(FeedDailyPick, (user_id, when))
    if existing is not None and existing.picks and not refresh:
        return list(existing.picks)

    # 轮换机制：新的一天优先避开用户在当前候选窗口已选中的论文。
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
    # Browser refresh and background translation can rebuild together. Keep the
    # composite key in place and let the database resolve the insert race.
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    statement = insert(FeedDailyPick).values(user_id=user_id, pick_date=when, picks=payload)
    if refresh or existing is not None:
        statement = statement.on_conflict_do_update(
            index_elements=["user_id", "pick_date"], set_={"picks": payload})
    else:
        statement = statement.on_conflict_do_nothing(index_elements=["user_id", "pick_date"])
    await db.execute(statement)
    stored = await db.scalar(select(FeedDailyPick.picks).where(
        FeedDailyPick.user_id == user_id, FeedDailyPick.pick_date == when))
    return list(stored or [])
