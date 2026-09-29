"""Strict wire contracts for the authenticated Settings center."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InterfaceSettings(SettingsModel):
    theme: Literal["system", "light", "dark"] = "system"
    density: Literal["comfortable", "compact"] = "comfortable"
    font_scale: Literal[90, 100, 110, 120] = 100
    reduce_motion: bool = False
    #: 界面语言。**默认中文** —— 资讯流是按中文使用者做的，把默认改成英文
    #: 会让现有用户下次打开时整片资讯变成另一种语言。
    #:
    #: 这个开关目前真正覆盖的是**资讯流与域词表**（平台上唯一做了本地化的
    #: 一片）；其余界面本来就是英文，所以选 en 得到的是一套一致的英文界面，
    #: 选 zh 得到中文资讯 + 英文工作区。设置页的说明里如实写了这一点 ——
    #: 一个声称"系统语言"却只改一半的开关，用户会以为是坏了。
    language: Literal["zh", "en"] = "zh"
    # "feed" = 打开平台先看今天领域发生了什么（wangd 2026-08-22 拍板做默认落地页）。
    default_landing: Literal["feed", "last_session", "projects", "new_research"] = "feed"
    show_run_usage: bool = True
    #: 开场那几张卡片教过没有。
    #:
    #: 这是这份偏好里**唯一一个真正的标记** —— 其余每一件事都能从真实状态推出来
    #: （有没有可用模型、有没有选过方向），而"这个人看没看过介绍"没有别的真相源。
    #: 它只管"要不要再教一遍"，**不**代表任何东西配好了：模型缺不缺由模型自己的
    #: 状态说，所以清空模型之后那条缺口提示照样回来，而开场不会再弹一次。
    #:
    #: 存在账号偏好里而不是浏览器本地：组织服务器上同一个人在两台机器上登录，
    #: 不该被教两遍。
    onboarding_done: bool = False
    #: 第一次进项目时那一圈气泡教过没有。
    #:
    #: 和上面那个分开记，因为教的是两件事：那个说"这台软件有哪几块"，这个说
    #: "一个课题在这里怎么走"。一个人可能在工作区里转好几天才第一次建项目，
    #: 合成一个标记的话，这一圈会在他还没有项目的时候被"教过"掉。
    project_guide_done: bool = False
    execution_detail: Literal["summary", "standard", "trace"] = "standard"
    auto_collapse_completed_tools: bool = True
    auto_collapse_completed_steps: bool = True
    follow_active_run: bool = True


class NotificationSettings(SettingsModel):
    """In-app event visibility preferences; no outbound delivery is implied."""

    decision_required: bool = True
    run_failed: bool = True
    run_completed: bool = True
    budget_warning: bool = True


class NotificationSettingsOut(NotificationSettings):
    delivery_capabilities: list[Literal["in_app"]] = Field(
        default_factory=lambda: ["in_app"]
    )


class UsageDay(SettingsModel):
    date: date
    total_tokens: int = Field(ge=0)
    run_count: int = Field(ge=0)


class UsageSummary(SettingsModel):
    session_count: int = Field(ge=0)
    run_count: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    known_cost: float = Field(ge=0, allow_inf_nan=False)
    cost_currency: str | None = None
    cost_known_runs: int = Field(ge=0)
    cost_unknown_runs: int = Field(ge=0)
    active_days: int = Field(ge=0)
    current_streak: int = Field(ge=0)
    longest_streak: int = Field(ge=0)
    daily: list[UsageDay]


class GovernanceAffiliation(SettingsModel):
    id: str
    name: str


class GovernanceMember(SettingsModel):
    id: str
    display_name: str
    email: str
    role: str
    institution: GovernanceAffiliation
    group: GovernanceAffiliation | None
    #: 停用的人也在名录里 —— 否则"停用"是一条单行道：人从列表里消失，再也没有
    #: 地方把他恢复回来。
    is_active: bool


class GovernanceScope(SettingsModel):
    kind: Literal["institution", "group", "individual"]
    id: str
    name: str


class GovernancePolicySource(SettingsModel):
    # 机构 / 课题组两种来源随指令表（RFC X2）一起没了；组织级指令层回来时再加。
    kind: Literal["platform", "personal"]
    name: str
    source_type: Literal["rbac", "personal_research"]
    status: Literal["active", "not_configured"]
    editable: bool
    version: str | None = None
    updated_at: datetime | None = None
    summary: str


class GovernanceMembers(SettingsModel):
    scope: GovernanceScope
    editable: bool
    effective_permissions: list[str]
    policy_sources: list[GovernancePolicySource]
    members: list[GovernanceMember]
