"""Pydantic schemas for Knowledge Base API."""

from pydantic import BaseModel, Field

# 这些枚举原本住在 app/models/{knowledge,memory}.py 里，随那两张空表的 ORM
# 模型一起退役。它们本身仍然是 API 契约的一部分（前端按这些取值渲染），
# 所以搬到契约所在的地方，而不是为了留几个 Enum 保住整套表定义。
import enum


class KBScope(str, enum.Enum):
    ORGANIZATION = "organization"
    PROJECT = "project"


class QualityTier(str, enum.Enum):
    """Source quality tier for evidence weighting."""
    TIER1 = "tier1"  # top venues (Nature, Science, NeurIPS, ICML, etc.)
    TIER2 = "tier2"  # good journals/conferences
    TIER3 = "tier3"  # preprints (arXiv, bioRxiv)
    TIER4 = "tier4"  # news, blogs, informal sources


class ChunkResponse(BaseModel):
    id: str
    kb_entry_id: str | None = None
    artifact_id: str | None = None
    text: str
    chunk_index: int
    section: str | None
    page_number: int | None
    token_count: int | None
    citability: str = "medium"

    model_config = {"from_attributes": True}


class KBSearchRequest(BaseModel):
    """Hybrid search: vector similarity + keyword + quality weighting."""
    query: str
    project_id: str | None = None
    scope: KBScope | None = None
    quality_tiers: list[QualityTier] | None = None
    tags: list[str] | None = None
    top_k: int = Field(default=10, ge=1, le=50)


class KBSearchResult(BaseModel):
    chunk: ChunkResponse
    source_title: str
    source_ref: str | None
    quality_tier: QualityTier | None
    score: float
    artifact_id: str | None = None
