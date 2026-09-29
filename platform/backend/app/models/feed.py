"""科研资讯流的持久层 —— 领域日报，不是无限流。

## 这一层存的是什么

**有时效的信息，不是经过准入的知识资产。** 这条边界决定了它落在 App Server
的 Postgres 里，而不是 harness 的 JSONL：知识与记忆归 harness（见
`app.services.harness_kb` 的纪律，以及迁移 `022_retire_platform_kb_domain`
清算的那次违反），而一条 arXiv 新论文在被用户认领之前不是任何人的知识 ——
它只是今天有人发了这么一篇。

资讯通往知识的路径只有一条：用户把它存进某个 Project 的 memory，之后走正常
的晋升门。KB 准入原则一毫米不动。

## 为什么不复用 `watchlists` / `dreaming_jobs`

它俩确实是为"定期采集外部内容"建的（007 号迁移，至今零使用），而且
`min_interval_seconds` / `consecutive_failures` / 成本上限这些字段想对了 ——
下面照抄了它们的思路。但形状对不上：

- `watchlists` 是**用户/组织**的关注对象（"我要盯 ACEsuit/mace 的 release"），
  而资讯源是全平台共享的公共源。挂 `organization_id=NULL` 表示"全局"是在
  给一张有主的表编一个无主的语义。
- `dreaming_jobs` 是**配置在源之上的作业**。但一个源该多久拉一次，是这个源
  自己的出版节奏决定的（arXiv 工作日一天一次、期刊按周、deadline 是日历），
  不是作业配置。拆成两张表再靠 `config: {"watchlist_ids": [...]}` 连回去，
  这层间接换不到任何东西。

所以 `feed_sources` 把"拉什么"和"多久拉一次"放在同一行。`watchlists` 保持
原样留给它本来的用途（用户级关注），`dreaming_jobs` 留给 KB dreaming。

## provenance-first

`FeedItem.url` 对外部来源**非空**（见 `feed.ingest`：没有出处的条目不入库）。
平台上出现一条没有出处的"新闻"，就是在自己身上开一个幻觉传播面 —— 对一个
把可审计当命根子的科研平台，这比少几条内容严重得多。摘要只能从抓到的原文
截取或改写，不许凭记忆生成。
"""

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, UTCDateTime

#: 一个源连续失败几次就自动停用。取值同 `dreaming_jobs.max_consecutive_failures`。
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5

#: JSONB 在 Postgres，JSON 在别处 —— 与 `artifact.py` 同一种端正写法。
#: （`watchlist.py` / `dreaming.py` 直接写死 JSONB，测试要靠 conftest 的
#: `@compiles` shim 兜。这里不再增加那份负担。）
_JSON = JSON().with_variant(JSONB(), "postgresql")


class FeedSourceKind:
    """源的种类。**不是 Enum**：与 `dreaming.py` 一致存成 String，且新增一种
    源不该要一次迁移。"""

    ARXIV = "arxiv"
    RSS = "rss"
    HF_DAILY_PAPERS = "hf_daily_papers"
    CONFERENCE_DEADLINES = "conference_deadlines"


class FeedItemKind:
    """条目的形态。前四种是采集来的原料，`digest` 是加工出来的，
    `post` 是用户自己发的。"""

    PAPER = "paper"
    RELEASE = "release"
    DEADLINE = "deadline"
    NEWS = "news"
    DIGEST = "digest"
    POST = "post"


class FeedVisibility:
    PLATFORM = "platform"
    ORGANIZATION = "organization"


class FeedEngagementAction:
    """用户对一条内容做过什么。`impression` 是"露过面"——它存在只为一件事：
    别把同一条重复推给同一个人。"""

    IMPRESSION = "impression"
    OPEN = "open"
    SAVE = "save"
    DISMISS = "dismiss"


class FeedSource(Base):
    """一个外部资讯源 —— 拉什么 + 多久拉一次 + 它现在健不健康。"""

    __tablename__ = "feed_sources"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    config: Mapped[dict] = mapped_column(_JSON, nullable=False)
    # arxiv: {"category": "cond-mat.mtrl-sci", "max_results": 30}
    # rss:   {"url": "https://www.nature.com/nature.rss"}
    # hf_daily_papers / conference_deadlines: {"url": "..."}

    #: 这个源产出的内容默认落在哪些域（arXiv 词表）。arXiv 源就是它自己那个
    #: 分类；一份综合期刊的 RSS 可能横跨几个域。条目自带的分类优先于它。
    domains: Mapped[list] = mapped_column(_JSON, nullable=False, default=list)

    #: 拉取周期。**是源的属性**：arXiv 工作日更新、期刊按周、deadline 表几乎
    #: 不动。一个全局 cron 只能取最快那个，等于把别的源都白拉一遍。
    poll_interval_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=6 * 3600
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    last_polled_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    last_item_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_MAX_CONSECUTIVE_FAILURES
    )

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # 同一个源不许配两遍：两行同源就是同一批内容被拉两次，去重能挡住
        # 落库，但拉取成本和"最近更新时间"这类判据已经分叉了。
        UniqueConstraint("kind", "name", name="uq_feed_sources_kind_name"),
        Index("ix_feed_sources_active_polled", "is_active", "last_polled_at"),
    )


