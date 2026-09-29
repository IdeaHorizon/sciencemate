"""Platform users and their mechanically enforced governance identity."""

from enum import StrEnum
from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, Index, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime


class UserRole(StrEnum):
    """一个人在他的组织里是什么身份 —— 只有两档。

    曾经还有第三档 `group_admin`（课题组管理员）。它从没在一个真组织里成立过：
    组没有表、界面上没有任何地方能设组，建组织的管理员 `group_id=None`、请柬继承
    发起人的组，于是所有人都没组；一个没组的 group_admin 在项目列表里看得见全部
    无组用户的项目、点进去却 403。要分组，就在同一台服务器上建几个组织
    （`RFC_ORGANISATION_PAGE_20260923` §3.1）。存量的 `group_admin` 行在启动时转成
    `researcher`（`database.normalise_retired_roles`）。
    """

    INSTITUTION_ADMIN = "institution_admin"
    RESEARCHER = "researcher"


#: 退役的身份 → 它现在是什么。库里还可能有这些值（旧版本写进去的），读到时按这张表转。
RETIRED_ROLES: dict[str, str] = {"group_admin": UserRole.RESEARCHER.value}


_JSON = JSON().with_variant(JSONB(), "postgresql")


class User(Base):
    """A platform user with a fixed role and affiliation scope."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    #: 邮箱**在一个组织里**唯一，不是在一台服务器上唯一（唯一索引见 `__table_args__`）。
    #:
    #: 一台服务器上可以住好几个组织，它们互相看不见。邮箱按服务器唯一的话，同一个人
    #: 就进不了第二个组织 —— 2026-09-22 wangd 在一台已经有他账号的机器上建第二个组织，
    #: 撞的正是这个，而那句「这台机器上已经有人用这个邮箱了」说的是一件本不该成立的事。
    email: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)

    # User preferences (injected into project context as User Memory)
    preferences: Mapped[dict | None] = mapped_column(_JSON, nullable=True)

    # Personal .md content (whitepaper: "个人的.md注入project context")
    personal_context_md: Mapped[str | None] = mapped_column(Text, nullable=True)

    role: Mapped[str] = mapped_column(String(32), nullable=False, default=UserRole.RESEARCHER.value)
    institution_id: Mapped[str] = mapped_column(String(64), nullable=False, default="local")
    institution_name: Mapped[str] = mapped_column(
        String(200), nullable=False, default="Local institution"
    )
    group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    group_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    is_active: Mapped[bool] = mapped_column(default=True)
    #: 管理员刚给他重置过密码 —— 他手上那个是一次性口令，登进来第一件事是改掉。
    #:
    #: 没有这一列的话，"重置"就等于"管理员知道这个人的密码"，而此后这个账号做的
    #: 任何事都说不清是谁做的。
    must_change_password: Mapped[bool] = mapped_column(default=False)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())

    # Relationships
    projects: Mapped[list["Project"]] = relationship(back_populates="owner")  # noqa: F821

    __table_args__ = (
        # 「谁」＝组织 + 邮箱。登录时先选组织再填邮箱，所以这里不会有歧义。
        #
        # 写成唯一**索引**而不是表约束：SQLite 给已有的表加表约束要重建整张表，
        # 而加一个索引只是一条 `CREATE UNIQUE INDEX` —— 已经装在同事机器上的
        # 那些组织服务器都是 SQLite，它们要能就地升上来。
        Index("uq_users_institution_email", "institution_id", "email", unique=True),
    )
