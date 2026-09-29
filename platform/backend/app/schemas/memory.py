"""Pydantic schemas for Memory System API."""

from datetime import datetime

from pydantic import BaseModel, Field

# 这些枚举原本住在 app/models/{knowledge,memory}.py 里，随那两张空表的 ORM
# 模型一起退役。它们本身仍然是 API 契约的一部分（前端按这些取值渲染），
# 所以搬到契约所在的地方，而不是为了留几个 Enum 保住整套表定义。
import enum


class MemoryLayer(str, enum.Enum):
    """Four memory layers (whitepaper §6.3)."""
    ORGANIZATION = "organization"
    PROJECT = "project"
    USER = "user"
    SESSION = "session"


class MemoryType(str, enum.Enum):
    """Content type classification for update policies."""
    FACTUAL = "factual"        # facts — no decay
    JUDGMENTAL = "judgmental"   # judgments/conclusions — decay after 6 months unused
    PREFERENCE = "preference"  # user/project preferences — user-managed
    RULE = "rule"              # explicit rules — no decay


class MemoryStatus(str, enum.Enum):
    ACTIVE = "active"
    STALE = "stale"              # 6+ months unused (judgmental type)
    ARCHIVED = "archived"        # manually archived or capacity-pruned
    CONFLICTED = "conflicted"    # contradicts another active memory
    SUPERSEDED = "superseded"    # invalidated by new KB evidence


class ConfidenceLevel(str, enum.Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"




# ─── Memory Entry schemas ────────────────────────────────────────────────────


class MemoryEntryCreate(BaseModel):
    content: str
    type: MemoryType
    layer: MemoryLayer
    project_id: str | None = None
    user_id: str | None = None
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM
    tags: list[str] | None = None
    topic: str | None = None
    source: dict = Field(
        ...,
        description="Provenance: {operation_id, node_id, artifact_id, paper_ref, reasoning}",
    )


class MemoryEntryUpdate(BaseModel):
    content: str | None = None
    confidence: ConfidenceLevel | None = None
    status: MemoryStatus | None = None
    tags: list[str] | None = None
    topic: str | None = None


class MemoryEntryResponse(BaseModel):
    id: str
    content: str
    type: MemoryType
    layer: MemoryLayer
    confidence: ConfidenceLevel
    status: MemoryStatus
    project_id: str | None
    user_id: str | None
    tags: list[str] | None
    topic: str | None
    source: dict
    created_at: datetime
    updated_at: datetime
    last_accessed_at: datetime | None
    last_verified_at: datetime | None
    superseded_by_id: str | None
    conflicts_with_ids: list[str] | None
    token_count: int | None

    model_config = {"from_attributes": True}


class MemorySearchRequest(BaseModel):
    """Search memory entries by various criteria."""
    query: str | None = None
    project_id: str | None = None
    user_id: str | None = None
    layer: MemoryLayer | None = None
    type: MemoryType | None = None
    status: MemoryStatus | None = MemoryStatus.ACTIVE
    tags: list[str] | None = None
    topic: str | None = None
    top_k: int = Field(default=20, ge=1, le=100)


class MemoryCapacityInfo(BaseModel):
    """Capacity information for a memory layer (Hermes-inspired bounded memory)."""
    layer: MemoryLayer
    project_id: str | None
    current_count: int
    soft_limit: int
    utilization: float  # current_count / soft_limit
    needs_consolidation: bool  # utilization >= 0.8


# ─── Research Skill schemas ─────────────────────────────────────────────────


class ResearchSkillCreate(BaseModel):
    name: str = Field(..., max_length=300)
    description: str
    node_types: list[str]
    steps: list[dict]
    tools_required: list[str] | None = None
    parameters: dict | None = None
    preconditions: list[dict] | None = None
    expected_outcome: str | None = None
    pitfalls: list[dict] | None = None
    extracted_from_node_id: str | None = None


class ResearchSkillUpdate(BaseModel):
    description: str | None = None
    steps: list[dict] | None = None
    tools_required: list[str] | None = None
    parameters: dict | None = None
    pitfalls: list[dict] | None = None
    expected_outcome: str | None = None


class ResearchSkillResponse(BaseModel):
    id: str
    name: str
    description: str
    node_types: list[str]
    steps: list[dict]
    tools_required: list[str] | None
    parameters: dict | None
    expected_outcome: str | None
    usage_count: int
    success_rate: float | None
    is_validated: bool
    is_deprecated: bool
    scope: str
    version: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ResearchSkillSummary(BaseModel):
    """Level 0 progressive loading: metadata only (~few tokens per skill)."""
    id: str
    name: str
    description: str
    node_types: list[str]
    usage_count: int
    is_validated: bool

    model_config = {"from_attributes": True}