class FeedItem(Base):
    """一条资讯。"""

    __tablename__ = "feed_items"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )

    #: 归一化的身份（`arxiv:2401.01234` / `doi:10.1038/xxx` / `url:...`）。
    #: 全局唯一 —— 同一篇论文从 arXiv 和某个 RSS 两处进来必须合成一条，否则
    #: 用户会在同一天的日报里看见同一篇两次。也是 M2 热度信号的挂载点：
    #: "几个渠道提到过它"要能机械算出来。
    canonical_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)

    #: 出处。外部来源必须有（`feed.ingest` 拦），用户发的帖子可以没有。
    url: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 从原文抓到的摘要原文。不是模型写的。
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 条目配图的**外部**地址。可空，而且大多数条目就是空的 —— 打真源查过
    #: （2026-08-23）：APS 系与 Phys.org/Quanta/MIT TR 带图，Nature 系、
    #: Science、arXiv 一个图片字段都没有。所以"有图"是少数派，界面必须把
    #: 无图当成正常态。
    #:
    #: ⚠️ 这个地址**不直接交给浏览器渲染**：那等于每次打开首页就向出版商
    #: 广播一次"这个用户在读这条"（IP + referrer）。前端走
    #: `/feed/items/{id}/image` 由后端代理取回，见那个端点的 docstring。
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    authors: Mapped[list] = mapped_column(_JSON, nullable=False, default=list)
    venue: Mapped[str | None] = mapped_column(String(200), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True, index=True
    )

    domains: Mapped[list] = mapped_column(_JSON, nullable=False, default=list)

    source_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("feed_sources.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: 用户自己发的内容的作者。停用/删号不该带走内容，所以 SET NULL。
    author_user_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: 组织身份取 `String(64)`，不是 UUID —— 这一层真正在用的组织标识是
    #: `User.institution_id` / `group_id`（`String(64)`，默认值就是字面量
    #: `"local"`），见 `policies.governance_scope_for`。`artifacts` 和
    #: `watchlists` 上那两列写的是 UUID，但它们从来没有被写过一行：第一次
    #: 往里存 `"local"` 就会在 Postgres 上炸。
    organization_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, default=FeedVisibility.PLATFORM
    )

    #: 形态相关的附加字段，不值得各开一列。
    #: deadline: {"deadline_at": "...", "conference": "NeurIPS 2026"}
    #: digest:   {"covers_item_ids": [...], "window_days": 7}
    extra: Mapped[dict] = mapped_column(_JSON, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), index=True
    )

    __table_args__ = (
        Index("ix_feed_items_kind_published", "kind", "published_at"),
        Index("ix_feed_items_visibility_org", "visibility", "organization_id"),
    )


class FeedEngagement(Base):
    """用户对一条内容做过的一个动作。

    这是 M2 隐式画像的原料，但 M1 就要有：没有它，"别重复推同一条"和
    "收藏"都做不了。
    """

    __tablename__ = "feed_engagements"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("feed_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now()
    )

    __table_args__ = (
        # 同一个人对同一条做同一个动作只记一次 —— 否则"看过几次"会被刷新
        # 次数放大，而它要回答的是"这条露过面没有"。
        UniqueConstraint("user_id", "item_id", "action", name="uq_feed_engagement_once"),
        Index("ix_feed_engagements_user_action", "user_id", "action"),
    )


class FeedDailyPick(Base):
    """某个用户某一天的 Top N。

    ## 为什么落库，而不是每次请求现算

    "今天的三条"必须**当天不变**。现算的话每刷新一次就换一批，用户读到一半
    回来那条就不见了 —— 那不是日报，是转盘。落库同时把成本焊死在"每人每天
    一次"，个性化理由要过一次模型时这一点尤其重要。

    存的是 item_id 的引用而不是内容副本：内容会被后续采集补全（摘要、热度），
    存副本就是让当天的选摘停在选中那一刻的样子。
    """

    __tablename__ = "feed_daily_picks"

    user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    pick_date: Mapped[date] = mapped_column(Date, primary_key=True)

    #: [{"item_id": ..., "reason": "...", "project_id": ... | None}]
    #: `reason` 是"为什么推给你"，`project_id` 是它对上了你的哪个课题。
    picks: Mapped[list] = mapped_column(_JSON, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now()
    )
