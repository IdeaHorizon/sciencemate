"""Artifact models — versioned research outputs and library resources.

Artifacts are either:
- PROJECT scope: tangible outputs of node execution (reports, code, figures, etc.)
- LIBRARY scope: external resources (papers, datasets, documentation, benchmarks)

KB stores structured knowledge *extracted from* artifacts, not the files themselves.
"""

from datetime import datetime
from enum import StrEnum
from functools import partial
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime

_StrEnum = partial(Enum, values_callable=lambda e: [x.value for x in e])
_JSON = JSON().with_variant(JSONB(), "postgresql")
_STRING_ARRAY = JSON().with_variant(ARRAY(String), "postgresql")


class ArtifactType(StrEnum):
    # ── project scope ──
    SURVEY_REPORT = "survey_report"
    ANALYSIS_REPORT = "analysis_report"
    EXPERIMENT_LOG = "experiment_log"
    RESEARCH_PLAN = "research_plan"
    PAPER_DRAFT = "paper_draft"
    PAPER_TEX = "paper_tex"
    PAPER_PDF = "paper_pdf"
    PAPER_OUTLINE = "paper_outline"
    REVIEW_REPORT = "review_report"
    CODE = "code"
    FIGURE = "figure"
    TABLE = "table"
    REPORT = "report"
    DATA_PROFILE = "data_profile"
    DATA_PIPELINE = "data_pipeline"
    DATASET = "dataset"
    OTHER = "other"
    MANUSCRIPT = "manuscript"
    PRE_REGISTRATION = "pre_registration"
    WRITING_VALIDATION_REPORT = "writing_validation_report"
    REVIEW_CRITIQUE = "review_critique"
    CLEAN_RESULTS = "clean_results"
    FIGURE_PACKAGE = "figure_package"
    HYPOTHESIS_INNOVATION_REPORT = "hypothesis_innovation_report"
    HYPOTHESIS_RESEARCH_OVERVIEW = "hypothesis_research_overview"
    LITERATURE_INDEX = "literature_index"
    WRITING_PREFLIGHT_PLAN = "writing_preflight_plan"

    # ── library scope ──
    PAPER = "paper"  # external paper PDF
    DOCUMENTATION = "documentation"  # technical docs
    BENCHMARK = "benchmark"  # benchmark data
    REFERENCE_CODE = "reference_code"  # open-source implementations
    NEWS_ARTICLE = "news_article"  # news / blog posts
    LAB_UPDATE = "lab_update"  # lab website updates


class Artifact(Base):
    """A versioned artifact: either a project output or a library resource."""

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    project_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    type: Mapped[ArtifactType] = mapped_column(_StrEnum(ArtifactType), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Current version number (incremented on each new version)
    current_version: Mapped[int] = mapped_column(Integer, default=1)

    # Extra data (flexible, type-specific)
    extra_data: Mapped[dict | None] = mapped_column(_JSON, nullable=True)

    # ⚠️ 这里曾经有九列 KB 领域字段（scope / organization_id / paper_metadata /
    # dataset_metadata / is_indexed_in_kb / kb_chunk_count / indexed_at /
    # structured_summary / concept_ids）。它们属于已退役的服务内 KB 领域模型，
    # 唯一的写者是 scripts/seed_kb_demo.py，线上真实项目里一行都没有。
    # 2026-08-27 随那个脚本一起删。

    # ── new: content hash for dedup ──
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    versions: Mapped[list["ArtifactVersion"]] = relationship(
        back_populates="artifact", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_artifacts_project_type", "project_id", "type"),
    )


class ArtifactVersion(Base):
    """产物的一个版本。**字节在 git 里**，这张表只是它的查询投影。

    类注释原文是 "Stored in object storage (S3/MinIO)" —— 那从来没有发生过：
    `storage_key` 唯一的写入是 `inline://…` 这个假 URI，全仓没有一处
    `import boto3`，compose 里那个 MinIO 容器至今空转（三者一并删于 2026-08-27）。
    """

    __tablename__ = "artifact_versions"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    artifact_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("artifacts.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    resource_key: Mapped[str | None] = mapped_column(String(500), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("sessions.session_id", ondelete="SET NULL"), nullable=True
    )
    #: 这一版是哪一"批"改动的一部分。从前它外键指向 `change_sets`；那张表随
    #: 第二份版本账一起删了（RFC X1），现在存的是发布用的幂等键（会话 id 或
    #: `delivery:<run_id>`），与写进 git 提交的 `Change-Set-ID:` trailer 同一个值。
    change_set_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lifecycle_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="published"
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)  # SHA-256
    git_commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    repository_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Is this a milestone version (user-marked as significant)?

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())

    # Relationship
    artifact: Mapped["Artifact"] = relationship(back_populates="versions")

    __table_args__ = (
        CheckConstraint(
            "lifecycle_status IN ('candidate', 'published', 'frozen')",
            name="ck_artifact_versions_lifecycle_status",
        ),
        Index("ix_artifact_versions_change_set", "change_set_id", "lifecycle_status"),
    )


