"""User-owned CNKI indexes and receipts from a browser on that user's computer."""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, UTCDateTime


class CnkiDevice(Base):
    __tablename__ = "cnki_devices"
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), default="我的 Chrome")
    pair_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    pair_expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    credential_fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_report: Mapped[dict] = mapped_column(JSON, default=dict)


class CnkiJob(Base):
    __tablename__ = "cnki_jobs"
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    query: Mapped[str] = mapped_column(Text)
    # SU: topic; LY: publication source; AU: author. Project queries also use SU.
    field: Mapped[str] = mapped_column(String(8), default="SU")
    category: Mapped[str] = mapped_column(String(16), default="domain")
    recurring: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    result_limit: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Last acknowledged page, not an assertion that a browser is still running.
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    lease_device_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False))
    lease_hash: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_report_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_report: Mapped[dict] = mapped_column(JSON, default=dict)


class CnkiPaper(Base):
    __tablename__ = "cnki_papers"
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    identity: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column(JSON)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    __table_args__ = (UniqueConstraint("user_id", "identity", name="uq_cnki_paper_owner_identity"),)


class CnkiJobPaper(Base):
    __tablename__ = "cnki_job_papers"
    job_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("cnki_jobs.id", ondelete="CASCADE"), primary_key=True)
    paper_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("cnki_papers.id", ondelete="CASCADE"), primary_key=True)
