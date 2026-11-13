from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PairIn(StrictModel):
    code: str = Field(min_length=20, max_length=100)
    name: str = Field(default="我的 Chrome", min_length=1, max_length=100)


class JobIn(StrictModel):
    query: str = Field(min_length=1, max_length=500)
    category: Literal["domain", "journal", "scholar", "project"] = "domain"
    recurring: bool = False
    limit: int | None = Field(default=None, ge=1)
    executor: Literal["server", "local"] = "server"
    document_kind: Literal["journal", "thesis", "conference", "book", "other"] | None = None

    @field_validator("query")
    @classmethod
    def strip_query(cls, value):
        if not value.strip():
            raise ValueError("检索词不能为空")
        return value.strip()


class SessionResetIn(StrictModel):
    job_ids: list[UUID] = Field(min_length=1, max_length=100)


class IndexIn(StrictModel):
    title: str = Field(min_length=1, max_length=2000)
    authors: list[str] = Field(default_factory=list, max_length=100)
    venue: str = Field(default="", max_length=1000)
    pub_date: str = Field(default="", max_length=100)
    year: int | None = Field(default=None, ge=1000, le=3000)
    url: str = Field(max_length=8000)
    doi: str = Field(default="", max_length=500)
    abstract: str = Field(default="", max_length=50000)
    keywords: list[str] = Field(default_factory=list, max_length=100)
    document_type: str = Field(default="期刊", max_length=30)
    citations: int = Field(default=0, ge=0)
    volume: str = Field(default="", max_length=100)
    issue: str = Field(default="", max_length=100)
    pages: str = Field(default="", max_length=100)
    institutions: str = Field(default="", max_length=5000)
    abstract_truncated: bool = False
    metadata_attempted: bool = False
    # Private export locator for deferred metadata work; never emitted in
    # LiteraturePaperOut. It does not contain a browser credential.
    export_id: str = Field(default="", max_length=16000)
    metadata_error: str = Field(default="", max_length=100)

    @field_validator("url")
    @classmethod
    def cnki_url(cls, value):
        p = urlsplit(value)
        if p.scheme != "https" or p.hostname not in {"kns.cnki.net", "kns.cnki.net.cn", "kns8.cnki.net", "oversea.cnki.net"} or p.username or p.password:
            raise ValueError("需要知网 HTTPS 文献链接")
        if not any(part in p.path.lower() for part in ("abstract", "detail")):
            raise ValueError("需要知网题录链接")
        return value


class HeartbeatIn(StrictModel):
    state: Literal["idle", "working", "waiting_for_verification", "browser_closed", "paused", "error"] = "idle"
    detail: str = Field(default="", max_length=300)


class BatchIn(StrictModel):
    lease: str = Field(min_length=20, max_length=100)
    page: int = Field(ge=1)
    papers: list[IndexIn] = Field(default_factory=list, max_length=20)
    page_complete: bool = False
    finished: bool = False
    total: int = Field(default=0, ge=0)


class ReportIn(HeartbeatIn):
    lease: str = Field(min_length=20, max_length=100)
