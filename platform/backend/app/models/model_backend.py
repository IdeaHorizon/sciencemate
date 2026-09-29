"""Governance-scoped model backends and per-user effective defaults."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime

#: 字符串列表列。PG 上是原生数组，SQLite（测试库）上退成 JSON —— 与
#: app/models/artifact.py 的 `_STRING_ARRAY` 同一个写法，别在这里发明第二种。
#: 没有走 SQL 层的数组查询（角色过滤全在 Python 里），所以不需要 GIN 索引。
_STRING_ARRAY = JSON().with_variant(ARRAY(String), "postgresql")


class ModelBackendConfig(Base):
    __tablename__ = "model_backend_configs"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    scope_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    credential_source: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    encrypted_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 上下文窗口是**模型的属性**，不是平台的（2026-08-18）。
    #
    # 在此之前它只有一个全局 env `LLM_CONTEXT_WINDOW`，而后端建 worker 环境时
    # 压根没传它 —— 于是不管选哪个模型，harness 一律吃 120000 的兜底默认值。
    # 实测后果：DeepSeek V4 有百万级窗口，摘要器却按 120k 的 70% 去压，一个
    # 会话压了 15 次，最后逼得它自己要求换 session。
    #
    # nullable：留空 = 用平台默认，别逼着老配置去填一个它不知道的数。
    context_window_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 这条连接被授权服务哪些**模型角色**（能力槽），以及它是哪些角色的
    # scope 默认。角色目录在 harness 的 shared/model_roles.yaml —— 平台读那
    # 一份，不在这里再枚举一次。
    #
    # 取代了原来的 `is_scope_default`（单个布尔）。两个字段同时表达"是不是
    # 默认"就会有一天不一致，而两边都不报错；所以是取代，不是并存。
    roles: Mapped[list[str]] = mapped_column(_STRING_ARRAY, nullable=False, default=list)
    default_for_roles: Mapped[list[str]] = mapped_column(
        _STRING_ARRAY, nullable=False, default=list
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 观测到的凭据健康：**存证据，不存判决**。status 由 backend_status() 现算。
    # last_probe_ok 三态：None=没探过/探不出结论，True=provider 认这把 key，
    # False=provider 明确拒绝（401/403）。
    last_probe_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    last_probe_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_probe_detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 视觉能力的观测，跟凭据健康**分开记**。"key 被拒"和"这个模型不认图"是
    # 两件事；合成一个字段会让状态词指错方向 —— 凭据完全正常，UI 却说
    # credentials_rejected，人就去换 key 了。三态同上：None=没探过/探不出。
    last_vision_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_vision_detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 这一条是**某个组织提供的**、这台桌面替它记下的一份（`services/organisation_models`）。
    # 组织是资源的提供者：成员加入之后，组织的模型直接出现在他自己的「设置 → 模型与密钥」
    # 里、标着来源 —— 不用自己再配。真相在组织服务器上；这里存的是指向它的那条路
    # （base_url = 组织服务器的模型网关）和一张只够调模型的通行证（encrypted_api_key），
    # 密钥本身从不离开服务器。两列都空 = 一条普通的、归这台机器自己的连接。
    provided_by_connection: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provided_backend_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by_user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    created_by = relationship("User")

    __table_args__ = (
        # 一条连接的身份是 **scope + provider + model + 端点**。
        #
        # 在此之前 base_url 不在唯一键里，于是"同一个模型名挂在两个不同端点"
        # ——一个完全合法、而且我们真的在用的场景——建不出来。2026-08-24 node20
        # 实测：已有「Kimi K3 · 积算」(openai_compatible/kimi-k3,
        # https://api.icompify.com/v1)，再加一条走自建网关的
        # http://47.103.79.174:30080/v1 直接撞约束。当时把 provider 从
        # openai_compatible 改写成 kimi 绕过去了——两者在这条路径上行为完全
        # 一致，provider 只是个标签——那不是修复，是把身份编进了一个不表达
        # 身份的字段里。
        #
        # 为什么是 COALESCE 表达式索引而不是把 base_url 直接加进
        # UniqueConstraint：PG（和 SQLite）里 NULL 不参与唯一性比较，
        # `(...,  NULL)` 和 `(..., NULL)` 互不冲突 —— 把 base_url 直接列进去，
        # 等于对所有"没填端点"的连接**取消**了这道约束，重复项从此静默共存。
        # COALESCE 把"没填"折进空串，那道约束原样保留。
        #
        # 折成空串而不是别的哨兵值，是因为下游本来就这么读：
        # `harness_runtime._provider_base_url` 判的是 `if config.base_url`，
        # `model_backends` 里是 `(config.base_url or "").rstrip("/")` ——
        # None 和 "" 在这套代码里从来就是同一件事（都表示"用 provider 默认"）。
        # 唯一键必须跟着这个既有语义走，否则 UI 上会出现两条一模一样的连接。
        Index(
            "uq_model_backend_connection",
            "scope_kind",
            "scope_id",
            "provider",
            "model",
            text("coalesce(base_url, '')"),
            unique=True,
        ),
        CheckConstraint(
            "scope_kind IN ('institution', 'group', 'personal')",
            name="ck_model_backend_scope_kind",
        ),
        CheckConstraint(
            "credential_source IN ('none', 'environment', 'encrypted')",
            name="ck_model_backend_credential_source",
        ),
        Index("ix_model_backend_scope", "scope_kind", "scope_id"),
    )


class UserModelBackendPreference(Base):
    """"这个用户，这个角色，用哪条连接"。

    主键含 role：主模型选 A、审图选 B 是完全正常的诉求，而旧表一个用户只有
    一行，等于把"用哪个模型"这个问题假设成只有一个答案。
    """

    __tablename__ = "user_model_backend_preferences"

    user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(64), primary_key=True)
    backend_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("model_backend_configs.id", ondelete="CASCADE"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    backend = relationship("ModelBackendConfig")
