"""Project and ProjectConfig models.

A Project is the top-level container for a research endeavor.
Each project has its own Research Graph, Knowledge Base, Memory, and budget.
"""

import enum
from datetime import datetime
from functools import partial
from uuid import uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime

#: Postgres 上是 JSONB，别的方言上是 JSON。裸写 JSONB 会让这张表在 SQLite 上
#: 根本建不出来 —— 个人档的库就是 SQLite，所以这不是「以后可能有用的兼容」，
#: 而是「个人档能不能启动」。同一种写法见 artifact.py / feed.py / user.py。
_JSON = JSON().with_variant(JSONB(), "postgresql")

# Use enum .value (lowercase) instead of .name (UPPERCASE) for PostgreSQL
_StrEnum = partial(Enum, values_callable=lambda e: [x.value for x in e])


class ProjectStatus(str, enum.Enum):
    """还在做 / 不做了。

    归档 = 冻结：只读（`policies.has_project_capability` 只放行「看」）、不再开新会话、后台的自动续跑
    跳过它；知识、会话、产出都在，能恢复。从前还有「暂停」「已完成」两种 —— 没有任何地方因为它们
    做了不一样的事，是假的状态，迁移 051 并掉了。
    """

    ACTIVE = "active"
    ARCHIVED = "archived"


class ProjectMembershipRole(str, enum.Enum):
    LEAD = "lead"
    RESEARCHER = "researcher"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


class ReportingLevel(str, enum.Enum):
    """How much the system reports to the user (whitepaper §8)."""
    LOW = "low"        # only report at decision points and failures
    MEDIUM = "medium"  # report at node completion + decision points
    HIGH = "high"      # report at every significant step


class OperationMode(str, enum.Enum):
    """Dual mode: assisted vs autonomous (whitepaper §2)."""
    ASSISTED = "assisted"      # human-led, system reports at key nodes
    AUTONOMOUS = "autonomous"  # system-led, pauses only at high-risk points


class EntryType(str, enum.Enum):
    """Seed graph entry types (whitepaper §4.7)."""
    FUZZY_IDEA = "fuzzy_idea"
    EXISTING_PROPOSAL = "existing_proposal"
    MID_PROJECT = "mid_project"
    SPECIFIC_TASK = "specific_task"
    FAILURE_RECOVERY = "failure_recovery"


class ReflectionMode(str, enum.Enum):
    """Critical reflection intensity (whitepaper §Deep Reflection)."""
    OFF = "off"
    LIGHT = "light"   # 2-3 primary dimensions at major transitions
    DEEP = "deep"     # all dimensions at every node completion


#: 项目可见范围的两个值。一处定义，schema、策略、迁移都读它。
PROJECT_VISIBLE_TO_THE_ORGANISATION = "organisation"
PROJECT_VISIBLE_TO_ITS_MEMBERS = "members"
PROJECT_VISIBILITIES = (PROJECT_VISIBLE_TO_THE_ORGANISATION, PROJECT_VISIBLE_TO_ITS_MEMBERS)


class Project(Base):
    """A research project — the top-level organizational unit."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    owner_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    research_domain: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[ProjectStatus] = mapped_column(
        _StrEnum(ProjectStatus), nullable=False, default=ProjectStatus.ACTIVE
    )

    # How the project was started
    entry_type: Mapped[EntryType | None] = mapped_column(_StrEnum(EntryType), nullable=True)
    #: 组织里谁看得见它（`RFC_ORGANISATION_PAGE_20260923` §3.3）：`organisation` = 组里所有人只读，
    #: `members` = 只有项目成员（管理员两种都看得见）。看得见 ≠ 在你的列表里 —— 「我的项目」
    #: 只有自己建的和是成员的（`mine`），组里别的项目在组织页的项目 tab 里。
    visibility: Mapped[str] = mapped_column(String(24), nullable=False, default=PROJECT_VISIBLE_TO_THE_ORGANISATION,
                                            server_default=PROJECT_VISIBLE_TO_THE_ORGANISATION)
    # current_revision_id 2026-09-05 删（RFC X1）：项目的当前状态**就是** main
    # 的 head。存一份指向 project_revisions 的 id，等于把 git 的事实抄进库里，
    # 而抄件会和事实分叉且不报错。

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    owner: Mapped["User"] = relationship(back_populates="projects")  # noqa: F821
    config: Mapped["ProjectConfig | None"] = relationship(
        back_populates="project", uselist=False, cascade="all, delete-orphan"
    )


class ProjectMembership(Base):
    """Explicit project capability boundary with soft-removal audit history."""

    __tablename__ = "project_memberships"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_user_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "role IN ('lead', 'researcher', 'reviewer', 'viewer')",
            name="ck_project_memberships_role",
        ),
        Index(
            "uq_project_memberships_project_user",
            "project_id",
            "user_id",
            unique=True,
        ),
        Index("ix_project_memberships_user_active", "user_id", "removed_at"),
    )


class ProjectConfig(Base):
    """Per-project configuration — controls agent behavior, tools, and budget."""

    __tablename__ = "project_configs"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("projects.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )

    # Operation mode
    operation_mode: Mapped[OperationMode] = mapped_column(
        _StrEnum(OperationMode), nullable=False, default=OperationMode.ASSISTED
    )
    reporting_level: Mapped[ReportingLevel] = mapped_column(
        _StrEnum(ReportingLevel), nullable=False, default=ReportingLevel.MEDIUM
    )
    #: 无人值守时**预先授权**的高危类别（`match_high_risk` 返回的那些标签）。
    #:
    #: 只在 operation_mode == AUTONOMOUS 时生效。NULL / [] = 每个高危点都停下
    #: 问人 —— 这必须是默认：授权范围只能由人显式给出，框架不替他推断。
    #:
    #: 这里存的是**授权声明**，不是判决；类别词表的真相源是
    #: `shared/lib/dangerous_commands`，这里只引用标签，不另抄一份。
    autonomous_authorized_risk_classes: Mapped[list | None] = mapped_column(
        _JSON, nullable=True
    )

    # LLM preferences
    preferred_model: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Tool whitelist (null = all allowed except blacklist)
    tool_whitelist: Mapped[list | None] = mapped_column(_JSON, nullable=True)

    # Custom harness overrides per node type
    harness_overrides: Mapped[dict | None] = mapped_column(_JSON, nullable=True)

    # Reflection mode
    reflection_mode: Mapped[ReflectionMode] = mapped_column(
        _StrEnum(ReflectionMode), nullable=False, default=ReflectionMode.OFF
    )

    # Research intent (from intake interview)
    # Structured intent produced by the LLM-driven intake process.
    # Contains: expected_output, target_quality, scope, constraints,
    # reflection_calibration, success_criteria, etc.
    research_intent: Mapped[dict | None] = mapped_column(_JSON, nullable=True)

    # Graph constraints
    max_concurrent_branches: Mapped[int] = mapped_column(default=3)
    cycle_soft_limit: Mapped[int] = mapped_column(default=3)
    cycle_hard_limit: Mapped[int] = mapped_column(default=5)

    # Notification channels (JSONB array of {type, config})
    notification_channels: Mapped[list | None] = mapped_column(_JSON, nullable=True)

    # Relationship
    project: Mapped["Project"] = relationship(back_populates="config")
