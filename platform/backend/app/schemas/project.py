"""Pydantic schemas for Project API."""

from datetime import datetime

from typing import Literal

from pydantic import BaseModel, Field

from app.models.project import (
    EntryType,
    OperationMode,
    ProjectStatus,
    ReflectionMode,
    ReportingLevel,
)


class ProjectCreate(BaseModel):
    name: str = Field(..., max_length=300)
    description: str | None = None
    research_domain: str | None = None
    entry_type: EntryType | None = None
    #: 这个项目住哪：空 = 本机，否则是一条连接的 id（某个组织）。
    #:
    #: **默认本机**，而且这个默认必须是"什么都不说"就能拿到的 —— 一个只想在自己
    #: 电脑上开个课题的人，不该先回答"你属于哪个组织"。选家是一次性的：项目出生
    #: 那一刻挑，之后只有"搬家"，没有"同步"。
    home: str = ""

    # Optional config overrides at creation time
    operation_mode: OperationMode = OperationMode.ASSISTED
    reporting_level: ReportingLevel = ReportingLevel.MEDIUM
    preferred_model: str | None = None


class ProjectUpdate(BaseModel):
    """User-visible, persisted Project metadata editable from Project Settings."""

    name: str | None = Field(None, min_length=1, max_length=300)
    description: str | None = None
    research_domain: str | None = Field(None, max_length=200)
    #: 组织里谁看得见（`organisation` / `members`）。个人档上没有别人，改了也不影响什么。
    visibility: Literal["organisation", "members"] | None = None

    model_config = {"extra": "forbid"}


class ProjectConfigUpdate(BaseModel):
    operation_mode: OperationMode | None = None
    #: 无人值守时预授权的高危类别（`match_high_risk` 返回的标签）。
    #: 只在 operation_mode == AUTONOMOUS 时生效；[] / None = 每个高危点都停下
    #: 问人。默认必须是"都停"——授权范围只能由人显式给出。
    autonomous_authorized_risk_classes: list[str] | None = None
    reporting_level: ReportingLevel | None = None
    preferred_model: str | None = None
    tool_whitelist: list[str] | None = None
    max_concurrent_branches: int | None = None
    cycle_soft_limit: int | None = None
    cycle_hard_limit: int | None = None


class ProjectResponse(BaseModel):
    id: str
    name: str
    description: str | None
    research_domain: str | None
    status: ProjectStatus
    entry_type: EntryType | None
    capabilities: list[str] = Field(default_factory=list)
    member_count: int = 0
    active_session_count: int = 0
    #: 它住在哪 —— `{"kind": "local"}` 或 `{"kind": "organisation", …}`。
    #: 侧栏按它分组；`reachable: false` 的那一组是上一次问到的名字，灰着摆。
    home: dict | None = None
    #: 组织里谁看得见它。
    visibility: str = "organisation"
    #: 在不在「我的项目」里（自己建的 / 是成员的）。`GET /projects/` 只答我的；只读打开的组里
    #: 别人的项目（详情）这里是 false。旧服务器不发它：缺省当 true。
    mine: bool = True
    #: 这个人能不能归档 / 恢复它（负责人或同组织管理员）。能力清单答不了：归档之后能力只剩「看」，
    #: 恢复按钮却还得画给这几个人。
    can_archive: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProjectDetailResponse(ProjectResponse):
    """Extended response with config and stats."""

    config: "ProjectConfigResponse | None" = None


class ProjectConfigResponse(BaseModel):
    """项目配置的读回视图。

    ⚠️ 这是一份**手写字段名单**，新增配置项不加进来就**静默看不见**。
    2026-08-11 实测：`autonomous_authorized_risk_classes`（无人值守预授权哪些
    高危类别）写进去了、库里也确实存着，但 `GET /projects/{id}` 永远返回
    `None` —— 于是"我到底授权了什么"从 API 根本查不到。

    授权范围看不见，比不能设置更糟：设不了会立刻发现，看不见会一直以为
    自己没授权。安全相关的设置尤其不能这样。

    加字段时**这里必须一起加** —— `test_project_config_response_covers_the_model`
    机械核对模型列与本 schema 字段的差集，漏了直接红，不靠人记得。
    """

    operation_mode: OperationMode
    reporting_level: ReportingLevel
    preferred_model: str | None
    tool_whitelist: list[str] | None
    max_concurrent_branches: int
    cycle_soft_limit: int
    cycle_hard_limit: int
    #: 无人值守时预授权的高危类别（`match_high_risk` 的标签）。
    #: None / [] = 每个高危点都停下问人。
    autonomous_authorized_risk_classes: list[str] | None = None
    reflection_mode: ReflectionMode | None = None
    harness_overrides: dict | None = None
    research_intent: dict | None = None
    notification_channels: list | None = None

    model_config = {"from_attributes": True}
