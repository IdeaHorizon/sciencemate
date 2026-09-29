"""用户画像 —— 推荐信号来自真实科研上下文，不是点击历史。

## 这一层的全部理由

别家的资讯产品只知道你点过什么。平台知道**你正在做什么研究**：你开了哪些
Project、它们的方向是什么、你自己声明过对哪些域感兴趣。这是外部产品拿不到
的信号，也是这个功能唯一值得做的地方 —— 否则它只是又一个 RSS 阅读器。

画像有三个来源，按可靠度排：

  1. **显式兴趣**（onboarding 选的域）—— 用户自己说的，最可靠；
  2. **Project 派生**（课题名/描述/方向里的术语）—— 他正在做的事；
  3. 行为（点开/收藏/忽略）—— M2 才接，因为冷启动阶段几乎没有数据。

## 为什么 Project 匹配走词，不走域

`Project.research_domain` 是 `String(200)` 的**自由文本**（"MLIP robustness"、
"低温段方法学"），不是域词表里的 slug。硬把它映射成一个分类，靠的是猜；
猜错的后果是整个课题的推荐全歪，而且没有任何症状。

用词面重叠反而更准，也更能说清理由 —— "这篇提到了 finite-size scaling，
和你的 Ising 课题对得上" 是个用户能验证的说法，"这篇属于 cond-mat.stat-mech"
不是。

## 中英混排

课题名常是中文，而论文标题几乎都是英文。词面匹配跨不了语言 —— 这是真实
限制，不假装解决。缓解办法是抽 ASCII 术语：中文课题描述里的技术词（MLIP、
DFT、Transformer）本来就是英文，它们照样抽得出来。中文部分抽二元字组，
用于匹配中文内容（用户自己发的帖子、中文域周报）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project
from app.models.user import User

#: 词面匹配里没有区分度的词。只列**技术写作里高频且不携带主题**的那些 ——
#: 列得太长会把真正的领域词误杀（"state"、"model" 在物理里是有意义的）。
_STOPWORDS = frozenset(
    """
    a an and are as at be by for from has have how in into is it its of on or
    that the their this to was were will with we our you your they them these
    those than then there here what which who whom when where why can could
    should would may might must not no nor but if else about after before
    between during over under again further once all any both each few more
    most other some such only own same so too very just also new using used
    use based via toward towards study studies research approach method methods
    result results paper article preprint work works show shows shown
    项目 课题 研究 方向 工作 分析 方法 一个 我们 进行 通过 以及 主要 相关
    """.split()
)

#: 术语最短长度。两个字母的 ASCII 串（AI 除外）基本都是噪音。
_MIN_TERM_LEN = 3

#: 明确保留的短术语 —— 被长度闸误杀会很可惜。
_SHORT_TERMS_KEPT = frozenset({"ai", "ml", "md", "dft", "nmr", "qm", "mo"})

_ASCII_TOKEN = re.compile(r"[a-z][a-z0-9+\-]{1,31}")
_CJK = re.compile(r"[一-鿿]{2,}")


@dataclass(slots=True)
class ProjectSignal:
    """一个课题的可匹配面。`terms` 是从课题文本抽出来的术语集合。"""

    project_id: str
    name: str
    terms: frozenset[str]


@dataclass(slots=True)
class Profile:
    """一个用户此刻的画像。"""

    user_id: str
    #: 显式选的域（骨架分类或 archive 名，archive 名表示"整个大类都要"）。
    domains: tuple[str, ...] = ()
    projects: tuple[ProjectSignal, ...] = ()
    #: 订阅的期刊（归一化门面名）与学者（casefold 姓名）。
    #:
    #: 推荐要**综合**学科 + 期刊 + 学者，不再只认学科：只订了期刊或学者的
    #: 用户，他的推荐流里就该有这些期刊/学者的新成果。三者都是"用户自己说的"，
    #: 可靠度同级，所以放在同一个画像里，而不是各自出去开一条独立管线。
    journals: frozenset[str] = field(default_factory=frozenset)
    scholars: frozenset[str] = field(default_factory=frozenset)
    #: 已经露过面 / 被忽略的条目，排序时要避开。
    suppressed_item_ids: frozenset[str] = field(default_factory=frozenset)
    onboarded: bool = False

    @property
    def has_signal(self) -> bool:
        """有没有任何个性化依据。没有的话推荐退化成"最新 + 最受关注"，
        而不是假装个性化。"""
        return (
            bool(self.domains)
            or bool(self.journals)
            or bool(self.scholars)
            or any(p.terms for p in self.projects)
        )


def extract_terms(*chunks: str | None) -> frozenset[str]:
    """从自由文本里抽可匹配的术语。

    ASCII 词按原样收（小写），中文抽二元字组。两者都过停用词和长度闸。
    """
    terms: set[str] = set()
    for chunk in chunks:
        if not chunk:
            continue
        lowered = chunk.lower()
        for match in _ASCII_TOKEN.finditer(lowered):
            token = match.group(0).strip("-+")
            if not token or token in _STOPWORDS:
                continue
            if len(token) >= _MIN_TERM_LEN or token in _SHORT_TERMS_KEPT:
                terms.add(token)
        for run in _CJK.findall(chunk):
            # 中文不分词，取二元字组：能覆盖绝大多数术语，且不引分词依赖。
            for i in range(len(run) - 1):
                bigram = run[i : i + 2]
                if bigram not in _STOPWORDS:
                    terms.add(bigram)
    return frozenset(terms)


def stored_interests(user: User) -> dict:
    """`User.preferences["feed"]` —— 没有就是空字典。

    存在 `preferences` 这个通用 JSON 袋子里而不是新开一列：它已经在装
    `interface` 和 `notifications` 了，兴趣是同一类东西（用户偏好），
    而且不用迁移。
    """
    preferences = user.preferences if isinstance(user.preferences, dict) else {}
    stored = preferences.get("feed")
    return stored if isinstance(stored, dict) else {}


def stored_domains(user: User) -> tuple[str, ...]:
    raw = stored_interests(user).get("domains")
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for entry in raw:
        slug = str(entry or "").strip()
        if slug and slug not in out:
            out.append(slug)
    return tuple(out)


def journal_key(value: object) -> str:
    """期刊名比对键：去掉一切非字母数字再 casefold。

    与 `/feed/subscriptions/items` 用的是同一套归一化 —— 两边各写一份的话，
    同一本刊会出现"订阅页认得出、推荐池认不出"的分歧（而且没有任何症状）。
    """
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _name_tokens(value: object) -> list[str]:
    """姓名切成词元（丢掉单字母，它们对区分人没有帮助）。"""
    return [
        token
        for token in re.split(r"[^a-z]+", str(value or "").casefold())
        if len(token) > 1
    ]


def author_matches(subscribed: object, author: object) -> bool:
    """订阅的学者姓名与论文作者字段是否指同一个人。

    ## 为什么不能直接比字符串

    同一个人的名字在不同数据库里长得不一样：`Shaoda Liu`（Crossref 的
    given+family 顺序）、`Liu, Shaoda`（family 在前）、`S. Liu`（缩写）。
    精确相等会让"订阅了学者却永远匹配不上"，而且没有任何报错。

    ## 判据

    姓（最后一个词元）必须命中，并且名要么整体命中、要么以首字母缩写形式
    命中。顺序无关，所以 `Liu Shaoda` 与 `Shaoda Liu` 等价。
    `J. Liu` 与 `Shaoda Liu` **不算**命中 —— 缩写只补首字母，不补任意一个人。
    """
    want = _name_tokens(subscribed)
    have = _name_tokens(author)
    if not want or not have:
        return False
    if len(want) == 1:
        return want[0] in have
    given, family = want[0], want[-1]
    if family not in have:
        return False
    for token in have:
        if token == given:
            return True
        if len(token) == 1 and given.startswith(token):
            return True
        if len(given) == 1 and token.startswith(given):
            return True
    return False


def stored_subscription_journals(user: User) -> frozenset[str]:
    """订阅期刊名（归一化）。"""
    raw = stored_interests(user).get("subscriptions")
    if not isinstance(raw, dict):
        return frozenset()
    journals = raw.get("journals")
    if not isinstance(journals, list):
        return frozenset()
    out: set[str] = set()
    for entry in journals:
        if isinstance(entry, dict):
            key = journal_key(entry.get("name"))
            if key:
                out.add(key)
    return frozenset(out)


def stored_subscription_scholars(user: User) -> frozenset[str]:
    """订阅学者姓名（casefold）。"""
    raw = stored_interests(user).get("subscriptions")
    if not isinstance(raw, dict):
        return frozenset()
    scholars = raw.get("scholars")
    if not isinstance(scholars, list):
        return frozenset()
    return frozenset(
        str(entry.get("name") or "").casefold().strip()
        for entry in scholars
        if isinstance(entry, dict) and str(entry.get("name") or "").strip()
    )


def is_onboarded(user: User) -> bool:
    """他有没有过完兴趣选择这一步。

    判据是"存过没有"，不是"域列表非空" —— 一个明确选择了"什么都不订、只看
    全站"的用户，和一个从没被问过的新用户，不是同一件事。把前者当后者，
    每次进来都会再弹一次问卷。
    """
    return bool(stored_interests(user).get("onboarded_at"))


async def build_profile(
    db: AsyncSession, *, user: User, suppressed_item_ids: frozenset[str] | None = None
) -> Profile:
    """现算一个画像。

    **不缓存、不落库**：画像是从"此刻有哪些 Project、偏好里写着什么"推出来
    的视图。存一份就要回答"什么时候刷新"，而它的每个输入都会变 —— 那是一个
    从不更新的字段的经典长法。
    """
    projects = (
        (
            await db.execute(
                select(Project)
                .where(Project.owner_id == user.id)
                .order_by(Project.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )
    signals = tuple(
        ProjectSignal(
            project_id=project.id,
            name=project.name,
            terms=extract_terms(project.name, project.research_domain, project.description),
        )
        for project in projects
    )
    # 手选的 ∪（推断的 − 删掉的）。推断那部分只有开了自动挖掘才会有内容 ——
    # 关着时 `effective_domains` 退化成 `stored_domains`，行为逐字不变。
    from app.services.feed.curation import effective_domains

    return Profile(
        user_id=user.id,
        domains=effective_domains(user),
        projects=signals,
        journals=stored_subscription_journals(user),
        scholars=stored_subscription_scholars(user),
        suppressed_item_ids=suppressed_item_ids or frozenset(),
        onboarded=is_onboarded(user),
    )


def suggested_domains_for(user: User, projects: list[Project]) -> tuple[str, ...]:
    """给 onboarding 用的**预填**建议。

    不给空白问卷：一个刚注册的科学家面对 155 个 arXiv 分类，最可能的动作是
    关掉它。所以从他已有的 Project 方向里机械猜几个，让他改而不是让他填。

    猜法是子串命中域词表 —— 保守，猜不出就返回空，由界面显示平台主战场的
    默认几项。**不编**：猜错的域会一直影响推荐，而用户不会知道是这里猜的。
    """
    from app.services.feed import domains as domain_service

    text = " ".join(
        part
        for project in projects
        for part in (project.name, project.research_domain, project.description)
        if part
    ).lower()
    if not text.strip():
        return ()
    hits: list[str] = []
    for slug in domain_service.domain_registry().spine_categories():
        label = domain_service.label(slug).lower()
        # 用标签命中而不是 slug 命中：没人会在课题描述里写 "cond-mat.mtrl-sci"，
        # 但 "materials science" 很常见。
        if label and label in text and slug not in hits:
            hits.append(slug)
    return tuple(hits[:6])
