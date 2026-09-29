"""科研资讯流的线上契约。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FeedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FeedItemOut(FeedModel):
    id: str
    kind: Literal["paper", "release", "deadline", "news", "digest", "post"]
    title: str
    url: str | None = None
    summary: str | None = None
    authors: list[str] = Field(default_factory=list)
    venue: str | None = None
    published_at: datetime | None = None
    domains: list[str] = Field(default_factory=list)
    #: 域的人读名，与 `domains` 同序。前端不该自己去查词表 —— 那就是把
    #: 词表抄到第三个地方。
    domain_labels: list[str] = Field(default_factory=list)
    source_name: str | None = None
    author_display_name: str | None = None
    #: 这条有没有配图。**不给外链**：前端拿到 URL 就会有人直接渲染它，而那
    #: 等于每次打开首页都向出版商广播一次。要图走 /feed/items/{id}/image。
    saved: bool = False
    extra: dict = Field(default_factory=dict)


class FeedCard(FeedModel):
    """单流里的一条：内容 + 可验证理由 + 是否今日必读。

    「今日必读」是每天替你挑的少数几条，挂在流最前、带角标；其余内容
    同一条流往下刷，每条照旧带一句能被当场验证的推荐理由。
    """

    item: FeedItemOut
    reason: str = ""
    project_id: str | None = None
    project_name: str | None = None
    is_today_pick: bool = False


class FeedTodayOut(FeedModel):
    """打开平台第一眼看到的一条流。"""

    feed: list[FeedCard] = Field(default_factory=list)
    onboarded: bool = False
    #: 没有任何个性化依据时为 true —— 界面据此说实话（"还没有你的方向，
    #: 先按最新给你看"），而不是假装这是为你挑的。
    personalized: bool = False
    #: 内容池为空时的原因，说人话。为空字符串表示一切正常。
    empty_reason: str = ""
    #: 当前是否正在按这个用户所选学科在线获取。前端仅在 true 时短轮询，
    #: 完成后自动停止，避免把“在线即时”变成永久轮询。
    refreshing: bool = False


class DomainOption(FeedModel):
    domain: str
    label: str
    kind: Literal["spine", "leaf", "archive"] = "spine"


class DomainGroup(FeedModel):
    archive: str
    label: str
    categories: list[DomainOption] = Field(default_factory=list)


class DomainCatalogOut(FeedModel):
    groups: list[DomainGroup] = Field(default_factory=list)


class InterestsOut(FeedModel):
    domains: list[str] = Field(default_factory=list)
    domain_labels: list[str] = Field(default_factory=list)
    #: agent 从你的课题推断出来的，与手选的**分开返回** —— 界面要标明出处，
    #: 而且要能单独删掉某一条。混在一起就成了一个替你决定看什么的黑箱。
    inferred: list[DomainOption] = Field(default_factory=list)
    #: 从用户已有 Project 机械猜出来的预填建议 —— 不给空白问卷。
    suggested: list[DomainOption] = Field(default_factory=list)
    onboarded: bool = False


class InterestsWrite(FeedModel):
    domains: list[str] = Field(default_factory=list, max_length=40)


class JournalSubscription(FeedModel):
    key: str
    name: str
    issn: str = ""
    eissn: str = ""
    impact_factor: float | None = None
    jcr_quartile: str | None = None


class ScholarSubscription(FeedModel):
    key: str
    name: str
    source: Literal["kb", "literature"] = "literature"


class SubscriptionsOut(FeedModel):
    journals: list[JournalSubscription] = Field(default_factory=list)
    scholars: list[ScholarSubscription] = Field(default_factory=list)


class SubscriptionsWrite(FeedModel):
    journals: list[JournalSubscription] = Field(default_factory=list, max_length=200)
    scholars: list[ScholarSubscription] = Field(default_factory=list, max_length=200)


class SubscriptionSearchOut(FeedModel):
    journals: list[JournalSubscription] = Field(default_factory=list)
    scholars: list[ScholarSubscription] = Field(default_factory=list)


class EngagementWrite(FeedModel):
    action: Literal["impression", "open", "save", "dismiss"]


class SharedLinkWrite(FeedModel):
    """转一条链接进来 —— 冷启动期摩擦最低的 UGC。"""

    url: str = Field(min_length=8, max_length=2000)
    comment: str = Field(default="", max_length=2000)
    domains: list[str] = Field(default_factory=list, max_length=3)
    visibility: Literal["platform", "organization"] = "organization"


class CurationOut(FeedModel):
    """自动挖掘开关的状态。

    `available` 与 `enabled` 是**两件事**：前者问"平台配了挖掘模型吗"，
    后者问"这个用户打开了吗"。合成一个布尔值，用户就分不清"我没开"和
    "开了也没用"，而这两种的处置完全不同。
    """

    enabled: bool = False
    #: `feed_curation` 角色配了可用模型吗。没有 → 开关显示为不可用。
    available: bool = False
    #: 正在为这个用户服务的挖掘模型（没有则为 None）。
    model_label: str | None = None
    #: 上次挖掘的时刻与失败原因（成功时为空）。
    last_run_at: datetime | None = None
    last_error: str | None = None
    #: 这次推断出来的方向与检索词 —— 必须看得见，不能是黑箱。
    inferred_domains: list[str] = Field(default_factory=list)
    inferred_domain_labels: list[str] = Field(default_factory=list)
    inferred_queries: list[str] = Field(default_factory=list)


class CurationWrite(FeedModel):
    enabled: bool


class SourceHealthOut(FeedModel):
    """源健康。一个静默不工作的源和一个"这周确实没新东西"的源，
    从外面看长得一样 —— 这个视图就是用来把它们分开的。"""

    id: str
    kind: str
    name: str
    is_active: bool
    domains: list[str] = Field(default_factory=list)
    poll_interval_seconds: int
    last_polled_at: datetime | None = None
    last_success_at: datetime | None = None
    last_item_count: int | None = None
    consecutive_failures: int = 0
    last_error: str | None = None


class FeedStatusOut(FeedModel):
    sources: list[SourceHealthOut] = Field(default_factory=list)
    total_items: int = 0
    collector_enabled: bool = True
    external_fetch_enabled: bool = True
    #: 域词表读不读得到。读不到时资讯的域标签会集体缺失 —— 这是平台自己的
    #: 故障，必须能一眼看到，而不是表现成"内容都没有分类"。
    domain_registry_available: bool = True
