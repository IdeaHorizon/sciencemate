"""Tenant-scoped personal research defaults and instructions."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, UTCDateTime

_JSON = JSON().with_variant(JSONB(), "postgresql")


class UserResearchSettings(Base):
    """One mechanically isolated personal settings record per tenant and user."""

    __tablename__ = "user_research_settings"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    response_language: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    citation_style: Mapped[str] = mapped_column(
        String(24), nullable=False, default="author_year"
    )
    evidence_standard: Mapped[str] = mapped_column(
        String(24), nullable=False, default="balanced"
    )
    instructions: Mapped[list] = mapped_column(_JSON, nullable=False, default=list)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", name="uq_user_research_settings_tenant_user"
        ),
        CheckConstraint(
            "response_language IN ('auto', 'zh-CN', 'en')",
            name="ck_user_research_settings_language",
        ),
        CheckConstraint(
            "citation_style IN ('author_year', 'numeric', 'apa')",
            name="ck_user_research_settings_citation",
        ),
        CheckConstraint(
            "evidence_standard IN ('balanced', 'strict', 'exploratory')",
            name="ck_user_research_settings_evidence",
        ),
        CheckConstraint("version >= 1", name="ck_user_research_settings_version"),
    )
